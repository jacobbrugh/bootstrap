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

    # Set True when the detected platform is `Platform.NIXOS_WSL`. The
    # Windows migration session extends the NIXOS_WSL phase list with
    # Windows-host phases that drive the host via `sh.run_powershell`.
    has_windows_host: bool = False

    # Sandbox hosts (CI runners, throwaway NixOS VMs, kubevirt instances)
    # get the host's own generated age key excluded from
    # `nix/secrets.yaml`'s creation_rule via the register phase's
    # `_NON_SENSITIVE_TAGS` logic, and have the "sandbox" tag force-added
    # to `registry.toml` so the sandbox nixosConfiguration module set
    # applies. Set at CLI entry from either the interactive sandbox
    # prompt or the `BOOTSTRAP_SANDBOX=1` non-interactive override.
    is_sandbox: bool = False

    # Populated by `ephemeral_secrets`. `github_token` comes from the
    # sops-nix-decrypted /run/secrets/bootstrap-github-token file.
    # `bootstrap_age_key_file` is the on-disk path to the bootstrap age
    # key (default /var/lib/nixos-bootstrap/age-key, pre-staged by the
    # operator) — the register phase passes it via SOPS_AGE_KEY_FILE
    # env to `sops updatekeys` when re-keying bot-secrets.yaml +
    # secrets.yaml. `repr=False` on the token keeps it out of log lines.
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
