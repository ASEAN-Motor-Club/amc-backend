# AMC Backend

## Dev environment

This project uses [Nix](https://nixos.org/) for reproducible development environments.

1.  **Install Nix:** If you don't have Nix installed, follow the instructions at [nix.dev](https://nix.dev/install-nix.html).
2.  **Enter the environment:**
    *   **Option A (Manual):** Run `nix develop` in the project root. This will drop you into a shell with all dependencies installed.
    *   **Option B (Automatic - Recommended):** Install [direnv](https://direnv.net/) and hook it into your shell. Then run `direnv allow` in the project root. The environment will automatically load when you cd into the directory.

Once in the environment:
```sh
$ backend/manage.py migrate # creates db and runs migrations
$ backend/manage.py runserver
```

### Managing Dependencies

Since the development environment is managed by Nix (via `uv2nix`), changes to `pyproject.toml` or `uv.lock` are not immediately reflected in the environment.

To add a new dependency:

1.  **Add the package:** Run `uv add <package_name>`. This updates `pyproject.toml` and `uv.lock`.
2.  **Update the environment:**
    *   If using `direnv`: Run `direnv reload`.
    *   If using `nix develop`: Exit the generic shell and run `nix develop` again.

This ensures Nix rebuilds the environment with the new dependencies.

## Django project
You're assumed to have some familiarity with Django.
- Please create migrations with `backend/manage.py makemigrations` when you make changes to `**/models.py`.
- API is served with `django-ninja`
- Detailed documentation for the **[Ministry of Commerce System](docs/ministry_system.md)** can be found in the `docs` folder.


## Adding Slash Commands

Slash commands are now handled via a registry system in the `src/amc/commands/` package.

To add a new command:
1.  Navigate to `src/amc/commands/`.
2.  Choose an appropriate category file (e.g., `general.py`, `vehicles.py`) or create a new one.
3.  Import `registry` and `CommandContext` from `amc.command_framework`.
4.  Decorate your async function with `@registry.register("/yourcommand")` (or a list of aliases).
5.  **Important**: If you created a new file, ensure you import it in `src/amc/commands/__init__.py` so it's registered.

Example:
```python
@registry.register("/greet")
async def cmd_greet(ctx: CommandContext, name: str):
    await ctx.reply(f"Hello, {name}!")
```


## Usage as NixOS Container

The flake exposes a `nixosModules.containers` module that allows you to run the backend as a systemd-nspawn container on NixOS.

### Configuration

Import the module in your NixOS configuration:

```nix
{
  inputs.amc-backend.url = "path:amc-backend";
  # ...
  outputs = { self, nixpkgs, amc-backend, ... }: {
    nixosConfigurations.my-server = nixpkgs.lib.nixosSystem {
      modules = [
        amc-backend.nixosModules.containers
        # ...
      ];
    };
  };
}
```

Configure the service:

```nix
services.amc-backend-containers = {
  enable = true;
  fqdn = "api.aseanmotorclub.com"; # The public domain name
  port = 9000; # Internal port for the container API
  relpPort = 2514; # Port for log ingestion
  
  # Path to the Necesse server named pipe for IPC
  necesseFifoPath = "/run/necesse-server/server.fifo"; 
  
  # Secret file containing env vars (see environmentFile option in systemd)
  secretFile = ./secrets/backend.age; 
  
  # List of allowed hosts for Django's ALLOWED_HOSTS
  allowedHosts = [ 
    "localhost"
    "api.aseanmotorclub.com"
  ];

  # Extra bind mounts for the container
  extraBindMounts = {
     "/some/host/path".isReadOnly = true;
  };

  # Configuration for the inner django service
  backendSettings = {
    workers = 4;
    environment = {
       DEBUG = "False";
       # ... other environment variables
    };
  };
};
```

This will:
1. Create a `amc-backend` container running the Django API and a Redis instance.
2. Create key `amc-log-listener` container for log ingestion.
3. Configure Nginx on the host to proxy requests to the container.


## Running Tests

We use `uv` and `nix` for dependency management and testing.

**Prerequisites:**
- Ensure you have `nix` installed and are in the dev shell (`nix develop`).

**Running the Test Suite:**
To run all tests (including the new integration tests):
```sh
$ src/manage.py test
```

To run specifically the command tests:
```sh
$ src/manage.py test amc.tests_commands
```

To run the integration tests for command routing:
```sh
$ src/manage.py test amc.tests_integration_commands
```
