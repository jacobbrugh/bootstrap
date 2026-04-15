"""register phase — add this host to `registry.toml` and `.sops.yaml`.

The hostname prompt + Darwin rename happen at CLI entry (see cli.py),
before this phase runs. `ctx.hostname` is already the final chosen name
by the time `run()` starts, and the SSH phase has already uploaded a
key built from that name.

Flow:

1. Ensure the canonical dotfiles checkout exists, is clean, and up-to-date.
2. Load `registry.toml` and `.sops.yaml`, branch on
   `(host_in_registry, local_age_key_exists, anchor_matches_pubkey)`:
     Case A: host NOT in registry → REGISTER NEW HOST sub-flow.
     Case B: host in registry, local key exists & matches an anchor by
             pubkey content → skip to symlink step.
     Case C: host in registry, local key MISSING → prompt to regenerate,
             then REPLACE EXISTING HOST KEY sub-flow.
     Case D: host in registry, local key exists but doesn't match any
             anchor → hard fail with manual-recovery instructions.
3. Register / re-register: generate or extract the host's age key,
   add it to `registry.toml` and `.sops.yaml`, `sops updatekeys` every
   affected secret file, verify the new host can decrypt, commit, push.
   The destructive block (file saves → updatekeys → commit → push) is
   wrapped in `git_ops.transactional_edit` so any exception does
   `git reset --hard` back to the HEAD we saw on entry, rolling back
   both uncommitted edits and local-but-unpushed commits.
4. Install the OS-specific default flake-path symlink. This runs in a
   `try/finally` covering the whole phase, so it fires on normal exit,
   early return (dry-run short-circuit, Case B skip), or any exception
   from the body — ensuring `darwin-rebuild` / `nixos-rebuild` /
   `home-manager` can resolve the flake even if a mid-phase crash
   left the registration incomplete.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path

from bootstrap.lib import (
    age_ops,
    gh,
    git_ops,
    host_info,
    prompts,
    registry_toml,
    sops_ops,
    sops_yaml,
    symlinks,
)
from bootstrap.lib.errors import (
    BootstrapError,
    DecisionTreeError,
    UserAbort,
)
from bootstrap.lib.paths import (
    DOTFILES_GIT_REMOTE,
    SOPS_AGE_KEY_FILE,
)
from bootstrap.lib.runtime import Context

NAME = "register"

_log = logging.getLogger(__name__)

# Tags that, when present on a host, exclude the host from `nix/secrets.yaml`
# and scope it to `nix/bot-secrets.yaml` only. Mirrors the creation_rules
# structure in `.sops.yaml`.
_NON_SENSITIVE_TAGS = frozenset({"sandbox", "kubevirt"})

# Repo-relative paths the register phase is allowed to modify.
_REGISTRY_REL = Path("nix/config/hosts/registry.toml")
_SOPS_YAML_REL = Path(".sops.yaml")
_BOT_SECRETS_REL = Path("nix/bot-secrets.yaml")
_SECRETS_REL = Path("nix/secrets.yaml")


async def run(ctx: Context) -> None:
    # Wrap the whole phase in try/finally so `_ensure_symlink` fires on
    # every exit path — normal completion, early return (dry-run
    # short-circuit, Case B skip), or any exception. If register crashes
    # mid-phase, the flake symlink still gets installed so a manual
    # darwin-rebuild / nixos-rebuild / home-manager can resolve the flake.
    try:
        # 1. Canonical repo ------------------------------------------------
        await git_ops.clone_or_pull(
            DOTFILES_GIT_REMOTE,
            ctx.canonical_repo,
            dry_run=ctx.dry_run,
        )

        # In dry-run mode, clone_or_pull is a no-op (git clone is
        # destructive=True), so on a fresh machine the canonical repo
        # doesn't exist after the "would run" log. Every subsequent step
        # in this phase reads files out of the canonical repo via direct
        # Path.read_text calls — not through sh.run — so the
        # dry-run/destructive plumbing doesn't apply to them. Detect the
        # missing-repo + dry-run state here and return early rather than
        # crashing on FileNotFoundError at the first registry_toml.load
        # call. The finally block still installs the symlink.
        if ctx.dry_run and not (ctx.canonical_repo / ".git").exists():
            _log.info(
                "[dry-run] canonical repo not present at %s — skipping rest of "
                "register phase. Run without --dry-run (or clone the dotfiles "
                "manually to %s) to exercise the full decision tree.",
                ctx.canonical_repo,
                ctx.canonical_repo,
            )
            return

        hostname = ctx.hostname

        # 2. Load registry + .sops.yaml and decide -------------------------
        registry_path = ctx.canonical_repo / _REGISTRY_REL
        sops_path = ctx.canonical_repo / _SOPS_YAML_REL
        bot_secrets_path = ctx.canonical_repo / _BOT_SECRETS_REL
        secrets_path = ctx.canonical_repo / _SECRETS_REL

        registry = registry_toml.load(registry_path)
        sops_doc = sops_yaml.load(sops_path)
        anchor_name = f"host_{hostname}"  # default used only for genuinely new hosts
        host_in_registry = registry_toml.has_host(registry, hostname)
        local_key_present = SOPS_AGE_KEY_FILE.exists()

        if host_in_registry and local_key_present:
            local_pubkey = await age_ops.extract_public_key(SOPS_AGE_KEY_FILE)
            # Match by pubkey VALUE, not anchor name. The existing .sops.yaml
            # uses ad-hoc anchor names that predate the host_<hostname>
            # convention: pc_jacobmac for mac1, server_nix1..5 for the NixOS
            # hosts, server_wsl1, server_lima1. Looking up by name would miss
            # every one of those and fall into a duplicate-anchor trap.
            existing_anchor = sops_yaml.find_anchor_by_pubkey(sops_doc, local_pubkey)
            if existing_anchor is not None:
                _log.info(
                    "host %s already registered under anchor %s — skipping edit",
                    hostname,
                    existing_anchor,
                )
                return
            raise DecisionTreeError(
                f"local age key at {SOPS_AGE_KEY_FILE} does not match any anchor "
                f"in .sops.yaml. Either restore the correct key file, or delete "
                f"this key file and re-run to generate + register a fresh one."
            )

        if host_in_registry and not local_key_present:
            regenerate = await prompts.confirm(
                f"Host {hostname} is registered but no local age key exists at "
                f"{SOPS_AGE_KEY_FILE}. Generate a new key and replace the registered one?",
                default=False,
                non_interactive=ctx.non_interactive,
            )
            if not regenerate:
                raise UserAbort(f"declined to regenerate missing age key for {hostname}")
            # Strip the stale anchor + every alias reference to it. The
            # add_age_key call further down will then re-declare the anchor
            # (same name, new pubkey) and add_to_creation_rule reinstates
            # the alias references.
            sops_yaml.remove_age_key(sops_doc, anchor_name)

        # 3. Register / re-register ----------------------------------------
        tags = await _select_tags(ctx)
        system = host_info.system_string()

        touched_sops: list[Path] = [_SOPS_YAML_REL, _BOT_SECRETS_REL]
        if not _NON_SENSITIVE_TAGS.intersection(tags):
            touched_sops.append(_SECRETS_REL)

        pubkey = await _ensure_age_key(ctx)

        # Build a git identity env for the commit. Fresh bootstrap machines
        # don't have `git config --global user.name/email` set yet, so we
        # derive it from the authenticated GitHub user (whose token we
        # already have in ctx from ephemeral_secrets) and pass it via
        # GIT_{AUTHOR,COMMITTER}_{NAME,EMAIL}. In dry-run, ctx.github_token
        # is None, the commit is a "would run" log, and env is irrelevant.
        #
        # BOOTSTRAP_TEST_GIT_AUTHOR_{NAME,EMAIL} env vars bypass the
        # `gh api user` call — used by the test-register CI job which
        # doesn't have a real GitHub token to call the API with.
        commit_env: dict[str, str] | None = None
        if not ctx.dry_run:
            test_name = os.environ.get("BOOTSTRAP_TEST_GIT_AUTHOR_NAME")
            test_email = os.environ.get("BOOTSTRAP_TEST_GIT_AUTHOR_EMAIL")
            if test_name and test_email:
                identity = gh.GitIdentity(name=test_name, email=test_email)
            else:
                assert ctx.github_token is not None
                identity = await gh.get_git_identity(ctx.github_token)
            commit_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": identity.name,
                "GIT_AUTHOR_EMAIL": identity.email,
                "GIT_COMMITTER_NAME": identity.name,
                "GIT_COMMITTER_EMAIL": identity.email,
            }

        # Wrap destructive edits in a transactional context: on any exception
        # before we successfully push, git reset --hard to the HEAD we had on
        # entry. Covers dirty working tree AND local-but-unpushed commits.
        async with git_ops.transactional_edit(ctx.canonical_repo, dry_run=ctx.dry_run):
            if host_in_registry:
                _log.info("re-registering %s (existing entry)", hostname)
            else:
                registry_toml.add_host(registry, hostname, system=system, tags=tags)
                _log.info("added %s to registry.toml", hostname)

            sops_yaml.add_age_key(sops_doc, anchor_name, pubkey)
            sops_yaml.add_to_creation_rule(
                sops_doc,
                _BOT_SECRETS_REL.as_posix() + "$",
                anchor_name,
            )
            if _SECRETS_REL in touched_sops:
                sops_yaml.add_to_creation_rule(
                    sops_doc,
                    _SECRETS_REL.as_posix() + "$",
                    anchor_name,
                )

            if not ctx.dry_run:
                registry_toml.save(registry, registry_path)
                sops_yaml.save(sops_doc, sops_path)

                # sops updatekeys — re-encrypt secret files against the new
                # recipient list. Gated on `not ctx.dry_run` because in dry-run
                # `ctx.bootstrap_age_key_file` is None (ephemeral_secrets never
                # touched 1Password) and we'd crash on the assert below.
                assert ctx.bootstrap_age_key_file is not None
                await sops_ops.update_keys(
                    bot_secrets_path,
                    age_key_file=ctx.bootstrap_age_key_file,
                    repo=ctx.canonical_repo,
                    dry_run=ctx.dry_run,
                )
                if _SECRETS_REL in touched_sops:
                    await sops_ops.update_keys(
                        secrets_path,
                        age_key_file=ctx.bootstrap_age_key_file,
                        repo=ctx.canonical_repo,
                        dry_run=ctx.dry_run,
                    )

                # Verify the re-encryption actually added the new host's key.
                # We read with the local (NEW) age key, not the bootstrap
                # key — if the new host key isn't in the recipient list,
                # this fails.
                await sops_ops.verify_decrypt(
                    bot_secrets_path,
                    age_key_file=SOPS_AGE_KEY_FILE,
                    repo=ctx.canonical_repo,
                )
                if _SECRETS_REL in touched_sops:
                    await sops_ops.verify_decrypt(
                        secrets_path,
                        age_key_file=SOPS_AGE_KEY_FILE,
                        repo=ctx.canonical_repo,
                    )

            commit_msg = _format_commit_message(hostname, system, tags, touched_sops)
            await git_ops.commit(
                ctx.canonical_repo,
                [_REGISTRY_REL, *touched_sops],
                commit_msg,
                dry_run=ctx.dry_run,
                env=commit_env,
            )
            await git_ops.push(ctx.canonical_repo, dry_run=ctx.dry_run)
    finally:
        # 4. Symlink default flake path ------------------------------------
        await _ensure_symlink(ctx)


# ── helpers ────────────────────────────────────────────────────────────


async def _ensure_age_key(ctx: Context) -> str:
    """Generate the host's own age keypair if missing. Return the public key."""
    if SOPS_AGE_KEY_FILE.exists():
        return await age_ops.extract_public_key(SOPS_AGE_KEY_FILE)
    return await age_ops.generate_keypair(SOPS_AGE_KEY_FILE, dry_run=ctx.dry_run)


