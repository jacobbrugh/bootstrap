"""Linux-HM onepassword — no-op.

Same reasoning as the NixOS phase: bootstrap secrets come from
`/run/secrets/bootstrap-github-token` (or the
`BOOTSTRAP_GITHUB_TOKEN_FILE` override for CI harnesses), written out
of band before the bootstrap CLI runs. No 1Password involvement here.
"""

from __future__ import annotations

import logging

from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    del ctx
    _log.info("onepassword phase is a no-op on linux-hm (secrets come from sops-nix / env-var override)")
