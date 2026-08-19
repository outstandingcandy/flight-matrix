"""Tests for the `aircraft_images` metadata columns and their migration.

`JetPhotosSink` writes 23 columns. `AircraftImage` declared 18 of them: `camera`,
`views`, `likes`, `badges` and `html_s3_path` were missing, so any database built
from the model — which is what a fresh deploy does — rejected every insert. The
sink's own test hid this by hand-writing a CREATE TABLE that had all 23.

The first test is the one that would have caught it: compare the model's columns
against the statement that uses them, instead of against a fixture's idea of the
schema.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from scripts.migrate_add_image_metadata_columns import NEW_COLUMNS, run_migration
from src.data.models import AircraftImage

_SINK_FILE = Path(__file__).resolve().parent.parent.parent / "src/scraper/sinks/jetphotos_sink.py"


def _columns_the_sink_writes() -> list[str]:
    """Return the column list from the sink's INSERT INTO aircraft_images."""
    match = re.search(r"INSERT INTO aircraft_images \((.*?)\) VALUES", _SINK_FILE.read_text(), re.S)
    assert match, "the sink no longer has a recognisable INSERT INTO aircraft_images"
    return [
        column.strip() for column in match.group(1).replace("\n", " ").split(",") if column.strip()
    ]


class TestModelMatchesTheSink:
    def test_the_model_declares_every_column_the_sink_writes(self) -> None:
        declared = set(AircraftImage.__table__.columns.keys())
        missing = [column for column in _columns_the_sink_writes() if column not in declared]
        assert missing == [], (
            f"JetPhotosSink writes columns AircraftImage does not declare: {missing}. "
            "A database built by create_all() will reject every image insert."
        )

    def test_the_id_is_auto_assigned_on_sqlite(self) -> None:
        # Only `INTEGER PRIMARY KEY` aliases SQLite's rowid; a BIGINT primary key
        # is not auto-assigned, and the sink's INSERT omits `id`.
        engine = create_engine("sqlite://")
        AircraftImage.__table__.create(engine)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO aircraft_images (registration, image_path) "
                    "VALUES ('B-1234', 'x.jpg')"
                )
            )
            conn.commit()
            assert conn.execute(text("SELECT id FROM aircraft_images")).scalar() is not None


class TestMigration:
    def _legacy_table(self, url: str) -> None:
        """Create `aircraft_images` as it looked before the five columns."""
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE aircraft_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registration VARCHAR(20) NOT NULL,
                    image_path VARCHAR(500) NOT NULL,
                    jetphotos_id VARCHAR(20)
                )
                """)
            )
            conn.execute(
                text(
                    "INSERT INTO aircraft_images (registration, image_path, jetphotos_id) "
                    "VALUES ('B-1234', 'x.jpg', '999')"
                )
            )
            conn.commit()
        engine.dispose()

    def test_it_adds_the_missing_columns(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'legacy.db'}"
        self._legacy_table(url)

        assert run_migration(url) is True

        columns = {c["name"] for c in inspect(create_engine(url)).get_columns("aircraft_images")}
        assert set(NEW_COLUMNS) <= columns

    def test_it_keeps_the_existing_rows(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'legacy.db'}"
        self._legacy_table(url)

        assert run_migration(url) is True

        engine = create_engine(url)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT registration, image_path, camera FROM aircraft_images")
            ).one()
        assert row.registration == "B-1234"
        assert row.image_path == "x.jpg"
        assert row.camera is None

    def test_it_is_safe_to_run_twice(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'legacy.db'}"
        self._legacy_table(url)

        assert run_migration(url) is True
        # SQLite has no ADD COLUMN IF NOT EXISTS, so a second pass would raise
        # unless the script checks the schema first.
        assert run_migration(url) is True

    def test_a_dry_run_changes_nothing(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'legacy.db'}"
        self._legacy_table(url)

        assert run_migration(url, dry_run=True) is True

        columns = {c["name"] for c in inspect(create_engine(url)).get_columns("aircraft_images")}
        assert not (set(NEW_COLUMNS) & columns)

    def test_it_reports_a_missing_table_instead_of_creating_one(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'empty.db'}"
        create_engine(url).connect().close()

        assert run_migration(url) is False

    def test_a_model_built_table_needs_no_migration(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'fresh.db'}"
        engine = create_engine(url)
        AircraftImage.__table__.create(engine)
        engine.dispose()

        # Nothing to add: the model is now the single source of truth, and the
        # migration exists only for databases created before it was.
        assert run_migration(url) is True
