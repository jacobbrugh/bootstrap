# ruff: noqa: F821
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from test_driver.machine import QemuMachine

    machine: QemuMachine

    # Absolute store path to the mock-wrapped CLI, injected by the nix-side
    # testScript prelude (tests/e2e-sandbox/default.nix). Absolute, not a bare
    # `bootstrap`: the first run's switch activates the dotfiles system, which
    # carries no bootstrap CLI on PATH, so the later runs would otherwise fail
    # with "command not found".
    BOOTSTRAP: str
    SOURCE: str

    def start_all() -> None: ...


GH_LOG = "/tmp/bootstrap-e2e-gh-calls.jsonl"
ORIGIN = "/home/jacob/dotfiles"

_BOOTSTRAP_ENV = (
    "export BOOTSTRAP_FLAKE=git+file:///mnt/shared/bootstrap-source && "
    f"export BOOTSTRAP_DOTFILES_REMOTE=file://{ORIGIN} && "
    "export BOOTSTRAP_SANDBOX=1 && "
    f"export BOOTSTRAP_E2E_GH_LOG={GH_LOG} && "
)
_RUN = "su - jacob -c '" + _BOOTSTRAP_ENV + BOOTSTRAP + "'"
# The push gate: the origin's pre-receive hook refuses every push while this
# file exists, which is how the first run is made to fail AFTER register
# generated the host key and rekeyed the clone — the shape a hard-killed or
# network-cut first boot leaves behind.
_BLOCK_PUSH = f"{ORIGIN}/.git/BLOCK_PUSH"


def _origin_head(m: "QemuMachine") -> str:
    return m.succeed(f"git -C {ORIGIN} rev-parse main").strip()


# The post phase's success line ("bootstrap complete — ..."), as a whole
# phrase. A bare substring test also matches prose that merely contains it:
# the no-age-key error a drifted host must print says the post phase shreds
# the key "once a bootstrap completes", which failed run 4 on every build.
_COMPLETE = re.compile(r"\bbootstrap complete\b")


def _completed(out: str) -> bool:
    return _COMPLETE.search(out) is not None


