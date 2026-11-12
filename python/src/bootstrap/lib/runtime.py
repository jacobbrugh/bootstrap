from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from bootstrap.platform import Platform


class Tier(Enum):
    """Which bootstrap age key a host was handed, and so what it may do at GitHub.

    SANDBOX: the ``bootstrap_sandbox`` identity — CI, e2e and sandbox VMs. No
    SSH key is uploaded anywhere; dotfiles is reached over https with the bot
    token. PRIVILEGED: the ``bootstrap_privileged`` identity from 1Password —
    real devices. The host's SSH key is uploaded to the ``jacobbrugh`` account
    with a token only this identity can decrypt, and dotfiles is reached over
    SSH with that key.
    """

    SANDBOX = "sandbox"
    PRIVILEGED = "privileged"


@dataclass(slots=True)
class Context:
    platform: Platform
    hostname: str
    canonical_repo: Path
    dry_run: bool = False

    has_windows_host: bool = False

    # BOOTSTRAP_SANDBOX=1: a harness declaring itself sandbox whatever key it
    # holds. Read once in cli.py, the only env reader.
    force_sandbox: bool = False

    # Decided by the loaded bootstrap age key (lib/secrets.py); None until the
    # key is loaded, and None on a host whose key `post` has already shredded.
    tier: Tier | None = None

    # The bootstrap age key, in exactly one of two delivery tiers:
    # a file staged on disk (NixOS: bootstrap-flash stages it, sops-nix
    # consumes it, post shreds it) or the key material held in memory
    # (darwin: read once from 1Password, never written to disk).
    bootstrap_age_key_file: Path | None = None
    bootstrap_age_key: str | None = field(default=None, repr=False)
    github_token: str | None = field(default=None, repr=False)

    @property
    def is_privileged(self) -> bool:
        """Only a host proven privileged by its key, and not overridden, is."""
        return self.tier is Tier.PRIVILEGED and not self.force_sandbox

    @property
    def is_sandbox(self) -> bool:
        return not self.is_privileged

    @property
    def sops_env_overlay(self) -> dict[str, str]:
        """Env vars handing the bootstrap age key to a sops subprocess.

        sops call sites consume this instead of a key path so they never
        know (or care) how the key was delivered.
        """
        if self.bootstrap_age_key is not None:
            return {"SOPS_AGE_KEY": self.bootstrap_age_key}
        if self.bootstrap_age_key_file is not None:
            return {"SOPS_AGE_KEY_FILE": str(self.bootstrap_age_key_file)}
        return {}
