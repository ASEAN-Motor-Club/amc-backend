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

