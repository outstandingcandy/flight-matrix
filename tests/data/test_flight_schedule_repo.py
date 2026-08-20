"""Tests for the shared `flight_schedules` writes.

Two things are worth testing here and both are about agreement between pieces
that can silently drift apart:

- The upsert's ON CONFLICT target has to match a real UNIQUE index. Production's
  is on `(fr24_flight_id, date(scheduled_time), flight_type)`; the model used to
  declare a different one, so this statement worked on Aurora and failed on every
  SQLite database. Re-scraping a board is the normal case, so "twice is still one
  row" is the behaviour the whole pipeline rests on.
- `airport_icao` is `String(4)`. The old code wrote the scraped code into it
  whatever its length, so a board scraped as `PEK` filled the ICAO column with a
  3-letter IATA code and every ICAO-keyed query missed.

`FlightData` — the scraper submodule's own model — stands in for a scraped row
rather than a local stub, so the `ScrapedFlight` protocol is checked against the
type that actually reaches it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from resilient_scraper.scrapers.aviation.fr24_aircraft.models import FlightData
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from src.data.flight_schedule_repo import (
    build_row,
    registrations_of,
    resolve_airport_codes,
    seed_registrations,
    upsert_flight_schedules,
)
from src.data.models import AircraftStaticInfo, Airport, FlightSchedule

NOW = datetime(2026, 8, 20, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[Connection]:
    """A SQLite connection with the three tables these writes touch.

    Built from the models, not from hand-written DDL: the indexes are half of
    what is under test, and a hand-written `CREATE TABLE` is exactly how the
    mismatch this module exists to fix stayed hidden.
    """
    engine = create_engine("sqlite://")
    for model in (FlightSchedule, Airport, AircraftStaticInfo):
        model.__table__.create(engine)
    with engine.connect() as connection:
        connection.execute(
            text(
                "INSERT INTO airports (icao_code, iata_code, name, latitude, longitude) "
                "VALUES ('ZBAA', 'PEK', 'Beijing Capital International Airport', 40.08, 116.58)"
            )
        )
        connection.commit()
        yield connection


def a_flight(**overrides: object) -> FlightData:
    """A scraped arrival with every field the upsert reads populated."""
    fields: dict[str, object] = {
        "flight_type": "arrival",
        "flight_number": "CA1234",
        "callsign": "CCA1234",
        "airline_name": "Air China",
        "airline_iata": "CA",
        "remote_airport_iata": "SHA",
        "remote_airport_name": "Shanghai Hongqiao",
        "aircraft_type": "B738",
        "aircraft_registration": "B-5678",
        "scheduled_time": datetime(2026, 8, 20, 9, 30, 0),
        "status": "Scheduled",
        "gate": "G1",
        "flight_id": "3c4d5e6f",
    }
    fields.update(overrides)
    return FlightData(**fields)  # type: ignore[arg-type]


def store(conn: Connection, *flights: FlightData, airport: str = "PEK") -> int:
    """Resolve the airport, build the rows and upsert them, as the sink does."""
    airport_iata, airport_icao = resolve_airport_codes(conn, airport)
    rows = [
        row
        for flight in flights
        if (
            row := build_row(
                flight,
                airport_iata=airport_iata,
                airport_icao=airport_icao,
                flight_type_hint="arrival",
                scraped_at=NOW,
            )
        )
        is not None
    ]
    written = upsert_flight_schedules(conn, rows)
    conn.commit()
    return written


class TestUpsertIsIdempotent:
    def test_rescraping_the_same_flight_updates_the_row_instead_of_adding_one(
        self, conn: Connection
    ) -> None:
        store(conn, a_flight())
        store(
            conn,
            a_flight(status="Landed 09:41", gate="G7", actual_time=datetime(2026, 8, 20, 9, 41)),
        )

        rows = conn.execute(text("SELECT status, gate FROM flight_schedules")).fetchall()
        assert rows == [("Landed 09:41", "G7")]

    def test_a_later_scheduled_time_on_the_same_day_stays_one_row(self, conn: Connection) -> None:
        """A delay moves `scheduled_time`, which is part of the conflict target
        only as its date — otherwise every re-scrape of a delayed flight would
        insert another row."""
        store(conn, a_flight())
        store(conn, a_flight(scheduled_time=datetime(2026, 8, 20, 11, 15, 0)))

        times = conn.execute(text("SELECT scheduled_time FROM flight_schedules")).fetchall()
        assert len(times) == 1
        assert "11:15" in str(times[0][0])

    def test_the_arrival_and_the_departure_of_one_id_are_two_rows(self, conn: Connection) -> None:
        store(conn, a_flight())
        store(conn, a_flight(flight_type="departure"))

        assert conn.execute(text("SELECT COUNT(*) FROM flight_schedules")).scalar() == 2

    def test_the_same_flight_on_another_day_is_another_row(self, conn: Connection) -> None:
        store(conn, a_flight())
        store(conn, a_flight(scheduled_time=datetime(2026, 8, 21, 9, 30, 0)))

        assert conn.execute(text("SELECT COUNT(*) FROM flight_schedules")).scalar() == 2

    def test_a_board_carrying_one_flight_twice_writes_one_row(self, conn: Connection) -> None:
        """ "Load more" can repeat a row. Sent as one executemany, both dialects
        reject this ("cannot affect row a second time")."""
        store(conn, a_flight(), a_flight(gate="G9"))

        rows = conn.execute(text("SELECT gate FROM flight_schedules")).fetchall()
        assert rows == [("G9",)]


class TestAirportCodes:
    def test_a_three_letter_code_never_lands_in_the_icao_column(self, conn: Connection) -> None:
        store(conn, a_flight(), airport="PEK")

        iata, icao = conn.execute(
            text("SELECT airport_iata, airport_icao FROM flight_schedules")
        ).fetchone()
        assert (iata, icao) == ("PEK", "ZBAA")

    def test_a_four_letter_code_fills_both_columns_too(self, conn: Connection) -> None:
        store(conn, a_flight(), airport="ZBAA")

        assert conn.execute(
            text("SELECT airport_iata, airport_icao FROM flight_schedules")
        ).fetchone() == ("PEK", "ZBAA")

    def test_an_unknown_airport_leaves_the_other_half_null(self, conn: Connection) -> None:
        """Better a missing code than an invented one: `airport_icao` is what the
        photo and schedule queries key on."""
        assert resolve_airport_codes(conn, "XXX") == ("XXX", None)
        assert resolve_airport_codes(conn, "ZZZZ") == (None, "ZZZZ")

    def test_a_code_of_neither_length_resolves_to_nothing(self, conn: Connection) -> None:
        assert resolve_airport_codes(conn, "") == (None, None)


class TestRowsThatCannotBeStored:
    def test_a_flight_with_no_scheduled_time_is_dropped(self) -> None:
        assert (
            build_row(
                a_flight(scheduled_time=None),
                airport_iata="PEK",
                airport_icao="ZBAA",
                flight_type_hint="arrival",
                scraped_at=NOW,
            )
            is None
        )

    def test_a_flight_with_neither_id_nor_number_is_dropped(self) -> None:
        assert (
            build_row(
                a_flight(flight_id=None, flight_number=None),
                airport_iata="PEK",
                airport_icao="ZBAA",
                flight_type_hint="arrival",
                scraped_at=NOW,
            )
            is None
        )

    def test_the_flight_number_stands_in_for_a_missing_id(self) -> None:
        row = build_row(
            a_flight(flight_id=None),
            airport_iata="PEK",
            airport_icao="ZBAA",
            flight_type_hint="arrival",
            scraped_at=NOW,
        )
        assert row is not None
        assert row["fr24_flight_id"] == "CA1234"

    def test_the_hint_supplies_a_flight_type_the_page_did_not(self) -> None:
        row = build_row(
            a_flight(flight_type=None),
            airport_iata="PEK",
            airport_icao="ZBAA",
            flight_type_hint="departure",
            scraped_at=NOW,
        )
        assert row is not None
        assert row["flight_type"] == "departure"


class TestRegistrations:
    def test_placeholders_and_stubs_are_not_registrations(self) -> None:
        rows = [
            {"aircraft_registration": "b-5678"},
            {"aircraft_registration": "UNKNOWN"},
            {"aircraft_registration": "N/A"},
            {"aircraft_registration": "-"},
            {"aircraft_registration": None},
            {},
        ]
        assert registrations_of(rows) == {"B-5678"}

    def test_a_new_registration_is_seeded_once(self, conn: Connection) -> None:
        assert seed_registrations(conn, {"B-5678"}, NOW) == 1
        assert seed_registrations(conn, {"B-5678"}, NOW) == 0

        row = conn.execute(
            text("SELECT data_source, last_updated FROM aircraft_static_info")
        ).fetchone()
        assert row[0] == "fr24_flights"
        # The OpenSearch sync uses last_updated as its watermark; a row without
        # one never reaches the index.
        assert row[1] is not None
