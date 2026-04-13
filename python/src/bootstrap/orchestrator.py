"""Phase orchestration.

Exposes a `run_<phase>(ctx)` function per phase that internally OS-dispatches
to the right `phases/<os>/<phase>.py` module. The secrets-dependent phases
(`ssh`, `register`) wrap themselves in `secrets.ephemeral_secrets` so
standalone invocations (`nix run github:jacobbrugh/bootstrap#ssh`) work.

`run_full(ctx)` runs the whole platform-appropriate sequence and opens the
secrets context exactly once, spanning the secrets-dependent subset of
phases — so we don't re-prompt 1Password for every phase.
"""

from __future__ import annotations

from bootstrap.lib import log, secrets
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context
from bootstrap.phases.common import register as common_register
from bootstrap.phases.common import ssh as common_ssh
from bootstrap.phases.darwin import keychain as darwin_keychain
from bootstrap.phases.darwin import onepassword as darwin_onepassword
from bootstrap.phases.darwin import post as darwin_post
from bootstrap.phases.darwin import prereqs as darwin_prereqs
from bootstrap.phases.darwin import switch as darwin_switch
from bootstrap.phases.linux import onepassword as linux_onepassword
from bootstrap.phases.linux import post as linux_post
from bootstrap.phases.linux import prereqs as linux_prereqs
from bootstrap.phases.linux import switch as linux_switch
from bootstrap.phases.nixos import onepassword as nixos_onepassword
from bootstrap.phases.nixos import post as nixos_post
from bootstrap.phases.nixos import prereqs as nixos_prereqs
from bootstrap.phases.nixos import switch as nixos_switch
from bootstrap.platform import Platform

# ── individual standalone phase entry points ──────────────────────────


def run_prereqs(ctx: Context) -> None:
    with log.phase("prereqs"):
        _dispatch_prereqs(ctx)


def run_onepassword(ctx: Context) -> None:
    with log.phase("onepassword"):
        _dispatch_onepassword(ctx)


def run_ssh(ctx: Context) -> None:
    """Standalone `ssh` entry point — opens its own secrets context."""
    with secrets.ephemeral_secrets(ctx):
        _run_ssh_inner(ctx)


def run_register(ctx: Context) -> None:
    """Standalone `register` entry point — opens its own secrets context."""
    with secrets.ephemeral_secrets(ctx):
        _run_register_inner(ctx)


def run_switch(ctx: Context) -> None:
    with log.phase("switch"):
        _dispatch_switch(ctx)


def run_post(ctx: Context) -> None:
    with log.phase("post"):
        _dispatch_post(ctx)


# Keychain (Darwin-only) has no standalone orchestrator helper — it's only
# ever reached as a sub-step of `run_ssh` inside `_run_ssh_inner`. There's
# no `bootstrap keychain` CLI subcommand and no `apps.keychain` flake app,
# which is deliberate: running keychain without a freshly generated SSH
# key from the ssh phase has no useful meaning.


# ── full-sequence entry point ─────────────────────────────────────────


def run_full(ctx: Context) -> None:
    """Run the entire platform-appropriate phase list in one invocation."""
    run_prereqs(ctx)
    run_onepassword(ctx)
    with secrets.ephemeral_secrets(ctx):
        _run_ssh_inner(ctx)
        _run_register_inner(ctx)
        with log.phase("switch"):
            _dispatch_switch(ctx)
    run_post(ctx)


# ── OS dispatch helpers ────────────────────────────────────────────────


def _run_ssh_inner(ctx: Context) -> None:
    """ssh phase body assuming the secrets context is already open."""
    with log.phase("ssh"):
        common_ssh.run(ctx)
    if ctx.platform is Platform.DARWIN:
        with log.phase("keychain"):
            darwin_keychain.run(ctx)


def _run_register_inner(ctx: Context) -> None:
    """register phase body assuming the secrets context is already open."""
    with log.phase("register"):
        common_register.run(ctx)


def _dispatch_prereqs(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            darwin_prereqs.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL:
            nixos_prereqs.run(ctx)
        case Platform.LINUX_HM:
            linux_prereqs.run(ctx)
        case _:
            raise BootstrapError(f"no prereqs phase for platform {ctx.platform.value}")


def _dispatch_onepassword(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            darwin_onepassword.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL:
            nixos_onepassword.run(ctx)
        case Platform.LINUX_HM:
            linux_onepassword.run(ctx)
        case _:
            raise BootstrapError(f"no onepassword phase for platform {ctx.platform.value}")


def _dispatch_switch(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            darwin_switch.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL:
            nixos_switch.run(ctx)
        case Platform.LINUX_HM:
            linux_switch.run(ctx)
        case _:
            raise BootstrapError(f"no switch phase for platform {ctx.platform.value}")


def _dispatch_post(ctx: Context) -> None:
    match ctx.platform:
        case Platform.DARWIN:
            darwin_post.run(ctx)
        case Platform.NIXOS | Platform.NIXOS_WSL:
            nixos_post.run(ctx)
        case Platform.LINUX_HM:
            linux_post.run(ctx)
        case _:
            raise BootstrapError(f"no post phase for platform {ctx.platform.value}")
