{
  description = "AMC Backend";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = inputs @ {
    self,
    nixpkgs,
    flake-parts,
    uv2nix,
    pyproject-nix,
    pyproject-build-systems,
    ...
  }:
    flake-parts.lib.mkFlake {inherit inputs;} {
      systems = [
        "x86_64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      flake = {
        nixosModules.backend = { config, pkgs, lib, ... }:
        let
          cfg = config.services.amc-backend;
        in
        {
          options.services.amc-backend = {
            enable = lib.mkEnableOption "Enable Module";
            host = lib.mkOption {
              type = lib.types.str;
              default = "0.0.0.0";
              example = true;
              description = "The host for the main process to listen to";
            };
            port = lib.mkOption {
              type = lib.types.int;
              default = 8000;
              example = true;
              description = "The port number for the main process to listen to";
            };
            workers = lib.mkOption {
              type = lib.types.int;
              default = 1;
              example = true;
              description = "The port number for the main process to listen to";
            };
          };
          config = lib.mkIf cfg.enable {
            systemd.services.amc-backend = {
              wantedBy = [ "multi-user.target" ]; 
              after = [ "network.target" ];
              description = "API Server";
              environment = {
                DB_URL = "sqlite:///var/lib/amc.sqlite3";
                SPATIALITE_LIBRARY_PATH = pkgs.libspatialite;
              };
              restartIfChanged = false;
              serviceConfig = {
                Type = "simple";
                Restart = "on-failure";
                RestartSec = "10";
              };
              script = ''
                ${self.packages.x86_64-linux.default}/bin/uvicorn amc_backend.asgi:app \
                  --host ${cfg.host} \
                  --port ${toString cfg.port} \
                  --workers ${toString cfg.workers}
              '';
            };
          };
        };
      };
      perSystem = {
        config,
        self',
        inputs',
        pkgs,
        system,
        ...
      }: let
        inherit (nixpkgs) lib;
        pkgs = nixpkgs.legacyPackages.${system};

        workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
        overlay = workspace.mkPyprojectOverlay {
          sourcePreference = "wheel";
        };

        pyprojectOverrides = final: prev: {
        };

        # Use Python 3.12 from nixpkgs
        python = pkgs.python312;

        # Construct package set
        pythonSet =
          # Use base package set from pyproject.nix builders
          (pkgs.callPackage pyproject-nix.build.packages {
            inherit python;
          }).overrideScope
            (
              lib.composeManyExtensions [
                pyproject-build-systems.overlays.default
                overlay
                pyprojectOverrides
              ]
            );

      in {
        packages.default = pythonSet.mkVirtualEnv "amc-backend-env" workspace.deps.default;
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            pkgs.ruff
            pkgs.nil
            pkgs.alejandra
            pkgs.nixos-rebuild
            pkgs.libspatialite
          ];
          env =
            {
              # Prevent uv from managing Python downloads
              UV_PYTHON_DOWNLOADS = "never";
              # Force uv to use nixpkgs Python interpreter
              UV_PYTHON = python.interpreter;
              SPATIALITE_LIBRARY_PATH = "${pkgs.libspatialite}/lib/libspatialite.dylib";
            }
            // lib.optionalAttrs pkgs.stdenv.isLinux {
              # Python libraries often load native shared objects using dlopen(3).
              # Setting LD_LIBRARY_PATH makes the dynamic library loader aware of libraries without using RPATH for lookup.
              LD_LIBRARY_PATH = lib.makeLibraryPath pkgs.pythonManylinuxPackages.manylinux1;
            };
          shellHook = ''
            unset PYTHONPATH
          '';
        };
      };
    };
}
