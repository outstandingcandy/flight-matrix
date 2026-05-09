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
    """
    test_config_root = tmp_path / "config"
    shutil.copytree(_SOURCE_CONFIG_DIR, test_config_root)

    # dotenv is loaded from the parent of the config file (see
    # YAMLConfig._load_env: `Path(config_file).parent / '.env'`). The
    # config file lives at `tmp/config/config.yaml`, so the .env we need
    # is at `tmp/config/.env`.
    (test_config_root / ".env").write_text(
        "\n".join(
            [
                "STAGE=local",
                "SKIP_AUTH=true",
                "LOCAL_DEV_EMAIL=test@example.com",
                "LOCAL_DEV_GROUPS=admins,flight-schedules-viewers",
                "DATABASE_URL=sqlite:///:memory:",
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
