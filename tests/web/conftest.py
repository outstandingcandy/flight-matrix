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
    """Test client for the FastAPI app.

    Historically this fixture returned Flask's ``test_client()``. The
    Flask half of ``web_app.py`` no longer serves any traffic — every
    route is on the FastAPI side (``app.py``) — so ``app_client`` now
    aliases ``app_client_fastapi``. The name stays for source-level
    compatibility across the ~180 tests that already say
    ``def test_...(app_client): ...``.

    Tests calling Flask-specific response methods (``.get_json()``,
    ``.get_data(as_text=True)``) were migrated to the httpx-style
    ``.json()`` / ``.text`` in the same commit that flipped the
    fixture.
    """
    yield from _make_fastapi_client(monkeypatch, tmp_path)


def _make_fastapi_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """Build a FastAPI ``TestClient`` isolated from the developer env.

    Shared body for the ``app_client`` and ``app_client_fastapi``
    fixtures — historically two clients, one Flask and one FastAPI;
    now the same ``TestClient(fastapi_app)`` with the Flask
    ``test_client()``-compatible surface (``.get`` / ``.post`` / ...
    plus ``.json()``/`.text` for response bodies).

    Test isolation:

    - Fresh tmp config dir + tmp SQLite file per fixture invocation.
    - ``web_app`` and ``src.web.*`` cleared from ``sys.modules`` so
      ``create_app()`` sees the new env on re-import.
    - ``client.application_module`` set to the re-imported ``web_app``
      so tests reaching for ``client.application_module.db_manager``
      to build ad-hoc tables keep working.
    """
    import importlib
    import sys

    test_config_path = _build_test_config_dir(tmp_path)

    monkeypatch.setenv("CONFIG_PATH", str(test_config_path))
    monkeypatch.setenv("STAGE", "local")
    monkeypatch.setenv("SKIP_AUTH", "true")
    monkeypatch.setenv("LOCAL_DEV_EMAIL", "test@example.com")
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "admins,flight-schedules-viewers")

    for name in list(sys.modules):
        if name == "web_app" or name == "app" or name.startswith("src.web."):
            del sys.modules[name]

    app_module = importlib.import_module("app")

    from starlette.testclient import TestClient

    fastapi_app = app_module.create_app()
    # ``raise_server_exceptions=False`` so a handler crashing inside a
    # route surfaces as a 500 the *test* sees, matching what the
    # frontend would see in production. The default re-raises the
    # exception through the assert — hides the global exception
    # handler and breaks any "confirm we return a generic 500" test.
    with TestClient(fastapi_app, raise_server_exceptions=False) as client:
        import web_app as web_app_module

        client.application_module = web_app_module  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def app_client_fastapi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """FastAPI TestClient — historically distinct from :func:`app_client`,
    now the same fixture. Kept under this name because ~40 tests
    already say ``def test_...(app_client_fastapi): ...``; renaming
    them isn't worth the churn."""
    yield from _make_fastapi_client(monkeypatch, tmp_path)
