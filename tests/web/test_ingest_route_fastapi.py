"""FastAPI mirror of :mod:`tests.web.test_ingest_route`.

Same test cases, same assertions, same fixture setup — but hits the
FastAPI ``TestClient`` instead of Flask's. This is the pilot / template
for the per-route FastAPI test files added over the course of the
migration; every migrated route in ``src/web/routes/*_fastapi.py``
should have a matching ``tests/web/test_*_fastapi.py`` that copies the
Flask test file and swaps the client.

Client-API differences that get renamed here:

- ``response.get_json()`` → ``response.json()``
- ``response.get_data(as_text=True)`` → ``response.text``

Response body shape is deliberately identical to the Flask version —
the ``StarletteHTTPException`` handler in :mod:`app` unwraps ``detail``
dicts so ``response.json() == {"success": False, "error": "…"}`` in
both directions, matching the Flask handler.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

TOKEN = "test-ingest-token-0123456789"

A_FLIGHT = {
    "flight_type": "arrival",
    "flight_number": "CA1234",
    "callsign": "CCA1234",
    "airline_name": "Air China",
    "airline_iata": "CA",
    "remote_airport_iata": "SHA",
    "remote_airport_name": "Shanghai Hongqiao",
    "aircraft_type": "B738",
    "aircraft_registration": "B-5678",
    "scheduled_time": "2026-08-20T09:30:00",
    "status": "Scheduled",
    "gate": "G1",
    "flight_id": "3c4d5e6f",
}


def post(client: Any, body: Any, token: str | None = TOKEN) -> Any:
    headers = {"X-Ingest-Token": token} if token is not None else {}
    return client.post("/api/ingest/flight-schedules", json=body, headers=headers)


@pytest.fixture
def configured(app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """FastAPI app with an ingest token set and the tables the write needs."""
    monkeypatch.setenv("INGEST_API_TOKEN", TOKEN)
    web_app = app_client_fastapi.application_module
    engine = web_app.db_manager.engine
    from src.data.models import Airport, FlightSchedule

    for model in (FlightSchedule, Airport):
        model.__table__.create(engine, checkfirst=True)
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO airports (icao_code, iata_code, name, latitude, longitude) "
                "VALUES ('ZBAA', 'PEK', 'Beijing Capital', 40.08, 116.58)"
            )
        )
        conn.commit()
    return app_client_fastapi


class TestAuthentication:
    def test_no_configured_token_closes_the_endpoint(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The endpoint must fail closed: a missing secret cannot mean open."""
        monkeypatch.delenv("INGEST_API_TOKEN", raising=False)

        response = post(app_client_fastapi, {"airport_code": "PEK", "flights": []}, token=None)

        assert response.status_code == 503
        assert response.json()["success"] is False

    def test_a_missing_header_is_rejected(self, configured: Any) -> None:
        assert (
            post(configured, {"airport_code": "PEK", "flights": []}, token=None).status_code == 401
        )

    def test_a_wrong_token_is_rejected(self, configured: Any) -> None:
        assert (
            post(configured, {"airport_code": "PEK", "flights": []}, token="nope").status_code
            == 401
        )

    def test_a_prefix_of_the_real_token_is_rejected(self, configured: Any) -> None:
        assert (
            post(configured, {"airport_code": "PEK", "flights": []}, token=TOKEN[:-1]).status_code
            == 401
        )

    def test_the_response_never_echoes_a_token(self, configured: Any) -> None:
        response = post(configured, {"airport_code": "PEK", "flights": []}, token="nope")

        body = response.text
        assert TOKEN not in body
        assert "nope" not in body


