"""Backward-compat re-exports for the legacy database module path.

The implementation moved to `src.data` in v0.1.x:

    - DatabaseManager      → src.data.db_manager
    - SnapshotRepository   → src.data.snapshot_repo   (new)
    - CooldownRepository   → src.data.cooldown_repo   (new)
    - schema helpers       → src.data.schema          (new)

All existing `from src.utils.database import X` imports continue to work;
new code should import from `src.data.*` directly.
"""

from __future__ import annotations

from src.data.db_manager import DatabaseManager, mask_database_url
from src.data.models import (
    AircraftSnapshot,
    Base,
    GeographicRegion,
    ReportCooldown,
)

__all__ = [
    "AircraftSnapshot",
    "Base",
    "DatabaseManager",
    "GeographicRegion",
    "ReportCooldown",
    "mask_database_url",
]
