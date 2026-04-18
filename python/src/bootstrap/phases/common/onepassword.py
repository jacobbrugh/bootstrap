"""Onepassword for NixOS + Linux-HM — no-op.

Bootstrap secrets come from `/run/secrets/bootstrap-github-token`
(written at sops-nix activation on NixOS) or the
`BOOTSTRAP_GITHUB_TOKEN_FILE` env-var override (CI harness, or a
manually-pre-staged Linux-HM host). Python never touches 1Password on
these platforms.

Darwin has its own `phases/darwin/onepassword.py` — it installs the
1Password GUI, fetches the age key via `op read`, and activates a
minimal nix-darwin Phase 0 config so sops-nix-darwin decrypts plaintext
to the same runtime-secrets path.

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
    _log.info(
        "onepassword phase is a no-op on NixOS / Linux-HM "
        "(secrets come from sops-nix / env-var override)"
    )
