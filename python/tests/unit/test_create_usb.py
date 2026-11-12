from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.phases.nixos import create_usb
from tests.unit.conftest import ShRecorder


def test_partition_paths_dry_run() -> None:
    esp, root = create_usb._partition_paths(Path("/dev/sdz"), dry_run=True)
    assert (esp, root) == (Path("/dev/sdz1"), Path("/dev/sdz2"))


def test_partition_paths_missing_is_error(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="could not find partitions"):
        create_usb._partition_paths(tmp_path / "not-a-device", dry_run=False)


def test_age_key_candidates_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", "/explicit/keys.txt")
    monkeypatch.setenv("SUDO_USER", "jacob")
    candidates = create_usb._age_key_file_candidates()
    assert candidates[0] == Path("/explicit/keys.txt")
    assert Path("/home/jacob/.config/sops/age/keys.txt") in candidates


def test_age_key_candidates_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    candidates = create_usb._age_key_file_candidates()
    assert candidates == [Path.home() / ".config" / "sops" / "age" / "keys.txt"]


def _age_keygen(path: Path) -> str:
    """Generate an identity file, return its public key."""
    out = subprocess.run(
        ["age-keygen", "-o", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    # age-keygen prints "Public key: age1..." on stderr
    return out.stderr.strip().split()[-1]


def _sops_yaml(flake_src: Path, **anchors: str) -> None:
    """Write a .sops.yaml declaring the given anchor -> pubkey pairs, in order."""
    flake_src.mkdir(exist_ok=True)
    lines = ["keys:"]
    lines += [f"  - &{name} {pub}" for name, pub in anchors.items()]
    (flake_src / ".sops.yaml").write_text("\n".join(lines) + "\n")


def _secret_of(key_file: Path) -> str:
    return next(
        line for line in key_file.read_text().splitlines() if line.startswith("AGE-SECRET-KEY-")
    )


def test_bootstrap_identity_prefers_privileged_over_sandbox(tmp_path: Path) -> None:
    """Real-device media must carry the privileged key.

    The repo declares &bootstrap_sandbox FIRST, so selecting by list position
    picked the sandbox key — the tier .sops.yaml calls "never a recipient of
    privileged dotfiles secrets", leaving the flashed host unable to decrypt
    fleet secrets. Selection is by anchor name, so declaration order is
    irrelevant.
    """
    sandbox_key = tmp_path / "sandbox.txt"
    privileged_key = tmp_path / "privileged.txt"
    sandbox_pub = _age_keygen(sandbox_key)
    privileged_pub = _age_keygen(privileged_key)

    combined = tmp_path / "keys.txt"
    combined.write_text(
        f"# operator keys\n{_secret_of(sandbox_key)}\n{_secret_of(privileged_key)}\n"
    )

    flake_src = tmp_path / "flake"
    _sops_yaml(
        flake_src,
        bootstrap_sandbox=sandbox_pub,
        bootstrap_privileged=privileged_pub,
    )

    chosen = asyncio.run(create_usb._bootstrap_identity(flake_src, combined))
    assert chosen == _secret_of(privileged_key)


def test_bootstrap_identity_falls_back_to_sandbox(tmp_path: Path) -> None:
    """CI stages only the sandbox key; media creation still has to work there."""
    sandbox_key = tmp_path / "sandbox.txt"
    sandbox_pub = _age_keygen(sandbox_key)
    privileged_pub = _age_keygen(tmp_path / "privileged.txt")

    flake_src = tmp_path / "flake"
    _sops_yaml(
        flake_src,
        bootstrap_sandbox=sandbox_pub,
        bootstrap_privileged=privileged_pub,
    )

    chosen = asyncio.run(create_usb._bootstrap_identity(flake_src, sandbox_key))
    assert chosen == _secret_of(sandbox_key)


def test_bootstrap_identity_no_match_is_error(tmp_path: Path) -> None:
    key_a = tmp_path / "a.txt"
    _age_keygen(key_a)

    flake_src = tmp_path / "flake"
    _sops_yaml(
        flake_src,
        bootstrap_sandbox=_age_keygen(tmp_path / "s.txt"),
        bootstrap_privileged=_age_keygen(tmp_path / "p.txt"),
    )

    with pytest.raises(BootstrapError, match="no identity in"):
        asyncio.run(create_usb._bootstrap_identity(flake_src, key_a))


def test_bootstrap_identity_missing_anchor_is_error(tmp_path: Path) -> None:
    """A renamed or dropped anchor must fail loudly, not silently pick the other."""
    key_a = tmp_path / "a.txt"
    pub_a = _age_keygen(key_a)

    flake_src = tmp_path / "flake"
    _sops_yaml(flake_src, bootstrap_sandbox=pub_a)

    with pytest.raises(BootstrapError, match="bootstrap_privileged"):
        asyncio.run(create_usb._bootstrap_identity(flake_src, key_a))


def test_run_refuses_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(BootstrapError, match="must run as root"):
        asyncio.run(create_usb.run(device=Path("/dev/null"), dry_run=False))


def test_teardown_noop_when_no_stale_mapping(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No leftover mapping -> the preflight touches nothing."""
    monkeypatch.setattr(create_usb, "_mapping_exists", lambda: False)
    asyncio.run(create_usb._teardown_stale_mapping(dry_run=False))
    assert sh_recorder.calls == []


def test_teardown_skipped_on_dry_run(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry run never runs destructive teardown even if a mapping is present."""
    monkeypatch.setattr(create_usb, "_mapping_exists", lambda: True)
    asyncio.run(create_usb._teardown_stale_mapping(dry_run=True))
    assert sh_recorder.calls == []


def _fake_teardown_run(
    argvs: list[tuple[str, ...]], *, close_fails: bool
) -> Callable[..., Awaitable[SimpleNamespace]]:
    async def fake_run(
        cmd: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        cwd: Path | None = None,
        env: object = None,
        input_text: str | None = None,
        dry_run: bool = False,
        destructive: bool = True,
    ) -> SimpleNamespace:
        argv = tuple(cmd)
        argvs.append(argv)
        failed = close_fails and argv[:2] == ("cryptsetup", "close")
        stdout = "/mnt-stale\n/mnt-stale/inner\n" if argv[0] == "findmnt" else ""
        return SimpleNamespace(
            cmd=argv,
            returncode=1 if failed else 0,
            stdout=stdout,
            stderr="",
            ok=lambda: not failed,
        )

    return fake_run


def test_teardown_unmounts_every_mountpoint_then_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stale mapping's mountpoints come from findmnt and are unmounted
    deepest first; `umount -R` on the device path itself unmounts nothing."""
    present = iter([True, False])
    monkeypatch.setattr(create_usb, "_mapping_exists", lambda: next(present))
    argvs: list[tuple[str, ...]] = []
    monkeypatch.setattr(sh, "run", _fake_teardown_run(argvs, close_fails=False))
    asyncio.run(create_usb._teardown_stale_mapping(dry_run=False))
    umounts = [a for a in argvs if a[0] == "umount"]
    assert umounts == [("umount", "-R", "/mnt-stale/inner"), ("umount", "-R", "/mnt-stale")]
    assert argvs.index(umounts[-1]) < argvs.index(("cryptsetup", "close", "usb-crypt"))
    assert not [a for a in argvs if a[0] == "dmsetup"]


def test_teardown_force_removes_when_close_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A holder still blocks `cryptsetup close`, so the mapping is force-removed
    with `dmsetup remove -f` to free the name for this run to reopen."""
    present = iter([True, True, False])
    monkeypatch.setattr(create_usb, "_mapping_exists", lambda: next(present))
    argvs: list[tuple[str, ...]] = []
    monkeypatch.setattr(sh, "run", _fake_teardown_run(argvs, close_fails=True))
    asyncio.run(create_usb._teardown_stale_mapping(dry_run=False))
    assert ("cryptsetup", "close", "usb-crypt") in argvs
    assert ("dmsetup", "remove", "-f", "usb-crypt") in argvs


def test_teardown_reports_a_mapping_that_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node still present after every step fails here, naming the cause,
    instead of as `cryptsetup open`'s "already exists"."""
    monkeypatch.setattr(create_usb, "_mapping_exists", lambda: True)
    argvs: list[tuple[str, ...]] = []
    monkeypatch.setattr(sh, "run", _fake_teardown_run(argvs, close_fails=True))
    with pytest.raises(BootstrapError, match="still present"):
        asyncio.run(create_usb._teardown_stale_mapping(dry_run=False))
