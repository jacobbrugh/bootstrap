"""Windows-host phases — intentionally empty in this change.

The next session migrates BootstrapUtils.psm1's Scoop / DSC / SSH / Tailscale /
WSL functions into typed Python phases here. Those phases run from inside WSL
and drive the Windows host via `bootstrap.lib.sh.run_powershell`.
"""
