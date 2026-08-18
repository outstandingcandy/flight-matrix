"""Tests for `src.data.dialect`.

The SQLite branches are *executed*, not just string-matched: the bugs these
helpers exist to prevent are all cases where SQL parsed fine and returned the
wrong thing, which a textual assertion cannot catch. The PostgreSQL branches are
asserted textually — they reproduce SQL that is already running in production,
so the check that matters is that it did not change.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from src.data.dialect import beijing_date, day_of, latest_rows, minutes_ago


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A SQLite engine with two aircraft, three snapshots between them."""
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE snaps (
                id INTEGER PRIMARY KEY,
                hex TEXT,
                registration TEXT,
                snapshot_time TEXT
            )
            """)
        )
        conn.execute(
            text("INSERT INTO snaps VALUES (:i, :h, :r, :t)"),
            [
                {"i": 1, "h": "abc", "r": "OLD-A", "t": "2026-01-01 10:00:00"},
                {"i": 2, "h": "abc", "r": "NEW-A", "t": "2026-01-02 10:00:00"},
                {"i": 3, "h": "def", "r": "ONLY-D", "t": "2026-01-01 09:00:00"},
            ],
        )
    try:
        yield eng
    finally:
        eng.dispose()


class TestLatestRows:
    def test_postgres_branch_uses_distinct_on(self) -> None:
        """The tuned production form, unchanged."""
        sql = latest_rows(
            columns="hex, registration",
            source="snaps",
            partition_by="hex",
            order_by="snapshot_time DESC",
            is_postgres=True,
        )
        assert "DISTINCT ON (hex)" in sql
        assert "ROW_NUMBER" not in sql
        assert "ORDER BY hex, snapshot_time DESC" in sql

    def test_sqlite_branch_uses_row_number(self) -> None:
        sql = latest_rows(
            columns="hex, registration",
            source="snaps",
            partition_by="hex",
            order_by="snapshot_time DESC",
            is_postgres=False,
        )
        assert "DISTINCT ON" not in sql
        assert "ROW_NUMBER() OVER" in sql

    def test_sqlite_branch_returns_one_newest_row_per_group(self, engine: Engine) -> None:
        """The behaviour `DISTINCT ON` gives, verified by execution."""
        sql = latest_rows(
            columns="hex, registration",
            source="snaps",
            partition_by="hex",
            order_by="snapshot_time DESC",
            is_postgres=False,
        )
        with engine.connect() as conn:
            rows = conn.execute(text(f"SELECT hex, registration FROM ({sql}) ORDER BY hex")).all()

        assert [tuple(r) for r in rows] == [("abc", "NEW-A"), ("def", "ONLY-D")]

    def test_sqlite_branch_honours_where(self, engine: Engine) -> None:
        sql = latest_rows(
            columns="hex, registration",
            source="snaps",
            partition_by="hex",
            order_by="snapshot_time DESC",
            where="hex = 'abc'",
            is_postgres=False,
        )
        with engine.connect() as conn:
            rows = conn.execute(text(f"SELECT hex FROM ({sql})")).all()

        assert [r[0] for r in rows] == ["abc"]

    def test_sqlite_branch_deduplicates_ties(self, engine: Engine) -> None:
        """A tie on `order_by` must still yield exactly one row.

        This is where the `INNER JOIN (SELECT MAX(ts) GROUP BY k)` idiom used
        elsewhere in web_app.py diverges from `DISTINCT ON`: it returns both
        tied rows. `ROW_NUMBER() = 1` returns one, matching PostgreSQL.
        """
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO snaps VALUES (4, 'abc', 'TIED', '2026-01-02 10:00:00')"))

        sql = latest_rows(
            columns="hex, registration",
            source="snaps",
            partition_by="hex",
            order_by="snapshot_time DESC",
            is_postgres=False,
        )
        with engine.connect() as conn:
            rows = conn.execute(text(f"SELECT hex FROM ({sql}) WHERE hex = 'abc'")).all()

        assert len(rows) == 1

    def test_sqlite_branch_supports_multi_column_partition(self, engine: Engine) -> None:
        sql = latest_rows(
            columns="hex, registration, snapshot_time",
            source="snaps",
            partition_by=f"hex, {day_of('snapshot_time', is_postgres=False)}",
            order_by="snapshot_time DESC",
            is_postgres=False,
        )
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT registration FROM ({sql}) ORDER BY registration")
            ).all()

        # abc has one row on each of two days; def has one row.
        assert [r[0] for r in rows] == ["NEW-A", "OLD-A", "ONLY-D"]


class TestDayOf:
    def test_postgres_branch(self) -> None:
        assert day_of("fs.scheduled_time", is_postgres=True) == "(fs.scheduled_time)::date"

    def test_sqlite_branch(self) -> None:
        assert day_of("fs.scheduled_time", is_postgres=False) == "DATE(fs.scheduled_time)"

    def test_sqlite_branch_returns_a_date_not_a_year(self, engine: Engine) -> None:
        """Regression guard for the trap this helper exists to avoid.

        `CAST(x AS DATE)` is accepted by SQLite and returns `2026` — an integer
        year. It produces wrong `GROUP BY` buckets and wrong comparisons with no
        error anywhere.
        """
        expr = day_of("snapshot_time", is_postgres=False)
        with engine.connect() as conn:
            value = conn.execute(text(f"SELECT {expr} FROM snaps WHERE id = 2")).scalar()
            trap = conn.execute(
                text("SELECT CAST(snapshot_time AS DATE) FROM snaps WHERE id = 2")
            ).scalar()

        assert value == "2026-01-02"
        assert trap == 2026, "if this changed, the docstring warning needs revisiting"

    def test_sqlite_branch_groups_by_calendar_day(self, engine: Engine) -> None:
        expr = day_of("snapshot_time", is_postgres=False)
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT {expr} AS d, COUNT(*) FROM snaps GROUP BY {expr} ORDER BY d")
            ).all()

        assert [tuple(r) for r in rows] == [("2026-01-01", 2), ("2026-01-02", 1)]


class TestBeijingDate:
    def test_postgres_branch_keeps_the_timezone_conversion(self) -> None:
        sql = beijing_date("fs.scheduled_time", is_postgres=True)
        assert "AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Shanghai'" in sql
        assert "TO_CHAR" in sql

    def test_sqlite_branch_shifts_by_eight_hours(self, engine: Engine) -> None:
        """A UTC evening timestamp belongs to the next Beijing day."""
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO snaps VALUES (9, 'zzz', 'EVENING', '2026-01-01 20:30:00')")
            )

        expr = beijing_date("snapshot_time", is_postgres=False)
        with engine.connect() as conn:
            value = conn.execute(text(f"SELECT {expr} FROM snaps WHERE id = 9")).scalar()

        assert value == "2026-01-02"

    def test_sqlite_branch_keeps_a_morning_timestamp_on_the_same_day(self, engine: Engine) -> None:
        expr = beijing_date("snapshot_time", is_postgres=False)
        with engine.connect() as conn:
            value = conn.execute(text(f"SELECT {expr} FROM snaps WHERE id = 2")).scalar()

        assert value == "2026-01-02"


class TestMinutesAgo:
    def test_postgres_branch(self) -> None:
        assert minutes_ago(15, is_postgres=True) == "NOW() - INTERVAL '15 minutes'"

    def test_sqlite_branch(self) -> None:
        assert minutes_ago(15, is_postgres=False) == "datetime('now', '-15 minutes')"

    def test_sqlite_branch_is_a_usable_timestamp(self, engine: Engine) -> None:
        expr = minutes_ago(30, is_postgres=False)
        with engine.connect() as conn:
            past, future = conn.execute(
                text(f"SELECT {expr} < CURRENT_TIMESTAMP, {expr} > CURRENT_TIMESTAMP")
            ).one()

        assert past == 1
        assert future == 0

    def test_non_int_input_is_coerced_not_interpolated(self) -> None:
        """The value lands in SQL text, so it must not survive as a string."""
        assert minutes_ago("15", is_postgres=False) == "datetime('now', '-15 minutes')"  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            minutes_ago("15 minutes'; DROP TABLE snaps --", is_postgres=False)  # type: ignore[arg-type]
