"""Tests for `/api/aircraft/search`, whose filters used to be interpolated.

The route has no `@login_required`, and it built its WHERE clause with
f-strings straight from the query string:

    conditions.append(f"registration LIKE '%{registration}%'")

so `?registration=N12345' OR registration LIKE '` closed the literal and
appended a disjunction, returning every row in `aircraft_snapshots` to an
anonymous caller. These tests seed two distinguishable aircraft and assert both
halves of the contract: ordinary searches still filter the way they used to,
and a payload that used to widen the clause now matches nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

# The aircraft a caller is allowed to find, and one it must not reach by
# breaking out of the LIKE pattern.
VISIBLE = "N12345"
HIDDEN = "SECRET1"


def _seed(client: Any) -> None:
    """Insert one snapshot each for a searchable and a non-matching aircraft."""
    db_manager = client.application_module.db_manager
    now = datetime.now(UTC).replace(tzinfo=None)

    session = db_manager.get_session()
    try:
        rows = ((1, "abc123", VISIBLE, "B738"), (2, "def456", HIDDEN, "A320"))
        for row_id, hex_code, registration, aircraft_type in rows:
            session.execute(
                text("""
                INSERT INTO aircraft_snapshots
                    (id, hex, registration, aircraft_type, flight_number,
                     is_military, snapshot_time)
                VALUES (:row_id, :hex, :registration, :aircraft_type, 'AA100',
                        :is_military, :snapshot_time)
                """),
                {
                    "row_id": row_id,
                    "hex": hex_code,
                    "registration": registration,
                    "aircraft_type": aircraft_type,
                    # Only the visible aircraft is military, so the is_military
                    # filter is also distinguishing rather than a no-op.
                    "is_military": registration == VISIBLE,
                    "snapshot_time": now - timedelta(minutes=row_id),
                },
            )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def seeded_client(app_client: Any) -> Any:
    _seed(app_client)
    return app_client


def _registrations(client: Any, query: str) -> list[str]:
    """Return the registrations `/api/aircraft/search?<query>` hands back."""
    response = client.get(f"/api/aircraft/search?{query}")
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    payload = response.get_json()
    assert payload["success"] is True
    return [row.get("r") for row in payload["data"]]


class TestOrdinarySearches:
    """The filters have to keep behaving exactly as they did."""

    def test_registration_matches_a_substring(self, seeded_client: Any) -> None:
        assert _registrations(seeded_client, "registration=1234") == [VISIBLE]

    def test_hex_matches_exactly(self, seeded_client: Any) -> None:
        assert _registrations(seeded_client, "hex=abc123") == [VISIBLE]
        assert _registrations(seeded_client, "hex=abc") == []

    def test_aircraft_type_matches_a_substring(self, seeded_client: Any) -> None:
        assert _registrations(seeded_client, "aircraft_type=A32") == [HIDDEN]

    def test_is_military_still_filters(self, seeded_client: Any) -> None:
        # Pinned because is_military is deliberately left as a literal: the
        # repository rewrites `= 1` to `= true` for Postgres by pattern-matching
        # the clause text, which a bound parameter would defeat.
        assert _registrations(seeded_client, "is_military=true") == [VISIBLE]
        assert _registrations(seeded_client, "is_military=false") == [HIDDEN]

    def test_no_filters_returns_both(self, seeded_client: Any) -> None:
        assert sorted(_registrations(seeded_client, "limit=10")) == [VISIBLE, HIDDEN]


class TestInjection:
    @pytest.mark.parametrize(
        "payload",
        [
            # Closes the LIKE literal and ORs in a match-everything term. This is
            # the one that worked: it returned both rows before the fix.
            "N12345' OR registration LIKE '",
            "' OR '1'='1",
            "%' OR 1=1 --",
            # Tries to break out of the enclosing parenthesised WHERE.
            "x') OR (registration IS NOT NULL",
        ],
    )
    def test_registration_payloads_cannot_widen_the_clause(
        self, seeded_client: Any, payload: str
    ) -> None:
        found = _registrations(seeded_client, f"registration={payload}")
        assert HIDDEN not in found, f"{payload!r} reached a row it must not match"
        # Treated as a literal pattern, none of these match either aircraft.
        assert found == []

    @pytest.mark.parametrize("param", ["hex", "aircraft_type"])
    def test_the_other_string_filters_are_bound_too(self, seeded_client: Any, param: str) -> None:
        found = _registrations(seeded_client, f"{param}=' OR '1'='1")
        assert found == []

    def test_a_statement_separator_does_not_drop_the_table(self, seeded_client: Any) -> None:
        # The driver refuses multiple statements per execute, so before the fix
        # this raised rather than dropping anything -- but a 500 on a payload is
        # still a payload reaching the parser. Now it is just a pattern.
        found = _registrations(seeded_client, "registration=x'; DROP TABLE aircraft_snapshots; --")
        assert found == []
        # The table is still there and still populated.
        assert sorted(_registrations(seeded_client, "limit=10")) == [VISIBLE, HIDDEN]

    def test_a_union_cannot_read_another_table(self, seeded_client: Any) -> None:
        found = _registrations(
            seeded_client,
            "registration=x' UNION SELECT registration FROM aircraft_static_info --",
        )
        assert found == []

    def test_wildcards_still_reach_the_pattern(self, seeded_client: Any) -> None:
        # Binding must not escape LIKE metacharacters: `%` and `_` are the
        # search feature, and callers rely on them.
        assert sorted(_registrations(seeded_client, "registration=%")) == [
            VISIBLE,
            HIDDEN,
        ]


class TestRepositoryPlumbing:
    def test_execute_filter_query_binds_the_params_it_is_given(self, seeded_client: Any) -> None:
        db_manager = seeded_client.application_module.db_manager

        rows = db_manager.execute_filter_query("registration = :reg", 10, {"reg": VISIBLE})
        assert [row["r"] for row in rows] == [VISIBLE]

    def test_a_caller_cannot_override_the_row_cap(self, seeded_client: Any) -> None:
        # `limit_count` is bound last, so a params dict carrying that key cannot
        # raise the limit the caller was given.
        db_manager = seeded_client.application_module.db_manager

        rows = db_manager.execute_filter_query("registration IS NOT NULL", 1, {"limit_count": 100})
        assert len(rows) == 1
