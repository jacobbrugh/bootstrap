"""Ephemeral lifecycle for the bootstrap age key + GitHub PAT.

Exposes one thing: the `ephemeral_secrets(ctx)` context manager. On entry
it extracts the bootstrap age key from 1Password (the single secret-zero
reference) into a mode-0600 file under `$XDG_RUNTIME_DIR/bootstrap/`, uses
that key to decrypt the bootstrap repo's own sops-encrypted secrets file
(bundled as package data via hatchling `force-include`), and populates
`ctx.bootstrap_age_key_file` / `ctx.github_token`. On exit — success or
failure — the age-key file is shredded and both context fields are cleared.

Everything except the age key lives in the committed sops file, so adding a
new bootstrap secret is an edit-and-commit operation, not another 1Password
item. Adding a new field later is `sops python/src/bootstrap/data/bootstrap-secrets.sops.yaml`
(after putting the bootstrap age key on disk).

All cleanup lives in a `finally:` — no atexit, no signal handlers. The
caller (orchestrator or standalone phase entry point) owns the lifetime
by wrapping its phase-running code in `with secrets.ephemeral_secrets(ctx):`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import cast

from ruamel.yaml import YAML

from bootstrap.lib import op, sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.paths import HOME
from bootstrap.lib.runtime import Context

_log = logging.getLogger(__name__)

OP_AGE_KEY_PATH = "op://Personal/bw2otnlpjhm434grbcbpb6dady/credential"

# Package-data file name (inside `bootstrap.data`) holding all sops-encrypted
# bootstrap secrets. Currently `github_token`; extend the plaintext + re-sops
# to add more fields over time.
_SECRETS_RESOURCE = "bootstrap-secrets.sops.yaml"


def _runtime_dir() -> Path:
    """Return a private, user-writable runtime directory for ephemeral files."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "bootstrap"
    # macOS doesn't set XDG_RUNTIME_DIR by default. Fall back to a private
    # dir under $HOME — still single-user, still shredded at exit.
    return HOME / ".cache" / "bootstrap" / "runtime"


def _shred(path: Path) -> None:
    """Overwrite a file with zeros then unlink it. Best-effort — never raises."""
    try:
        if not path.exists():
            return
        size = path.stat().st_size
        with path.open("r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
        path.unlink()
    except OSError as exc:
        _log.warning("failed to shred %s: %s", path, exc)


def _decrypt_sops_secrets(age_key_file: Path) -> dict[str, str]:
    """Decrypt the bundled `bootstrap-secrets.sops.yaml` and return its fields.

    The file ships as package data, so at runtime it lives under the Nix
    store. sops decrypts it using the ephemeral age key file extracted from
    1Password. Result is parsed with ruamel.yaml `safe` and returned as a
    flat `{field: value}` dict.
    """
    resource = resources.files("bootstrap.data") / _SECRETS_RESOURCE
    with resources.as_file(resource) as sops_path:
        env = {**os.environ, "SOPS_AGE_KEY_FILE": str(age_key_file)}
        result = sh.run(
            ["sops", "decrypt", str(sops_path)],
            env=env,
            destructive=False,
        )
    yaml = YAML(typ="safe")
    loaded = yaml.load(result.stdout)
    if not isinstance(loaded, dict):
        raise BootstrapError(
            f"expected a mapping in {_SECRETS_RESOURCE}, got {type(loaded).__name__}"
        )
    return {str(k): str(v) for k, v in cast("dict[object, object]", loaded).items()}


#: Sentinels used in dry-run mode in lieu of the real secrets. Nothing in
#: the codebase compares against these values (the short-circuits at the
#: consumer call sites check `ctx.dry_run` directly), but using recognizable
#: constants makes misuse obvious if it ever happens.
_DRY_RUN_AGE_KEY_FILE = Path("/dev/null")
_DRY_RUN_GITHUB_TOKEN = "DRY_RUN_FAKE_TOKEN"


@contextlib.contextmanager
def ephemeral_secrets(ctx: Context) -> Iterator[None]:
    """Materialize the bootstrap age key + GH token for the duration of `ctx`.

    Populates `ctx.bootstrap_age_key_file` and `ctx.github_token` on entry.
    Shreds the age-key file and clears both context fields on exit, whether
    the wrapped block succeeded or raised.

    In dry-run mode, short-circuits without touching 1Password or sops:
    sets fake sentinel values so downstream phases can see non-None secrets
    in their prereq checks, and relies on each consumer's own `ctx.dry_run`
    guard to avoid actually using them. This keeps dry-run completely
    offline — no 1Password session requirement, no audit entries, no real
    secret material on disk.
    """
    if ctx.dry_run:
        _log.info(
            "[dry-run] skipping 1Password extraction and sops decrypt; "
            "populating ctx with sentinel secrets"
        )
        ctx.bootstrap_age_key_file = _DRY_RUN_AGE_KEY_FILE
        ctx.github_token = _DRY_RUN_GITHUB_TOKEN
        try:
            yield
        finally:
            ctx.bootstrap_age_key_file = None
            ctx.github_token = None
        return

    _log.info("materializing bootstrap secret zero from 1Password")
    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(stat.S_IRWXU)

    age_file = runtime / "bootstrap-age-key"
    age_file.write_text(op.read(OP_AGE_KEY_PATH) + "\n")
    age_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    try:
        secrets_fields = _decrypt_sops_secrets(age_file)
        try:
            ctx.github_token = secrets_fields["github_token"]
        except KeyError as exc:
            raise BootstrapError(
                f"{_SECRETS_RESOURCE} missing required field 'github_token'"
            ) from exc
        ctx.bootstrap_age_key_file = age_file
        _log.info(
            "bootstrap secrets ready (age key at %s; %d field(s) decrypted from %s)",
            age_file,
            len(secrets_fields),
            _SECRETS_RESOURCE,
        )
        try:
            yield
        finally:
            _log.info("shredding bootstrap secrets")
            _shred(age_file)
            ctx.bootstrap_age_key_file = None
            ctx.github_token = None
    except BaseException:
        # If we raised before entering the yield block (e.g., sops decrypt
        # failed), still shred the age key file before propagating.
        _shred(age_file)
        raise
