"""Shared pytest fixtures.

The fixtures here cover the pattern used throughout Flight Matrix tests:
an in-memory SQLite `DatabaseManager` with schema bootstrapped, and the
matching repositories ready to use.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("STAGE", "local")
os.environ.setdefault("SKIP_AUTH", "true")
os.environ.setdefault("LOCAL_DEV_EMAIL", "test@example.com")
os.environ.setdefault("LOCAL_DEV_GROUPS", "admins")


@pytest.fixture
def db_manager() -> Iterator:
    """An in-memory SQLite `DatabaseManager` with schema bootstrapped.

    Yields the manager; closes the engine at teardown.
    """
    from src.data.db_manager import DatabaseManager

    dm = DatabaseManager(":memory:")
    try:
        yield dm
    finally:
        dm.close()


@pytest.fixture
def snapshot_repo(db_manager):
    """The `SnapshotRepository` bound to the in-memory DB."""
    return db_manager.snapshots


@pytest.fixture
def cooldown_repo(db_manager):
    """The `CooldownRepository` bound to the in-memory DB.

    Also ensures the `report_cooldowns` table is bootstrapped; the core
    schema creates it, but the on-demand helper is what production uses
    on older DBs so exercise both paths.
    """
    db_manager.ensure_report_tables_exist()
    return db_manager.cooldowns
