from __future__ import annotations

from bootstrap.cli import _rewrite_positional_hostname


def test_bare_hostname_becomes_option() -> None:
    assert _rewrite_positional_hostname(["nix9"]) == ["--hostname", "nix9"]


def test_hostname_with_flags() -> None:
    assert _rewrite_positional_hostname(["--verbose", "nix9", "--dry-run"]) == [
        "--verbose",
        "--hostname",
        "nix9",
        "--dry-run",
    ]


def test_subcommand_passes_through() -> None:
    assert _rewrite_positional_hostname(["register", "--dry-run"]) == [
        "register",
        "--dry-run",
    ]


def test_explicit_hostname_option_value_not_rewritten() -> None:
    argv = ["--hostname", "nix9", "--verbose"]
    assert _rewrite_positional_hostname(argv) == argv


def test_empty_argv() -> None:
    assert _rewrite_positional_hostname([]) == []


def test_flags_only() -> None:
    argv = ["--dry-run", "-v"]
    assert _rewrite_positional_hostname(argv) == argv


def test_every_subcommand_passes_through() -> None:
    """`firmware` was once missing from the hand-kept set, so
    `bootstrap firmware --apply` became `--hostname firmware --apply` — a full
    bootstrap instead of a firmware flash."""
    for name in ("firmware", "flash", "create-usb", "prereqs", "ssh", "post"):
        argv = [name, "--apply"]
        assert _rewrite_positional_hostname(argv) == argv
