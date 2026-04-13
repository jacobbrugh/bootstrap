"""Smoke tests — verify the package imports cleanly and the CLI is wired up."""

from __future__ import annotations

import bootstrap
from bootstrap import cli


def test_version_is_set() -> None:
    assert bootstrap.__version__ == "0.1.0"


def test_main_entry_point_is_callable() -> None:
    """`bootstrap.cli.main` is the `[project.scripts] bootstrap = ...` target."""
    assert callable(cli.main)


def test_typer_app_has_all_phase_subcommands() -> None:
    """Every declared phase must be reachable as a Typer subcommand.

    Uses `cli.app.registered_commands` (Typer's public API for introspecting
    registered subcommands) rather than `cli.app.info.name`, which isn't
    documented as a stable read interface.
    """
    command_names = {c.name for c in cli.app.registered_commands}
    expected = {"prereqs", "onepassword", "ssh", "register", "switch", "post"}
    missing = expected - command_names
    assert not missing, f"missing CLI subcommands: {missing}"


def test_phase_entry_points_exist() -> None:
    """Every `[project.scripts]` entry in pyproject.toml must resolve to a callable here."""
    entries = [
        "phase_prereqs",
        "phase_onepassword",
        "phase_ssh",
        "phase_register",
        "phase_switch",
        "phase_post",
    ]
    for name in entries:
        assert callable(getattr(cli, name)), f"missing entry point: {name}"
