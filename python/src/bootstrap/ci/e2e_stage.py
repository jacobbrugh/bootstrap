"""bootstrap-e2e-stage <tier> — stage one e2e tier's fixtures on the CI daemon host.

Runs in the Jenkins `nix-bootstrap` pod before `nix build .#e2e-<tier>`. Every
e2e VM reads its fixtures from a directory the pod and the CI builder's nix-daemon both
see (the rw hostPath /tmp/bootstrap-e2e-shared, mounted into the guests over
9p), and this tool is the one writer of that directory:

* a dotfiles clone with the tier's host registered (what `nix run .#add-host`
  would have done), symlinks dropped (9p ELOOP), `receive.denyCurrentBranch
  updateInstead` so the guest can push to it;
* this checkout, copied as the bootstrap source the guest evaluates;
* the tier's bootstrap age key and the secrets it can decrypt — for the
  privileged tier an EPHEMERAL identity standing in for the real privileged
  key (which CI must never hold): the `bootstrap_privileged` anchor in both
  repos' .sops.yaml is rewritten to it, the bootstrap secrets are rekeyed to
  it, a `secrets-privileged.yaml` carrying the fixture token is encrypted to
  it, and every dotfiles file the privileged rule set selects is either
  rekeyed (when the sandbox key is also a recipient) or replaced by a
  shape-preserving dummy encrypted per its rule (secrets.yaml, the logscrub
  literals);
* a persistent `file://` binary cache the guests substitute from instead of
  fetching over qemu SLIRP: the closure the dotfiles CI already built on this
  daemon, both flakes with their inputs, and the installer toplevel.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from ruamel.yaml import YAML

from bootstrap.lib import log, sh, sops_anchors, sops_yaml
from bootstrap.lib.errors import BootstrapError, ShellError
from bootstrap.lib.runtime import Tier

_log = logging.getLogger(__name__)

SHARE_ROOT = Path("/tmp/bootstrap-e2e-shared")
CACHE_DIR = SHARE_ROOT / "cache"
#: Every fleet host closure the dotfiles CI built, rooted by its Warm-closures
#: stage (dotfiles Jenkinsfile). Its closure is what the guests' dotfiles
#: switch would otherwise download path by path.
CI_DOTFILES_PROFILE = Path("/nix/var/nix/profiles/ci-dotfiles")
DOTFILES_HTTPS = "https://github.com/jacobbrugh/dotfiles.git"
#: The value mock-gh treats as the jacobbrugh-owned token (tests/e2e-lib/mock-gh.nix).
PRIVILEGED_FIXTURE_TOKEN = "e2e-privileged-token"
LUKS_PASSPHRASE = "usb-e2e-passphrase"
DUMMY_VALUE = "e2e-dummy"
#: Cache entries older than this are pruned at stage start — the window the CI
#: builder's own collector keeps CI closures warm for.
CACHE_MAX_AGE_S = 14 * 24 * 3600
#: The two hosts the privileged e2e flashes; the second is the one it boots.
PRIVILEGED_HOSTS = ("usb-e2e-tang", "usb-e2e-initrd")

_REGISTRY_FIXTURE: dict[Tier, str] = {
    # An indexed template plus an override section, so the run exercises the
    # same expansion path fromRegistry.nix uses for the real fleet.
    Tier.SANDBOX: (
        '\n[e2e]\nsystem = "x86_64-linux"\nendIndex = 0\n\n[e2e0]\ntags = ["sandbox"]\n'
    ),
    # Untagged: privileged. The tang-tpm2 one is flashed and inspected on
    # disk; the initrd-key one is flashed AND booted, because unattended boot
    # with no TPM and no network is the property worth proving.
    Tier.PRIVILEGED: (
        f"\n[{PRIVILEGED_HOSTS[0]}]\n"
        'system = "x86_64-linux"\n'
        f"\n[{PRIVILEGED_HOSTS[1]}]\n"
        'system = "x86_64-linux"\n'
        'luksUnlock = "initrd-key"\n'
    ),
}


def registry_fixture(tier: Tier) -> str:
    return _REGISTRY_FIXTURE[tier]


def _sops_env(age_key: str) -> dict[str, str]:
    return {**os.environ, "SOPS_AGE_KEY": age_key}


async def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = await sh.run(["git", "-C", str(repo), *args], env=env, destructive=True)
    return result.stdout


def ensure_nix_conf(nix_conf: Path) -> None:
    """The daemon settings the vm-test build needs, appended once.

    sandbox = relaxed + the e2e drv's __noChroot run the build unsandboxed in
    the daemon's own netns (the guests' SLIRP egress needs real outbound
    network); extra-sandbox-paths keeps the share visible to a sandboxed
    rebuild of the same drv.
    """
    wanted = ["sandbox = relaxed", f"extra-sandbox-paths = {SHARE_ROOT}"]
    existing = nix_conf.read_text() if nix_conf.exists() else ""
    missing = [line for line in wanted if line not in existing.splitlines()]
    if missing:
        nix_conf.parent.mkdir(parents=True, exist_ok=True)
        with nix_conf.open("a") as fh:
            fh.write("".join(f"{line}\n" for line in missing))


def delete_symlinks(root: Path) -> int:
    """9p serves a symlink as ELOOP; the guest restores them from the index."""
    count = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            path.unlink()
            count += 1
    return count


async def copy_source(source: Path, dst: Path) -> None:
    """This source tree as the guest's bootstrap source: a fresh one-commit repo.

    Tracked files only — `result` out-links and caches never ride along. The
    guest evaluates it as a git+file flake and the dirty secrets.yaml (luks
    passphrase, rekeys) rides that ref, so it has to be a git repository; it
    is made here rather than copied, because the source may be a directory
    inside a larger repository, whose .git is neither here nor this tree's.
    """
    listing = await sh.run(["git", "-C", str(source), "ls-files", "-z"], destructive=False)
    for rel in listing.stdout.split("\0"):
        if not rel:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, target)
        shutil.copymode(source / rel, target)
    await sh.run(["git", "init", "-q", "-b", "main", str(dst)], destructive=True)
    await _git(dst, "add", "-A")
    await _git(
        dst,
        "-c",
        "user.email=ci@example.com",
        "-c",
        "user.name=ci",
        "commit",
        "-q",
        "-m",
        "the bootstrap source under test",
    )


# Every sops call runs with cwd = the repo that owns the file. sops finds
# .sops.yaml by walking up from the CURRENT directory, not the file's, and
# matches path_regex against the file's path relative to that config — so a
# call from the pod's workspace against /tmp/…/bootstrap-source/secrets.yaml
# resolved the checkout's .sops.yaml and matched nothing.


async def sops_set(repo: Path, rel: Path, key: str, value: str, *, age_key: str) -> None:
    await sh.run(
        ["sops", "set", str(rel), f'["{key}"]', json.dumps(value)],
        cwd=repo,
        env=_sops_env(age_key),
        destructive=True,
    )


async def sops_updatekeys(repo: Path, rel: Path, *, age_key: str) -> None:
    await sh.run(
        ["sops", "updatekeys", "--yes", str(rel)],
        cwd=repo,
        env=_sops_env(age_key),
        destructive=True,
    )


async def sops_extract(repo: Path, rel: Path, key: str, *, age_key: str) -> str:
    result = await sh.run(
        ["sops", "-d", "--extract", f'["{key}"]', str(rel)],
        cwd=repo,
        env=_sops_env(age_key),
        destructive=False,
    )
    return result.stdout.strip()


async def sops_encrypt_into(repo: Path, rel: Path, plaintext: str) -> None:
    """Encrypt plaintext as `rel` under the repo's own .sops.yaml rules.

    `--filename-override` makes sops pick the creation rule for `rel`, so the
    recipients are exactly what the (rewritten) rule says; encryption needs
    only their public keys.
    """
    with tempfile.NamedTemporaryFile("w", suffix=rel.suffix, delete=False) as staged:
        staged.write(plaintext)
        staged_path = Path(staged.name)
    try:
        result = await sh.run(
            ["sops", "--encrypt", "--filename-override", str(rel), str(staged_path)],
            cwd=repo,
            destructive=False,
        )
    finally:
        staged_path.unlink(missing_ok=True)
    (repo / rel).write_text(result.stdout)


def recipients_of(path: Path) -> set[str]:
    """The age recipients a sops file is encrypted to (YAML or the binary/JSON form)."""
    text = path.read_text()
    doc: Any = json.loads(text) if text.lstrip().startswith("{") else YAML(typ="safe").load(text)
    if not isinstance(doc, dict):
        raise BootstrapError(f"{path}: not a sops document")
    meta = doc.get("sops", {})
    entries = meta.get("age", []) if isinstance(meta, dict) else []
    return {str(e["recipient"]) for e in entries if isinstance(e, dict) and "recipient" in e}


def _replace_enc(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _replace_enc(v) for k, v in node.items() if k != "sops"}
    if isinstance(node, list):
        return [_replace_enc(v) for v in node]
    if isinstance(node, str) and node.startswith("ENC["):
        return DUMMY_VALUE
    return node


def dummy_plaintext(ciphertext: str) -> str:
    """A plaintext with the encrypted file's shape and every value replaced.

    Top-level keys, nesting and metadata (a SopsSecret's apiVersion/kind/
    spec) survive; only the values and the `sops` block go. What sops-nix and
    the logscrub daemon read at activation is the shape, so a dummy of the
    same shape activates where the real file would.
    """
    if ciphertext.lstrip().startswith("{"):
        return json.dumps(_replace_enc(json.loads(ciphertext)), indent=2) + "\n"
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    doc = _replace_enc(yaml.load(ciphertext))
    buf = io.StringIO()
    yaml.dump(doc, buf)
    return buf.getvalue()


async def tracked_files_matching(repo: Path, path_regexes: list[str]) -> list[Path]:
    listing = await sh.run(["git", "-C", str(repo), "ls-files", "-z"], destructive=False)
    compiled = [re.compile(r) for r in path_regexes]
    return [
        Path(rel)
        for rel in listing.stdout.split("\0")
        if rel and any(c.search(rel) for c in compiled)
    ]


async def stage_privileged_fixture(
    *, bootstrap_src: Path, dotfiles: Path, ci_key: str, key_out: Path
) -> None:
    """An ephemeral identity playing the privileged tier, in both repos.

    The real privileged key never reaches CI (that is the tier boundary), so
    the privileged e2e gets a key generated here and treated as privileged by
    every consumer: tier detection reads the rewritten anchor, the bootstrap
    secrets are rekeyed to it, and the dotfiles files a privileged host
    decrypts at activation are made decryptable by it.
    """
    await sh.run(["age-keygen", "-o", str(key_out)], destructive=True)
    ephemeral_pub = (
        await sh.run(["age-keygen", "-y", str(key_out)], destructive=False)
    ).stdout.strip()
    # Reduce to the bare AGE-SECRET-KEY line: create-usb bakes only that line
    # onto the stick (create_usb._bootstrap_identity), and the media-inspect
    # step compares the two byte-for-byte — age-keygen's `# created:` /
    # `# public key:` comment lines would make them differ.
    secret_line = next(
        line for line in key_out.read_text().splitlines() if line.startswith("AGE-SECRET-KEY-")
    )
    key_out.write_text(secret_line + "\n")
    key_out.chmod(0o644)
    ephemeral_secret = key_out.read_text()
    sandbox_pub = sops_anchors.anchor_pubkeys(bootstrap_src / ".sops.yaml")[
        sops_anchors.SANDBOX_ANCHOR
    ]
    # The Jenkins bootstrap-age-key credential — a recipient of the committed
    # privileged-rule files, whichever tier it is; every rekey below decrypts
    # with it.
    _log.info("ephemeral privileged identity: %s…", ephemeral_pub[:24])

    for sops_path in (bootstrap_src / ".sops.yaml", dotfiles / ".sops.yaml"):
        doc = sops_yaml.load(sops_path)
        sops_yaml.set_anchored_pubkey(doc, sops_anchors.PRIVILEGED_ANCHOR, ephemeral_pub)
        sops_yaml.save(doc, sops_path)

    # bootstrap secrets.yaml is re-keyed from the committed {sandbox, privileged}
    # set to {sandbox, ephemeral}: the CI key (a recipient of the committed
    # file) decrypts, and the ephemeral key — the media key the flashed VM
    # stages and uses — becomes a recipient in place of the real privileged
    # one. The check below therefore reads it back with the EPHEMERAL key, the
    # one flash/secret_zero will actually use; the CI key is no longer a
    # recipient once its own tier's anchor has been overwritten.
    before = recipients_of(bootstrap_src / "secrets.yaml")
    await sops_updatekeys(bootstrap_src, Path("secrets.yaml"), age_key=ci_key)
    after = recipients_of(bootstrap_src / "secrets.yaml")
    _log.info(
        "bootstrap secrets.yaml recipients: %d -> %d (sandbox kept %s, ephemeral added %s)",
        len(before),
        len(after),
        sandbox_pub in after,
        ephemeral_pub in after,
    )
    assert ephemeral_pub in after, sorted(k[:24] for k in after)
    check = await sops_extract(
        bootstrap_src, Path("secrets.yaml"), "luks_passphrase", age_key=ephemeral_secret
    )
    assert check == LUKS_PASSPHRASE, "the media (ephemeral) key cannot decrypt the rekeyed secrets"
    await sops_encrypt_into(
        bootstrap_src,
        Path("secrets-privileged.yaml"),
        f"github_ssh_key_token: {PRIVILEGED_FIXTURE_TOKEN}\n",
    )
    assert recipients_of(bootstrap_src / "secrets-privileged.yaml") == {ephemeral_pub}
    check = await sops_extract(
        bootstrap_src,
        Path("secrets-privileged.yaml"),
        "github_ssh_key_token",
        age_key=ephemeral_secret,
    )
    assert check == PRIVILEGED_FIXTURE_TOKEN

    # dotfiles: every file the privileged rule set selects.
    doc = sops_yaml.load(dotfiles / ".sops.yaml")
    regexes = sops_yaml.rules_with_anchor(doc, sops_anchors.PRIVILEGED_ANCHOR)
    files = await tracked_files_matching(dotfiles, regexes)
    rekeyed: list[str] = []
    dummied: list[str] = []
    for rel in files:
        if sandbox_pub in recipients_of(dotfiles / rel):
            await sops_updatekeys(dotfiles, rel, age_key=ci_key)
            rekeyed.append(rel.as_posix())
        else:
            await sops_encrypt_into(dotfiles, rel, dummy_plaintext((dotfiles / rel).read_text()))
            dummied.append(rel.as_posix())
        assert ephemeral_pub in recipients_of(dotfiles / rel), rel
    _log.info("dotfiles rekeyed to the ephemeral key: %s", ", ".join(rekeyed))
    _log.info("dotfiles replaced by same-shape dummies: %s", ", ".join(dummied))
    await _git(dotfiles, "add", "--", ".sops.yaml", *[f.as_posix() for f in files])
    await _git(
        dotfiles,
        "-c",
        "user.email=ci@example.com",
        "-c",
        "user.name=ci",
        "commit",
        "-q",
        "-m",
        "test: ephemeral privileged identity for the e2e",
    )


def prune_cache(cache: Path, *, now: float, max_age_s: float = CACHE_MAX_AGE_S) -> int:
    """Drop narinfos and NARs untouched for longer than the retention window."""
    removed = 0
    if not cache.exists():
        return 0
    for path in cache.rglob("*"):
        if path.is_file() and now - path.stat().st_mtime > max_age_s:
            path.unlink()
            removed += 1
    return removed


async def fill_cache(*, cache: Path, share: Path) -> None:
    """The file:// cache every guest substitutes from, filled incrementally.

    zstd rather than none: the whole fleet closure set uncompressed would be
    tens of gigabytes on the CI builder's disk, and zstd decompresses faster than 9p
    serves the bytes anyway. `nix copy` skips paths whose narinfo is already
    present, so a warm cache costs seconds.
    """
    cache.mkdir(parents=True, exist_ok=True)
    store_uri = f"file://{cache}?compression=zstd"
    removed = prune_cache(cache, now=time.time())
    _log.info("cache %s: pruned %d stale file(s)", cache, removed)

    if CI_DOTFILES_PROFILE.exists():
        closure = await sh.run(["nix-store", "-qR", str(CI_DOTFILES_PROFILE)], destructive=False)
        paths = [p for p in closure.stdout.split("\n") if p]
        _log.info("copying the ci-dotfiles closure (%d paths) into the cache", len(paths))
        await sh.run(
            ["nix", "copy", "--to", store_uri, "--stdin"],
            input_text="\n".join(paths) + "\n",
            capture=False,
            destructive=True,
        )
    else:
        _log.warning(
            "%s absent — no dotfiles CI closure to seed the cache from", CI_DOTFILES_PROFILE
        )

    for flake in (share / "dotfiles", share / "bootstrap-source"):
        _log.info("archiving %s and its inputs into the cache", flake)
        await sh.run(
            ["nix", "flake", "archive", "--to", store_uri, f"git+file://{flake}"],
            capture=False,
            destructive=True,
        )

    _log.info("building the installer toplevel from the staged source for the cache")
    built = await sh.run(
        [
            "nix",
            "build",
            "--no-link",
            "--print-out-paths",
            f"git+file://{share / 'bootstrap-source'}"
            "#nixosConfigurations.installer-usb.config.system.build.toplevel",
        ],
        destructive=True,
    )
    toplevel = built.stdout.strip()
    await sh.run(["nix", "copy", "--to", store_uri, toplevel], capture=False, destructive=True)
    size = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file())
    _log.info("cache %s now %.1f GiB", cache, size / 2**30)


async def stage(
    tier: Tier,
    *,
    source: Path,
    share_root: Path,
    cache: Path,
    ci_key: str,
    dotfiles_from: Path | None = None,
) -> None:
    ensure_nix_conf(Path.home() / ".config/nix/nix.conf")

    share = share_root / tier.value
    if share.exists():
        shutil.rmtree(share)
    share.mkdir(parents=True)
    dotfiles = share / "dotfiles"
    bootstrap_src = share / "bootstrap-source"

    # A dotfiles checkout handed in is the one this bootstrap source came out
    # of, so the two are tested at the same commit; without one, GitHub's main.
    origin = DOTFILES_HTTPS if dotfiles_from is None else f"file://{dotfiles_from.resolve()}"
    _log.info("=== cloning dotfiles (%s) → %s ===", origin, dotfiles)
    # For GitHub the pod's credential helper (nixEnv) authenticates; the token
    # never enters the URL, so it never lands in .git/config or in the guests.
    await sh.run(["git", "clone", "--depth", "1", origin, str(dotfiles)], destructive=True)
    # The guests push to and read `main`; a CI checkout is a detached HEAD.
    await _git(dotfiles, "checkout", "-q", "-B", "main")
    _log.info("deleted %d symlink(s) from the dotfiles clone", delete_symlinks(dotfiles))
    await _git(dotfiles, "config", "receive.denyCurrentBranch", "updateInstead")
    with (dotfiles / "host_registry.toml").open("a") as fh:
        fh.write(registry_fixture(tier))
    await _git(
        dotfiles,
        "-c",
        "user.email=ci@example.com",
        "-c",
        "user.name=ci",
        "commit",
        "-q",
        "-m",
        f"test: pre-register the {tier.value} e2e host(s)",
        "--",
        "host_registry.toml",
    )

    _log.info("=== copying the bootstrap source → %s ===", bootstrap_src)
    await copy_source(source, bootstrap_src)
    await sops_set(
        bootstrap_src, Path("secrets.yaml"), "luks_passphrase", LUKS_PASSPHRASE, age_key=ci_key
    )

    # Read the attic token now, while the committed secrets.yaml still has the
    # CI key as a recipient: the privileged fixture re-keys it to the ephemeral
    # media key and drops the CI key.
    attic = await sops_extract(bootstrap_src, Path("secrets.yaml"), "attic_token", age_key=ci_key)
    (share / "nix-netrc").write_text(f"machine cache.jacobbrugh.net\npassword {attic}\n")
    (share / "nix-netrc").chmod(0o644)

    key_out = share / "age-key"
    if tier is Tier.PRIVILEGED:
        _log.info("=== staging the ephemeral privileged identity ===")
        await stage_privileged_fixture(
            bootstrap_src=bootstrap_src, dotfiles=dotfiles, ci_key=ci_key, key_out=key_out
        )
    else:
        key_out.write_text(ci_key.rstrip("\n") + "\n")
        key_out.chmod(0o644)

    # Before the ownership pass below: `nix flake archive` opens the clone
    # through libgit2, which refuses a repository not owned by the current
    # user, and the clone is about to be handed to uid 1000 for the guest.
    _log.info("=== filling the binary cache at %s ===", cache)
    await fill_cache(cache=cache, share=share)

    # 9p: the guest's jacob (uid 1000) pushes into the dotfiles clone and reads
    # everything else. Numeric chown + world-writable mode, since 9p's uid
    # passthrough varies by qemu version.
    for path in share.rglob("*"):
        mode = path.stat().st_mode
        path.chmod(
            mode
            | stat.S_IRUSR
            | stat.S_IRGRP
            | stat.S_IROTH
            | (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH if path.is_dir() else 0)
        )
    await sh.run(["chown", "-R", "1000:100", str(dotfiles)], destructive=True)
    await sh.run(["chmod", "-R", "a+rwX", str(dotfiles)], destructive=True)

    _log.info("[bold green]%s e2e fixtures staged[/] at %s", tier.value, share)


app = typer.Typer(name="bootstrap-e2e-stage", add_completion=False)


@app.command()
def _main(
    tier: Annotated[str, typer.Argument(help="sandbox or privileged")],
    dotfiles: Annotated[
        Path | None,
        typer.Option(help="a dotfiles checkout to test against instead of GitHub's main"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Stage one e2e tier's fixtures (CI only; needs SOPS_AGE_KEY = the sandbox key)."""
    log.setup(verbose=verbose)
    try:
        chosen = Tier(tier)
    except ValueError as exc:
        raise typer.BadParameter(f"tier must be sandbox or privileged, not {tier!r}") from exc
    ci_key = os.environ.get("SOPS_AGE_KEY", "")
    if not ci_key.strip():
        _log.error("SOPS_AGE_KEY (the Jenkins bootstrap-age-key credential) is not set")
        raise typer.Exit(code=1)
    try:
        asyncio.run(
            stage(
                chosen,
                source=Path.cwd(),
                share_root=SHARE_ROOT,
                cache=CACHE_DIR,
                ci_key=ci_key,
                dotfiles_from=dotfiles,
            )
        )
    except ShellError as exc:
        _log.error("[bold red]staging failed:[/] %s", exc)
        _log.error("full stderr of %s:\n%s", exc.cmd, exc.stderr)
        raise typer.Exit(code=1) from exc
    except BootstrapError as exc:
        _log.error("[bold red]staging failed:[/] %s", exc)
        raise typer.Exit(code=1) from exc


def main() -> None:
    app()
