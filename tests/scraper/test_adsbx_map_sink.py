"""Tests for `ADSBxMapSink` and the retention prune that keeps its table bounded.

Two things are new here and neither has any runtime signal when it breaks. The
sink's destination table is now injected, because the same schema serves the
military-only table and the full-fleet `adsbx_positions` table on different
retention clocks — and a table name that reaches the DDL but not the upsert
produces an INSERT against a table that does not exist. And the column list is
now derived in one place, so a field added to the CREATE but forgotten in the
VALUES fails every write of every region.

The prune is tested against a real database rather than by asserting on SQL
text: the batching form (`id IN (SELECT id ... LIMIT n)`) is the only one both
Postgres and SQLite accept, and the civil predicate has to catch a null `mil`
flag or those rows never expire under either horizon.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.adsbx_map.models import (
    ADSBxMapAircraftData,
    ADSBxMapResult,
)
from sqlalchemy import create_engine, inspect, text

from src.scraper.sinks.adsbx_map_sink import (
    _COLUMNS,
    _PRUNE_BATCH,
    _UPDATE_COLUMNS,
    MILITARY_TABLE,
    POSITIONS_TABLE,
    ADSBxMapSink,
    prune_positions,
)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A file-backed SQLite URL.

    Not `:memory:` — the sink and the assertions open separate connections, and
    an in-memory database is per-connection under some pool configurations.
    """
    return f"sqlite:///{tmp_path / 'adsbx.db'}"


def _task(task_key: str = "north_eurafrica") -> ScraperTask:
    return ScraperTask(task_type="adsbx_map", task_key=task_key)


def _result(*aircraft: ADSBxMapAircraftData, task_key: str = "north_eurafrica") -> ADSBxMapResult:
    return ADSBxMapResult(
        success=True,
        task_key=task_key,
        task_type="adsbx_map",
        aircraft=list(aircraft),
        aircraft_count=len(aircraft),
    )


class TestColumnList:
    """`_COLUMNS` is the single source for the CREATE, the INSERT and the UPDATE."""

    def test_every_written_column_exists_in_the_created_table(self, db_url: str) -> None:
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        actual = {c["name"] for c in inspect(sink.db_engine).get_columns(POSITIONS_TABLE)}

        missing = set(_COLUMNS) - actual
        assert not missing, f"{sorted(missing)} are written but never created"

    def test_the_conflict_key_is_never_overwritten(self) -> None:
        """Updating `hex` or `feed_timestamp` would rewrite the row's identity."""
        assert "hex" not in _UPDATE_COLUMNS
        assert "feed_timestamp" not in _UPDATE_COLUMNS

    def test_the_winning_region_is_recorded_once(self) -> None:
        """Windows overlap by design, so the second region to arrive must not
        claim a row the first one already wrote."""
        assert "scrape_task_key" not in _UPDATE_COLUMNS


class TestTableSelection:
    def test_the_default_is_the_original_military_table(self, db_url: str) -> None:
        sink = ADSBxMapSink(db_url)

        assert sink.table == MILITARY_TABLE
        assert inspect(sink.db_engine).has_table(MILITARY_TABLE)

    def test_the_positions_table_is_created_on_request(self, db_url: str) -> None:
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        assert inspect(sink.db_engine).has_table(POSITIONS_TABLE)

    def test_both_tables_can_coexist_in_one_database(self, db_url: str) -> None:
        """Index names are database-wide on Postgres, so the two schemas must not
        try to create the same index."""
        ADSBxMapSink(db_url, table=MILITARY_TABLE)
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        insp = inspect(sink.db_engine)
        assert insp.has_table(MILITARY_TABLE)
        assert insp.has_table(POSITIONS_TABLE)
        mil_ix = {i["name"] for i in insp.get_indexes(MILITARY_TABLE)}
        pos_ix = {i["name"] for i in insp.get_indexes(POSITIONS_TABLE)}
        assert mil_ix and pos_ix
        assert not (mil_ix & pos_ix), "the two tables share an index name"

    def test_writes_land_in_the_table_that_was_asked_for(self, db_url: str) -> None:
        ADSBxMapSink(db_url, table=MILITARY_TABLE)
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        sink.on_success(_task(), _result(ADSBxMapAircraftData(hex="3c4b26")))

        with sink.db_engine.connect() as conn:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {POSITIONS_TABLE}")).scalar() == 1
            assert conn.execute(text(f"SELECT COUNT(*) FROM {MILITARY_TABLE}")).scalar() == 0

    @pytest.mark.parametrize(
        "bad", ["adsbx positions", "adsbx-positions", "t; DROP TABLE x", "", "1t"]
    )
    def test_a_name_that_is_not_an_identifier_is_refused(self, db_url: str, bad: str) -> None:
        """The name is interpolated into DDL and DML, never bound as a parameter."""
        with pytest.raises(ValueError):
            ADSBxMapSink(db_url, table=bad)


