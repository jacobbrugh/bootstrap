"""Clone the bootstrap flake and splice host-specific files into it.

Both nixos-install/nixos-rebuild targets that consume ``host-hardware.nix``
(`#bootstrap-secret-zero` from an already-booted host, and the flashed
install `bootstrap-flash` performs) go through this module: materialize a
writable copy of the flake source, write ``host-hardware.nix``, and splice
the optional ``host-networking.nix`` and any extra files. The copy is
consumed as a ``path:`` flake, so every file — splices included — is part
of the flake without git tracking.

Two ``host-hardware.nix`` shapes:

- ``inject_bootloader=False`` (secret_zero on a booted host): the machine's
  ``hardware-configuration.nix`` is copied in verbatim. The host already has
  a working bootloader on disk; the config deliberately declares no
  ``boot.loader.*`` so activation leaves it alone.
- ``inject_bootloader=True`` (bootstrap-flash onto a blank disk): a wrapper
  is written that imports the copied ``hardware-configuration.nix`` and adds
  systemd-boot + the target hostname — ``nixos-generate-config`` writes
  bootloader settings only to the un-imported ``configuration.nix``, and
  without a loader stanza ``nixos-install`` runs NixOS's warn-and-exit-0
  no-bootloader stub and the disk doesn't boot. The wrapper also carries
  the host's LUKS unlock stanza (``lib.boot_config``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from bootstrap.lib import boot_config, sh
from bootstrap.lib.errors import BootstrapError

_log = logging.getLogger(__name__)

# Last-resort fallback, and the one an admin host building the installer media
# actually uses: the public repo, fetched as a tarball with no credential.
_DEFAULT_FLAKE = "github:jacobbrugh/bootstrap"

# bootstrap-create-usb bakes the flake source into the installer system at this
# path (installer-usb.nix), and bootstrap-flash copies it onto every target it
# installs: flash runs before the machine has a network it can count on, and a
# first boot must not depend on GitHub answering.
_BAKED_FLAKE = Path("/etc/bootstrap-flake")


def flake_ref() -> str:
    """The bootstrap flake reference: env seam → baked media copy → GitHub."""
    env = os.environ.get("BOOTSTRAP_FLAKE")
    if env:
        return env
    if _BAKED_FLAKE.exists():
        return f"path:{_BAKED_FLAKE.resolve()}"
    return _DEFAULT_FLAKE


async def source_path(ref: str) -> Path:
    """Resolve a flake reference to its source tree in the store."""
    result = await sh.run(
        ["nix", "flake", "metadata", "--json", ref],
        destructive=False,
    )
    path = json.loads(result.stdout).get("path")
    if not isinstance(path, str):
        raise BootstrapError(f"nix flake metadata for {ref!r} returned no path")
    return Path(path)


_NIX_DARWIN_UNPINNED = "github:nix-darwin/nix-darwin"


async def nix_darwin_pinned_ref() -> str:
    """The nix-darwin CLI ref, pinned to this flake's locked input rev.

    The bootstrap flake keeps a ``nix-darwin`` input solely so its lock
    entry version-pins the `nix run nix-darwin -- switch` first-activation
    fallback in the darwin switch phase — running master at runtime would
    let an upstream CLI change break a fresh Mac. Falls back to master
    with a warning if the lock metadata is unreadable.
    """
    try:
        result = await sh.run(
            ["nix", "flake", "metadata", "--json", flake_ref()],
            destructive=False,
        )
        locked = json.loads(result.stdout)["locks"]["nodes"]["nix-darwin"]["locked"]
        return f"github:{locked['owner']}/{locked['repo']}/{locked['rev']}"
    except (BootstrapError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _log.warning(
            "could not resolve the pinned nix-darwin rev (%s) — falling back to master",
            exc,
        )
        return _NIX_DARWIN_UNPINNED


async def prepare_clone(*, flake_ref: str, clone: Path) -> None:
    """Materialize a writable copy of the flake source at `clone`.

    A plain directory consumed as a `path:` flake — every file is part of
    the flake without git-tracking, splices included, and resolving the
    source via `nix flake metadata` works for git, github, and the baked
    store-path reference alike (the latter fully offline).
    """
    src = await source_path(flake_ref)
    if clone.exists():
        _log.info("removing stale clone at %s", clone)
        shutil.rmtree(clone)
    _log.info("copying flake source %s → %s", src, clone)
    shutil.copytree(src, clone)
    # Store copies arrive read-only; the splice needs to write.
    await sh.run(["chmod", "-R", "u+w", str(clone)], destructive=True)


def splice(
    *,
    clone: Path,
    hardware_config: Path,
    networking_config: Path | None = None,
    hostname: str | None = None,
    inject_bootloader: bool = False,
    luks_unlock: str = boot_config.LUKS_UNLOCK_DEFAULT,
) -> None:
    """Write host files into the clone (consumed as a `path:` flake).

    `hardware_config` must exist — a missing hardware config means the
    machine state this splice encodes is absent, and evaluating against an
    empty stub would build an unbootable/incomplete system.
    """
    if not hardware_config.is_file():
        raise BootstrapError(
            f"hardware configuration not found at {hardware_config} — "
            "run nixos-generate-config first (bootstrap-flash does this; on "
            "a booted host the installer should have left it in /etc/nixos)"
        )
    if inject_bootloader and hostname is None:
        raise BootstrapError("inject_bootloader requires a hostname to bake")

    hw_dst = clone / "host-hardware.nix"

    if inject_bootloader:
        assert hostname is not None
        inner_dst = clone / "hardware-configuration.nix"
        _log.info("splicing %s → %s", hardware_config, inner_dst)
        shutil.copyfile(hardware_config, inner_dst)
        _log.info("writing bootloader+hostname wrapper (%s) → %s", luks_unlock, hw_dst)
        hw_dst.write_text(boot_config.flake_wrapper(hostname=hostname, method=luks_unlock))
    else:
        _log.info("splicing %s → %s", hardware_config, hw_dst)
        shutil.copyfile(hardware_config, hw_dst)

    if networking_config is not None and networking_config.is_file():
        net_dst = clone / "host-networking.nix"
        _log.info("splicing %s → %s", networking_config, net_dst)
        shutil.copyfile(networking_config, net_dst)

    # The clone ships a .git dir when the source was a git checkout; a
    # path: flake with a .git dir present would be fetched as a *git* flake
    # and hide the spliced (untracked) files. Drop it so the tree is a
    # plain path flake.
    git_dir = clone / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)