def run() -> None:
    start_all()
    machine.wait_for_unit("multi-user.target")

    # Network preflight. The guest's eth0 is qemu SLIRP user networking; DNS
    # is pinned to public resolvers in the node config (the SLIRP DNS proxy
    # at 10.0.2.3 forwards to the build host's resolver, which broke on the
    # CI daemon — see tests/e2e-sandbox/default.nix). Stage 1 proves raw
    # outbound TCP through the NAT (no DNS involved); stage 2 proves DNS.
    _, preflight = machine.execute(
        "ip addr; echo ---; ip route; echo ---; cat /etc/resolv.conf; "
        "echo ---; curl -4 -sS -m 10 -o /dev/null -w 'curl-1.1.1.1:%{http_code}' "
        "https://1.1.1.1/ 2>&1; echo; getent hosts github.com; echo getent-rc=$?"
    )
    print(f"[net-preflight]\n{preflight}")
    machine.wait_until_succeeds(
        "curl -4 -sS -m 10 -o /dev/null https://1.1.1.1/", timeout=120
    )
    machine.wait_until_succeeds("getent hosts github.com", timeout=120)

    # The staged binary cache is mounted and answers.
    machine.succeed("test -d /mnt/cache/nar")

    machine.succeed(
        "install -m 0440 -o root -g wheel /mnt/shared/age-key /var/lib/bootstrap/age-key"
    )
    machine.succeed(
        "install -m 0600 -o root -g root /mnt/shared/nix-netrc /var/lib/bootstrap/nix-netrc"
    )

    machine.succeed("test -f /etc/nixos/hardware-configuration.nix")
    machine.succeed("test -d /mnt/shared/bootstrap-source")
    machine.succeed("test -d /mnt/shared/dotfiles")

    # Copy dotfiles off 9p onto local ext4 inside the VM. git push to a
    # file:// URL on 9p hits "unable to create temporary object directory"
    # in receive-pack across every securityModel/perm combo. Local ext4
    # avoids the issue entirely; the 9p share stays for staging only.
    machine.succeed(f"cp -a /mnt/shared/dotfiles {ORIGIN}")
    machine.succeed(f"chown -R jacob:users {ORIGIN}")
    # The stage tool's symlink deletion (worked around 9p ELOOP) left the
    # working tree dirty relative to the index. updateInstead refuses to
    # accept the push if the tree is dirty; restore symlinks from the index.
    machine.succeed(f"su - jacob -c 'git -C {ORIGIN} checkout -- .'")
    machine.succeed(
        f"su - jacob -c 'git -C {ORIGIN} config receive.denyCurrentBranch updateInstead'"
    )
    machine.succeed(
        "su - jacob -c 'git config --global --add safe.directory /mnt/shared/bootstrap-source'"
    )
    # bootstrap never mutates host_registry.toml — the stage tool pre-registered
    # e2e0 the way an operator would with `nix run .#add-host`.
    machine.succeed(f"grep -q '^\\[e2e0\\]' {ORIGIN}/host_registry.toml")

    machine.succeed(f"git config --global --add safe.directory {ORIGIN}")
    machine.succeed(
        f"cat > {ORIGIN}/.git/hooks/pre-receive <<'HOOKEOF'\n"
        "#!/bin/sh\n"
        f"if [ -e {_BLOCK_PUSH} ]; then\n"
        "  echo 'e2e: push blocked' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
        "HOOKEOF"
    )
    machine.succeed(f"chmod +x {ORIGIN}/.git/hooks/pre-receive")
    machine.succeed(f"chown jacob:users {ORIGIN}/.git/hooks/pre-receive")
    machine.succeed(f"touch {_BLOCK_PUSH} && chown jacob:users {_BLOCK_PUSH}")

    # ── Run 1: register fails after generating the key ────────────────────
    # The push is refused, so the host key exists on this disk while origin
    # has never heard of it. Timeout: 60min covers a cold cache building fresh
    # dotfiles closure pieces (the secret-zero switch happens here too).
    rc, out = machine.execute(_RUN + " 2>&1", timeout=3600)
    assert rc != 0, (
        f"the first run must fail at the blocked push, got rc=0:\n{out[-4000:]}"
    )
    assert not _completed(out), out[-4000:]
    assert "bootstrap failed:" in out, out[-4000:]
    machine.succeed("test -f /home/jacob/.config/sops/age/keys.txt")
    # A failed register leaves the host as it found it: the staged host files
    # under a real /etc/nixos, no flake symlink claiming "bootstrapped".
    machine.succeed("test -d /etc/nixos && test ! -L /etc/nixos")
    machine.succeed("test -f /etc/nixos/hardware-configuration.nix")
    machine.fail(f"git -C {ORIGIN} log --format=%s main | grep -q 'harvest+rekey'")
    machine.succeed("test -f /var/lib/bootstrap/age-key")

    # ── Run 2: the retry heals ────────────────────────────────────────────
    # Before the fix this run took the flake-symlink fast path, skipped
    # register, accepted the exit-4 switch and printed "bootstrap complete"
    # over a system whose secrets never decrypted, as one real flash did.
    machine.succeed(f"rm {_BLOCK_PUSH}")
    machine.succeed(_RUN, timeout=3600)

    machine.fail("test -f /var/lib/bootstrap/age-key")

    # ── The tier: a sandbox host uploads nothing, anywhere ───────────────
    calls = [
        json.loads(line)
        for line in machine.succeed(f"cat {GH_LOG}").splitlines()
        if line
    ]
    assert calls, "mock-gh recorded no calls at all"
    uploads = [c for c in calls if c["argv"][:1] == ["ssh-key"]]
    assert uploads == [], f"a sandbox host uploaded an SSH key: {uploads}"
    key_lists = [c for c in calls if c["argv"][:2] == ["api", "/user/keys"]]
    assert key_lists == [], f"a sandbox host queried GitHub keys: {key_lists}"
    assert all(c["token"] == "bot" for c in calls), (
        f"a non-bot token reached gh: {calls}"
    )
    print(f"[e2e-sandbox] gh calls: {[c['argv'] for c in calls]}")

    log_subject = machine.succeed(f"git -C {ORIGIN} log -1 --format=%s main").strip()
    assert "harvest+rekey for e2e0" in log_subject, (
        f"expected 'harvest+rekey for e2e0' in HEAD subject, got: {log_subject!r}"
    )
    author = machine.succeed(f"git -C {ORIGIN} log -1 --format=%an main").strip()
    assert author == "jacobbrugh-bot", (
        f"sandbox harvest must be authored by the bot, got {author!r}"
    )

    sops_yaml = machine.succeed(f"git -C {ORIGIN} show main:.sops.yaml")
    assert "host_e2e0" in sops_yaml, (
        f".sops.yaml missing host_e2e0 anchor: {sops_yaml!r}"
    )
    # In every rule the sandbox anchor is in (bot-secrets, the eval fixtures)
    # and in none it is not (secrets.yaml, the logscrub literals).
    assert_host_in_exactly_the_tier_rules(sops_yaml, "host_e2e0", "bootstrap_sandbox")
    # ...anchoring exactly the key this disk holds.
    assert host_anchor(sops_yaml, "e2e0") == host_pubkey(machine)

    registry = machine.succeed(f"git -C {ORIGIN} show main:host_registry.toml")
    assert "[e2e]" in registry and "[e2e0]" in registry, (
        f"host_registry.toml missing [e2e]/[e2e0] entries: {registry!r}"
    )

    harvested = machine.succeed(
        f"git -C {ORIGIN} show main:config/hosts/e2e0/hardware.nix"
    )
    assert "fileSystems" in harvested, (
        f"harvested hardware.nix missing fileSystems declaration: {harvested!r}"
    )

    nixos_target = machine.succeed("readlink /etc/nixos").strip()
    assert nixos_target == "/home/jacob/.local/share/chezmoi", (
        f"/etc/nixos should point at the canonical dotfiles path, got: {nixos_target!r}"
    )

    machine.succeed("test -s /run/secrets/headscale_preauth_key")
    machine.succeed("test -s /etc/kubernetes/kubeconfig")
    machine.succeed("test -s /etc/argocd/token")

    rc, _ = machine.execute("systemctl is-active --quiet attic-watch-store.service")
    assert rc != 0, "attic-watch-store.service should NOT be active on a sandbox host"

    assert_healthy(machine, origin=ORIGIN)

    first_system = machine.succeed("readlink /run/current-system").strip()
    first_head = _origin_head(machine)

    # ── Run 3: a completed host, run again, changes nothing ──────────────
    machine.succeed(_RUN, timeout=1200)

    second_system = machine.succeed("readlink /run/current-system").strip()
    assert first_system == second_system, (
        "second bootstrap run changed /run/current-system — idempotency violated. "
        f"first: {first_system!r}, second: {second_system!r}"
    )
    assert _origin_head(machine) == first_head, (
        "a re-run of a registered host pushed a commit — register isn't idempotent"
    )
    assert_healthy(machine, origin=ORIGIN)

    # ── Run 4: key drift on a completed host fails loudly ─────────────────
    # The host key is regenerated under the bootstrap's feet, as a re-flash
    # does. post already shredded the bootstrap age key, so nothing on this
    # disk can seal the new key in: the run must say so and name the
    # re-flash, not print "bootstrap complete" over a broken system.
    machine.succeed(
        "su - jacob -c 'age-keygen -o /home/jacob/.config/sops/age/keys.txt.new "
        "&& mv -f /home/jacob/.config/sops/age/keys.txt.new "
        "/home/jacob/.config/sops/age/keys.txt'"
    )
    rc, out = machine.execute(_RUN + " 2>&1", timeout=1200)
    assert rc != 0, f"a drifted host must not bootstrap:\n{out[-4000:]}"
    assert not _completed(out), out[-4000:]
    assert "bootstrap age key" in out and "re-flash" in out, out[-4000:]
    assert machine.succeed("readlink /run/current-system").strip() == first_system
    assert _origin_head(machine) == first_head

    print("[bootstrap-e2e-sandbox] PASSED")


run()
