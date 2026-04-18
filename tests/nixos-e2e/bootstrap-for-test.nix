# `bootstrapForTest`: the production bootstrap package with the mock
# `gh` prepended to PATH so `gh api user` / `gh api /user/keys` /
# `gh ssh-key add` from the ssh + register phases hit a local stub
# instead of the real GitHub API.
#
# That's the only divergence from the real package. sops-nix on the
# test VM reads the sandbox bootstrap age key from
# /mnt/shared/sandbox-key (9p-mounted at boot) and decrypts the
# committed `secrets/bootstrap-secrets.sops.yaml` to
# `/run/secrets/bootstrap-github-token` — same code path the real
# bootstrap uses, just with a sandbox-tier age key and a mocked `gh`.

{
  bootstrap,
  mockGh,
}:

bootstrap.overrideAttrs (old: {
  pname = "bootstrap-for-test";
  # Append another --prefix to the existing makeWrapperArgs; the last
  # prepend wins, so mockGh/bin ends up ahead of the production gh
  # already on PATH from the flake.nix makeBinPath invocation.
  makeWrapperArgs = (old.makeWrapperArgs or [ ]) ++ [
    "--prefix"
    "PATH"
    ":"
    "${mockGh}/bin"
  ];
})
