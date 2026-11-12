{
  bootstrap,
  mockGh,
}:

bootstrap.overrideAttrs (old: {
  pname = "bootstrap-for-test";
  makeWrapperArgs = (old.makeWrapperArgs or [ ]) ++ [
    "--prefix"
    "PATH"
    ":"
    "${mockGh}/bin"
  ];
})