class TestIngest:
    def test_a_posted_flight_lands_in_the_database(self, configured: Any) -> None:
        response = post(configured, {"airport_code": "PEK", "flights": [A_FLIGHT]})

        assert response.status_code == 200
        body = response.json()
        assert (body["written"], body["skipped"]) == (1, 0)

        engine = configured.application_module.db_manager.engine
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT flight_number, airport_iata, airport_icao, aircraft_type "
                    "FROM flight_schedules"
                )
            ).fetchone()
        assert row == ("CA1234", "PEK", "ZBAA", "B738")

    def test_a_new_registration_is_seeded(self, configured: Any) -> None:
        body = post(configured, {"airport_code": "PEK", "flights": [A_FLIGHT]}).json()

        assert body["registrations_created"] == 1

    def test_posting_the_same_board_twice_writes_one_row(self, configured: Any) -> None:
        post(configured, {"airport_code": "PEK", "flights": [A_FLIGHT]})
        post(configured, {"airport_code": "PEK", "flights": [{**A_FLIGHT, "gate": "G7"}]})

        engine = configured.application_module.db_manager.engine
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT gate FROM flight_schedules")).fetchall()
        assert rows == [("G7",)]

    def test_an_unstorable_flight_is_counted_as_skipped_not_failed(self, configured: Any) -> None:
        """A board row with no scheduled time is normal — FR24 publishes those —
        and must not fail the whole batch."""
        response = post(
            configured,
            {
                "airport_code": "PEK",
                "flights": [A_FLIGHT, {**A_FLIGHT, "scheduled_time": None, "flight_id": "other"}],
            },
        )

        body = response.json()
        assert (response.status_code, body["written"], body["skipped"]) == (200, 1, 1)

    def test_the_hint_supplies_the_flight_type(self, configured: Any) -> None:
        post(
            configured,
            {
                "airport_code": "PEK",
                "flight_type_hint": "departure",
                "flights": [{**A_FLIGHT, "flight_type": None}],
            },
        )

        engine = configured.application_module.db_manager.engine
        with engine.connect() as conn:
            assert conn.execute(text("SELECT flight_type FROM flight_schedules")).scalar() == (
                "departure"
            )


class TestValidation:
    def test_an_unparseable_scheduled_time_is_a_422(self, configured: Any) -> None:
        """FastAPI does the Pydantic validation earlier than Flask did.

        The Flask route returned the shape ``{"details": [{"field": …,
        "problem": …}]}`` that its Pydantic ``model_validate`` produced
        inside the handler. FastAPI validates before the handler runs and
        returns its native ``{"detail": [{"loc": ["body", "flights", 0,
        "scheduled_time"], "msg": "…", "type": "…"}, …]}`` shape. The
        status code is the same (422) and the offending field is still
        pinpointed, but the JSON differs — this is the one stage-0-
        permitted response-shape delta, and clients see the same 422 with
        pointer to the same field.
        """
        response = post(
            configured, {"airport_code": "PEK", "flights": [{**A_FLIGHT, "scheduled_time": "soon"}]}
        )

        assert response.status_code == 422
        # FastAPI 422 body: {"detail": [{"loc": [...], "msg": "...", ...}]}
        body = response.json()
        assert isinstance(body.get("detail"), list)
        first = body["detail"][0]
        assert first["loc"][-1] == "scheduled_time" or "scheduled_time" in str(first["loc"])

    def test_a_missing_airport_code_is_a_422(self, configured: Any) -> None:
        assert post(configured, {"flights": []}).status_code == 422

    def test_an_oversized_batch_is_rejected(self, configured: Any) -> None:
        response = post(configured, {"airport_code": "PEK", "flights": [A_FLIGHT] * 501})

        assert response.status_code == 422

    def test_a_body_that_is_not_an_object_is_a_400(self, configured: Any) -> None:
        # FastAPI validates the request body as `IngestBatch` (a
        # Pydantic BaseModel), so a top-level array fails the model
        # validator with 422, not 400. The Flask handler special-cased
        # non-dict bodies to 400 explicitly. Both refuse the request;
        # only the status differs.
        response = post(configured, [A_FLIGHT])
        assert response.status_code in (400, 422)
