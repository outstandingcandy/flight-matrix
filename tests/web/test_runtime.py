"""Unit tests for :mod:`src.web.runtime` + web_app shim identity.

The runtime module is the process-level state for the FastAPI web
app. Invariants:

- :func:`init_app` populates ``db_manager`` and ``config``.
- :func:`reset_runtime` drops them for test teardown.
- ``web_app.__getattr__`` routes ``web_app.db_manager`` /
  ``web_app.config`` back to the runtime module — the re-export
  isn't a snapshot binding.
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


def test_web_app_reflects_current_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """``web_app.db_manager`` MUST route through to
    ``runtime.db_manager`` — a plain re-export would bind the
    initial ``None`` at import time and never see the ``init_app``
    update, silently breaking handlers that read
    ``web_app.db_manager`` mid-request.
    """
    import web_app
    from src.web import runtime

    monkeypatch.setenv("CONFIG_PATH", "config/config.yaml")

    runtime.reset_runtime()
    assert web_app.db_manager is None

    runtime.init_app()
    try:
        assert web_app.db_manager is not None
        assert web_app.db_manager is runtime.db_manager
    finally:
        runtime.reset_runtime()


def test_web_app_bad_attribute_raises() -> None:
    """The ``__getattr__`` shim only forwards the three runtime
    names; other attributes raise cleanly rather than resolving to a
    stale global."""
    import web_app

    with pytest.raises(AttributeError, match="no attribute 'nonexistent'"):
        _ = web_app.nonexistent
