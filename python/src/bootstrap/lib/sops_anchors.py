"""Anchor name -> age public key, read off a ``.sops.yaml``'s text.

The anchor name is the stable identity of a bootstrap key tier
(``bootstrap_privileged``, ``bootstrap_sandbox``); a round-trip YAML load
exposes list position far more readily than the anchor a key was declared
under, so this reads the file's text. Shared by media creation (which key to
bake), tier detection (which key was baked) and the CI staging tool.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The two bootstrap identities, as anchored in this repo's and the dotfiles
#: repo's .sops.yaml. Privileged: real-device bootstraps, private half in
#: 1Password. Sandbox: CI and e2e/sandbox VMs, never a recipient of
#: privileged dotfiles secrets.
PRIVILEGED_ANCHOR = "bootstrap_privileged"
SANDBOX_ANCHOR = "bootstrap_sandbox"

_ANCHOR_RE = re.compile(r"&([A-Za-z0-9_-]+)\s+(age1[a-z0-9]+)")


def anchor_pubkeys(sops_yaml_path: Path) -> dict[str, str]:
    """Map every ``&anchor age1...`` declaration in the file to its public key."""
    return dict((name, pubkey) for name, pubkey in _ANCHOR_RE.findall(sops_yaml_path.read_text()))
