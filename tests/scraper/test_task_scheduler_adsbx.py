"""Tests for the scheduler's ADSBx wiring: the region grid and retention pruning.

Two behaviours here have no other test and no runtime signal when they break.

The scheduler builds its own region list rather than reusing the task source's,
so it can silently drift back to fr24_map's 50-window grid — which for ADSBx
means re-scraping the same airspace up to eight times per cycle.

And `prune_positions` was the first caller of any retention logic in this
codebase: `snapshot_repo.cleanup_old_data` has existed unused since it was
written, so "there is a cleanup function" and "rows are actually deleted" are
very different claims. Both the target gate and the hourly gate matter — the gate
is what stops a 60-second cycle from issuing thousands of DELETEs a day to expire
the same rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from src.scraper.sinks.adsbx_map_sink import MILITARY_TABLE, POSITIONS_TABLE, ADSBxMapSink
from src.scraper.sources.adsbx_map_source import ADSBX_REGIONS
from src.scraper.task_scheduler import TaskScheduler


def _config(**adsbx: Any) -> dict[str, Any]:
    """Minimal scheduler config with adsbx_map enabled."""
    return {
        "scraper": {
            "scheduler": {"check_interval": 60, "orphan_cleanup_threshold": 300},
            "worker": {"task_timeout": 300},
            "scrapers": {
                "adsbx_map": {
                    "enabled": True,
                    "global_coverage": True,
                    "max_priority": 5,
                    **adsbx,
                }
            },
        }
    }


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A file-backed SQLite URL with the scraper queue schema bootstrapped.

    The scheduler reads and writes `scraper_tasks` / `scraper_workers` but does
    not create them; production relies on `TaskQueue.ensure_tables_exist`.
    """
    from src.scraper.task_queue import TaskQueue

    url = f"sqlite:///{tmp_path / 'scheduler.db'}"
    queue = TaskQueue(url)
    queue.ensure_tables_exist()
    queue.engine.dispose()
    return url


class TestRegionGrid:
    def test_the_scheduler_uses_the_adsbx_grid_not_the_fr24_one(self, db_url: str) -> None:
        scheduler = TaskScheduler(_config(), db_url)

        names = {r["name"] for r in scheduler.task_types["adsbx_map"]["regions"]}
        assert names == set(ADSBX_REGIONS)

    def test_priority_trims_the_grid(self, db_url: str) -> None:
        scheduler = TaskScheduler(_config(max_priority=1), db_url)

        regions = scheduler.task_types["adsbx_map"]["regions"]
        assert regions
        assert all(r["priority"] == 1 for r in regions)

    def test_the_long_cycle_gap_is_the_default(self, db_url: str) -> None:
        """A measured pass is ~37,700 rows over ~18 minutes; the old 60-second
        default belonged to a military-only feed of ~150 rows."""
        scheduler = TaskScheduler(_config(), db_url)

        assert scheduler.task_types["adsbx_map"]["min_cycle_gap"] == 1800

    def test_each_task_carries_the_region_and_the_flag_filter(self, db_url: str) -> None:
        scheduler = TaskScheduler(_config(db_flags=1), db_url)
        config = scheduler.task_types["adsbx_map"]

        created = scheduler.create_adsbx_map_tasks(
            regions=config["regions"], db_flags=config["db_flags"]
        )

        assert created == len(ADSBX_REGIONS)
        with scheduler.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT task_key, payload FROM scraper_tasks WHERE task_type = 'adsbx_map'")
            ).all()
        assert {r.task_key for r in rows} == set(ADSBX_REGIONS)
        assert all('"dbFlags": 1' in r.payload for r in rows)


