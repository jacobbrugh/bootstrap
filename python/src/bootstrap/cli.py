from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from bootstrap import orchestrator
from bootstrap.lib import host_info, log, sh, symlinks
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.paths import CANONICAL_DOTFILES
from bootstrap.lib.runtime import Context
from bootstrap.phases.nixos import create_usb, firmware, flash
from bootstrap.platform import Platform, detect

_log = logging.getLogger(__name__)

app = typer.Typer(
    name="bootstrap",
    no_args_is_help=False,
    add_completion=False,
    help=(
        "Fresh-machine bootstrap CLI. Run with no arguments to execute the full "
        "OS-appropriate phase list. Use a subcommand to run a single phase. The "
        "host must already exist in the dotfiles registry "
        "(host_registry.toml) — run `nix run .#add-host` from a "
        "dotfiles checkout to add it before bootstrapping."
    ),
)

DryRun = Annotated[
    bool,
    typer.Option(
        "--dry-run",
        help=(
            "Log destructive operations as 'would run: …' instead of executing "
            "them. Read-only commands still execute so decision trees are testable."
        ),
    ),
]
Hostname = Annotated[
    str | None,
    typer.Option(
        "--hostname",
        help=(
            "Hostname for this machine. Resolution order: this flag, then "
            "$BOOTSTRAP_HOSTNAME, then the system-detected hostname. Required "
            "on a Mac, whose detected name is not its registry name."
        ),
    ),
]
Verbose = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Enable DEBUG-level logging."),
]


def _fail_on_bootstrap_error(exc: BootstrapError) -> typer.Exit:
    _log.error("[bold red]bootstrap failed:[/] %s", exc)
    return typer.Exit(code=1)


async def _resolve_hostname(arg: str | None, *, platform: Platform) -> str:
    """Hostname resolution: CLI arg → env var → detected (never on darwin).

    The arg overrides the env var; the env var overrides detection. A fresh
    Mac's detected name is whatever Setup Assistant derived from the owner's
    name, which is no host in the registry: running with it renamed the Mac,
    uploaded an ssh key under that name, and only then failed in register. So
    on darwin the name has to be given.
    """
    if arg:
        chosen = arg.strip()
    else:
        env = os.environ.get("BOOTSTRAP_HOSTNAME")
        if env:
            chosen = env.strip()
        elif platform is Platform.DARWIN:
            raise BootstrapError(
                "no hostname given. Pass this Mac's name from host_registry.toml: "
                "`curl -fsSL https://jacobbrugh.net/bootstrap.sh | bash -s -- <host>`"
            )
        else:
            chosen = host_info.sanitize_hostname_default(await host_info.detect_hostname())
    host_info.validate_hostname(chosen)
    return chosen


def _resolve_force_sandbox() -> bool:
    """BOOTSTRAP_SANDBOX=1: a harness declares itself sandbox whatever key it holds.

    The tier itself is decided from the loaded bootstrap age key
    (lib/secrets.py); this only ever lowers it.
    """
    raw = os.environ.get("BOOTSTRAP_SANDBOX")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "y")


def _run_phase(
    *,
    dry_run: bool,
    hostname: str | None,
    verbose: bool,
    entry: str,
) -> None:
    log.setup(verbose=verbose)
    # Every `nix` this process runs is a flake command, and a machine that has
    # only just had Nix installed enables neither feature (on a Mac the prereqs
    # phase has also moved /etc/nix/nix.conf aside for nix-darwin). A caller's
    # own NIX_CONFIG is left alone.
    os.environ.setdefault("NIX_CONFIG", "extra-experimental-features = nix-command flakes")
    platform = detect()
    if platform is Platform.UNSUPPORTED:
        raise BootstrapError(f"unsupported platform: {sys.platform}")

    async def _go() -> None:
        chosen = await _resolve_hostname(hostname, platform=platform)
        force_sandbox = _resolve_force_sandbox()

        if platform is Platform.DARWIN:
            _log.info("setting machine hostname to %s", chosen)
            await sh.prime_sudo(dry_run=dry_run)
            await host_info.rename_darwin(chosen, dry_run=dry_run)

        # The default path, unless this host keeps its checkout elsewhere (a
        # registry `dotfilesDir`): the flake symlink a completed register left
        # is then what says where.
        canonical = CANONICAL_DOTFILES
        if "BOOTSTRAP_CANONICAL_DOTFILES" not in os.environ and not (canonical / ".git").exists():
            canonical = symlinks.linked_checkout(platform) or canonical

        ctx = Context(
            platform=platform,
            hostname=chosen,
            canonical_repo=canonical,
            dry_run=dry_run,
            has_windows_host=(platform is Platform.NIXOS_WSL),
            force_sandbox=force_sandbox,
        )
        coro = getattr(orchestrator, entry)
        await coro(ctx)

    try:
        asyncio.run(_go())
    except BootstrapError as exc:
        raise _fail_on_bootstrap_error(exc) from exc


