# ruff: noqa: F821
# Spliced into both e2e driver scripts by their default.nix (like sops_rules.py):
# the one definition of "this host came up working" that every bootstrap run,
# retry, re-run and re-flash is held to. It is what one real flash failed: the unit
# printed "bootstrap complete" over a system whose secrets never decrypted.
#
# Imports are aliased: the driver lints the concatenation with pyflakes, and a
# plain `import json` here would be a "redefinition of unused" of the script's
# own import below it.
import json as _json
import re as _re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from test_driver.machine import QemuMachine as _Machine

HOST_KEY = "/home/jacob/.config/sops/age/keys.txt"
BOT_SECRETS_REL = "modules/security/secrets/bot-secrets.yaml"
_MANIFEST_RE = r"sops-install-secrets\S*\s+(\S+manifest\.json)"


def assert_healthy(m: "_Machine", *, origin: str) -> None:
    """The switched system is up: every secret it declares exists and is
    non-empty, the operator's home-manager activation is active, no unit is
    failed, and the host's own age key opens the origin's bot-secrets."""
    activate = m.succeed("cat /run/current-system/activate")
    match = _re.search(_MANIFEST_RE, activate)
    assert match is not None, "the running generation names no sops-nix manifest"
    manifest = _json.loads(m.succeed(f"cat {match.group(1)}"))
    paths = [entry["path"] for entry in manifest["secrets"]]
    assert paths, "the running generation declares no sops secrets"
    for path in paths:
        m.succeed(f"test -s {path}")

    rc, hm_state = m.execute("systemctl is-active home-manager-jacob.service")
    assert rc == 0 and hm_state.strip() == "active", (
        f"home-manager-jacob.service is {hm_state.strip()!r}"
    )
    failed = m.succeed("systemctl --failed --no-legend --plain").strip()
    assert failed == "", f"failed units after the bootstrap:\n{failed}"

    # Decrypted in the guest and discarded: the sandbox tier's values are real.
    m.succeed(
        f"git -C {origin} show main:{BOT_SECRETS_REL} > /tmp/e2e-bot-secrets.yaml"
    )
    m.succeed(
        f"su - jacob -c 'SOPS_AGE_KEY_FILE={HOST_KEY} sops --decrypt "
        "/tmp/e2e-bot-secrets.yaml > /dev/null'"
    )


def host_anchor(sops_yaml_text: str, host: str) -> str:
    """The public key .sops.yaml anchors as host_<host>."""
    match = _re.search(rf"&host_{_re.escape(host)}\s+(age1\S+)", sops_yaml_text)
    assert match is not None, (
        f".sops.yaml carries no host_{host} anchor:\n{sops_yaml_text}"
    )
    return match.group(1)


def host_pubkey(m: "_Machine") -> str:
    """The public half of the key on this disk, as the operator user reads it."""
    return m.succeed(f"su - jacob -c 'age-keygen -y {HOST_KEY}'").strip()
