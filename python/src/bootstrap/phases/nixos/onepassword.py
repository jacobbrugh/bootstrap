"""NixOS onepassword — no-op.

The bootstrap age key is consumed entirely at Phase 0 NixOS activation
time by sops-nix (see `nix/nixos/default.nix` + the repo-root
`secrets/` directory). The decrypted `github_token` lands at
`/run/secrets/bootstrap-github-token` before the bootstrap CLI ever
runs. Nothing for this phase to do.

Kept as a phase for orchestrator consistency + backward compat with
per-phase entry points (`bootstrap-onepassword`).
"""

from __future__ import annotations

import logging

from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    del ctx
    _log.info("onepassword phase is a no-op on NixOS (sops-nix handles secrets at activation)")
