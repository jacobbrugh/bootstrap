"""macOS TCC (Transparency, Consent, Control) manual-step metadata.

The `post` phase uses this list to tell the user what permission grants
are still required after `darwin-rebuild switch` completes, and opens the
corresponding System Settings pane for each.

None of these can be granted programmatically on a SIP-enabled personal
Mac without MDM. They're irreducibly manual — even with MDM, Input
Monitoring and System Extensions always require a user-side click.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TccStep:
    name: str
    pane_url: str
    required_by: tuple[str, ...]
    instructions: str


# Ordered so the user moves through the panes in the sequence most first-run
# apps will prompt for them: Accessibility first (most apps), Input
# Monitoring next (skhd), then System Extensions last (Tailscale, Karabiner).
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
