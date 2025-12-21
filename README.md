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
