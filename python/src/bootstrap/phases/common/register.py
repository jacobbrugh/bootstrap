"""register phase — add this host to `registry.toml` and `.sops.yaml`.

Full decision tree:

1. Ensure the canonical dotfiles checkout exists, is clean, and up-to-date.
2. Prompt for the desired hostname (default: current system hostname).
3. On Darwin, if the chosen hostname differs from the current system
   hostname, rename the machine via `scutil --set` (requires sudo).
4. Branch on `(host_in_registry, local_age_key_exists, keys_match)`:
     Case A: host NOT in registry → REGISTER NEW HOST sub-flow.
     Case B: host in registry, local key exists & matches → skip to symlink step.
     Case C: host in registry, local key MISSING → prompt to regenerate,
             then REPLACE EXISTING HOST KEY sub-flow.
     Case D: host in registry, local key exists but MISMATCHES registered
             pubkey → hard fail with manual-recovery instructions.
5. Ensure the OS-specific default flake path is a symlink to the canonical
   repo (always runs, idempotent).

Every sub-flow that modifies files uses `git_ops.diff_scope_check` as a
paranoid guard before committing, so we never accidentally bundle
unrelated changes into the registration commit.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from bootstrap.lib import (
    age_ops,
    git_ops,
    host_info,
    log,
    prompts,
    registry_toml,
    sh,
    sops_ops,
    sops_yaml,
    symlinks,
)
from bootstrap.lib.errors import (
    BootstrapError,
    DecisionTreeError,
    PrereqMissing,
    UserAbort,
)
from bootstrap.lib.paths import (
    DOTFILES_GIT_REMOTE,
    SOPS_AGE_KEY_FILE,
)
from bootstrap.lib.runtime import Context
from bootstrap.platform import Platform

NAME = "register"

_log = log.get(__name__)

# Tags that, when present on a host, exclude the host from `nix/secrets.yaml`
# and scope it to `nix/bot-secrets.yaml` only. Mirrors the creation_rules
# structure in `.sops.yaml`.
_NON_SENSITIVE_TAGS = frozenset({"sandbox", "kubevirt"})

# Repo-relative paths the register phase is allowed to modify.
_REGISTRY_REL = Path("nix/config/hosts/registry.toml")
_SOPS_YAML_REL = Path(".sops.yaml")
_BOT_SECRETS_REL = Path("nix/bot-secrets.yaml")
_SECRETS_REL = Path("nix/secrets.yaml")


def run(ctx: Context) -> None:
    if ctx.bootstrap_age_key_file is None:
        raise PrereqMissing(
            "ctx.bootstrap_age_key_file",
            where="wrap in `secrets.ephemeral_secrets(ctx)`",
        )

    # 1. Canonical repo ----------------------------------------------------
    git_ops.clone_or_pull(
        DOTFILES_GIT_REMOTE,
        ctx.canonical_repo,
        dry_run=ctx.dry_run,
    )

    # In dry-run mode, clone_or_pull is a no-op (git clone is destructive=True),
    # so on a fresh machine the canonical repo doesn't exist after the "would
    # run" log. Every subsequent step in this phase reads files out of the
    # canonical repo via direct Path.read_text calls — not through sh.run —
    # so the dry-run/destructive plumbing doesn't apply to them. Detect the
    # missing-repo + dry-run state here and skip the rest of the phase with
    # a clear message, rather than crashing on FileNotFoundError at the first
    # registry_toml.load call.
    if ctx.dry_run and not (ctx.canonical_repo / ".git").exists():
        _log.info(
            "[dry-run] canonical repo not present at %s — skipping rest of "
            "register phase. Run without --dry-run (or clone the dotfiles "
            "manually to %s) to exercise the full decision tree.",
            ctx.canonical_repo,
            ctx.canonical_repo,
        )
        _ensure_symlink(ctx)
        return

    # 2. Hostname prompt ---------------------------------------------------
    # `scutil --get LocalHostName` on macOS returns the user-friendly name
    # like "Jacobs-Mac-mini", but our registry requires `[a-z][a-z0-9-]*`
    # (DNS-safe lowercase). Lowercase the default so accepting it Just
    # Works. If the user types a mixed-case name, validate_hostname raises
    # a clear error — that's fine.
    chosen = prompts.text(
        "hostname for this machine:",
        default=ctx.hostname.lower(),
        non_interactive=ctx.non_interactive,
    )
    host_info.validate_hostname(chosen)

    # 3. Darwin rename if needed ------------------------------------------
    if chosen != ctx.hostname and ctx.platform is Platform.DARWIN:
        _log.info("renaming machine from %s to %s", ctx.hostname, chosen)
        sh.prime_sudo(dry_run=ctx.dry_run)
        host_info.rename_darwin(chosen, dry_run=ctx.dry_run)
        ctx.hostname = chosen
    elif chosen != ctx.hostname:
        # On Linux/NixOS we don't auto-rename — the nixos-rebuild config
        # controls the hostname directly. The user picked a name; trust it.
        ctx.hostname = chosen

    hostname = ctx.hostname

    # 4. Load registry + .sops.yaml and decide -----------------------------
    registry_path = ctx.canonical_repo / _REGISTRY_REL
    sops_path = ctx.canonical_repo / _SOPS_YAML_REL
    bot_secrets_path = ctx.canonical_repo / _BOT_SECRETS_REL
    secrets_path = ctx.canonical_repo / _SECRETS_REL

    registry = registry_toml.load(registry_path)
    sops_doc = sops_yaml.load(sops_path)
    anchor_name = f"host_{hostname}"
    host_in_registry = registry_toml.has_host(registry, hostname)
    local_key_present = SOPS_AGE_KEY_FILE.exists()

    if host_in_registry and local_key_present:
        local_pubkey = age_ops.extract_public_key(SOPS_AGE_KEY_FILE)
        registered_pubkey = sops_yaml.get_registered_pubkey(sops_doc, anchor_name)
        if registered_pubkey is not None:
            if registered_pubkey == local_pubkey:
                _log.info(
                    "host %s already registered with matching age key — skipping edit",
                    hostname,
                )
                _ensure_symlink(ctx)
                return
            raise DecisionTreeError(
                f"local age key at {SOPS_AGE_KEY_FILE} does not match the pubkey "
                f"registered for {hostname!r} in .sops.yaml. Either restore the "
                f"correct key file, or delete the {anchor_name} anchor from .sops.yaml "
                f"and re-run to replace it."
            )
        # Host is in registry.toml but has no sops anchor — partial state.
        _log.warning(
            "host %s in registry.toml but no %s anchor in .sops.yaml — treating as re-register",
            hostname,
            anchor_name,
        )

    if host_in_registry and not local_key_present:
        regenerate = prompts.confirm(
            f"Host {hostname} is registered but no local age key exists at "
            f"{SOPS_AGE_KEY_FILE}. Generate a new key and replace the registered one?",
            default=False,
            non_interactive=ctx.non_interactive,
        )
        if not regenerate:
            raise UserAbort(f"declined to regenerate missing age key for {hostname}")
        # Strip the stale anchor + every alias reference to it. The add_age_key
        # call further down will then re-declare the anchor (same name, new
        # pubkey) and add_to_creation_rule reinstates the alias references.
        sops_yaml.remove_age_key(sops_doc, anchor_name)

    # 5. Register / re-register --------------------------------------------
    # Everything from here through `git push` is wrapped in a transactional
    # edit context: if any step fails, the working tree AND any commits
    # created in the block are rolled back to the entry HEAD so the next
    # bootstrap run starts clean. Only the host's own age-key file on disk
    # lives outside the transaction — it's created idempotently and a
    # later run will pick it up.
    tags = _select_tags(ctx)
    system = host_info.system_string()

    touched_sops: list[Path] = [_SOPS_YAML_REL, _BOT_SECRETS_REL]
    if not _NON_SENSITIVE_TAGS.intersection(tags):
        touched_sops.append(_SECRETS_REL)

    with git_ops.transactional_edit(ctx.canonical_repo, dry_run=ctx.dry_run):
        pubkey = _ensure_age_key(ctx)

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

        # sops updatekeys — re-encrypt secret files against the new recipient list.
        sops_ops.update_keys(
            bot_secrets_path,
            age_key_file=ctx.bootstrap_age_key_file,
            repo=ctx.canonical_repo,
            dry_run=ctx.dry_run,
        )
        if _SECRETS_REL in touched_sops:
            sops_ops.update_keys(
                secrets_path,
                age_key_file=ctx.bootstrap_age_key_file,
                repo=ctx.canonical_repo,
                dry_run=ctx.dry_run,
            )

        # Verify the re-encryption actually added the new host's key. We
        # read with the local (NEW) age key, not the bootstrap key — if
        # the new host key isn't in the recipient list, this fails.
        if not ctx.dry_run:
            sops_ops.verify_decrypt(
                bot_secrets_path,
                age_key_file=SOPS_AGE_KEY_FILE,
                repo=ctx.canonical_repo,
            )
            if _SECRETS_REL in touched_sops:
                sops_ops.verify_decrypt(
                    secrets_path,
                    age_key_file=SOPS_AGE_KEY_FILE,
                    repo=ctx.canonical_repo,
                )

        # Paranoid scope check before committing.
        allowed: set[Path] = {_REGISTRY_REL, *touched_sops}
        if not ctx.dry_run:
            git_ops.diff_scope_check(ctx.canonical_repo, allowed)

        commit_msg = _format_commit_message(hostname, system, tags, touched_sops)
        git_ops.commit(
            ctx.canonical_repo,
            [_REGISTRY_REL, *touched_sops],
            commit_msg,
            dry_run=ctx.dry_run,
        )
        git_ops.push(ctx.canonical_repo, dry_run=ctx.dry_run)

    # 6. Symlink default flake path ---------------------------------------
    _ensure_symlink(ctx)


# ── helpers ────────────────────────────────────────────────────────────


def _ensure_age_key(ctx: Context) -> str:
    """Generate the host's own age keypair if missing. Return the public key."""
    if SOPS_AGE_KEY_FILE.exists():
        return age_ops.extract_public_key(SOPS_AGE_KEY_FILE)
    return age_ops.generate_keypair(SOPS_AGE_KEY_FILE, dry_run=ctx.dry_run)


def _select_tags(ctx: Context) -> list[str]:
    """Prompt the user for tags, enumerated from `nix/config/tags/*.nix`."""
    tags_dir = ctx.canonical_repo / "nix" / "config" / "tags"
    if not tags_dir.exists():
        raise BootstrapError(f"tags directory missing: {tags_dir}")
    choices = sorted(f.stem for f in tags_dir.glob("*.nix") if f.stem != "default")
    if not choices:
        raise BootstrapError(f"no tag modules found under {tags_dir}")
    return prompts.checkbox(
        "select tags for this host (space to toggle, enter to confirm):",
        choices=choices,
        non_interactive=ctx.non_interactive,
    )


def _ensure_symlink(ctx: Context) -> None:
    """Install the OS default flake-path symlink if not already correct."""
    symlinks.install_flake_symlink(ctx.platform, dry_run=ctx.dry_run)


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
