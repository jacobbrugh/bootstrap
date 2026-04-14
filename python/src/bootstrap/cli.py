"""Top-level CLI surface.

Each `[project.scripts]` entry point in `pyproject.toml` maps to a function
here. The Typer app is the single source of truth for help text; the
per-binary `phase_*` shims at the bottom delegate to it so subcommand and
standalone-binary invocations share the same argument parsing.

Typer callbacks are synchronous (Typer doesn't support async callbacks
directly). Each callback wraps `asyncio.run(coroutine)` internally, so
the entire orchestrator + phase graph runs inside one asyncio event loop
per CLI invocation. `questionary.text(msg).ask_async()` and every
subprocess call via `asyncio.create_subprocess_exec` cooperate with that
single loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Annotated

import typer

from bootstrap import orchestrator
from bootstrap.lib import host_info, log
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.paths import CANONICAL_DOTFILES
from bootstrap.lib.runtime import Context
from bootstrap.platform import Platform, detect

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


async def _build_context(
    *,
    dry_run: bool,
    non_interactive: bool,
    verbose: bool,
) -> Context:
    log.setup(verbose=verbose)
    platform = detect()
    if platform is Platform.UNSUPPORTED:
        raise BootstrapError(f"unsupported platform: {sys.platform}")
    hostname = await host_info.detect_hostname()
    return Context(
        platform=platform,
        hostname=hostname,
        canonical_repo=CANONICAL_DOTFILES,
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        has_windows_host=(platform is Platform.NIXOS_WSL),
    )


def _fail_on_bootstrap_error(exc: BootstrapError) -> typer.Exit:
    logging.getLogger(__name__).error("[bold red]bootstrap failed:[/] %s", exc)
    return typer.Exit(code=1)


def _run_phase(
    runner: asyncio.Future[None] | None,
    *,
    dry_run: bool,
    non_interactive: bool,
    verbose: bool,
    entry: str,
) -> None:
    """Drive one CLI entry point through asyncio.run, handling BootstrapError.

    `entry` is the orchestrator function name to call. We look it up via
    getattr rather than taking a callable so the caller doesn't need to
    annotate the awkward `Callable[[Context], Awaitable[None]]` type.
    """
    del runner  # unused, only exists to keep the type checker calm below

    async def _go() -> None:
        ctx = await _build_context(
            dry_run=dry_run,
            non_interactive=non_interactive,
            verbose=verbose,
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
        None,
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
        None,
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
        None,
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
        None,
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
        None,
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
        None,
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
        None,
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
