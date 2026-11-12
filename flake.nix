{
  description = "Bootstrap — NixOS/Windows install configs + typed Python CLI";

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
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # No darwin nix config lives in this repo (darwin secret-zero holds its
    # key in memory and decrypts with plain sops — see lib/secrets.py). The
    # input exists solely so flake.lock pins the rev that
    # phases/darwin/switch.py's first-activation `nix run nix-darwin` uses
    # (resolved at runtime via `nix flake metadata`).
    nix-darwin = {
      url = "github:nix-darwin/nix-darwin";
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
      sops-nix,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system (pkgsFor system));

      mkBootstrap =
        pkgs:
        pkgs.python3.pkgs.buildPythonApplication {
          pname = "bootstrap";
          version = "0.1.0";
          src = pkgs.lib.cleanSource ./python;
          pyproject = true;

          build-system = with pkgs.python3.pkgs; [ hatchling ];

          dependencies = with pkgs.python3.pkgs; [
            typer
            rich
            tomlkit
            ruamel-yaml
          ];

          nativeCheckInputs =
            (with pkgs.python3.pkgs; [
              pytestCheckHook
              mypy
            ])
            ++ [
              # flake_splice unit tests git-init temp repos and run git add;
              # create_usb identity tests generate age keypairs; the register
              # decision tests encrypt and rekey a real sops file, so the
              # host-key decrypt check is proven on real crypto, not a fake.
              pkgs.git
              pkgs.age
              pkgs.sops
            ];

          pytestFlagsArray = [ "tests/unit" ];

          pythonImportsCheck = [
            "bootstrap"
            "bootstrap.cli"
            "bootstrap.ci.e2e_stage"
          ];

          postCheck = ''
            echo "running mypy --strict..."
            mypy --strict --config-file pyproject.toml src/bootstrap tests
          '';

          makeWrapperArgs = [
            "--prefix"
            "PATH"
            ":"
            (pkgs.lib.makeBinPath (
              (with pkgs; [
                git
                gh
                sops
                age
                _1password-cli
                openssh
                coreutils
              ])
              # Linux-only tools for the flash pipeline: disko partitioning,
              # nixos-install/nixos-generate-config/nixos-enter, unlock-keyslot
              # enrollment (clevis luks bind: jose, tang pin via curl, tpm2 pin;
              # systemd-cryptenroll for tpm2), LUKS + fs tooling for
              # bootstrap-create-usb.
              ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux (
                with pkgs;
                [
                  disko
                  flashrom
                  dmidecode
                  nixos-install-tools
                  clevis
                  jose
                  tpm2-tools
                  curl
                  cryptsetup
                  systemd
                  gptfdisk
                  util-linux
                  e2fsprogs
                  dosfstools
                ]
              )
            ))
          ]
          # The disko CLI evaluates its config with `import <nixpkgs>`; pin
          # NIX_PATH to this flake's nixpkgs so bootstrap-flash works where
          # no channel exists (the USB installer). Flake commands ignore
          # NIX_PATH, so nothing else is affected.
          ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
            "--set"
            "NIX_PATH"
            "nixpkgs=${nixpkgs}"
          ];

          meta = {
            description = "Typed fresh-machine bootstrap CLI for jacobbrugh/dotfiles";
            mainProgram = "bootstrap";
            license = pkgs.lib.licenses.mit;
          };
        };

      # The flake source baked into installer media (/etc/bootstrap-flake).
      # Filtered to what bootstrap-flash consumes — flake plumbing, the
      # NixOS configs, the Python package, and the sops material — so
      # test/CI/doc edits don't invalidate the installer-usb closure (and
      # with it every cached e2e leg).
      flashFlakeSource =
        let
          keep = [
            "flake.nix"
            "flake.lock"
            "secrets.yaml"
            "secrets-privileged.yaml"
            ".sops.yaml"
            "nix"
            "python"
            "host-hardware.nix"
            "host-networking.nix"
            "windows-bootstrap.nix"
          ];
        in
        nixpkgs.lib.cleanSourceWith {
          name = "bootstrap-flash-source";
          src = self;
          filter =
            path: _type:
            builtins.elem (baseNameOf path) keep
            || nixpkgs.lib.any (root: nixpkgs.lib.hasPrefix "${self}/${root}/" (path + "/")) [
              "nix"
              "python"
            ];
        };

      # The Dasharo coreboot image bootstrap-firmware writes to a Protectli
      # VP4600-series board, pinned by content hash so the conversion is offline
      # and reproducible. sha256 is the value the publisher ships alongside it
      # at <same URL>.sha256; Protectli distribute the identical v1.2.0 image
      # for this board in their own updater repo.
      protectliVp46xxRom = (pkgsFor "x86_64-linux").fetchurl {
        url = "https://dl.3mdeb.com/open-source-firmware/Dasharo/protectli_vault_cml/v1.2.0/protectli_vp46xx_v1.2.0.rom";
        hash = "sha256-3uA7xN5SUBIxa5guDDAdJGpWLNmnn2FkmHaUlCqUClc=";
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
      nixosConfigurations = {
        wsl-bootstrap = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            sops-nix.nixosModules.sops
            nixos-wsl.nixosModules.default
            ./nix/nixos
            ./nix/nixos/wsl.nix
            (if builtins.pathExists ./host-hardware.nix then ./host-hardware.nix else { })
          ];
        };

        bootstrap-secret-zero-wsl = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            sops-nix.nixosModules.sops
            nixos-wsl.nixosModules.default
            ./nix/nixos/bootstrap-secret-zero-wsl.nix
          ];
        };
      }
      // {
        # The LUKS USB installer system bootstrap-create-usb flashes.
        installer-usb = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            (import ./nix/nixos/installer-usb.nix {
              bootstrap = mkBootstrap (pkgsFor "x86_64-linux");
              flakeSource = flashFlakeSource;
              firmwareRom = protectliVp46xxRom;
            })
          ];
        };
      }
      // (
        if builtins.pathExists ./host-hardware.nix then
          {
            bootstrap-secret-zero = nixpkgs.lib.nixosSystem {
              system = "x86_64-linux";
              # The CLI, for the first-boot unit the flash wrapper imports
              # (nix/nixos/bootstrap-firstboot.nix). Same mkBootstrap call as
              # installer-usb, so the derivation is already in the installer's
              # store and nixos-install copies it into the target's closure.
              specialArgs = {
                bootstrap = mkBootstrap (pkgsFor "x86_64-linux");
              };
              modules = [
                sops-nix.nixosModules.sops
                ./nix/nixos/bootstrap-secret-zero.nix
              ];
            };
          }
        else
          { }
      );

      winConfigurations.bootstrap = nix-win.lib.winSystem {
        pkgs = nixpkgs.legacyPackages."x86_64-linux";
        specialArgs = {
          czData = {
            username = "jacob";
          };
        };
        modules = [ ./windows-bootstrap.nix ];
      };

      packages = forAllSystems (
        _system: pkgs:
        {
          default = mkBootstrap pkgs;
          bootstrap = mkBootstrap pkgs;
        }
        // pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          # One e2e per bootstrap tier (README § Two tiers), run in parallel by
          # the Jenkinsfile after `bootstrap-e2e-stage <tier>` has staged each
          # one's fixtures. __noChroot + the CI client's `sandbox = relaxed`
          # (written by that tool) run these builds unsandboxed so the test
          # VMs' SLIRP egress reaches the internet.
          # `src = self` makes the whole flake source an input of each test
          # derivation. The VMs consume nix/nixos/*.nix at RUN time from the
          # staged copy of the source, not through the Python package, so
          # without it a change to those modules left the derivation — and
          # the CI stage that "built" it — untouched, and no VM ever ran.
          e2e-sandbox =
            ((import ./tests/e2e-sandbox { inherit pkgs sops-nix; }).mkTest {
              bootstrap = mkBootstrap pkgs;
              src = self;
            }).overrideTestDerivation
              (_: {
                __noChroot = true;
              });
          e2e-privileged =
            ((import ./tests/e2e-privileged { inherit pkgs sops-nix; }).mkTest {
              bootstrap = mkBootstrap pkgs;
              src = self;
            }).overrideTestDerivation
              (_: {
                __noChroot = true;
              });
        }
      );

      apps = forAllSystems (
        _system: pkgs:
        {
          default = mkApp pkgs "bootstrap";
          prereqs = mkApp pkgs "bootstrap-prereqs";
          secret-zero = mkApp pkgs "bootstrap-secret-zero";
          ssh = mkApp pkgs "bootstrap-ssh";
          register = mkApp pkgs "bootstrap-register";
          switch = mkApp pkgs "bootstrap-switch";
          post = mkApp pkgs "bootstrap-post";
        }
        // pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          create-usb = mkApp pkgs "bootstrap-create-usb";
          flash = mkApp pkgs "bootstrap-flash";
          firmware = mkApp pkgs "bootstrap-firmware";
          # CI only: stages one e2e tier's fixtures and binary cache on the
          # daemon host (python/src/bootstrap/ci/e2e_stage.py).
          e2e-stage = mkApp pkgs "bootstrap-e2e-stage";
        }
      );

      checks = forAllSystems (
        system: pkgs:
        let
          mypyEnv = pkgs.python3.withPackages (
            ps: with ps; [
              mypy
              typer
              rich
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
        // pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux (
          let
            # Typechecks the concatenation the test driver runs — the shared
            # sops-rule helper spliced ahead of the script — so a name the
            # script takes from the prelude is checked against its real
            # definition rather than a stub.
            mkMypyScriptCheck =
              name: scripts:
              pkgs.runCommand name
                {
                  nativeBuildInputs = [ mypyEnv ];
                }
                ''
                  export PYTHONPATH=${pkgs.nixos-test-driver}/${pkgs.python3.sitePackages}
                  cat ${pkgs.lib.concatStringsSep " " scripts} > test_script.py
                  ${mypyEnv}/bin/mypy --strict test_script.py
                  touch $out
                '';
          in
          {
            mypy-e2e-sandbox-test-script = mkMypyScriptCheck "mypy-e2e-sandbox-test-script" [
              ./tests/e2e-lib/sops_rules.py
              ./tests/e2e-lib/health.py
              ./tests/e2e-sandbox/test_script.py
            ];
            mypy-e2e-privileged-test-script = mkMypyScriptCheck "mypy-e2e-privileged-test-script" [
              ./tests/e2e-lib/sops_rules.py
              ./tests/e2e-lib/health.py
              ./tests/e2e-privileged/test_script.py
            ];
          }
        )
      );

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
