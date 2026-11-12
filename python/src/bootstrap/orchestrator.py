from __future__ import annotations

import logging
import os

from bootstrap.lib import git_ops, log, registry_toml, secrets, sops_yaml
from bootstrap.lib.errors import BootstrapError, ShellError
from bootstrap.lib.paths import DOTFILES_REGISTRY_REL, DOTFILES_SOPS_YAML_REL
from bootstrap.lib.runtime import Context
from bootstrap.phases.common import prereqs as common_prereqs
from bootstrap.phases.common import register as common_register
from bootstrap.phases.common import ssh as common_ssh
from bootstrap.phases.darwin import keychain as darwin_keychain
from bootstrap.phases.darwin import post as darwin_post
from bootstrap.phases.darwin import prereqs as darwin_prereqs
from bootstrap.phases.darwin import secret_zero as darwin_secret_zero
from bootstrap.phases.darwin import switch as darwin_switch
from bootstrap.phases.linux import post as linux_post
from bootstrap.phases.linux import secret_zero as linux_secret_zero
from bootstrap.phases.linux import switch as linux_switch
from bootstrap.phases.nixos import post as nixos_post
from bootstrap.phases.nixos import secret_zero as nixos_secret_zero
from bootstrap.phases.nixos import switch as nixos_switch
from bootstrap.platform import Platform

_log = logging.getLogger(__name__)


async def run_prereqs(ctx: Context) -> None:
    with log.phase("prereqs"):
        await _dispatch_prereqs(ctx)


async def run_secret_zero(ctx: Context) -> None:
    with log.phase("secret_zero"):
        await _dispatch_secret_zero(ctx)


async def run_ssh(ctx: Context) -> None:
    async with secrets.ephemeral_secrets(ctx):
        await _run_ssh_inner(ctx)


async def run_register(ctx: Context) -> None:
    async with secrets.ephemeral_secrets(ctx):
        await _run_register_inner(ctx)


async def run_switch(ctx: Context) -> None:
    with log.phase("switch"):
        await _dispatch_switch(ctx)


async def run_post(ctx: Context) -> None:
    with log.phase("post"):
        await _dispatch_post(ctx)


async def run_full(ctx: Context) -> None:
    await run_prereqs(ctx)
    if await _registered(ctx):
        _log.info(
            "host is registered — its key decrypts its secrets and its host files "
            "are committed; switch + post"
        )
        with log.phase("switch"):
            await _dispatch_switch(ctx)
        await run_post(ctx)
        return
    await run_secret_zero(ctx)
    async with secrets.ephemeral_secrets(ctx):
        await _run_ssh_inner(ctx)
        await _run_register_inner(ctx)
    with log.phase("switch"):
        await _dispatch_switch(ctx)
    await run_post(ctx)


async def _registered(ctx: Context) -> bool:
    """register's post-condition, against a clone force-synced to origin/main.

    This is what decides whether a run needs the bootstrap secrets at all — not
    a marker like the flake symlink, which register installs even when it fails
    (and which resolves even when dangling), so a host that never registered or
    whose key drifted would otherwise switch onto secrets it cannot decrypt on
    every retry, forever. No clone means register has not run: it clones. The
    refresh matters on its own: the switch builds this host from the clone via
    the flake symlink, and a host whose first switch failed would otherwise
    rebuild that same first-run commit on every retry while every pushed fix
    sat unbuilt on origin. dotfiles_remote returns the clone's existing remote
    when the tier key is already gone, and is_registered needs only the host's
    own key and the clone, so a healthy re-run never enters ephemeral_secrets —
    on darwin, that is what spares a switch-only re-run a fresh 1Password
    approval.
    """
    if not (ctx.canonical_repo / ".git").exists():
        return False
    if not await _refresh_canonical_clone(ctx):
        return False
    registry = registry_toml.load(ctx.canonical_repo / DOTFILES_REGISTRY_REL)
    if not registry_toml.has_host(registry, ctx.hostname):
        return False
    sops_doc = sops_yaml.load(ctx.canonical_repo / DOTFILES_SOPS_YAML_REL)
    tags = registry_toml.get_tags(registry, ctx.hostname)
    return await common_register.is_registered(ctx, sops_doc, tags)


async def _refresh_canonical_clone(ctx: Context) -> bool:
    """Advance the canonical clone to origin/main; False when that needs the bot token.

    This runs before any secret is loaded. An ssh remote authenticates with the
    host's own key, and an https remote on a host that has switched into
    dotfiles with the system git credential dotfiles installs. A sandbox host
    whose first run cloned over https and then failed before its first switch
    has neither: its fetch of the private repo is refused, and aborting there
    made every retry fail the same way without ever reaching the token. Such a
    host is simply not registered yet: the full path loads the token and
    register refreshes the clone with it.
    """
    remote = await common_register.dotfiles_remote(ctx)
    over_https = remote.startswith("https://")
    with log.phase("refresh-clone"):
        try:
            await git_ops.clone_or_pull(
                remote,
                ctx.canonical_repo,
                dry_run=ctx.dry_run,
                # No terminal prompt: with no credential the fetch fails at
                # once instead of asking a console for a username.
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"} if over_https else None,
            )
        except ShellError as exc:
            if not over_https:
                raise
            _log.info(
                "the clone's https remote cannot be fetched without the bootstrap "
                "token (%s) — registering with it",
                exc,
            )
            return False
    return True


async def _run_ssh_inner(ctx: Context) -> None:
    with log.phase("ssh"):
        await common_ssh.run(ctx)
    if ctx.platform is Platform.DARWIN:
        with log.phase("keychain"):
            await darwin_keychain.run(ctx)


async def _run_register_inner(ctx: Context) -> None:
    # Inside ephemeral_secrets, where the token exists: register hands it to
    # its own git processes for an https remote (lib/git_ops.git_token_env)
    # and nothing is written to the system. The switch needs no credential —
    # dotfiles has no private flake input and builds from the local clone.
    with log.phase("register"):
        await common_register.run(ctx)


async def _dispatch_prereqs(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            await darwin_prereqs.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL | Platform.LINUX_HM:
            await common_prereqs.run(ctx)
        case _:
            raise BootstrapError(f"no prereqs phase for platform {ctx.platform.value}")


async def _dispatch_secret_zero(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            await darwin_secret_zero.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL:
            await nixos_secret_zero.run(ctx)
        case Platform.LINUX_HM:
            await linux_secret_zero.run(ctx)
        case _:
            raise BootstrapError(f"no secret_zero phase for platform {ctx.platform.value}")


async def _dispatch_switch(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            await darwin_switch.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL:
            await nixos_switch.run(ctx)
        case Platform.LINUX_HM:
            await linux_switch.run(ctx)
        case _:
            raise BootstrapError(f"no switch phase for platform {ctx.platform.value}")


async def _dispatch_post(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            await darwin_post.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL:
            await nixos_post.run(ctx)
        case Platform.LINUX_HM:
            await linux_post.run(ctx)
        case _:
            raise BootstrapError(f"no post phase for platform {ctx.platform.value}")
