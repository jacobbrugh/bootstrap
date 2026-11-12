# A `gh` stand-in for the e2e VMs: the bootstrap's three GitHub calls,
# answered locally and logged. No network, no real account.
#
# Every invocation is appended as one JSON line to BOOTSTRAP_E2E_GH_LOG —
# argv plus WHICH token GITHUB_TOKEN held ("privileged" for the fixture
# token the CI stage encrypts into secrets-privileged.yaml, "bot" for
# anything else) — so a test can assert not just that the phase behaved but
# exactly which calls it made and with which credential. `api user` answers
# as jacobbrugh for the privileged token and jacobbrugh-bot otherwise, which
# is what lets the ssh phase's owner guard run for real in the VM.
#
# Keys added with `ssh-key add` are remembered in BOOTSTRAP_E2E_GH_KEYS (next
# to the log) and listed by `api /user/keys`, so the ssh phase's real
# already-registered check is visible here: a retried bootstrap uploads its
# key exactly once, and a test can assert that count across runs.
{ writers }:

writers.writePython3Bin "gh"
  {
    flakeIgnore = [ "E501" ];
  }
  ''
    import json
    import os
    import sys

    PRIVILEGED_TOKEN = "e2e-privileged-token"
    LOG = os.environ.get("BOOTSTRAP_E2E_GH_LOG", "/tmp/bootstrap-e2e-gh-calls.jsonl")
    KEYS = os.environ.get("BOOTSTRAP_E2E_GH_KEYS", LOG + ".keys.json")

    argv = sys.argv[1:]
    token = os.environ.get("GITHUB_TOKEN", "")
    which = "privileged" if token == PRIVILEGED_TOKEN else ("bot" if token else "none")
    with open(LOG, "a") as fh:
        fh.write(json.dumps({"argv": argv, "token": which}) + "\n")


    def fail(msg):
        print(f"mock gh: {msg}", file=sys.stderr)
        sys.exit(2)


    def load_keys():
        try:
            with open(KEYS) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return []


    if argv[:2] == ["api", "user"]:
        if which == "privileged":
            user = {"login": "jacobbrugh", "id": 1, "name": "Jacob Brugh", "email": "jacob@example.com"}
        else:
            user = {"login": "jacobbrugh-bot", "id": 2, "name": "jacobbrugh-bot", "email": None}
        print(json.dumps(user))
    elif argv[:2] == ["api", "/user/keys"]:
        keys = load_keys()
        if "--jq" in argv:
            # --jq .[].key prints one key per line; nothing for an empty list.
            print("".join(k["key"] + "\n" for k in keys), end="")
        else:
            print(json.dumps(keys))
    elif argv[:2] == ["ssh-key", "add"]:
        with open(argv[2]) as fh:
            parts = fh.read().split(maxsplit=2)
        keys = load_keys()
        keys.append({"id": len(keys) + 1, "key": " ".join(parts[:2])})
        with open(KEYS, "w") as fh:
            json.dump(keys, fh)
        print("mock gh: ssh-key add (recorded)", file=sys.stderr)
    else:
        fail(f"unhandled: {' '.join(argv)}")
  ''