@app.callback(invoke_without_command=True)
def _root(
    typer_ctx: typer.Context,
    dry_run: DryRun = False,
    hostname: Hostname = None,
    verbose: Verbose = False,
) -> None:
    """Run the full OS-appropriate phase list when no subcommand is given."""
    if typer_ctx.invoked_subcommand is not None:
        return
    _run_phase(
        dry_run=dry_run,
        hostname=hostname,
        verbose=verbose,
        entry="run_full",
    )


@app.command("prereqs")
def _cmd_prereqs(
    dry_run: DryRun = False,
    hostname: Hostname = None,
    verbose: Verbose = False,
) -> None:
    """Install OS prerequisites (Homebrew on Darwin; dirs elsewhere)."""
    _run_phase(
        dry_run=dry_run,
        hostname=hostname,
        verbose=verbose,
        entry="run_prereqs",
    )


@app.command("secret-zero")
def _cmd_secret_zero(
    dry_run: DryRun = False,
    hostname: Hostname = None,
    verbose: Verbose = False,
) -> None:
    """Make the bootstrap secrets reachable (Darwin: a signed-in 1Password CLI)."""
    _run_phase(
        dry_run=dry_run,
        hostname=hostname,
        verbose=verbose,
        entry="run_secret_zero",
    )


@app.command("ssh")
def _cmd_ssh(
    dry_run: DryRun = False,
    hostname: Hostname = None,
    verbose: Verbose = False,
) -> None:
    """Generate SSH key, upload to GitHub, add to keychain (Darwin)."""
    _run_phase(
        dry_run=dry_run,
        hostname=hostname,
        verbose=verbose,
        entry="run_ssh",
    )


@app.command("register")
def _cmd_register(
    dry_run: DryRun = False,
    hostname: Hostname = None,
    verbose: Verbose = False,
) -> None:
    """Clone dotfiles + verify host in host_registry.toml + .sops.yaml rekey."""
    _run_phase(
        dry_run=dry_run,
        hostname=hostname,
        verbose=verbose,
        entry="run_register",
    )


@app.command("switch")
def _cmd_switch(
    dry_run: DryRun = False,
    hostname: Hostname = None,
    verbose: Verbose = False,
) -> None:
    """Run the OS-native switch (darwin-rebuild / nixos-rebuild / home-manager)."""
    _run_phase(
        dry_run=dry_run,
        hostname=hostname,
        verbose=verbose,
        entry="run_switch",
    )