class TestRetention:
    """`prune_adsbx_positions`, the only scheduled retention in the codebase."""

    @staticmethod
    def _seed(db_url: str, table: str) -> None:
        sink = ADSBxMapSink(db_url, table=table)
        stale = datetime.now(UTC) - timedelta(days=60)
        with sink.db_engine.connect() as conn:
            conn.execute(
                text(f"INSERT INTO {table} (hex, mil, scraped_at) VALUES (:hex, :mil, :ts)"),
                [
                    {"hex": "3c4b26", "mil": False, "ts": stale},
                    {"hex": "43c6e1", "mil": True, "ts": stale},
                ],
            )
            conn.commit()
        sink.db_engine.dispose()

    @staticmethod
    def _count(scheduler: TaskScheduler, table: str) -> int:
        with scheduler.engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)

    def test_expired_rows_are_deleted(self, db_url: str) -> None:
        self._seed(db_url, POSITIONS_TABLE)
        scheduler = TaskScheduler(_config(target="positions"), db_url)

        deleted = scheduler.prune_adsbx_positions(
            "positions", {"civil_hours": 168, "military_hours": 720}
        )

        assert deleted == 2
        assert self._count(scheduler, POSITIONS_TABLE) == 0

    def test_the_military_table_is_never_pruned(self, db_url: str) -> None:
        """It has accumulated without a retention policy since it was created, so
        applying one here would delete history nobody asked to lose."""
        self._seed(db_url, MILITARY_TABLE)
        scheduler = TaskScheduler(_config(target="military"), db_url)

        assert scheduler.prune_adsbx_positions("military", {"civil_hours": 1}) == 0
        assert self._count(scheduler, MILITARY_TABLE) == 2

    def test_the_snapshots_target_is_not_pruned_either(self, db_url: str) -> None:
        self._seed(db_url, POSITIONS_TABLE)
        scheduler = TaskScheduler(_config(target="snapshots"), db_url)

        assert scheduler.prune_adsbx_positions("snapshots", {"civil_hours": 1}) == 0
        assert self._count(scheduler, POSITIONS_TABLE) == 2

    def test_a_second_call_within_the_hour_is_skipped(self, db_url: str) -> None:
        self._seed(db_url, POSITIONS_TABLE)
        scheduler = TaskScheduler(_config(target="positions"), db_url)
        retention = {"civil_hours": 168, "military_hours": 720}

        scheduler.prune_adsbx_positions("positions", retention)
        self._seed(db_url, POSITIONS_TABLE)

        assert scheduler.prune_adsbx_positions("positions", retention) == 0
        assert self._count(scheduler, POSITIONS_TABLE) == 2

    def test_the_gate_reopens_after_an_hour(self, db_url: str) -> None:
        self._seed(db_url, POSITIONS_TABLE)
        scheduler = TaskScheduler(_config(target="positions"), db_url)
        scheduler._last_prune = datetime.now(UTC) - timedelta(hours=2)

        assert scheduler.prune_adsbx_positions("positions", {"civil_hours": 1}) == 2

    def test_absent_retention_config_falls_back_to_the_documented_horizons(
        self, db_url: str
    ) -> None:
        """A config that omits the block must still expire rows, not keep forever."""
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)
        with sink.db_engine.connect() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {POSITIONS_TABLE} (hex, mil, scraped_at) VALUES (:hex, :mil, :ts)"
                ),
                [
                    # 10 days: past the 7-day civil horizon, inside the 30-day one.
                    {
                        "hex": "3c4b26",
                        "mil": False,
                        "ts": datetime.now(UTC) - timedelta(days=10),
                    },
                    {
                        "hex": "43c6e1",
                        "mil": True,
                        "ts": datetime.now(UTC) - timedelta(days=10),
                    },
                ],
            )
            conn.commit()
        sink.db_engine.dispose()
        scheduler = TaskScheduler(_config(target="positions"), db_url)

        assert scheduler.prune_adsbx_positions("positions", {}) == 1
        with scheduler.engine.connect() as conn:
            assert conn.execute(text(f"SELECT hex FROM {POSITIONS_TABLE}")).scalar() == "43c6e1"

    def test_the_cycle_prunes_without_being_asked(self, db_url: str) -> None:
        """The whole point: `cleanup_old_data` has existed unused for as long as
        it has existed, so retention has to be reached from `schedule_cycle`."""
        self._seed(db_url, POSITIONS_TABLE)
        scheduler = TaskScheduler(
            _config(target="positions", retention={"civil_hours": 1, "military_hours": 1}),
            db_url,
        )

        results = scheduler.schedule_cycle()

        assert results["adsbx_pruned"] == 2
        assert self._count(scheduler, POSITIONS_TABLE) == 0