class TestWrites:
    def test_a_civil_row_is_stored_with_its_position_and_age(self, db_url: str) -> None:
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)
        feed_time = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

        sink.on_success(
            _task(),
            _result(
                ADSBxMapAircraftData(
                    hex="3c4b26",
                    flight="DLH123",
                    latitude=51.2,
                    longitude=-0.5,
                    altitude_baro=30000,
                    seen_pos=12.5,
                    mil=False,
                    timestamp=feed_time,
                )
            ),
        )

        with sink.db_engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT hex, flight, latitude, altitude_baro, seen_pos, mil, "
                    f"scrape_task_key FROM {POSITIONS_TABLE}"
                )
            ).one()

        assert row.hex == "3c4b26"
        assert row.flight == "DLH123"
        assert row.latitude == pytest.approx(51.2)
        assert row.altitude_baro == 30000
        # Without seen_pos the row's age is unrecoverable: `timestamp` already has
        # it subtracted, so nothing downstream can tell a fresh position from a
        # ten-minute-old one.
        assert row.seen_pos == pytest.approx(12.5)
        assert not row.mil
        assert row.scrape_task_key == "north_eurafrica"

    def test_rows_without_a_hex_are_skipped_not_written_as_null(self, db_url: str) -> None:
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        sink.on_success(
            _task(),
            _result(ADSBxMapAircraftData(hex="3c4b26"), ADSBxMapAircraftData(hex="")),
        )

        with sink.db_engine.connect() as conn:
            hexes = [r[0] for r in conn.execute(text(f"SELECT hex FROM {POSITIONS_TABLE}"))]
        assert hexes == ["3c4b26"]

    def test_the_same_position_arriving_twice_updates_rather_than_duplicates(
        self, db_url: str
    ) -> None:
        """Overlapping windows re-report the same aircraft at the same feed time."""
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)
        feed_time = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

        sink.on_success(
            _task("north_eurafrica"),
            _result(
                ADSBxMapAircraftData(hex="3c4b26", altitude_baro=30000, timestamp=feed_time),
                task_key="north_eurafrica",
            ),
        )
        sink.on_success(
            _task("north_asia"),
            _result(
                ADSBxMapAircraftData(hex="3c4b26", altitude_baro=32000, timestamp=feed_time),
                task_key="north_asia",
            ),
        )

        with sink.db_engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT altitude_baro, scrape_task_key FROM {POSITIONS_TABLE}")
            ).all()

        assert len(rows) == 1
        assert rows[0].altitude_baro == 32000, "the later report should win"
        assert rows[0].scrape_task_key == "north_eurafrica", "the first region keeps the credit"

    def test_a_new_feed_time_is_a_new_row(self, db_url: str) -> None:
        """The table is a position history, not a current-state table."""
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        for minute in (0, 30):
            sink.on_success(
                _task(),
                _result(
                    ADSBxMapAircraftData(
                        hex="3c4b26",
                        timestamp=datetime(2026, 8, 17, 12, minute, tzinfo=UTC),
                    )
                ),
            )

        with sink.db_engine.connect() as conn:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {POSITIONS_TABLE}")).scalar() == 2

    def test_an_empty_result_is_not_an_error(self, db_url: str) -> None:
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        sink.on_success(_task(), _result())

        with sink.db_engine.connect() as conn:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {POSITIONS_TABLE}")).scalar() == 0

    def test_no_database_url_disables_the_sink_silently(self) -> None:
        sink = ADSBxMapSink("")

        assert sink.db_engine is None
        # Must not raise: a no-DB run is how the live tests exercise the scraper.
        sink.on_success(_task(), _result(ADSBxMapAircraftData(hex="3c4b26")))


class TestAdditiveMigration:
    """`CREATE TABLE IF NOT EXISTS` is a no-op against an existing table.

    So a column added to the DDL never reaches a deployment that already has the
    table, and every INSERT afterwards fails on the unknown name — for as long as
    nobody reads the logs.
    """

    def test_a_column_missing_from_an_older_table_is_added(self, db_url: str) -> None:
        engine = create_engine(db_url)
        legacy = ", ".join(f"{c} TEXT" for c in _COLUMNS if c != "seen_pos")
        with engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE {POSITIONS_TABLE} "
                    f"(id INTEGER PRIMARY KEY AUTOINCREMENT, {legacy}, "
                    f"UNIQUE (hex, feed_timestamp))"
                )
            )
            conn.commit()

        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        assert "seen_pos" in sink._existing_columns()
        # And the write path works against the migrated table.
        sink.on_success(_task(), _result(ADSBxMapAircraftData(hex="3c4b26", seen_pos=4.0)))
        with sink.db_engine.connect() as conn:
            assert conn.execute(text(f"SELECT seen_pos FROM {POSITIONS_TABLE}")).scalar() == 4.0

    def test_a_current_table_is_left_alone(self, db_url: str) -> None:
        ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)

        assert "seen_pos" in sink._existing_columns()

    def test_inspecting_a_missing_table_returns_nothing_rather_than_raising(
        self, db_url: str
    ) -> None:
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)
        sink.table = "never_created"

        assert sink._existing_columns() == set()


