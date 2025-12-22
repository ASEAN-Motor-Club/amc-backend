# AMC Backend

## Dev environment

```sh
$ nix --version # If you don't have hix, install Nix on your system (https://nix.dev/install-nix.html)
$ nix develop # If you want to skip this step, look into `nix-direnv`)
$ uv run backend/manage.py migrate # creates db and runs migrations
$ uv run backend/manage.py runserver
```

## Django project
You're assumed to have some familiarity with Django.
- Please create migrations with `uv run backend/manage.py makemigrations` when you make changes to `**/models.py`.
- API is served with `django-ninja`


## Adding Slash Commands

Slash commands are now handled via a registry system in the `src/amc/commands/` package.

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

## Running Tests

We use `uv` and `nix` for dependency management and testing.

**Prerequisites:**
- Ensure you have `nix` installed and are in the dev shell (`nix develop`).

**Running the Test Suite:**
To run all tests (including the new integration tests):
```sh
$ uv run src/manage.py test
```

To run specifically the command tests:
```sh
$ uv run src/manage.py test amc.tests_commands
```

To run the integration tests for command routing:
```sh
$ uv run src/manage.py test amc.tests_integration_commands
```
