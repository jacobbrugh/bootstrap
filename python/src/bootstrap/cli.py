"""Top-level CLI surface.

Each `[project.scripts]` entry point in `pyproject.toml` maps to a function
here. The Typer app is the single source of truth for help text; the
per-binary `phase_*` shims at the bottom delegate to it so subcommand and
standalone-binary invocations share the same argument parsing.

Typer callbacks are synchronous (Typer doesn't support async callbacks
directly). Each callback wraps `asyncio.run(coroutine)` internally, so
the entire orchestrator + phase graph runs inside one asyncio event loop
per CLI invocation.

The hostname prompt + Darwin rename happens at CLI entry, BEFORE any
phase runs. The reason is that `ssh.py` builds the key comment and the
GitHub key title from `ctx.hostname`, and `ssh` runs before `register`.
If we deferred the prompt to register (as the original design did), the
SSH key would always be uploaded with the pre-rename OS hostname
(e.g. `jacobs-mac-mini-bootstrap`) even when the user renamed to
`mac2`. Prompting at CLI entry + passing `chosen` into `Context.hostname`
is the single place every downstream phase reads from.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Annotated

import typer

from bootstrap import orchestrator
from bootstrap.lib import host_info, log, prompts, sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.paths import CANONICAL_DOTFILES
from bootstrap.lib.runtime import Context
from bootstrap.platform import Platform, detect

_log = logging.getLogger(__name__)

app = typer.Typer(
    name="bootstrap",
    no_args_is_help=False,
    add_completion=False,
    help=(
        "Fresh-machine bootstrap CLI. Run with no arguments to execute the full "
        "OS-appropriate phase list. Use a subcommand to run a single phase."
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
NonInteractive = Annotated[
    bool,
    typer.Option(
        "--non-interactive",
        help=(
            "Fail fast instead of prompting. Intended for CI/automation; "
            "interactive bootstrap should omit this flag."
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


def _run_phase(
    *,
    dry_run: bool,
    non_interactive: bool,
    verbose: bool,
    entry: str,
) -> None:
    """Drive one CLI entry point through asyncio.run, handling BootstrapError.

    Logging setup + sync platform/hostname detection happens before the
    event loop — the hostname lookup is a single blocking scutil call and
    doesn't need asyncio to do it.

    `entry` is the orchestrator function name to call. We look it up via
    getattr rather than taking a callable so the caller doesn't need to
    annotate the awkward `Callable[[Context], Awaitable[None]]` type.
    """
    log.setup(verbose=verbose)
    platform = detect()
    if platform is Platform.UNSUPPORTED:
        raise BootstrapError(f"unsupported platform: {sys.platform}")
    detected_hostname = host_info.detect_hostname()
    # BOOTSTRAP_HOSTNAME overrides the prompt default. Used by
    # `scripts/test-register-local.sh` to drive `--non-interactive`
    # runs against a fake hostname without touching the real machine.
    default_hostname = os.environ.get("BOOTSTRAP_HOSTNAME") or (
        host_info.sanitize_hostname_default(detected_hostname)
    )

    async def _go() -> None:
        # Prompt for hostname at CLI entry so ssh.py builds its key
        # comment / GitHub title from the final name, not the pre-rename
        # scutil value. Register phase no longer prompts or renames.
        #
        # The rename has no rollback path: if a later phase fails, the
        # Mac keeps the new hostname but its registration in the dotfiles
        # repo may be incomplete. Recovery is to re-run the bootstrap —
        # the rename below is unconditional on Darwin and idempotent, so
        # a re-run with the same name is a no-op.
        chosen = await prompts.text(
            "hostname for this machine:",
            default=default_hostname,
            non_interactive=non_interactive,
        )
        host_info.validate_hostname(chosen)
        if platform is Platform.DARWIN and not os.environ.get("BOOTSTRAP_SKIP_RENAME"):
            # Always call rename_darwin on Darwin, even when chosen matches
            # detected_hostname. `detect_hostname` reads LocalHostName, but
            # macOS setup assistant also has ComputerName (where the human
            # form lives — "Jacob's MacBook") and HostName (DNS-level). If
            # the user accepts the default, we still want to overwrite the
            # other two from the user-typed form with whatever weird
            # characters macOS seeded them with. `scutil --set X <same>` is
            # an idempotent write, so this is free when the values already
            # match.
            #
            # BOOTSTRAP_SKIP_RENAME is a test-only escape hatch: the local
            # test harness runs `bootstrap register` against a throwaway
            # dotfiles checkout with a fake hostname, and we don't want the
            # test to actually rename the developer's Mac.
            _log.info("setting machine hostname to %s", chosen)
            await sh.prime_sudo(dry_run=dry_run)
            await host_info.rename_darwin(chosen, dry_run=dry_run)

        ctx = Context(
            platform=platform,
            hostname=chosen,
            canonical_repo=CANONICAL_DOTFILES,
            dry_run=dry_run,
            non_interactive=non_interactive,
            verbose=verbose,
            has_windows_host=(platform is Platform.NIXOS_WSL),
        )
        coro = getattr(orchestrator, entry)
        await coro(ctx)

    try:
        asyncio.run(_go())
    except BootstrapError as exc:
        raise _fail_on_bootstrap_error(exc) from exc


# ── root: full run when no subcommand is given ────────────────────────


@app.callback(invoke_without_command=True)
def _root(
    typer_ctx: typer.Context,
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    verbose: Verbose = False,
) -> None:
    """Run the full OS-appropriate phase list when no subcommand is given."""
    if typer_ctx.invoked_subcommand is not None:
        return
    _run_phase(
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        entry="run_full",
    )


# ── per-phase subcommands ──────────────────────────────────────────────


@app.command("prereqs")
def _cmd_prereqs(
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    verbose: Verbose = False,
) -> None:
    """Install OS prerequisites (Homebrew on Darwin; dirs elsewhere)."""
    _run_phase(
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        entry="run_prereqs",
    )


@app.command("onepassword")
def _cmd_onepassword(
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    verbose: Verbose = False,
) -> None:
    """Install 1Password (Darwin) and wait for sign-in."""
    _run_phase(
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        entry="run_onepassword",
    )


@app.command("ssh")
def _cmd_ssh(
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    verbose: Verbose = False,
) -> None:
    """Generate SSH key, upload to GitHub, add to keychain (Darwin)."""
    _run_phase(
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        entry="run_ssh",
    )


@app.command("register")
def _cmd_register(
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    verbose: Verbose = False,
) -> None:
    """Clone dotfiles + register this host in registry.toml + .sops.yaml."""
    _run_phase(
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        entry="run_register",
    )


@app.command("switch")
def _cmd_switch(
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    verbose: Verbose = False,
) -> None:
    """Run the OS-native switch (darwin-rebuild / nixos-rebuild / home-manager)."""
    _run_phase(
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        entry="run_switch",
    )


@app.command("post")
def _cmd_post(
    dry_run: DryRun = False,
    non_interactive: NonInteractive = False,
    verbose: Verbose = False,
) -> None:
    """Auto-open System Settings panes for manual TCC gates (Darwin)."""
    _run_phase(
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        entry="run_post",
    )


# ── module-level entry function for the `bootstrap` console_script ─────


def main() -> None:
    """`bootstrap` entry point — delegates to the Typer app."""
    app()


# ── per-phase entry-point shims for the `bootstrap-<phase>` binaries ──
# Each one invokes the Typer app with the phase name pre-injected so the
# argument parser and exit handling stay consistent with the subcommand form.


def _invoke_subcommand(name: str) -> None:
    """Invoke `bootstrap <name>` with whatever flags were on the real argv.

    Used by the `phase_*` shims so `bootstrap-prereqs --help` and
    `bootstrap-prereqs --dry-run` work the same as `bootstrap prereqs --help`
    / `bootstrap prereqs --dry-run`.
    """
    app([name, *sys.argv[1:]])


def phase_prereqs() -> None:
    """`bootstrap-prereqs` entry point."""
    _invoke_subcommand("prereqs")


def phase_onepassword() -> None:
    """`bootstrap-onepassword` entry point."""
    _invoke_subcommand("onepassword")


def phase_ssh() -> None:
    """`bootstrap-ssh` entry point."""
    _invoke_subcommand("ssh")


def phase_register() -> None:
    """`bootstrap-register` entry point."""
    _invoke_subcommand("register")


def phase_switch() -> None:
    """`bootstrap-switch` entry point."""
    _invoke_subcommand("switch")


def phase_post() -> None:
    """`bootstrap-post` entry point."""
    _invoke_subcommand("post")
