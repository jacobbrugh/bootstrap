# bootstrap

Typed Python CLI for the jacobbrugh fresh-machine bootstrap. Packaged via `buildPythonApplication` from the parent Nix flake; normally invoked as `nix run github:jacobbrugh/bootstrap` rather than installed to a Python environment directly.

Development iteration:

```sh
nix develop        # in the repo root
cd python
pytest -xvs tests/unit/
mypy --strict src/bootstrap
```
