"""Unit tests for :mod:`src.web.runtime`.

The runtime module is the process-level state for the FastAPI web
app. Invariants:

- :func:`init_app` populates ``db_manager`` and ``config``.
- :func:`reset_runtime` drops them for test teardown.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_init_app_populates_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh env → ``init_app`` fills ``db_manager`` and ``config``."""
    from src.web import runtime

    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("CONFIG_PATH", "config/config.yaml")

    runtime.reset_runtime()
    assert runtime.db_manager is None
    assert runtime.config is None

    runtime.init_app()
    try:
        assert runtime.db_manager is not None
        assert runtime.config is not None
    finally:
        runtime.reset_runtime()


def test_reset_runtime_drops_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """After ``init_app`` + ``reset_runtime``, both globals are None."""
    from src.web import runtime

    monkeypatch.setenv("CONFIG_PATH", "config/config.yaml")

    runtime.init_app()
    assert runtime.db_manager is not None

    runtime.reset_runtime()
    assert runtime.db_manager is None
    assert runtime.config is None


# The former ``web_app`` compatibility-shim tests were dropped when
# ``web_app.py`` itself was deleted. FastAPI handlers now import
# directly from :mod:`src.web.runtime` / :mod:`src.web.image_helpers`
# / …, so there's no shim to guard.
