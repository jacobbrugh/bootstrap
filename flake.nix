{
  description = "Bootstrap — Phase 0 NixOS/Windows install configs + Phase 1 typed Python CLI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixos-wsl = {
      url = "github:nix-community/NixOS-WSL/main";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    nix-win = {
      url = "github:jacobbrugh/nix-win";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      nixos-wsl,
      nix-win,
      git-hooks,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      # Per-system nixpkgs instance with unfree allowed — _1password-cli is
      # unfree and required by the bootstrap runtime PATH.
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system (pkgsFor system));

      # ── Phase 1: typed Python bootstrap CLI ─────────────────────────────
      mkBootstrap =
        pkgs:
        pkgs.python3.pkgs.buildPythonApplication {
          pname = "bootstrap";
          version = "0.1.0";
          # Compose `python/` + the repo-root `secrets/` into a single
          # source tree so hatchling's force-include can resolve paths
          # like `../secrets/bootstrap-secrets.sops.yaml` from
          # pyproject.toml (which lives under python/). `sourceRoot`
          # then cds the build into the nested python/ dir.
          src = pkgs.lib.fileset.toSource {
            root = ./.;
            fileset = pkgs.lib.fileset.unions [
              ./python
              ./secrets
            ];
          };
          sourceRoot = "source/python";
          pyproject = true;

          build-system = with pkgs.python3.pkgs; [ hatchling ];

          dependencies = with pkgs.python3.pkgs; [
            typer
            rich
            questionary
            tomlkit
            ruamel-yaml
          ];

          nativeCheckInputs = with pkgs.python3.pkgs; [
            pytestCheckHook
            mypy
          ];

          pytestFlagsArray = [ "tests/unit" ];

          pythonImportsCheck = [
            "bootstrap"
            "bootstrap.cli"
          ];

          postCheck = ''
            echo "running mypy --strict..."
            mypy --strict --config-file pyproject.toml src/bootstrap tests
          '';

          # Runtime tools: every wrapped binary in $out/bin gets these on PATH.
          # Nix pins the versions; phase code never has to install/find/validate them.
          makeWrapperArgs = [
            "--prefix"
            "PATH"
            ":"
            (pkgs.lib.makeBinPath (
              with pkgs;
              [
                git
                gh
                sops
                age
                _1password-cli
                openssh
                coreutils
              ]
            ))
          ];

          meta = {
            description = "Typed fresh-machine bootstrap CLI for jacobbrugh/dotfiles";
            mainProgram = "bootstrap";
            license = pkgs.lib.licenses.mit;
          };
        };

      mkApp = pkgs: binName: {
        type = "app";
        program = "${mkBootstrap pkgs}/bin/${binName}";
        meta = {
          description = "Bootstrap CLI: ${binName}";
          license = pkgs.lib.licenses.mit;
        };
      };
    in
    {
      # ── Phase 0: minimal NixOS configs for fresh installs ───────────────
      # Defaults live in nix/nixos/default.nix (bare-metal path).
      # wsl-bootstrap composes default.nix + nix/nixos/wsl.nix override.
      # host-hardware.nix is per-host and committed by the user before the
      # bare-metal install (`cp /etc/nixos/hardware-configuration.nix
      # host-hardware.nix`), so the `bootstrap` config is only exposed when
      # that file is present — otherwise `nix flake check` would trip on
      # the missing module.
      nixosConfigurations = {
        wsl-bootstrap = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            nixos-wsl.nixosModules.default
            ./nix/nixos
            ./nix/nixos/wsl.nix
            (if builtins.pathExists ./host-hardware.nix then ./host-hardware.nix else { })
          ];
        };
      }
      // (
        if builtins.pathExists ./host-hardware.nix then
          {
            bootstrap = nixpkgs.lib.nixosSystem {
              system = "x86_64-linux";
              modules = [
                ./nix/nixos
                ./host-hardware.nix
                (if builtins.pathExists ./host-networking.nix then ./host-networking.nix else { })
              ];
            };
          }
        else
          { }
      );

      # ── Phase 0: minimal Windows config (bootstrap state) ────────────
      # Evaluated inside WSL via:
      #   nix-win switch --flake github:jacobbrugh/bootstrap#bootstrap
      winConfigurations.bootstrap = nix-win.lib.winSystem {
        pkgs = nixpkgs.legacyPackages."x86_64-linux";
        specialArgs = {
          czData = {
            username = "jacob";
          };
        };
        modules = [ ./windows-bootstrap.nix ];
      };

      # ── Phase 1: typed Python bootstrap as Nix packages + flake apps ──
      #
      # `e2e-nixos-sandbox` (Linux only) is the end-to-end test that boots
      # a NixOS VM, runs the full production bootstrap, and activates
      # `nixosConfigurations.e2e-sandbox` from the real dotfiles flake.
      # Lives under `packages` rather than `checks` so `nix flake check`
      # doesn't try to evaluate/run it — the VM test takes minutes, needs
      # KVM + an Attic netrc + a populated /tmp/bootstrap-e2e-shared, and
      # only makes sense from the `test-e2e-nixos` GHA workflow.
      packages = forAllSystems (
        _system: pkgs:
        {
          default = mkBootstrap pkgs;
          bootstrap = mkBootstrap pkgs;
        }
        // pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          e2e-nixos-sandbox = (import ./tests/nixos-e2e { inherit pkgs; }).mkTest {
            bootstrap = mkBootstrap pkgs;
          };
        }
      );

      apps = forAllSystems (
        _system: pkgs: {
          default = mkApp pkgs "bootstrap";
          prereqs = mkApp pkgs "bootstrap-prereqs";
          onepassword = mkApp pkgs "bootstrap-onepassword";
          ssh = mkApp pkgs "bootstrap-ssh";
          register = mkApp pkgs "bootstrap-register";
          switch = mkApp pkgs "bootstrap-switch";
          post = mkApp pkgs "bootstrap-post";
        }
      );

      # ── Checks: flake check + pre-commit hooks ────────────────────────
      #
      # The e2e VM test lives under `packages.<sys>.e2e-nixos-sandbox`
      # rather than here, so `nix flake check` stays fast + hermetic
      # and doesn't try to launch a VM that needs KVM + Attic auth +
      # a pre-populated shared dir.
      checks = forAllSystems (
        system: pkgs:
        let
          # Wrap mypy in a python env that includes the runtime deps so strict
          # analysis sees real types for ruamel/tomlkit/rich instead of falling
          # back to Any. The `entry` line below uses `${mypyEnv}/bin/mypy` so the
          # hook runs the wrapped binary directly — git-hooks.nix doesn't
          # automatically prefix overridden entries with the package's bin dir.
          mypyEnv = pkgs.python3.withPackages (
            ps: with ps; [
              mypy
              typer
              rich
              questionary
              tomlkit
              ruamel-yaml
            ]
          );
        in
        {
          bootstrap = mkBootstrap pkgs;
          pre-commit = git-hooks.lib.${system}.run {
            src = ./.;
            hooks = {
              nixfmt.enable = true;
              ruff.enable = true;
              ruff-format.enable = true;
              mypy = {
                enable = true;
                name = "mypy (strict)";
                package = mypyEnv;
                entry = "${mypyEnv}/bin/mypy --strict --config-file python/pyproject.toml python/src/bootstrap";
                pass_filenames = false;
                files = "^python/src/bootstrap/.*\\.py$";
                language = "system";
              };
            };
          };
        }
      );

      # ── Dev shell: nix develop ────────────────────────────────────────
      devShells = forAllSystems (
        system: pkgs: {
          default = pkgs.mkShell {
            inputsFrom = [ (mkBootstrap pkgs) ];
            packages = with pkgs; [
              python3.pkgs.pytest
              python3.pkgs.mypy
              ruff
              nixfmt
            ];
            shellHook = self.checks.${system}.pre-commit.shellHook;
          };
        }
      );

      formatter = forAllSystems (_system: pkgs: pkgs.nixfmt);
    };
}
