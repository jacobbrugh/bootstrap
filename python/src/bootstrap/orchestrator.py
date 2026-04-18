"""Phase orchestration.

Exposes an `async def run_<phase>(ctx)` function per phase that
OS-dispatches to the right `phases/<os>/<phase>.py` module. The secrets-
dependent phases (`ssh`, `register`) wrap themselves in an
`async with secrets.ephemeral_secrets(ctx):` block so standalone
invocations (`nix run github:jacobbrugh/bootstrap#ssh`) work.

`run_full(ctx)` runs the whole platform-appropriate sequence and opens
the secrets context exactly once, spanning the secrets-dependent subset
of phases — so we don't re-prompt 1Password for every phase.
"""

from __future__ import annotations

from bootstrap.lib import log, secrets
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context
from bootstrap.phases.common import onepassword as common_onepassword
from bootstrap.phases.common import post as common_post
from bootstrap.phases.common import prereqs as common_prereqs
from bootstrap.phases.common import register as common_register
from bootstrap.phases.common import ssh as common_ssh
from bootstrap.phases.darwin import keychain as darwin_keychain
from bootstrap.phases.darwin import onepassword as darwin_onepassword
from bootstrap.phases.darwin import post as darwin_post
from bootstrap.phases.darwin import prereqs as darwin_prereqs
from bootstrap.phases.darwin import switch as darwin_switch
from bootstrap.phases.linux import switch as linux_switch
from bootstrap.phases.nixos import switch as nixos_switch
from bootstrap.platform import Platform

# ── individual standalone phase entry points ──────────────────────────


async def run_prereqs(ctx: Context) -> None:
    with log.phase("prereqs"):
        await _dispatch_prereqs(ctx)


async def run_onepassword(ctx: Context) -> None:
    with log.phase("onepassword"):
        await _dispatch_onepassword(ctx)


async def run_ssh(ctx: Context) -> None:
    """Standalone `ssh` entry point — opens its own secrets context."""
    async with secrets.ephemeral_secrets(ctx):
        await _run_ssh_inner(ctx)


async def run_register(ctx: Context) -> None:
    """Standalone `register` entry point — opens its own secrets context."""
    async with secrets.ephemeral_secrets(ctx):
        await _run_register_inner(ctx)


async def run_switch(ctx: Context) -> None:
    with log.phase("switch"):
        await _dispatch_switch(ctx)


async def run_post(ctx: Context) -> None:
    with log.phase("post"):
        await _dispatch_post(ctx)


# Keychain (Darwin-only) has no standalone orchestrator helper — it's only
# ever reached as a sub-step of `run_ssh` inside `_run_ssh_inner`. There's
# no `bootstrap keychain` CLI subcommand and no `apps.keychain` flake app,
# which is deliberate: running keychain without a freshly generated SSH
# key from the ssh phase has no useful meaning.


# ── full-sequence entry point ─────────────────────────────────────────


async def run_full(ctx: Context) -> None:
    """Run the entire platform-appropriate phase list in one invocation.

    `ephemeral_secrets` wraps only ssh + register because those are the
    only phases that touch the bootstrap age key / GitHub token. The
    switch phase uses the HOST's own age key (written by register into
    ~/.config/sops/age/keys.txt) for sops-nix activation, not the
    bootstrap key, so we exit the secrets context before switching and
    shred the bootstrap key 10-20 minutes earlier.
    """
    await run_prereqs(ctx)
    await run_onepassword(ctx)
    async with secrets.ephemeral_secrets(ctx):
        await _run_ssh_inner(ctx)
        await _run_register_inner(ctx)
    with log.phase("switch"):
        await _dispatch_switch(ctx)
    await run_post(ctx)


# ── OS dispatch helpers ────────────────────────────────────────────────


async def _run_ssh_inner(ctx: Context) -> None:
    """ssh phase body assuming the secrets context is already open."""
    with log.phase("ssh"):
        await common_ssh.run(ctx)
    if ctx.platform is Platform.DARWIN:
        with log.phase("keychain"):
            await darwin_keychain.run(ctx)


async def _run_register_inner(ctx: Context) -> None:
    """register phase body assuming the secrets context is already open."""
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


async def _dispatch_onepassword(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            await darwin_onepassword.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL | Platform.LINUX_HM:
            await common_onepassword.run(ctx)
        case _:
            raise BootstrapError(f"no onepassword phase for platform {ctx.platform.value}")


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
        case Platform.NIXOS | Platform.NIXOS_WSL | Platform.LINUX_HM:
            await common_post.run(ctx)
        case _:
            raise BootstrapError(f"no post phase for platform {ctx.platform.value}")
