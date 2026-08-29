"""Unit tests for :mod:`src.web.time_helpers`.

These are pure functions; a single module with straight ``assert``
statements is the whole coverage plan. The interesting cases are the
driver-portability ones — ``_to_iso`` / ``_to_datetime`` have to
handle both the ``datetime`` psycopg2 hands back and the ``str``
SQLite hands back for a raw-SQL timestamp column.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.web.time_helpers import (
    BEIJING_TZ,
    UTC,
    _to_datetime,
    _to_iso,
    convert_beijing_to_utc,
    convert_utc_to_beijing,
)

# ---------------------------------------------------------------------------
# _to_iso — sqlite string vs psycopg2 datetime


class TestToIso:
    def test_none_returns_none(self) -> None:
        assert _to_iso(None) is None

    def test_empty_string_returns_none(self) -> None:
        # Falsy-string branch: preserves the Flask-era "empty → None" contract.
        assert _to_iso("") is None

    def test_datetime_is_isoformatted(self) -> None:
        dt = datetime(2026, 8, 28, 12, 34, 56)
        assert _to_iso(dt) == "2026-08-28T12:34:56"

    def test_string_passes_through_unchanged(self) -> None:
        """SQLite hands back the stored string; ``_to_iso`` should not
        try to reparse it just to re-format it."""
        assert _to_iso("2026-08-28T12:34:56") == "2026-08-28T12:34:56"


# ---------------------------------------------------------------------------
# _to_datetime — the opposite direction


class TestToDatetime:
    def test_none_returns_none(self) -> None:
        assert _to_datetime(None) is None

    def test_datetime_passes_through(self) -> None:
        dt = datetime(2026, 8, 28, 12, 34, 56)
        assert _to_datetime(dt) is dt

    def test_valid_iso_string_parses(self) -> None:
        got = _to_datetime("2026-08-28T12:34:56")
        assert got == datetime(2026, 8, 28, 12, 34, 56)

    def test_unparseable_string_returns_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """Junk in a raw-SQL cell must not crash the caller — the whole
        point of the helper is that it never raises."""
        with caplog.at_level("WARNING"):
            assert _to_datetime("not-a-timestamp") is None
        assert any("Unparseable timestamp" in rec.message for rec in caplog.records)

    def test_unexpected_type_returns_none(self) -> None:
        # An integer column mistakenly used as a timestamp shouldn't blow up.
        assert _to_datetime(12345) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# convert_utc_to_beijing — display formatter


class TestConvertUtcToBeijing:
    def test_none_returns_none(self) -> None:
        assert convert_utc_to_beijing(None) is None

    def test_naive_datetime_localised_to_utc_then_beijing(self) -> None:
        # 12:00 UTC = 20:00 Beijing (UTC+8, DST doesn't apply)
        got = convert_utc_to_beijing(datetime(2026, 8, 28, 12, 0, 0))
        assert got == "2026-08-28 20:00:00"

    def test_z_suffix_string_parses(self) -> None:
        assert convert_utc_to_beijing("2026-08-28T12:00:00Z") == "2026-08-28 20:00:00"

    def test_offset_suffix_string_parses(self) -> None:
        # +00:00 explicit offset.
        assert convert_utc_to_beijing("2026-08-28T12:00:00+00:00") == "2026-08-28 20:00:00"

    def test_utc_word_suffix_string_parses(self) -> None:
        # Some legacy code stamps values with a trailing " UTC" — this
        # branch is why the Flask handler has the substring check.
        assert convert_utc_to_beijing("2026-08-28T12:00:00 UTC") == "2026-08-28 20:00:00"

    def test_aware_utc_datetime(self) -> None:
        aware = UTC.localize(datetime(2026, 8, 28, 12, 0, 0))
        assert convert_utc_to_beijing(aware) == "2026-08-28 20:00:00"

    def test_garbage_input_falls_back_to_str(self) -> None:
        """A parse error MUST NOT propagate — the display path just
        renders whatever it got so the frontend has something."""
        got = convert_utc_to_beijing("garbage")
        assert got == "garbage"


# ---------------------------------------------------------------------------
# convert_beijing_to_utc — SQL-window helper


class TestConvertBeijingToUtc:
    def test_none_returns_none(self) -> None:
        assert convert_beijing_to_utc(None) is None

    def test_beijing_string_returns_naive_utc(self) -> None:
        # 20:00 Beijing = 12:00 UTC. DB stores naive UTC, so the
        # returned value MUST NOT carry a tzinfo — the SQL layer would
        # otherwise trip "can't compare offset-naive and offset-aware".
        got = convert_beijing_to_utc("2026-08-28T20:00:00")
        assert got == datetime(2026, 8, 28, 12, 0, 0)
        assert got is not None
        assert got.tzinfo is None

    def test_already_aware_beijing_string(self) -> None:
        """Explicit +08:00 offset — accept and normalise."""
        got = convert_beijing_to_utc("2026-08-28T20:00:00+08:00")
        assert got == datetime(2026, 8, 28, 12, 0, 0)

    def test_garbage_returns_none(self) -> None:
        # Falls through to the last-ditch fromisoformat which also
        # rejects → None. Silent on garbage, same as the Flask-era
        # handler.
        assert convert_beijing_to_utc("not-a-timestamp") is None


# ---------------------------------------------------------------------------
# Web_app re-export sanity check.
#
# ``web_app.py`` still exposes these names for the Flask half. If the
# re-export ever regresses (e.g. someone drops the import line during a
# cleanup), a dozen FastAPI handlers currently ``from web_app import
# _to_iso`` and would fail. Guard.


# The ``web_app`` re-export sanity check was removed when the module
# was deleted. FastAPI handlers ``from src.web.time_helpers import
# ...`` directly now.
