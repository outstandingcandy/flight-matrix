"""Shared fixtures for Flask-client-based tests.

The `app_client` fixture boots the full Flask app in skip-auth mode with
an in-memory SQLite database, runs `init_app()` to populate the
`db_manager` / `config` globals, and returns a Flask test client.

**Safety:** We deliberately run the app against a *test* config dir that
has its own tiny `.env` with only SQLite settings. Without this, the
project's production `.env` at the repo root (if present) would leak
into the tests via `yaml_config.YAMLConfig._load_env()`, and the tests
could hit a production Aurora cluster. The tmp-dir approach is the only
way to fully isolate dotenv loading from the developer's workstation.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SOURCE_CONFIG_DIR = _REPO_ROOT / "config"


def _build_test_config_dir(tmp_path: Path) -> Path:
    """Copy the project's config/ into tmp and drop a tiny .env next to it.

    Returns the path to the root `config.yaml` the app should use.

    Historically DATABASE_URL was ``sqlite:///:memory:``. That works for
    the Flask test client (same thread — SingletonThreadPool shares a
    raw connection) but breaks against the FastAPI TestClient, which
    runs async endpoints on a separate thread: each thread gets its own
    in-memory database, so a row a handler wrote is invisible to the
    test that asserts on it. Using a tmp-file database sidesteps the
    thread-locality entirely and works identically for both clients.
    """
    test_config_root = tmp_path / "config"
    shutil.copytree(_SOURCE_CONFIG_DIR, test_config_root)

    db_file = tmp_path / "test.db"
    (test_config_root / ".env").write_text(
        "\n".join(
            [
                "STAGE=local",
                "SKIP_AUTH=true",
                "LOCAL_DEV_EMAIL=test@example.com",
                "LOCAL_DEV_GROUPS=admins,flight-schedules-viewers",
                f"DATABASE_URL=sqlite:///{db_file}",
                "FLASK_SECRET_KEY=test_secret_for_pytest_only",
                "TAVILY_API_KEY=",
                "ADSB_API_KEY=",
                # Clear a set of env vars that yaml_config reads directly
                # from os.environ after dotenv load, so they don't pick up
                # the developer's real values:
                "DB_HOST=",
                "DB_USERNAME=",
                "DB_PASSWORD=",
                "DB_NAME=",
                "DB_ENDPOINT=",
                "USE_IAM_AUTH=false",
                "COGNITO_USER_POOL_ID=",
                "COGNITO_CLIENT_ID=",
                "COGNITO_CLIENT_SECRET=",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return test_config_root / "config.yaml"


def _reload_web_app() -> Any:
    """Drop cached web_app + src.web modules so a fresh import re-executes

    module-level code (auth shim decision, TTLCache, …).
    """
    for name in list(sys.modules):
        if name == "web_app" or name.startswith("src.web."):
            del sys.modules[name]
    return importlib.import_module("web_app")


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """Fresh Flask test client isolated from the developer's real `.env`.

    Yields a `flask.testing.FlaskClient`. Every call gets a fresh app
    instance and an in-memory SQLite database.
    """
    test_config_path = _build_test_config_dir(tmp_path)

    # Point the app at the test config. CONFIG_PATH is what init_app reads.
    monkeypatch.setenv("CONFIG_PATH", str(test_config_path))
    # Also set a bunch of things directly, since `.env`'s override=True
    # trumps os.environ. We re-set after YAMLConfig loads the tmp .env.
    monkeypatch.setenv("STAGE", "local")
    monkeypatch.setenv("SKIP_AUTH", "true")
    monkeypatch.setenv("LOCAL_DEV_EMAIL", "test@example.com")
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "admins,flight-schedules-viewers")

    web_app = _reload_web_app()
    web_app.init_app()

    client = web_app.app.test_client()
    client.application_module = web_app  # type: ignore[attr-defined]
    yield client


@pytest.fixture
def app_client_fastapi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """Fresh FastAPI test client for routes that have been ported off Flask.

    Coexists with :func:`app_client` while the migration is in flight.
    Tests for routes already migrated (see the ``*_fastapi.py`` modules
    under ``src/web/routes/``) should use this fixture; tests for routes
    still living in ``web_app.py`` keep using ``app_client``. When
    stage 0 finishes, ``app_client`` and its Flask reload path go
    together and this fixture takes over the plain name.

    Yields a ``starlette.testclient.TestClient`` wrapped so tests can
    still write ``client.application_module.db_manager`` — the FastAPI
    lifespan calls ``web_app.init_app()`` so that attribute points at
    the same DatabaseManager instance the Flask fixture would expose.

    Test isolation is the same as the Flask fixture: a tmp config dir
    with its own tiny ``.env``, an in-memory SQLite database, cached
    modules cleared so ``app.py`` re-imports cleanly.
    """
    import importlib
    import sys

    test_config_path = _build_test_config_dir(tmp_path)

    monkeypatch.setenv("CONFIG_PATH", str(test_config_path))
    monkeypatch.setenv("STAGE", "local")
    monkeypatch.setenv("SKIP_AUTH", "true")
    monkeypatch.setenv("LOCAL_DEV_EMAIL", "test@example.com")
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "admins,flight-schedules-viewers")

    # Drop cached copies so create_app() sees the new env. Both web_app
    # (which the lifespan re-imports) and app.py itself need clearing.
    for name in list(sys.modules):
        if name == "web_app" or name == "app" or name.startswith("src.web."):
            del sys.modules[name]

    app_module = importlib.import_module("app")

    from starlette.testclient import TestClient

    # `with TestClient(app) as client` triggers ASGI lifespan (which is
    # what populates app.state.db_manager via web_app.init_app()).
    # `pytest.fixture` doesn't accept a context manager directly, so we
    # enter/exit manually and yield the live client.
    fastapi_app = app_module.create_app()
    with TestClient(fastapi_app) as client:
        # Legacy attribute — tests written for the Flask fixture reach
        # for `client.application_module.db_manager` to build ad-hoc
        # tables. Point it at the just-imported web_app the lifespan
        # already initialised (same instance under fastapi_app.state).
        import web_app as web_app_module

        client.application_module = web_app_module  # type: ignore[attr-defined]
        yield client
