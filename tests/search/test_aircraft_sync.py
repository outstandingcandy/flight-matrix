"""Tests for the `aircraft_static_info` → OpenSearch sync.

Nothing dual-writes to the index; it is brought back in step by re-reading rows
whose `last_updated` is at or after a watermark the index itself holds. That
makes two things worth proving against a real (SQLite) database rather than a
mock: the watermark window actually selects the rows it should, and the query
survives being handed a list of registrations — the `IN` clause is expanded by
SQLAlchemy, and getting that wrong is a crash rather than a wrong answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.data.models import AircraftStaticInfo, Base
from src.search.aircraft_index import AircraftSearchIndex, build_document
from src.search.aircraft_sync import DEFAULT_OVERLAP, incremental_start, sync_aircraft_index
from tests.search.fake_opensearch import FakeOpenSearch

NOW = datetime(2026, 8, 17, 12, 0, 0)


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AircraftStaticInfo.__table__])
    yield engine
    engine.dispose()


@pytest.fixture
def index() -> AircraftSearchIndex:
    cluster_index = AircraftSearchIndex(FakeOpenSearch())
    cluster_index.ensure_index()
    return cluster_index


def _insert(engine: Engine, **columns: Any) -> None:
    """Insert one aircraft row, defaulting the columns the tests don't care about."""
    row: dict[str, Any] = {"last_updated": NOW, **columns}
    names = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    with engine.begin() as connection:
        connection.execute(
            text(f"INSERT INTO aircraft_static_info ({names}) VALUES ({placeholders})"), row
        )


class TestFullPass:
    def test_every_row_reaches_the_index(self, engine: Engine, index: AircraftSearchIndex) -> None:
        _insert(engine, registration="B-1234", operator="Air China")
        _insert(engine, registration="N703PA", operator="Pan Am")

        stats = sync_aircraft_index(engine, index)

        assert stats.indexed == 2
        assert sorted(index.client.documents) == ["B-1234", "N703PA"]
        assert index.client.documents["B-1234"]["operator"] == "Air China"

    def test_rows_are_split_into_bulk_batches(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        for i in range(5):
            _insert(engine, registration=f"B-{i}")

        stats = sync_aircraft_index(engine, index, batch_size=2)

        assert (stats.indexed, stats.batches) == (5, 3)

    def test_the_index_is_refreshed_once_at_the_end(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        """Per-batch refreshes would make a full pass over the fleet far slower
        for no benefit — nothing reads the index until the pass finishes."""
        for i in range(5):
            _insert(engine, registration=f"B-{i}")

        sync_aircraft_index(engine, index, batch_size=2)

        assert index.client.refreshes == [index.index]

    def test_an_empty_table_does_not_refresh(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        stats = sync_aircraft_index(engine, index)

        assert stats.indexed == 0
        assert index.client.refreshes == []

    def test_a_second_pass_overwrites_rather_than_duplicates(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        _insert(engine, registration="B-1234", operator="Air China")
        sync_aircraft_index(engine, index)

        with engine.begin() as connection:
            connection.execute(text("UPDATE aircraft_static_info SET operator = 'Air China Cargo'"))
        sync_aircraft_index(engine, index)

        assert list(index.client.documents) == ["B-1234"]
        assert index.client.documents["B-1234"]["operator"] == "Air China Cargo"


class TestIncremental:
    def test_only_rows_at_or_after_the_watermark_are_indexed(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        _insert(engine, registration="OLD", last_updated=NOW - timedelta(days=1))
        _insert(engine, registration="NEW", last_updated=NOW)

        stats = sync_aircraft_index(engine, index, since=NOW - timedelta(minutes=5))

        assert stats.indexed == 1
        assert list(index.client.documents) == ["NEW"]

    def test_the_watermark_reaches_back_before_the_newest_document(
        self, index: AircraftSearchIndex
    ) -> None:
        """The database clock and the index are not the same clock, and rows are
        written inside transactions that commit after their timestamp is taken.
        Without the overlap those rows would never be picked up."""
        index.index_documents([build_document({"registration": "B-1", "last_updated": NOW})])

        assert incremental_start(index) == NOW - DEFAULT_OVERLAP

    def test_an_empty_index_has_no_start_point(self, index: AircraftSearchIndex) -> None:
        """`--incremental` reads this as "run a full pass"."""
        assert incremental_start(index) is None

    def test_re_indexing_the_overlap_window_is_harmless(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        """Which is what makes a generous overlap free: same id, same body."""
        _insert(engine, registration="B-1234")
        sync_aircraft_index(engine, index)

        sync_aircraft_index(engine, index, since=incremental_start(index))

        assert index.document_count() == 1


class TestRegistrationRepair:
    def test_only_the_named_registrations_are_indexed(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        _insert(engine, registration="B-1234")
        _insert(engine, registration="N703PA")

        stats = sync_aircraft_index(engine, index, registrations=["B-1234"])

        assert stats.indexed == 1
        assert list(index.client.documents) == ["B-1234"]

    def test_several_registrations_expand_into_the_in_clause(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        _insert(engine, registration="B-1234")
        _insert(engine, registration="N703PA")
        _insert(engine, registration="G-ABCD")

        stats = sync_aircraft_index(engine, index, registrations=["B-1234", "G-ABCD"])

        assert stats.indexed == 2

    def test_an_empty_list_means_no_work_rather_than_everything(
        self, engine: Engine, index: AircraftSearchIndex
    ) -> None:
        """`IN ()` is a syntax error in most dialects, and "index the whole
        fleet" is the opposite of what the caller asked for."""
        _insert(engine, registration="B-1234")

        stats = sync_aircraft_index(engine, index, registrations=[])

        assert stats.indexed == 0
        assert index.client.documents == {}