class TestPrune:
    """Retention, which is what makes a full-fleet table affordable at all."""

    @pytest.fixture
    def seeded(self, db_url: str) -> Iterator[tuple]:
        """A positions table holding civil and military rows of several ages."""
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)
        now = datetime.now(UTC)
        rows = [
            ("civil_fresh", False, 1),
            ("civil_old", False, 200),
            ("civil_null_fresh", None, 1),
            ("civil_null_old", None, 200),
            ("mil_fresh", True, 1),
            ("mil_middle", True, 200),
            ("mil_ancient", True, 800),
        ]
        with sink.db_engine.connect() as conn:
            for i, (key, mil, age_hours) in enumerate(rows):
                conn.execute(
                    text(
                        f"INSERT INTO {POSITIONS_TABLE} "
                        f"(hex, mil, scraped_at, scrape_task_key) "
                        f"VALUES (:hex, :mil, :ts, :key)"
                    ),
                    {
                        "hex": f"{i:06x}",
                        "mil": mil,
                        "ts": now - timedelta(hours=age_hours),
                        "key": key,
                    },
                )
            conn.commit()
        yield sink.db_engine, POSITIONS_TABLE
        sink.db_engine.dispose()

    @staticmethod
    def _survivors(engine, table: str) -> set[str]:
        with engine.connect() as conn:
            return {r[0] for r in conn.execute(text(f"SELECT scrape_task_key FROM {table}"))}

    def test_civil_expires_sooner_than_military(self, seeded: tuple) -> None:
        engine, table = seeded

        deleted = prune_positions(engine, table, civil_hours=168, military_hours=720)

        assert deleted == 3
        assert self._survivors(engine, table) == {
            "civil_fresh",
            "civil_null_fresh",
            "mil_fresh",
            "mil_middle",
        }

    def test_a_null_military_flag_is_treated_as_civil(self, seeded: tuple) -> None:
        """`mil` is nullable; without the IS NULL arm those rows never expire."""
        engine, table = seeded

        prune_positions(engine, table, civil_hours=168, military_hours=720)

        assert "civil_null_old" not in self._survivors(engine, table)

    def test_a_zero_horizon_disables_that_half(self, seeded: tuple) -> None:
        engine, table = seeded

        deleted = prune_positions(engine, table, civil_hours=0, military_hours=720)

        assert deleted == 1
        assert "civil_old" in self._survivors(engine, table)
        assert "mil_ancient" not in self._survivors(engine, table)

    def test_nothing_expired_deletes_nothing(self, seeded: tuple) -> None:
        engine, table = seeded

        assert prune_positions(engine, table, civil_hours=10_000, military_hours=10_000) == 0

    def test_more_rows_than_one_batch_are_all_removed(self, db_url: str) -> None:
        """The batching must loop, not stop after the first `LIMIT`."""
        sink = ADSBxMapSink(db_url, table=POSITIONS_TABLE)
        stale = datetime.now(UTC) - timedelta(days=30)
        count = _PRUNE_BATCH + 25
        with sink.db_engine.connect() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {POSITIONS_TABLE} (hex, mil, scraped_at) VALUES (:hex, :mil, :ts)"
                ),
                [{"hex": f"{i:06x}", "mil": False, "ts": stale} for i in range(count)],
            )
            conn.commit()

        deleted = prune_positions(sink.db_engine, POSITIONS_TABLE, civil_hours=1, military_hours=1)

        assert deleted == count
        with sink.db_engine.connect() as conn:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {POSITIONS_TABLE}")).scalar() == 0

    def test_a_missing_table_is_reported_not_raised(self, db_url: str) -> None:
        """The scheduler runs every cycle; a misconfigured target must not kill it."""
        engine = create_engine(db_url)

        assert prune_positions(engine, "never_created", civil_hours=1, military_hours=1) == 0

    @pytest.mark.parametrize("bad", ["a b", "t; DROP TABLE x", ""])
    def test_a_name_that_is_not_an_identifier_is_refused(self, db_url: str, bad: str) -> None:
        with pytest.raises(ValueError):
            prune_positions(create_engine(db_url), bad, civil_hours=1, military_hours=1)
