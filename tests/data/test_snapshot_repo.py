"""Tests for SnapshotRepository helper methods."""

from __future__ import annotations

import pytest

from src.data.snapshot_repo import SnapshotRepository


class TestSafeAltitude:
    def test_none(self) -> None:
        assert SnapshotRepository._safe_altitude(None) is None

    def test_ground(self) -> None:
        assert SnapshotRepository._safe_altitude("ground") == 0
        assert SnapshotRepository._safe_altitude("GROUND") == 0

    def test_numeric_string(self) -> None:
        assert SnapshotRepository._safe_altitude("12500") == 12500
        assert SnapshotRepository._safe_altitude("12500.7") == 12500

    def test_invalid_string(self) -> None:
        assert SnapshotRepository._safe_altitude("n/a") is None

    def test_int(self) -> None:
        assert SnapshotRepository._safe_altitude(32000) == 32000

    def test_float(self) -> None:
        assert SnapshotRepository._safe_altitude(32000.5) == 32000


class TestSafeString:
    def test_none(self) -> None:
        assert SnapshotRepository._safe_string(None, 10) is None

    def test_empty_string(self) -> None:
        assert SnapshotRepository._safe_string("", 10) is None
        assert SnapshotRepository._safe_string("   ", 10) is None

    def test_truncation(self) -> None:
        assert SnapshotRepository._safe_string("abcdefghij", 5) == "abcde"

    def test_trim_whitespace(self) -> None:
        assert SnapshotRepository._safe_string("  ABC  ", 10) == "ABC"

    def test_non_string_coerced(self) -> None:
        assert SnapshotRepository._safe_string(123, 10) == "123"


class TestConvertBooleanSyntax:
    def test_military_equals_1(self) -> None:
        out = SnapshotRepository._convert_boolean_syntax("is_military = 1")
        assert out == "is_military = true"

    def test_interesting_equals_0(self) -> None:
        out = SnapshotRepository._convert_boolean_syntax("is_interesting = 0")
        assert out == "is_interesting = false"

    def test_case_insensitive(self) -> None:
        # Match is case-insensitive; the substitution lowercases the field name
        # (the replacement string is the lowercase `field` from the loop).
        out = SnapshotRepository._convert_boolean_syntax("IS_MILITARY = 1 AND altitude > 100")
        assert "is_military = true" in out
        assert "altitude > 100" in out

    def test_only_boolean_fields_touched(self) -> None:
        # altitude = 1 should stay as altitude = 1 (not a boolean field).
        out = SnapshotRepository._convert_boolean_syntax("altitude = 1")
        assert out == "altitude = 1"

    def test_no_match_returns_input_unchanged(self) -> None:
        clause = "flight_number LIKE 'VIP%'"
        assert SnapshotRepository._convert_boolean_syntax(clause) == clause


class TestParseJson:
    def test_none_on_non_string(self) -> None:
        assert SnapshotRepository._parse_json(None) is None
        assert SnapshotRepository._parse_json(123) is None
        assert SnapshotRepository._parse_json({"a": 1}) is None  # already a dict

    def test_valid_json(self) -> None:
        assert SnapshotRepository._parse_json('{"a": 1}') == {"a": 1}

    def test_invalid_json(self) -> None:
        assert SnapshotRepository._parse_json("{not json") is None


class TestCleanupOldData:
    def test_cleanup_empty_table_is_noop(self, snapshot_repo: SnapshotRepository) -> None:
        # Runs without raising even when no rows match.
        snapshot_repo.cleanup_old_data(hours_to_keep=1)


class TestGetStatistics:
    def test_shape(self, snapshot_repo: SnapshotRepository) -> None:
        stats = snapshot_repo.get_statistics("sqlite:///:memory:")
        assert set(stats.keys()) == {
            "total_snapshots",
            "recent_snapshots_1h",
            "unique_aircraft_total",
            "military_aircraft_total",
            "database_url",
        }
        assert stats["total_snapshots"] == 0
