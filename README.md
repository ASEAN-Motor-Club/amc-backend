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

Slash commands are now handled via a registry system in `src/amc/commands.py`.

To add a new command:
1.  Open `src/amc/commands.py`.
2.  Import `registry` and `CommandContext`.
3.  Decorate your async function with `@registry.register("/yourcommand")` (or a list of aliases).
4.  Your function should accept `ctx: CommandContext` as the first argument, followed by typed arguments for any capture groups.

Example:
```python
@registry.register("/greet")
async def cmd_greet(ctx: CommandContext, name: str):
    await ctx.reply(f"Hello, {name}!")
```

**Testing:**
Add a corresponding test case in `src/amc/tests_commands.py` to verify your command logic and mocking.
