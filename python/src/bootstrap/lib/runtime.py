"""The Context dataclass carried through the orchestrator and every phase.

Phases never read environment variables or detect state directly — they
receive a `Context` built by the orchestrator. Makes every phase trivially
testable by constructing a Context in a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bootstrap.platform import Platform


@dataclass(slots=True)
class Context:
    """Shared runtime state for a single bootstrap invocation."""

    platform: Platform
    hostname: str
    canonical_repo: Path
    dry_run: bool = False
    non_interactive: bool = False
    verbose: bool = False

    # Set True when the detected platform is `Platform.NIXOS_WSL`. The
    # Windows migration session extends the NIXOS_WSL phase list with
    # Windows-host phases that drive the host via `sh.run_powershell`.
    has_windows_host: bool = False

    # Sandbox hosts (CI runners, throwaway NixOS VMs, kubevirt instances)
    # get the restricted "sandbox" bootstrap age key, which can decrypt
    # `bootstrap-secrets-sandbox.sops.yaml` only — so it yields the bot
    # GitHub PAT rather than the user PAT, and the host's own generated
    # age key is excluded from `nix/secrets.yaml`'s creation_rule. Set at
    # CLI entry from either the interactive sandbox prompt or the
    # `BOOTSTRAP_SANDBOX=1` non-interactive override.
    is_sandbox: bool = False

    # Populated by the `ephemeral_secrets` context manager. `bootstrap_age_key_file` is an
    # ephemeral path (under `$XDG_RUNTIME_DIR`) cleaned up at process exit;
    # `github_token` is held in memory only and `repr=False` keeps it out of
    # accidental log lines.
    bootstrap_age_key_file: Path | None = None
    github_token: str | None = field(default=None, repr=False)

    @property
    def sops_env_overlay(self) -> dict[str, str]:
        """Environment overlay for invoking sops with the bootstrap age key.

        Intended for merging into `os.environ` when calling `sh.run` with an
        explicit `env=` argument from a register-phase sops operation.
        """
        if self.bootstrap_age_key_file is None:
            return {}
        return {"SOPS_AGE_KEY_FILE": str(self.bootstrap_age_key_file)}