@app.command("create-usb")
def _cmd_create_usb(
    device: Annotated[
        Path,
        typer.Argument(help="USB block device to wipe and flash (e.g. /dev/sdc)."),
    ],
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Wipe DEVICE and flash the LUKS-encrypted bootstrap USB installer."""
    log.setup(verbose=verbose)
    try:
        platform = detect()
        if platform in (Platform.DARWIN, Platform.UNSUPPORTED):
            raise BootstrapError(
                "bootstrap-create-usb needs a Linux host (it partitions the "
                "device and runs nixos-install)"
            )
        asyncio.run(create_usb.run(device=device, dry_run=dry_run))
    except BootstrapError as exc:
        raise _fail_on_bootstrap_error(exc) from exc


@app.command("firmware")
def _cmd_firmware(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Write the flash chip. Without this the command only reports.",
        ),
    ] = False,
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Convert this Protectli's firmware to Dasharo coreboot (reports by default)."""
    log.setup(verbose=verbose)
    try:
        platform = detect()
        if platform in (Platform.DARWIN, Platform.UNSUPPORTED):
            raise BootstrapError(
                "bootstrap-firmware runs on the NixOS USB installer only (it "
                "drives flashrom's internal programmer against this machine)"
            )
        asyncio.run(firmware.run(apply=apply, dry_run=dry_run))
    except BootstrapError as exc:
        raise _fail_on_bootstrap_error(exc) from exc


@app.command("flash")
def _cmd_flash(
    hostname: Annotated[
        str,
        typer.Argument(help="Target hostname (must exist in host_registry.toml)."),
    ],
    disk: Annotated[
        Path | None,
        typer.Option(
            "--disk",
            help=(
                "Disk to wipe and install onto, e.g. /dev/nvme0n1. Omit when the "
                "machine has exactly one eligible disk; required when it has more."
            ),
        ),
    ] = None,
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Flash this machine from the USB installer (wipes the target disk)."""
    log.setup(verbose=verbose)
    try:
        platform = detect()
        if platform in (Platform.DARWIN, Platform.UNSUPPORTED):
            raise BootstrapError("bootstrap-flash runs on the NixOS USB installer only")
        host_info.validate_hostname(hostname)
        asyncio.run(flash.run(hostname=hostname, disk=disk, dry_run=dry_run))
    except BootstrapError as exc:
        raise _fail_on_bootstrap_error(exc) from exc


@app.command("post")
def _cmd_post(
    dry_run: DryRun = False,
    hostname: Hostname = None,
    verbose: Verbose = False,
) -> None:
    """Auto-open System Settings panes for manual TCC gates (Darwin)."""
    _run_phase(
        dry_run=dry_run,
        hostname=hostname,
        verbose=verbose,
        entry="run_post",
    )


# Subcommand names the bare `bootstrap` entry point must NOT swallow as a
# positional hostname. Derived from the registrations above: a hand-kept copy
# once omitted `firmware`, so `bootstrap firmware --apply` was rewritten to
# `--hostname firmware --apply` and ran the full bootstrap instead.
_PHASE_COMMANDS = frozenset(c.name for c in app.registered_commands if c.name is not None)

# Root options that consume a following value token.
_VALUE_OPTIONS = frozenset({"--hostname"})


def _rewrite_positional_hostname(argv: list[str]) -> list[str]:
    """Support `bootstrap [hostname]` (the production contract) on a typer group.

    A positional argument on a click/typer *group* would swallow subcommand
    names, so the positional form is normalized to `--hostname <value>` before
    typer parses: the first bare token that is neither a subcommand name nor
    the value of a preceding value-taking option is rewritten.
    """
    expecting_value = False
    for i, token in enumerate(argv):
        if expecting_value:
            expecting_value = False
            continue
        if token.startswith("-"):
            expecting_value = token in _VALUE_OPTIONS
            continue
        if token in _PHASE_COMMANDS:
            return argv
        return [*argv[:i], "--hostname", token, *argv[i + 1 :]]
    return argv


def main() -> None:
    app(_rewrite_positional_hostname(sys.argv[1:]))


def _invoke_subcommand(name: str) -> None:
    app([name, *sys.argv[1:]])


def phase_prereqs() -> None:
    _invoke_subcommand("prereqs")


def phase_secret_zero() -> None:
    _invoke_subcommand("secret-zero")


def phase_ssh() -> None:
    _invoke_subcommand("ssh")


def phase_register() -> None:
    _invoke_subcommand("register")


def phase_switch() -> None:
    _invoke_subcommand("switch")


def phase_post() -> None:
    _invoke_subcommand("post")


def phase_create_usb() -> None:
    _invoke_subcommand("create-usb")


def phase_flash() -> None:
    _invoke_subcommand("flash")


def phase_firmware() -> None:
    _invoke_subcommand("firmware")
