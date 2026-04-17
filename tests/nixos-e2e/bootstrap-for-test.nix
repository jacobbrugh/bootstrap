# `bootstrapForTest`: the production bootstrap package with the mock
# `gh` prepended to PATH so `gh api user` / `gh api /user/keys` /
# `gh ssh-key add` from the ssh + register phases hit a local stub
# instead of the real GitHub API.
#
# That's the only divergence from the real package — the bundled
# `bootstrap-secrets-sandbox.sops.yaml` ships as-is, and the test
# supplies the real sandbox bootstrap age key via SOPS_AGE_KEY_FILE
# so secrets.py takes the production headless path end-to-end.

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
