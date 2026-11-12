from __future__ import annotations

from bootstrap import cli


def test_main_entry_point_is_callable() -> None:
    assert callable(cli.main)


def test_typer_app_has_all_phase_subcommands() -> None:
    command_names = {c.name for c in cli.app.registered_commands}
    expected = {
        "prereqs",
        "secret-zero",
        "ssh",
        "register",
        "switch",
        "post",
        "create-usb",
        "flash",
        "firmware",
    }
    missing = expected - command_names
    assert not missing, f"missing CLI subcommands: {missing}"


def test_phase_entry_points_exist() -> None:
    entries = [
        "phase_prereqs",
        "phase_secret_zero",
        "phase_ssh",
        "phase_register",
        "phase_switch",
        "phase_post",
        "phase_create_usb",
        "phase_flash",
        "phase_firmware",
    ]
    for name in entries:
        assert callable(getattr(cli, name)), f"missing entry point: {name}"
