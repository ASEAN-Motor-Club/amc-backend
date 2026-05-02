{
  description = "AMC Backend";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";

    git-hooks-nix = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

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

    ragenix = {
      url = "github:yaxitech/ragenix";
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
    ragenix,
    git-hooks-nix,
    ...
  }: let
    inherit (nixpkgs) lib;
    # TODO: patch on packager level
    # uv2nix makes it harder to patch source code, since we're importing wheels not sdist
    # These are needed for GeoDjango
    mkPostgisDeps = pkgs: {
      GEOS_LIBRARY_PATH = ''${pkgs.geos}/lib/libgeos_c.${
          if pkgs.stdenv.hostPlatform.isDarwin
          then "dylib"
          else "so"
        }'';
      GDAL_LIBRARY_PATH = ''${pkgs.gdal}/lib/libgdal.${
          if pkgs.stdenv.hostPlatform.isDarwin
          then "dylib"
          else "so"
        }'';
    };
    backendOptionsSubmodule = {
      options = {
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
        allowedHosts = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [];
          example = ["www.example.com"];
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
        environmentFile = lib.mkOption {
          type = lib.types.path;
        };
      };
    };
  in
    flake-parts.lib.mkFlake {inherit inputs;} {
      imports = [
        git-hooks-nix.flakeModule
      ];
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
        overlays.scripts = final: prev: {
          amc-scripts = self.packages.${prev.system}.scripts;
        };

        nixosModules.containers = {
          config,
          pkgs,
          lib,
          ...
        }: let
          cfg = config.services.amc-backend-containers;
        in {
          options.services.amc-backend-containers = {
            enable = lib.mkEnableOption "AMC Backend containers";
            fqdn = lib.mkOption {
              type = lib.types.str;
            };
            allowedHosts = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [];
            };
            port = lib.mkOption {
              type = lib.types.int;
              default = 8000;
            };
            relpPort = lib.mkOption {
              type = lib.types.int;
              default = 2514;
            };
            secretFile = lib.mkOption {
              type = lib.types.nullOr lib.types.path;
              default = null;
            };
            extraBindMounts = lib.mkOption {
              type = lib.types.attrsOf lib.types.anything;
              default = {};
            };
            backendSettings = lib.mkOption {
              type = lib.types.submodule backendOptionsSubmodule;
              default = {};
            };
          };
          config = lib.mkIf cfg.enable {
            services.nginx.virtualHosts.${cfg.fqdn} = {
              enableACME = true;
              forceSSL = true;
              locations = {
                "/" = {
                  proxyPass = "http://127.0.0.1:${toString cfg.port}/api/";
                  recommendedProxySettings = true;
                  extraConfig = ''
                    add_header 'Access-Control-Allow-Origin' '*' always;
                    add_header 'Access-Control-Allow-Methods' 'POST, PUT, DELETE, GET, PATCH, OPTIONS' always;
                  '';
                };
                "/api" = {
                  proxyPass = "http://127.0.0.1:${toString cfg.port}";
                  recommendedProxySettings = true;
                  extraConfig = ''
                    add_header 'Access-Control-Allow-Origin' '*' always;
                    add_header 'Access-Control-Allow-Methods' 'POST, PUT, DELETE, GET, PATCH, OPTIONS' always;
                  '';
                };
                "/admin" = {
                  proxyPass = "http://127.0.0.1:${toString cfg.port}";
                  recommendedProxySettings = true;
                };
                "/static/" = let
                  inherit (self.packages.${pkgs.system}) staticRoot;
                in {
                  alias = "${staticRoot}/";
                };
              };
            };
            containers.amc-backend = {
              autoStart = true;
              restartIfChanged = true;
              # Forward PostgreSQL port for Tailscale-only bot read access
              forwardPorts = [
                {
                  containerPort = 5432;
                  hostPort = 5432;
                  protocol = "tcp";
                }
              ];
              bindMounts =
                {
                  "/etc/ssh/ssh_host_ed25519_key".isReadOnly = true;
                }
                // cfg.extraBindMounts;
              config = {
                config,
                pkgs,
                ...
              }: {
                imports = [
                  self.nixosModules.backend
                  ragenix.nixosModules.default
                ];
                system.stateVersion = "25.05";
                environment.variables = {
                  inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
                };
                age.identityPaths = ["/etc/ssh/ssh_host_ed25519_key"];
                age.secrets.backend = lib.mkIf (cfg.secretFile != null) {
                  file = cfg.secretFile;
                  mode = "400";
                  owner = config.services.amc-backend.user;
                };
                services.amc-backend =
                  cfg.backendSettings
                  // {
                    enable = lib.mkDefault true;
                    port = lib.mkDefault cfg.port;
                    allowedHosts = lib.mkDefault ([cfg.fqdn] ++ cfg.allowedHosts);
                    environmentFile = config.age.secrets.backend.path;
                  };
              };
            };
            containers.amc-log-listener = {
              autoStart = true;
              restartIfChanged = false;
              config = {
                config,
                pkgs,
                ...
              }: {
                imports = [
                  self.nixosModules.log-listener
                ];
                system.stateVersion = "25.05";
                services.amc-log-listener = {
                  enable = true;
                  inherit (cfg) relpPort;
                };
              };
            };
          };
        };

        nixosModules.log-listener = {
          config,
          pkgs,
          lib,
          ...
        }: let
          cfg = config.services.amc-log-listener;
          ingestLogsWrapper = pkgs.writeShellScriptBin "ingest_logs_wrapper" ''
            export REDIS_PORT=${toString cfg.redisPort}
            exec ${pkgs.amc-scripts}/bin/ingest_logs
          '';
        in {
          options.services.amc-log-listener = {
            enable = lib.mkEnableOption "Log listener";
            relpPort = lib.mkOption {
              type = lib.types.int;
              default = 2514;
              example = true;
              description = "The port number for RELP log listener";
            };
            redisPort = lib.mkOption {
              type = lib.types.int;
              default = 6379;
              description = "The Redis port to enqueue log jobs to";
            };
          };
          config = lib.mkIf cfg.enable {
            nixpkgs.overlays = [self.overlays.scripts];
            services.rsyslogd = {
              enable = true;
              extraConfig = ''
                module(load="imrelp")
                module(load="omprog")

                input(type="imrelp" port="${toString cfg.relpPort}" maxDataSize="10k" ruleset="mt-in")
                Ruleset(name="mt-in") {
                  action (
                    type="omprog"
                    binary="${ingestLogsWrapper}/bin/ingest_logs_wrapper"
                    reportFailures="on"
                  )
                }
              '';
            };
            # Ensure syslog shuts down quickly (default 90s blocks container stop)
            systemd.services.syslog.serviceConfig.TimeoutStopSec = "5s";
          };
        };

        nixosModules.backend = {
          config,
          pkgs,
          lib,
          ...
        }: let
          cfg = config.services.amc-backend;
        in {
          options.services.amc-backend = backendOptionsSubmodule.options;
          config = lib.mkIf cfg.enable {
            nixpkgs.overlays = [self.overlays.default];
            nixpkgs.config.allowUnfree = true; # for timescaledb

            users.users.${cfg.user} = {
              isSystemUser = true;
              inherit (cfg) group;
              extraGroups = ["modders"];
              description = "AMC Backend";
            };
            users.groups.modders.gid = 987;
            users.groups.${cfg.group} = {
              members = [cfg.user];
            };

            services.postgresql = {
              enable = true;
              package = pkgs.postgresql_16;
              extensions = with pkgs.postgresql_16.pkgs; [postgis timescaledb pg_partman];
              ensureDatabases = [
                cfg.user
              ];
              ensureUsers = [
                {
                  name = cfg.user;
                  ensureDBOwnership = true;
                  ensureClauses.superuser = true;
                }
                {
                  name = "amc_bot_reader";
                  ensureClauses.login = true;
                }
              ];
              # Grant SELECT to bot reader + RLS on finance tables
              # Runs on first DB init only; for existing DBs run sql/setup_bot_reader.sql
              initialScript = pkgs.writeText "init-bot-reader.sql" ''
                GRANT USAGE ON SCHEMA public TO amc_bot_reader;
                GRANT SELECT ON ALL TABLES IN SCHEMA public TO amc_bot_reader;
                ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO amc_bot_reader;

                ALTER TABLE amc_finance_account ENABLE ROW LEVEL SECURITY;
                ALTER TABLE amc_finance_ledgerentry ENABLE ROW LEVEL SECURITY;
                ALTER TABLE amc_finance_journalentry ENABLE ROW LEVEL SECURITY;

                CREATE POLICY bot_deny_account ON amc_finance_account
                  FOR SELECT TO amc_bot_reader USING (false);
                CREATE POLICY bot_deny_ledger ON amc_finance_ledgerentry
                  FOR SELECT TO amc_bot_reader USING (false);
                CREATE POLICY bot_deny_journal ON amc_finance_journalentry
                  FOR SELECT TO amc_bot_reader USING (false);
              '';
              settings = {
                client_encoding = "UTF8";
                timezone = "UTC";
                listen_addresses = pkgs.lib.mkForce "*"; # Container-internal; host firewall limits exposure
                shared_preload_libraries = "timescaledb";
              };
              authentication = pkgs.lib.mkOverride 10 ''
                local all all trust
                host all all ::1/128 trust
                # Bot read-only access from Tailscale subnet only
                host amc amc_bot_reader 100.64.0.0/10 md5
              '';
            };
            services.redis.servers."amc-backend".enable = true;
            services.redis.servers."amc-backend".port = 6379;

            systemd.services.reset-amc-backend = {
              description = "Full reset";
              environment = {
                inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
                DJANGO_STATIC_ROOT = self.packages.x86_64-linux.staticRoot;
                DJANGO_SETTINGS_MODULE = "amc_backend.settings";
              };
              serviceConfig = {
                Type = "oneshot";
                User = cfg.user;
                Group = cfg.group;
                EnvironmentFile = cfg.environmentFile;
              };
              script = ''
                ${self.packages.x86_64-linux.default}/bin/django-admin flush --noinput
                ${self.packages.x86_64-linux.default}/bin/django-admin migrate
                ${self.packages.x86_64-linux.default}/bin/django-admin createsuperuser --noinput
              '';
            };

            systemd.services.amc-backend = {
              wantedBy = ["multi-user.target"];
              requires = ["amc-backend-migrate.service"];
              after = ["network.target" "amc-backend-migrate.service"];
              description = "API Server";
              environment =
                {
                  inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
                  DJANGO_STATIC_ROOT = self.packages.x86_64-linux.staticRoot;
                  ALLOWED_HOSTS = lib.strings.concatStringsSep " " cfg.allowedHosts;
                  DJANGO_SETTINGS_MODULE = "amc_backend.settings";
                }
                // cfg.environment;
              restartIfChanged = false;
              serviceConfig = {
                Type = "simple";
                User = cfg.user;
                Group = cfg.group;
                Restart = "on-failure";
                RestartSec = "10";
                TimeoutStopSec = "10";
                EnvironmentFile = cfg.environmentFile;
              };
              script = ''
                ${self.packages.x86_64-linux.default}/bin/uvicorn amc_backend.asgi:application \
                  --host ${cfg.host} \
                  --port ${toString cfg.port} \
                  --workers ${toString cfg.workers}
              '';
            };

            systemd.services.amc-worker = {
              wantedBy = ["multi-user.target"];
              after = ["network.target"];
              description = "Job queue and background worker";
              environment =
                {
                  inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
                  DJANGO_SETTINGS_MODULE = "amc_backend.settings";
                }
                // cfg.environment;
              restartIfChanged = false;
              serviceConfig = {
                Type = "simple";
                User = cfg.user;
                Group = cfg.group;
                Restart = "on-failure";
                RestartSec = "10";
                TimeoutStopSec = "10";
                EnvironmentFile = cfg.environmentFile;
              };
              script = ''
                ${self.packages.x86_64-linux.default}/bin/arq amc_backend.worker.WorkerSettings
              '';
            };

            systemd.services.dummy-server = {
              wantedBy = ["multi-user.target"];
              after = ["network.target"];
              description = "Dummy server";
              environment =
                {
                }
                // cfg.environment;
              restartIfChanged = true;
              serviceConfig = {
                Type = "simple";
                User = cfg.user;
                Group = cfg.group;
                Restart = "on-failure";
                RestartSec = "10";
                TimeoutStopSec = "10";
              };
              script = ''
                ${self.packages.x86_64-linux.scripts}/bin/dummy_server
              '';
            };

            systemd.services.amc-backend-migrate = {
              description = "Migrate backend db";
              requires = ["postgresql.service"];
              after = ["postgresql.service"];
              environment =
                {
                  DJANGO_SETTINGS_MODULE = "amc_backend.settings";
                  inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
                }
                // cfg.environment;
              restartIfChanged = false;
              serviceConfig = {
                Type = "oneshot";
                User = cfg.user;
                Group = cfg.group;
                EnvironmentFile = cfg.environmentFile;
              };
              script = ''
                ${self.packages.x86_64-linux.default}/bin/amc-manage migrate
              '';
            };
            environment.systemPackages = [
              self.packages.x86_64-linux.default
              # Convenience wrapper: `amcm <command>` runs amc-manage as the amc user
              # Inherits cfg.environment so management commands can reach mod/game servers
              (let
                envVars =
                  cfg.environment
                  // {
                    DJANGO_SETTINGS_MODULE = "amc_backend.settings";
                  };
                envExports = lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "${k}=${v}") envVars);
              in
                pkgs.writeShellScriptBin "amcm" ''
                  exec su -s /bin/sh ${cfg.user} -c \
                    "${envExports} exec ${self.packages.x86_64-linux.default}/bin/amc-manage $(printf ' %q' "$@")"
                '')
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
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true; # for timescaledb
        };

        workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};
        overlay = workspace.mkPyprojectOverlay {
          sourcePreference = "wheel";
        };

        editableOverlay = workspace.mkEditablePyprojectOverlay {
          root = "$REPO_ROOT";
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

        staticRoot = let
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

        virtualenv = (pythonSet.overrideScope editableOverlay).mkVirtualEnv "amc-backend-dev-env" workspace.deps.all;
      in {
        packages.default = pythonSet.mkVirtualEnv "amc-backend-env" workspace.deps.default;
        packages.scripts = pythonSet.mkVirtualEnv "amc-scripts-env" {scripts = [];};
        packages.staticRoot = staticRoot;

        checks.ruff =
          pkgs.runCommand "ruff-check" {
            buildInputs = [pkgs.ruff];
          } ''
            export RUFF_CACHE_DIR=$(mktemp -d)
            ruff check ${./.} --cache-dir "$RUFF_CACHE_DIR"
            touch $out
          '';

        checks.django-check =
          pkgs.runCommand "django-check" {
            buildInputs = [virtualenv pkgs.libspatialite];
            inherit (mkPostgisDeps pkgs) GEOS_LIBRARY_PATH GDAL_LIBRARY_PATH;
            SPATIALITE_LIBRARY_PATH = "${pkgs.libspatialite}/lib/libspatialite.${
              if pkgs.stdenv.hostPlatform.isDarwin
              then "dylib"
              else "so"
            }";
          } ''
            python ${./.}/src/manage.py check
            touch $out
          '';

        checks.pytest =
          pkgs.runCommand "amc-backend-pytest" {
            buildInputs = [
              virtualenv
              (pkgs.postgresql_16.withPackages (p: [p.postgis p.timescaledb]))
              pkgs.redis
              pkgs.gdal
              pkgs.geos
            ];
            GDAL_LIBRARY_PATH = "${pkgs.gdal}/lib/libgdal${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";
            GEOS_LIBRARY_PATH = "${pkgs.geos}/lib/libgeos_c${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";
          } ''
            export HOME=$(mktemp -d)
            export DJANGO_SETTINGS_MODULE=amc_backend.settings

            # Cleanup trap: stop postgres and release SysV shared memory on exit.
            # Without this, failed builds leak shared memory segments that
            # eventually exhaust the system-wide shmmni limit (32 on macOS).
            cleanup() {
              set +e
              # Terminate lingering test DB connections so pytest-django can drop them
              psql -h "$PGHOST" postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname LIKE 'test_%';" > /dev/null 2>&1
              pg_ctl -D "$PGDATA" stop -m fast > /dev/null 2>&1
              redis-cli shutdown nosave > /dev/null 2>&1
              # Reclaim any SysV shared memory segments we own
              ipcs -m 2>/dev/null | awk -v user="$(whoami)" '$5 == user {print $2}' | while read -r id; do
                ipcrm -m "$id" 2>/dev/null
              done
              true
            }
            trap cleanup EXIT

            # Setup local Postgres in sandbox
            export PGHOST=$HOME/postgres
            export PGDATA=$PGHOST/data
            export PGUSER=$(whoami)
            mkdir -p $PGHOST
            initdb -D $PGDATA -c shared_memory_type=mmap > /dev/null

            # Start postgres on a unix socket in the sandbox (timescaledb requires shared_preload_libraries)
            pg_ctl -w -D $PGDATA -o "-k $PGHOST -h ''' -c shared_preload_libraries=timescaledb" start > /dev/null
            createdb -h $PGHOST amc
            psql -h $PGHOST amc -c "CREATE EXTENSION IF NOT EXISTS postgis;" > /dev/null
            psql -h $PGHOST amc -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" > /dev/null

            # Setup local Redis in sandbox
            redis-server --port 6379 --daemonize yes > /dev/null

            # Copy source to writable directory
            cp -r ${./.}/src .
            chmod -R +w .
            export PYTHONPATH=src

            # Run migrations
            python src/manage.py migrate > /dev/null

            # Run pytest
            python -m pytest src/ --tb=short -q

            touch $out
          '';

        # Git hooks configuration
        pre-commit.settings.hooks = {
          ruff.enable = true;
        };

        devShells.default = pkgs.mkShell {
          packages =
            [
              virtualenv
              pkgs.gettext
              pkgs.uv
              pkgs.jq
              pkgs.nil
              pkgs.alejandra
              pkgs.nixos-rebuild
              pkgs.libspatialite
              (pkgs.postgresql_16.withPackages (p: [p.postgis p.timescaledb]))
              pkgs.redis
              pkgs.pre-commit
            ]
            ++ config.pre-commit.settings.enabledPackages;
          env =
            {
              # Needed for postgis
              # GDAL_LIBRARY_PATH  = "${pkgs.gdal}/lib/libgdal.dylib";

              # Prevent uv from managing Python downloads
              UV_PYTHON_DOWNLOADS = "never";
              UV_NO_SYNC = "1";
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
            ${config.pre-commit.shellHook}
            unset PYTHONPATH
            export REPO_ROOT=$(git rev-parse --show-toplevel)
          '';
        };
      };
    };
}
