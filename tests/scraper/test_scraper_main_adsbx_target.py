"""Tests for how `scraper.scrapers.adsbx_map.target` routes the ADSBx pipeline.

`target` decides two things that live in different functions and are easy to
desynchronise: which sink (and therefore which table) receives the rows, and
whether the scraper's own military filter runs. Getting the second wrong is the
expensive mistake — with `military_only` left on, a full-fleet target writes only
`dbFlags & 1` hits and silently discards about 97% of every region, after the
rows have already crossed the CDP bridge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.scraper_main import _build_scraper_configs, _build_sinks_and_augment_configs


def _configs(db_url: str, **adsbx: Any) -> dict[str, tuple[type, dict[str, Any]]]:
    return _build_scraper_configs(
        {"scraper": {"scrapers": {"adsbx_map": {"enabled": True, **adsbx}}}},
        database_url=db_url,
        local_mode=False,
        no_db=False,
        max_notes=None,
        max_comments=None,
        max_replies=None,
    )


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'targets.db'}"


class TestMilitaryFilterDefault:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [("military", True), (None, True), ("positions", False), ("snapshots", False)],
    )
    def test_a_full_fleet_target_turns_the_filter_off(
        self, db_url: str, target: str | None, expected: bool
    ) -> None:
        adsbx: dict[str, Any] = {} if target is None else {"target": target}

        cfg = _configs(db_url, **adsbx)["adsbx_map"][1]

        assert cfg["military_only"] is expected

    def test_an_explicit_setting_still_wins(self, db_url: str) -> None:
        """The default is a convenience, not a lock — a positions run that wants
        only military rows must still be expressible."""
        cfg = _configs(db_url, target="positions", military_only=True)["adsbx_map"][1]

        assert cfg["military_only"] is True

    def test_the_target_is_normalised_for_the_sink_to_read(self, db_url: str) -> None:
        cfg = _configs(db_url, target="Positions")["adsbx_map"][1]

        assert cfg["target"] == "positions"
        assert cfg["military_only"] is False


class TestSinkSelection:
    def test_the_positions_target_writes_the_full_fleet_table(self, db_url: str) -> None:
        from src.scraper.sinks.adsbx_map_sink import POSITIONS_TABLE, ADSBxMapSink

        sinks = _build_sinks_and_augment_configs(_configs(db_url, target="positions"), db_url)

        sink = sinks["adsbx_map"]
        assert isinstance(sink, ADSBxMapSink)
        assert sink.table == POSITIONS_TABLE

    def test_the_default_target_keeps_the_military_table(self, db_url: str) -> None:
        from src.scraper.sinks.adsbx_map_sink import MILITARY_TABLE, ADSBxMapSink

        sinks = _build_sinks_and_augment_configs(_configs(db_url), db_url)

        sink = sinks["adsbx_map"]
        assert isinstance(sink, ADSBxMapSink)
        assert sink.table == MILITARY_TABLE

    def test_the_snapshots_target_uses_the_other_sink_entirely(self, db_url: str) -> None:
        """`aircraft_snapshots` is a different schema, not a different table name:
        it stores the whole source row again as JSON and bootstraps an
        `aircraft_static_info` row per unseen registration."""
        from src.scraper.sinks.adsbx_snapshots_sink import ADSBxSnapshotsSink

        sinks = _build_sinks_and_augment_configs(_configs(db_url, target="snapshots"), db_url)

        assert isinstance(sinks["adsbx_map"], ADSBxSnapshotsSink)

    def test_no_database_url_builds_no_sinks(self) -> None:
        assert _build_sinks_and_augment_configs(_configs(""), "") == {}
