# Minimal `gh` stub — just enough surface for the bootstrap's ssh +
# register phases. All the real network calls the bootstrap makes
# against `gh` are:
#
#   - `gh api user`               — ssh phase (get_git_identity) +
#                                   register phase (commit author)
#   - `gh api /user/keys --jq …`  — ssh phase (ssh_key_registered)
#   - `gh ssh-key add …`          — ssh phase (ssh_key_add)
#
# Responses are static:
#   - `gh api user` returns a canned JSON profile so the commit author
#     is deterministic (`E2E Bot <e2e-bot@example.com>`).
#   - `/user/keys` returns an empty list so ssh_key_registered → False
#     and ssh_key_add actually runs (exercising the upload path).
#   - `gh ssh-key add …` is a no-op that exits 0.
#
# Anything else `gh` might be called with exits 2 with a message; a
# regression that adds a new `gh` call won't pass silently.

{ writeShellScriptBin }:

writeShellScriptBin "gh" ''
  set -eu

  case "''${1:-}" in
    api)
      case "''${2:-}" in
        user)
          cat <<'JSON'
  {
    "login": "e2e-bot",
    "id": 424242,
    "name": "E2E Bot",
    "email": "e2e-bot@example.com"
  }
  JSON
          exit 0
          ;;
        /user/keys)
          # Empty array — ssh_key_registered sees no matching keys and
          # falls through to ssh_key_add, which exercises the upload.
          echo "[]"
          exit 0
          ;;
        *)
          echo "mock gh: unhandled \`gh api $2\`" >&2
          exit 2
          ;;
      esac
      ;;
    ssh-key)
      case "''${2:-}" in
        add)
          # args: ssh-key add <pubkey-path> --title X --type Y
          echo "mock gh: ssh-key add (no-op)" >&2
          exit 0
          ;;
        *)
          echo "mock gh: unhandled \`gh ssh-key $2\`" >&2
          exit 2
          ;;
      esac
      ;;
    *)
      echo "mock gh: unhandled subcommand \`$1\`" >&2
      exit 2
      ;;
  esac
''
