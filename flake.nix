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
    let
      # TODO: patch on packager level
      # uv2nix makes it harder to patch source code, since we're importing wheels not sdist
      # These are needed for GeoDjango
      mkPostgisDeps = pkgs: {
        GEOS_LIBRARY_PATH = ''${pkgs.geos}/lib/libgeos_c.${if pkgs.stdenv.hostPlatform.isDarwin then "dylib" else "so"}'';
        GDAL_LIBRARY_PATH = ''${pkgs.gdal}/lib/libgdal.${if pkgs.stdenv.hostPlatform.isDarwin then "dylib" else "so"}'';
      };
    in
    flake-parts.lib.mkFlake {inherit inputs;} {
      systems = [
        "x86_64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      flake = {
        overlays.default = final: prev: {
          amc-backend = self.packages.${prev.system}.default;
          amc-backend-static = self.packages.${prev.system}.staticRoot;
        };
        nixosModules.backend = { config, pkgs, lib, ... }:
        let
          cfg = config.services.amc-backend;
        in
        {
          options.services.amc-backend = {
            enable = lib.mkEnableOption "Enable Module";
            user = lib.mkOption {
              type = lib.types.str;
              default = "amc";
              description = "The user that the process runs under";
            };
            group = lib.mkOption {
              type = lib.types.str;
              default = "amc";
              description = "The user group that the process runs under";
            };
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
            environment = lib.mkOption {
              type = lib.types.attrsOf lib.types.str;
              default = {};
              description = "Environment variables";
            };
          };
          config = lib.mkIf cfg.enable {
            nixpkgs.overlays = [ self.overlays.default ];

            users.users.${cfg.user} = {
              isSystemUser = true;
              group = cfg.group;
              description = "AMC Backend";
            };
            users.groups.${cfg.group} = {
              members = [ cfg.user ];
            };

            services.postgresql = {
              enable = true;
              package = pkgs.postgresql_16;
              extensions = with pkgs.postgresql_16.pkgs; [ postgis timescaledb ];
              ensureDatabases = [
                cfg.user
              ];
              ensureUsers = [
                { name = cfg.user; ensureDBOwnership = true; }
              ];
              settings = {
                client_encoding = "UTF8";
                timezone = "UTC";
              };
              authentication = pkgs.lib.mkOverride 10 ''
                local all all trust
                host all all ::1/128 trust
              '';
            };
            services.redis.servers."amc-backend".enable = true;
            services.redis.servers."amc-backend".port = 6379;

            systemd.services.amc-backend = {
              wantedBy = [ "multi-user.target" ]; 
              after = [ "network.target" ];
              description = "API Server";
              environment = {
                inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
                DJANGO_STATIC_ROOT = self.packages.x86_64-linux.staticRoot;
              } // cfg.environment;
              restartIfChanged = true;
              serviceConfig = {
                Type = "simple";
                User = cfg.user;
                Group = cfg.group;
                Restart = "on-failure";
                RestartSec = "10";
              };
              script = ''
                ${self.packages.x86_64-linux.default}/bin/uvicorn amc_backend.asgi:application \
                  --host ${cfg.host} \
                  --port ${toString cfg.port} \
                  --workers ${toString cfg.workers}
              '';
            };

            systemd.services.amc-worker = {
              wantedBy = [ "multi-user.target" ]; 
              after = [ "network.target" ];
              description = "Job queue and background worker";
              environment = {
                inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
                DJANGO_SETTINGS_MODULE = "amc_backend.settings";
              } // cfg.environment;
              restartIfChanged = true;
              serviceConfig = {
                Type = "simple";
                User = cfg.user;
                Group = cfg.group;
                Restart = "on-failure";
                RestartSec = "10";
              };
              script = ''
                ${self.packages.x86_64-linux.default}/bin/arq amc.worker.WorkerSettings
              '';
            };

            systemd.services.amc-backend-migrate = {
              description = "Migrate backend db";
              environment = {
                DJANGO_SETTINGS_MODULE = "amc_backend.settings";
              } // cfg.environment;
              restartIfChanged = false;
              serviceConfig = {
                Type = "oneshot";
                User = cfg.user;
                Group = cfg.group;
              };
              script = ''
                ${self.packages.x86_64-linux.default}/bin/django-admin migrate
              '';
            };
            environment.systemPackages = [
              self.packages.x86_64-linux.default
            ];
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

        staticRoot = 
          let
            inherit (pkgs) stdenv;
            venv = self'.packages.default;
          in
          stdenv.mkDerivation {
            name = "amc-backend-static";
            inherit (pythonSet.amc-backend) src;

            dontConfigure = true;
            dontBuild = true;
            inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;

            nativeBuildInputs = [
              venv
            ];

            installPhase = ''
              env DJANGO_STATIC_ROOT="$out" python src/manage.py collectstatic --noinput
            '';
          };
      in {
        packages.default = pythonSet.mkVirtualEnv "amc-backend-env" workspace.deps.default;
        packages.staticRoot = staticRoot;
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            pkgs.ruff
            pkgs.basedpyright
            pkgs.jq
            pkgs.nil
            pkgs.alejandra
            pkgs.nixos-rebuild
            pkgs.libspatialite
            (pkgs.postgresql_16.withPackages(p: [p.postgis]))
            pkgs.redis
          ];
          env =
            {
              # Needed for postgis
              # GDAL_LIBRARY_PATH  = "${pkgs.gdal}/lib/libgdal.dylib";

              # Prevent uv from managing Python downloads
              UV_PYTHON_DOWNLOADS = "never";
              # Force uv to use nixpkgs Python interpreter
              UV_PYTHON = python.interpreter;
              SPATIALITE_LIBRARY_PATH = "${pkgs.libspatialite}/lib/libspatialite.dylib";
              inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
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
