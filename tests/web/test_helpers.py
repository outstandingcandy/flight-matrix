"""Unit tests for :mod:`src.web.helpers`.

Same shape as :mod:`tests.web.test_time_helpers` — pure-function
coverage plus a fresh-import re-export sanity check so a future
cleanup dropping the ``web_app.py`` shim breaks loudly here rather
than silently in a dozen FastAPI handlers that still import from
``web_app``.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.web.helpers import (
    SPECIAL_ATTENTION_LEVELS,
    extract_livery_indicator,
    get_aircraft_type_name,
    table_exists,
)


class TestSpecialAttentionLevels:
    def test_contains_both_language_forms(self) -> None:
        """Data comes in both Chinese and English; the tuple must
        include both so the ``category=special`` filter matches
        either flavour."""
        assert "高" in SPECIAL_ATTENTION_LEVELS
        assert "极高" in SPECIAL_ATTENTION_LEVELS
        assert "high" in SPECIAL_ATTENTION_LEVELS
        assert "very high" in SPECIAL_ATTENTION_LEVELS


class TestTableExists:
    def test_missing_table_returns_false(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with Session(engine) as session:
            assert table_exists(session, "nonexistent") is False

    def test_existing_table_returns_true(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with Session(engine) as session:
            session.execute(text("CREATE TABLE foo (id INTEGER PRIMARY KEY)"))
            session.commit()
            assert table_exists(session, "foo") is True

    def test_bad_bind_returns_false_not_raise(self) -> None:
        """The helper's whole reason to exist is that the caller must
        not have to guard every call — a broken bind falls back to
        False, keeps the admin page rendering."""

        class BrokenSession:
            def get_bind(self):
                raise RuntimeError("simulated")

        assert table_exists(BrokenSession(), "anything") is False  # type: ignore[arg-type]


class TestGetAircraftTypeName:
    def test_known_code_returns_chinese_name(self) -> None:
        assert get_aircraft_type_name("B738") == "波音737-800"
        assert get_aircraft_type_name("A320") == "空客A320"

    def test_unknown_code_returns_code_unchanged(self) -> None:
        # Contract: falls back to the raw code so callers can just
        # dispatch on the return without a None check.
        assert get_aircraft_type_name("XYZ9") == "XYZ9"

    def test_empty_string_returns_empty_string(self) -> None:
        assert get_aircraft_type_name("") == ""


class TestExtractLiveryIndicator:
    def test_returns_none_for_empty(self) -> None:
        assert extract_livery_indicator("") is None
        assert extract_livery_indicator(None) is None  # type: ignore[arg-type]

    def test_livery_suffix_stripped(self) -> None:
        # "SkyTeam Livery" → "SkyTeam" — the Livery suffix is meta,
        # not part of the indicator's display.
        assert extract_livery_indicator("China Eastern (SkyTeam Livery)") == "SkyTeam"

    def test_alliance_kept_as_is(self) -> None:
        # "Alliance" isn't the suffix pattern the sub strips; keep the
        # full string.
        assert extract_livery_indicator("Delta (Star Alliance)") == "Star Alliance"

    def test_no_match_returns_none(self) -> None:
        assert extract_livery_indicator("Boring Airline") is None

    def test_empty_parens_returns_none(self) -> None:
        # A bare "( Livery )" strips to empty → None, not "".
        assert extract_livery_indicator("Airline ( Livery )") is None

    def test_retro_keyword_matches(self) -> None:
        assert extract_livery_indicator("Airline (Retro)") == "Retro"


# The ``web_app`` re-export sanity check was removed when the
# module was deleted. FastAPI handlers now ``from src.web.helpers
# import ...`` directly.