async def _select_tags(ctx: Context) -> list[str]:
    """Prompt the user for tags, enumerated from `nix/config/tags/*.nix`."""
    tags_dir = ctx.canonical_repo / "nix" / "config" / "tags"
    if not tags_dir.exists():
        raise BootstrapError(f"tags directory missing: {tags_dir}")
    choices = sorted(f.stem for f in tags_dir.glob("*.nix") if f.stem != "default")
    if not choices:
        raise BootstrapError(f"no tag modules found under {tags_dir}")
    return await prompts.checkbox(
        "select tags for this host (space to toggle, enter to confirm):",
        choices=choices,
        non_interactive=ctx.non_interactive,
    )


async def _ensure_symlink(ctx: Context) -> None:
    """Install the OS default flake-path symlink if not already correct."""
    await symlinks.install_flake_symlink(ctx.platform, dry_run=ctx.dry_run)


def _format_commit_message(
    hostname: str,
    system: str,
    tags: Iterable[str],
    touched_sops: Iterable[Path],
) -> str:
    tag_list = ", ".join(sorted(tags)) or "(none)"
    rekeyed = ", ".join(p.as_posix() for p in touched_sops if p != _SOPS_YAML_REL)
    return (
        f"bootstrap: register host {hostname}\n"
        f"\n"
        f"- system: {system}\n"
        f"- tags: {tag_list}\n"
        f"- anchor: host_{hostname}\n"
        f"- re-keyed: {rekeyed}\n"
        f"\n"
        f"Generated by bootstrap-register."
    )
