"""The register phase: this host joins the dotfiles repo.

Clone dotfiles, verify the host is in ``host_registry.toml``, give it an age
identity, add that identity to every dotfiles sops file its tier decrypts,
rekey those files, harvest the files bootstrap-flash staged under
``/etc/nixos``, commit and push.

Tier decides two things here (lib/secrets.py decides the tier):

* the remote — a privileged host clones and pushes over SSH with the key the
  ssh phase uploaded to the ``jacobbrugh`` account; a sandbox host, which has
  no key on any account, uses https with the bot token, which this phase hands
  to its own git processes (lib/git_ops.git_token_env);
* the sops files — the host's anchor is added to **every** creation rule
  whose recipients include the tier's bootstrap anchor. That set is what a
  host of the tier decrypts at activation (secrets.yaml, gate-secrets,
  bot-secrets, the logscrub literals, the eval fixtures for privileged;
  bot-secrets and the eval fixtures for sandbox); a hard-coded pair of files once left every new
  privileged host unable to decrypt the logscrub literals at its first switch.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from bootstrap.lib import (
    age_ops,
    gh,
    git_ops,
    registry_toml,
    secrets,
    sh,
    sops_anchors,
    sops_ops,
    sops_yaml,
    symlinks,
)
from bootstrap.lib.errors import BootstrapError, ShellError
from bootstrap.lib.paths import (
    DOTFILES_GIT_REMOTE_HTTPS,
    DOTFILES_GIT_REMOTE_OVERRIDE,
    DOTFILES_GIT_REMOTE_SSH,
    DOTFILES_HOSTS_DIR_REL,
    DOTFILES_REGISTRY_REL,
    DOTFILES_SOPS_YAML_REL,
    SOPS_AGE_KEY_FILE,
)
from bootstrap.lib.runtime import Context
from bootstrap.platform import Platform

NAME = "register"

_log = logging.getLogger(__name__)


_HARVEST_SOURCES: dict[str, Path] = {
    "hardware.nix": Path("/etc/nixos/hardware-configuration.nix"),
    "networking.nix": Path("/etc/nixos/networking.nix"),
    # bootstrap-flash generates this from the registry's luksUnlock: the
    # bootloader a harvested hardware.nix never declares, plus this host's LUKS
    # unlock. Without it the first dotfiles switch fails NixOS's
    # boot.loader.grub.devices assertion.
    "boot.nix": Path("/etc/nixos/boot.nix"),
}


async def dotfiles_remote(ctx: Context) -> str:
    """The remote this host clones and pushes: env seam, else by tier.

    With the key already shredded (a re-run after a completed bootstrap) the
    tier is unknown; an existing clone then keeps whichever remote it has.
    """
    if DOTFILES_GIT_REMOTE_OVERRIDE:
        return DOTFILES_GIT_REMOTE_OVERRIDE
    if ctx.tier is None and (ctx.canonical_repo / ".git").exists():
        return await git_ops.remote_url(ctx.canonical_repo)
    return DOTFILES_GIT_REMOTE_SSH if ctx.is_privileged else DOTFILES_GIT_REMOTE_HTTPS


def tier_anchor(ctx: Context, tags: Iterable[str] = ()) -> str:
    """The bootstrap anchor of this host's tier.

    From the loaded bootstrap key when there is one. Without it — a completed
    host whose key post shredded — from the registry: a host tagged sandbox (or
    a BOOTSTRAP_SANDBOX run) is sandbox, every other host is privileged. That is
    the same fact `run` refuses on when a privileged key meets a sandbox tag.
    """
    if ctx.tier is not None:
        return sops_anchors.PRIVILEGED_ANCHOR if ctx.is_privileged else sops_anchors.SANDBOX_ANCHOR
    if ctx.force_sandbox or "sandbox" in tags:
        return sops_anchors.SANDBOX_ANCHOR
    return sops_anchors.PRIVILEGED_ANCHOR


async def _host_key_decrypts(ctx: Context, rel_files: list[Path]) -> bool:
    """Whether the age key on this disk decrypts every one of the files."""
    if not SOPS_AGE_KEY_FILE.exists():
        _log.info("no host age key at %s", SOPS_AGE_KEY_FILE)
        return False
    host_key_env = {"SOPS_AGE_KEY_FILE": str(SOPS_AGE_KEY_FILE)}
    for rel in rel_files:
        try:
            await sops_ops.verify_decrypt(
                ctx.canonical_repo / rel,
                env_overlay=host_key_env,
                repo=ctx.canonical_repo,
            )
        except ShellError as exc:
            _log.info("the host key does not decrypt %s: %s", rel, exc)
            return False
    return True


async def is_registered(ctx: Context, sops_doc: CommentedMap, tags: Iterable[str]) -> bool:
    """register's post-condition, checked against the state the switch consumes.

    True iff the key at SOPS_AGE_KEY_FILE decrypts every sops file of this
    host's tier as they stand in the canonical clone, and nothing bootstrap-flash
    staged under /etc/nixos differs from what dotfiles carries for this host.
    Needs neither the bootstrap key nor the network, so a completed host can
    answer it after post shredded everything else.
    """
    tags = list(tags)
    anchor = tier_anchor(ctx, tags)
    rules = sops_yaml.tier_rules_for_host(sops_doc, anchor, f"host_{ctx.hostname}")
    if rules is None:
        _log.info("host %s is not a recipient of every %s rule", ctx.hostname, anchor)
        return False
    files = await _files_matching(ctx.canonical_repo, rules)
    if not files:
        _log.info("no tracked sops file matches the tier rules — nothing proves the key")
        return False
    if not await _host_key_decrypts(ctx, files):
        return False
    pending = _harvest_nixos_host_configs(
        platform=ctx.platform,
        canonical_repo=ctx.canonical_repo,
        hostname=ctx.hostname,
        dry_run=ctx.dry_run,
        check_only=True,
    )
    if pending:
        _log.info("staged host files differ from dotfiles: %s", ", ".join(map(str, pending)))
        return False
    return True


async def _commit_identity(ctx: Context) -> gh.GitIdentity:
    """Privileged commits are authored by the privileged account, sandbox by the bot."""
    assert ctx.github_token is not None
    if ctx.is_privileged:
        return await gh.get_git_identity(await secrets.privileged_github_token(ctx))
    return await gh.get_git_identity(ctx.github_token)


async def run(ctx: Context) -> None:
    try:
        remote = await dotfiles_remote(ctx)
        # An https remote (a sandbox host) authenticates with the bot token on
        # this process alone; an ssh remote uses the key the ssh phase
        # uploaded. Nothing is written to the system for either.
        git_env = (
            git_ops.git_token_env(ctx.github_token)
            if remote.startswith("https://") and ctx.github_token is not None
            else None
        )
        await git_ops.clone_or_pull(
            remote,
            ctx.canonical_repo,
            dry_run=ctx.dry_run,
            env=git_env,
        )

        if ctx.dry_run and not (ctx.canonical_repo / ".git").exists():
            _log.info(
                "[dry-run] canonical repo not present at %s — skipping rest of "
                "register phase. Run without --dry-run (or clone the dotfiles "
                "manually to %s) to exercise the full decision tree.",
                ctx.canonical_repo,
                ctx.canonical_repo,
            )
            await _ensure_symlink(ctx)
            return

        hostname = ctx.hostname

        registry = registry_toml.load(ctx.canonical_repo / DOTFILES_REGISTRY_REL)
        anchor_name = f"host_{hostname}"

        if not registry_toml.has_host(registry, hostname):
            raise BootstrapError(
                f"host {hostname!r} is not in host_registry.toml. "
                f"Add it via `nix run path:{ctx.canonical_repo}#add-host` "
                f"(from any dotfiles checkout), then re-run bootstrap."
            )

        # The registry is in the repo, so where this host keeps its checkout
        # is only known once it is cloned: move the clone there before
        # anything records its path.
        pinned = registry_toml.get_dotfiles_dir(registry, hostname)
        if pinned is not None and Path(pinned) != ctx.canonical_repo:
            await _relocate_clone(ctx, Path(pinned))

        sops_path = ctx.canonical_repo / DOTFILES_SOPS_YAML_REL
        sops_doc = sops_yaml.load(sops_path)

        tags = registry_toml.get_tags(registry, hostname)
        _log.info(
            "host %s found in host_registry.toml with tags %s",
            hostname,
            tags or "(none)",
        )
        if ctx.is_privileged and "sandbox" in tags:
            raise BootstrapError(
                f"host {hostname!r} is tagged sandbox in host_registry.toml but this "
                "bootstrap holds the privileged age key. A sandbox host is flashed "
                "from sandbox media; refusing before any key is registered or "
                "uploaded."
            )
        _log.info("tier: %s", "privileged" if ctx.is_privileged else "sandbox")

        # The post-condition, not a marker: the key on this disk decrypts every
        # tier file in the freshly synced clone and the staged host files are
        # committed. A key that is merely *named* in .sops.yaml proves nothing
        # about what the encrypted files are sealed to.
        if await is_registered(ctx, sops_doc, tags):
            _log.info(
                "host %s is registered: its key decrypts every %s sops file and its "
                "host files are committed — nothing to do",
                hostname,
                tier_anchor(ctx, tags),
            )
            await _ensure_symlink(ctx)
            return

        local_key_present = SOPS_AGE_KEY_FILE.exists()
        if local_key_present:
            local_pubkey = await age_ops.extract_public_key(SOPS_AGE_KEY_FILE)
            existing_anchor = sops_yaml.find_anchor_by_pubkey(sops_doc, local_pubkey)
            if existing_anchor is not None:
                _log.info(
                    "host %s's key is anchored as %s but is not sealed into every tier "
                    "file (or its host files changed) — repairing",
                    hostname,
                    existing_anchor,
                )
            else:
                _log.info(
                    "keyfile at %s isn't anchored in .sops.yaml — will add its pubkey "
                    "under host_%s",
                    SOPS_AGE_KEY_FILE,
                    hostname,
                )
                if sops_yaml.has_anchor(sops_doc, anchor_name):
                    # A previous registration of this host — a register that
                    # generated this key and failed before its push, or an
                    # earlier flash — left its key under our anchor name.
                    # add_age_key refuses a duplicate anchor, so the stale one
                    # goes the way the key-absent branch below sends it.
                    _log.warning(
                        "anchor %s names another key (a previous registration of this "
                        "host) — replacing it with the key on this disk",
                        anchor_name,
                    )
                    sops_yaml.remove_age_key(sops_doc, anchor_name)
        else:
            _log.warning(
                "host %s is registered but no local age key exists at %s — "
                "auto-regenerating and replacing the registered pubkey",
                hostname,
                SOPS_AGE_KEY_FILE,
            )
            try:
                sops_yaml.remove_age_key(sops_doc, anchor_name)
            except BootstrapError as exc:
                _log.warning(
                    "no stale anchor named %s to remove (%s) — proceeding",
                    anchor_name,
                    exc,
                )

        pubkey = await _ensure_age_key(ctx)

        existing_anchor_for_pubkey = sops_yaml.find_anchor_by_pubkey(sops_doc, pubkey)
        if existing_anchor_for_pubkey is not None:
            anchor_name = existing_anchor_for_pubkey
            _log.info(
                "pubkey already declared in .sops.yaml under anchor %s — reusing",
                anchor_name,
            )

        commit_env: dict[str, str] | None = None
        if not ctx.dry_run:
            identity = await _commit_identity(ctx)
            commit_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": identity.name,
                "GIT_AUTHOR_EMAIL": identity.email,
                "GIT_COMMITTER_NAME": identity.name,
                "GIT_COMMITTER_EMAIL": identity.email,
            }

        async with git_ops.transactional_edit(ctx.canonical_repo, dry_run=ctx.dry_run):
            harvested_paths = _harvest_nixos_host_configs(
                platform=ctx.platform,
                canonical_repo=ctx.canonical_repo,
                hostname=hostname,
                dry_run=ctx.dry_run,
            )

            rules_before = set(sops_yaml.rules_with_anchor(sops_doc, anchor_name))
            if existing_anchor_for_pubkey is None:
                sops_yaml.add_age_key(sops_doc, anchor_name, pubkey)
            rule_regexes = sops_yaml.add_to_rules_with_anchor(
                sops_doc, tier_anchor(ctx, tags), anchor_name
            )
            rekey_files = await _files_matching(ctx.canonical_repo, rule_regexes)
            # The encrypted files are rewritten only when the sealing has to
            # change: the anchor was added or replaced, the host joined a rule,
            # or the key on disk does not open every file. A host whose only
            # drift is a re-harvested hardware.nix commits that alone —
            # re-encrypting files it already decrypts would be churn.
            sops_changed = existing_anchor_for_pubkey is None or set(rule_regexes) != rules_before
            needs_rekey = sops_changed or not await _host_key_decrypts(ctx, rekey_files)
            _log.info(
                "%s joins %d sops rule(s) → %d file(s): %s%s",
                anchor_name,
                len(rule_regexes),
                len(rekey_files),
                ", ".join(p.as_posix() for p in rekey_files),
                "" if needs_rekey else " (already sealed to this host's key — not rekeyed)",
            )
            touched_sops: list[Path] = [DOTFILES_SOPS_YAML_REL, *rekey_files] if needs_rekey else []

            if needs_rekey and not ctx.dry_run:
                sops_yaml.save(sops_doc, sops_path)

                bootstrap_key_env = ctx.sops_env_overlay
                if not bootstrap_key_env:
                    raise BootstrapError(
                        "registering this host needs the bootstrap age key to "
                        "rekey the dotfiles sops files, but none is loaded "
                        "(shredded by an earlier completed bootstrap?). The age "
                        "key on this disk no longer opens this host's secrets and "
                        "cannot be sealed in without it: re-flash this host from "
                        "media built from the current bootstrap "
                        "(bootstrap-create-usb), or point BOOTSTRAP_AGE_KEY_FILE "
                        "at a copy of the bootstrap age key."
                    )
                host_key_env = {"SOPS_AGE_KEY_FILE": str(SOPS_AGE_KEY_FILE)}
                for rel in rekey_files:
                    await sops_ops.update_keys(
                        ctx.canonical_repo / rel,
                        env_overlay=bootstrap_key_env,
                        repo=ctx.canonical_repo,
                        dry_run=ctx.dry_run,
                    )
                    await sops_ops.verify_decrypt(
                        ctx.canonical_repo / rel,
                        env_overlay=host_key_env,
                        repo=ctx.canonical_repo,
                    )

            to_commit = [*touched_sops, *harvested_paths]
            if to_commit:
                commit_msg = _format_commit_message(hostname, tags, touched_sops)
                await git_ops.commit(
                    ctx.canonical_repo,
                    to_commit,
                    commit_msg,
                    dry_run=ctx.dry_run,
                    env=commit_env,
                )
                await git_ops.push(ctx.canonical_repo, dry_run=ctx.dry_run, env=git_env)
            else:
                _log.info("nothing to commit for %s", hostname)
    except BaseException:
        # No symlink on failure. Installing it here used to mark the host
        # "bootstrapped" for the next run, which then skipped register forever;
        # and install_flake_symlink moves a real /etc/nixos aside, which is
        # exactly where the staged hardware.nix / boot.nix a retry must harvest
        # still live. A failed register leaves the host as it found it.
        raise
    # The switch builds this host from the clone through the flake symlink:
    # the last step of a register that succeeded, never of one that did not.
    await _ensure_symlink(ctx)


async def _files_matching(repo: Path, path_regexes: list[str]) -> list[Path]:
    """Every tracked file one of the rule regexes selects, in tracked order.

    Matched the way sops resolves a rule for a file: an unanchored search of
    the repo-relative path (Go's regexp.MatchString).
    """
    result = await sh.run(["git", "-C", str(repo), "ls-files", "-z"], destructive=False)
    compiled = [re.compile(r) for r in path_regexes]
    matched: list[Path] = []
    for raw in result.stdout.split("\0"):
        if raw and any(c.search(raw) for c in compiled):
            matched.append(Path(raw))
    return matched


def _harvest_nixos_host_configs(
    *,
    platform: Platform,
    canonical_repo: Path,
    hostname: str,
    dry_run: bool,
    check_only: bool = False,
) -> list[Path]:
    """Copy what bootstrap-flash staged under /etc/nixos into config/hosts/<host>/.

    A file already in dotfiles is overwritten when its content differs: a
    re-flash regenerates the LUKS and filesystem UUIDs in
    hardware-configuration.nix, and a switch onto the previous disk's UUIDs
    would not boot. An identical file is left alone so a repeat bootstrap
    commits nothing. With check_only the files that WOULD be written are
    returned and nothing is touched — the byte comparison is register's
    "harvest done" condition.
    """
    if platform not in (Platform.NIXOS, Platform.NIXOS_WSL):
        return []

    dest_dir_rel = DOTFILES_HOSTS_DIR_REL / hostname
    dest_dir_abs = canonical_repo / dest_dir_rel
    written: list[Path] = []

    for dest_name, src in _HARVEST_SOURCES.items():
        if not src.is_file():
            continue
        dest_rel = dest_dir_rel / dest_name
        dest_abs = canonical_repo / dest_rel
        if dest_abs.exists():
            if dest_abs.read_bytes() == src.read_bytes():
                if not check_only:
                    _log.info("%s already present in dotfiles and identical — unchanged", dest_rel)
                continue
            if not check_only:
                _log.warning(
                    "%s differs from %s on this host — overwriting (a re-flash changes "
                    "the disk it names)",
                    dest_rel,
                    src,
                )
        if check_only:
            written.append(dest_rel)
            continue
        if dry_run:
            _log.info("would copy %s → %s", src, dest_abs)
            continue
        dest_dir_abs.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest_abs)
        _log.info("harvested %s → %s", src, dest_rel)
        written.append(dest_rel)
    return written


async def _ensure_age_key(ctx: Context) -> str:
    if SOPS_AGE_KEY_FILE.exists():
        return await age_ops.extract_public_key(SOPS_AGE_KEY_FILE)
    return await age_ops.generate_keypair(SOPS_AGE_KEY_FILE, dry_run=ctx.dry_run)


async def _relocate_clone(ctx: Context, wanted: Path) -> None:
    """Move the fresh clone to the host's registry `dotfilesDir` and use it from there."""
    if not wanted.is_absolute():
        # A Windows-native path: that checkout is not this run's clone.
        _log.info("dotfilesDir %s is not a path on this platform — leaving the clone", wanted)
        return
    if wanted.exists() and (not wanted.is_dir() or any(wanted.iterdir())):
        raise BootstrapError(
            f"host {ctx.hostname!r} sets dotfilesDir = {str(wanted)!r}, but that path "
            f"already holds something other than the clone at {ctx.canonical_repo}"
        )
    _log.info("moving the dotfiles clone to this host's dotfilesDir: %s", wanted)
    if not ctx.dry_run:
        wanted.parent.mkdir(parents=True, exist_ok=True)
        if wanted.exists():
            wanted.rmdir()
        shutil.move(str(ctx.canonical_repo), str(wanted))
        ctx.canonical_repo = wanted


async def _ensure_symlink(ctx: Context) -> None:
    await symlinks.install_flake_symlink(ctx.platform, ctx.canonical_repo, dry_run=ctx.dry_run)


def _format_commit_message(
    hostname: str,
    tags: Iterable[str],
    touched_sops: Iterable[Path],
) -> str:
    tag_list = ", ".join(sorted(tags)) or "(none)"
    rekeyed = ", ".join(p.as_posix() for p in touched_sops if p != DOTFILES_SOPS_YAML_REL)
    return (
        f"bootstrap: harvest+rekey for {hostname}\n"
        f"\n"
        f"- tags: {tag_list}\n"
        f"- anchor: host_{hostname}\n"
        f"- re-keyed: {rekeyed}\n"
        f"\n"
        f"Generated by bootstrap-register."
    )
