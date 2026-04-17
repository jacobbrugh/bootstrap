"""Darwin onepassword — raise for now; Darwin bootstrap path is unimplemented.

Bootstrap secrets now come exclusively from a plaintext file written by
sops-nix at Phase 0 NixOS activation — see `lib/secrets.py`. Darwin
doesn't have sops-nix. Re-introducing Darwin support needs one of:
  - A nix-darwin sops-nix equivalent that writes plaintext at a known
    path before the bootstrap CLI runs.
  - A pre-bootstrap manual step that writes the plaintext token to
    `$BOOTSTRAP_GITHUB_TOKEN_FILE` + the age key to
    `$BOOTSTRAP_AGE_KEY_FILE`, then runs bootstrap.

Option 2 is trivially supported already — just set the two env vars
before invoking bootstrap.
"""

from __future__ import annotations

import logging

from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    del ctx
    raise BootstrapError(
        "Darwin bootstrap is temporarily unsupported. Secrets now come from "
        "sops-nix on NixOS; Darwin has no equivalent. Pre-stage the token + "
        "age-key manually and set BOOTSTRAP_GITHUB_TOKEN_FILE + "
        "BOOTSTRAP_AGE_KEY_FILE, or skip this phase."
    )
