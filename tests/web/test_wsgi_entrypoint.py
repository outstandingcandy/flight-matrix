"""Tests for the gunicorn entry point.

`web_app.py` builds the Flask app at import time but leaves `db_manager` as
`None` until `init_app()` runs. Nothing about serving an uninitialised app looks
broken from the outside: it starts, the login redirect works, a health check on
`/` passes — and every route that touches a table answers 500 with
`'NoneType' object has no attribute 'get_session'`.

That is exactly what the `gcp` VM target did, because its systemd unit pointed
gunicorn at `web_app:app`. Both halves of the fix are pinned here: the module
that gunicorn loads must initialise, and the unit must name that module.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_UNIT_FILE = _REPO_ROOT / "scripts" / "systemd" / "flight-matrix-web.service"


def _gunicorn_app_spec(unit_text: str) -> str:
    """Return the `module:attribute` gunicorn is told to serve."""
    exec_start = re.search(r"^ExecStart=(.*?)(?=^\w)", unit_text, re.M | re.S)
    assert exec_start, "the unit has no ExecStart"
    # Continuation lines end in a backslash; the app spec is the last argument.
    command = exec_start.group(1).replace("\\\n", " ")
    return command.split()[-1]


class TestSystemdUnit:
    def test_the_unit_serves_an_entry_point_that_initialises(self) -> None:
        spec = _gunicorn_app_spec(_UNIT_FILE.read_text(encoding="utf-8"))
        assert spec == "wsgi:app", (
            f"the unit serves {spec!r}; web_app:app never calls init_app(), so every "
            "database-backed route would answer 500 while the service looks healthy"
        )


class TestWsgiModule:
    def test_importing_it_initialises_the_database_manager(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Reuses the app_client fixture's isolation: a copy of config/ in tmp with
        # its own .env, so importing the app here cannot reach a real database.
        from tests.web.conftest import _build_test_config_dir

        monkeypatch.setenv("CONFIG_PATH", str(_build_test_config_dir(tmp_path)))
        monkeypatch.setenv("STAGE", "local")
        monkeypatch.setenv("SKIP_AUTH", "true")

        for name in list(sys.modules):
            if name in ("wsgi", "web_app") or name.startswith("src.web."):
                del sys.modules[name]

        wsgi: Any = importlib.import_module("wsgi")

        web_app = sys.modules["web_app"]
        assert web_app.db_manager is not None, (
            "importing wsgi must leave web_app.db_manager usable — that is the module's entire job"
        )
        assert wsgi.app is web_app.app, "wsgi must export the app it initialised"

    def test_a_database_backed_route_answers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The end-to-end shape of the production failure: an unauthenticated route
        # that reads a table. Uninitialised, this is a 500.
        from tests.web.conftest import _build_test_config_dir

        monkeypatch.setenv("CONFIG_PATH", str(_build_test_config_dir(tmp_path)))
        monkeypatch.setenv("STAGE", "local")
        monkeypatch.setenv("SKIP_AUTH", "true")

        for name in list(sys.modules):
            if name in ("wsgi", "web_app") or name.startswith("src.web."):
                del sys.modules[name]

        wsgi: Any = importlib.import_module("wsgi")

        response = wsgi.app.test_client().get("/api/aircraft/search?registration=ZZ&limit=1")
        assert response.status_code == 200, response.get_data(as_text=True)[:400]
