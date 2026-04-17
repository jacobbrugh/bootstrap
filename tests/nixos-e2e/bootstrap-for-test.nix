# `bootstrapForTest`: the production bootstrap package with its bundled
# `bootstrap-secrets-{devbox,sandbox}.sops.yaml` swapped for the
# fixture-generated test variant, and the mock `gh` prepended to PATH
# so all `gh` calls from the ssh + register phases hit the stub.
#
# Two points of substitution:
#
#   1. **postInstall file drop** — copies the test-encrypted sops file
#      into `$out/lib/python*/site-packages/bootstrap/data/` under the
#      variant-specific name (`bootstrap-secrets-devbox.sops.yaml` or
#      `...-sandbox...`). `secrets.py::_secrets_resource_name(ctx)`
#      prefers the variant-specific file; the legacy monolithic file
#      bundled by pyproject.toml is left in place but ignored when the
#      variant file exists.
#
#   2. **makeWrapperArgs PATH prefix** — the generated `bin/bootstrap`
#      wrappers are re-wrapped with mockGh prepended to PATH. The
#      original --prefix from flake.nix still fires, so `git`, `sops`,
#      `age`, etc. remain on PATH — mockGh just shadows the real `gh`.

{
  bootstrap,
  fixture,
  mockGh,
  variant,
}:

bootstrap.overrideAttrs (old: {
  pname = "bootstrap-for-test-${variant}";
  postInstall = (old.postInstall or "") + ''
    # Layout after buildPythonApplication is
    # $out/lib/python3.XX/site-packages/bootstrap/data/*.sops.yaml.
    # Glob the version dir so we don't hardcode the python minor.
    data_dir=$(echo $out/lib/python*/site-packages/bootstrap/data)
    if [[ ! -d "$data_dir" ]]; then
      echo "bootstrap-for-test: expected data dir under $out/lib/python*/site-packages/bootstrap/data" >&2
      exit 1
    fi
    cp ${fixture}/bootstrap-secrets-${variant}.sops.yaml \
       "$data_dir/bootstrap-secrets-${variant}.sops.yaml"
    chmod 0644 "$data_dir/bootstrap-secrets-${variant}.sops.yaml"
    echo "bootstrap-for-test: bundled $data_dir/bootstrap-secrets-${variant}.sops.yaml" >&2
  '';
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
