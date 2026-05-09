"""Session-level health checks for test infrastructure dependencies.

Validates PostgreSQL and Redis are reachable before tests run.
Provides a clear error message instead of cryptic connection failures.
"""

import subprocess

import pytest


def _pg_isready() -> bool:
    try:
        r = subprocess.run(["pg_isready"], capture_output=True, timeout=5)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _redis_ping() -> bool:
    try:
        r = subprocess.run(["redis-cli", "ping"], capture_output=True, timeout=5)
        return r.returncode == 0 and b"PONG" in r.stdout
    except FileNotFoundError:
        return False


@pytest.fixture(autouse=True, scope="session")
def _check_infra():
    """Fail fast with actionable messages when test deps are missing."""
    if not _pg_isready():
        pytest.fail(
            "PostgreSQL is not running.\n"
            "  Dev: re-enter the directory so direnv auto-starts it, or run:\n"
            "       pg_ctl -D .direnv/postgres start\n"
            "  Nix: nix flake check handles this automatically."
        )
    if not _redis_ping():
        pytest.fail(
            "Redis is not running.\n"
            "  Dev: re-enter the directory so direnv auto-starts it, or run:\n"
            "       redis-server --port 6379 --daemonize yes\n"
            "  Nix: nix flake check handles this automatically."
        )
