from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TccStep:
    name: str
    pane_url: str
    required_by: tuple[str, ...]
    instructions: str


STEPS: tuple[TccStep, ...] = (
    TccStep(
        name="Accessibility",
        pane_url="x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        required_by=("AeroSpace", "skhd", "Hammerspoon"),
        instructions=(
            "Toggle AeroSpace, skhd, and Hammerspoon on. Each first-run may "
            "prompt you to approve from the app itself; clicking through "
            "that prompt opens this pane with the app pre-highlighted."
        ),
    ),
    TccStep(
        name="Input Monitoring",
        pane_url="x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
        required_by=("skhd",),
        instructions=(
            "Toggle skhd on. Input Monitoring cannot be silently granted even "
            "with MDM — the toggle itself is always a user action."
        ),
    ),
    TccStep(
        name="System Extensions",
        pane_url="x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
        required_by=("Tailscale", "Karabiner-DriverKit-VirtualHIDDevice"),
        instructions=(
            "Open Login Items & Extensions → Driver Extensions (and Network "
            "Extensions for Tailscale). Approve each blocked extension."
        ),
    ),
)
