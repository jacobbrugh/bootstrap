from __future__ import annotations

import logging
import shutil
from pathlib import Path

from bootstrap.lib import flake_splice, sh
from bootstrap.lib.errors import BootstrapError, ShellError
from bootstrap.lib.paths import BOOTSTRAP_AGE_KEY
from bootstrap.lib.runtime import Context
from bootstrap.platform import Platform

NAME = "secret_zero"

_log = logging.getLogger(__name__)

_AGE_KEY_PATH = BOOTSTRAP_AGE_KEY
_PLAINTEXT_TOKEN_PATH = Path("/run/secrets/bootstrap-github-token")

_HOST_HARDWARE_SRC = Path("/etc/nixos/hardware-configuration.nix")
_HOST_NETWORKING_SRC = Path("/etc/nixos/networking.nix")

_CLONE_DIR = Path("/tmp/bootstrap-secret-zero-flake")


def _bootstrap_age_key_present() -> bool:
    try:
        return _AGE_KEY_PATH.is_file()
    except PermissionError:
        return True


def _token_materialized() -> bool:
    """The phase's post-condition: the bootstrap token is on disk and non-empty."""
    try:
        return _PLAINTEXT_TOKEN_PATH.is_file() and _PLAINTEXT_TOKEN_PATH.stat().st_size > 0
    except PermissionError:
        return True


async def run(ctx: Context) -> None:
    if _token_materialized():
        _log.info(
            "%s present — secret_zero already materialized, skipping",
            _PLAINTEXT_TOKEN_PATH,
        )
        return

    if not _bootstrap_age_key_present():
        # A completed bootstrap: post shredded the key, and the dotfiles
        # generation declares no bootstrap token. Nothing here can or need be
        # materialized; whether anything else is needed is register's call.
        _log.info(
            "no bootstrap token at %s and no bootstrap age key at %s — a completed "
            "bootstrap; nothing to materialize",
            _PLAINTEXT_TOKEN_PATH,
            _AGE_KEY_PATH,
        )
        return

    nix_path = shutil.which("nix")
    if nix_path is None:
        raise BootstrapError(
            "nix not found in PATH — the bootstrap wrapper should have put it there"
        )

    # flake_splice.flake_ref() resolves env seam -> baked copy -> GitHub. A
    # freshly flashed machine uses the baked copy at /etc/bootstrap-flake, so
    # its first boot does not depend on reaching GitHub.
    flake_ref = flake_splice.flake_ref()

    if ctx.platform is Platform.NIXOS_WSL:
        await _activate_wsl(ctx, flake_ref=flake_ref)
    else:
        await _activate_bare_metal(ctx, flake_ref=flake_ref)


async def _activate_wsl(ctx: Context, *, flake_ref: str) -> None:
    target = f"{flake_ref}#bootstrap-secret-zero-wsl"
    _log.info("activating NixOS-WSL secret-zero via %s", target)
    if ctx.dry_run:
        _log.info("would run: sudo nixos-rebuild switch --flake %s", target)
        return
    await _run_nixos_rebuild(target)


async def _activate_bare_metal(ctx: Context, *, flake_ref: str) -> None:
    clone = _CLONE_DIR
    _log.info("preparing local clone of bootstrap flake at %s", clone)

    if ctx.dry_run:
        _log.info(
            "would clone %s into %s, splice host-hardware.nix, run nixos-rebuild",
            flake_ref,
            clone,
        )
        return

    await flake_splice.prepare_clone(flake_ref=flake_ref, clone=clone)
    # An already-booted host keeps whatever bootloader it has, so the splice
    # deliberately injects no boot.loader.* stanza (inject_bootloader=False).
    flake_splice.splice(
        clone=clone,
        hardware_config=_HOST_HARDWARE_SRC,
        networking_config=_HOST_NETWORKING_SRC,
    )

    target = f"path:{clone}#bootstrap-secret-zero"
    _log.info("activating bare-metal NixOS secret-zero via %s", target)
    await _run_nixos_rebuild(target)


async def _run_nixos_rebuild(target: str) -> None:
    await sh.prime_sudo(dry_run=False)
    result = await sh.sudo_run(
        ["nixos-rebuild", "switch", "--flake", target],
        capture=False,
        check=False,
        destructive=True,
    )
    if result.ok():
        return
    if result.returncode == 4 and _PLAINTEXT_TOKEN_PATH.is_file():
        _log.warning(
            "nixos-rebuild exited 4 (some unit restarts failed) but %s was "
            "materialized — accepting the partial-success activation",
            _PLAINTEXT_TOKEN_PATH,
        )
        return
    raise ShellError(list(result.cmd), result.returncode, result.stderr or "")
