"""Tests for the airport-board sink that posts to the ingest API.

The sink exists so a board scraped on a workstation reaches a database it cannot
connect to. Three things are worth pinning down:

- The payload is the same `FlightData` the direct sink writes, serialised so the
  route's Pydantic model accepts it. A field renamed on the way out would be
  dropped silently by `extra="ignore"`.
- A board larger than the route's limit is split, not rejected.
- A refused batch raises. `bind_sink` logs the exception with a traceback; a
  swallowed failure would look exactly like an airport with no flights.

And one wiring test: `scraper_main` must pick this sink over the database one
when `scraper.ingest` is configured, including under --no-db, which is the whole
point of the arrangement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
import requests
from resilient_scraper.scrapers.aviation.fr24_aircraft.models import FlightData
from resilient_scraper.scrapers.aviation.fr24_airport.models import FR24FlightsResult

from src.core.exceptions import APIError
from src.scraper.sinks.fr24_airport_api_sink import FR24AirportApiSink
from src.scraper_main import _build_ingest_target, _build_sinks_and_augment_configs

BASE_URL = "https://example.org:8443"
TOKEN = "shared-secret"


class FakeResponse:
    def __init__(self, status_code: int = 200, body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = {"success": True, "written": 0} if body is None else body
        self.text = text or str(self._body)

    def json(self) -> Any:
        if self._body is _NOT_JSON:
            raise ValueError("not json")
        return self._body


_NOT_JSON = object()


class Recorder:
    """Stands in for `requests.post`, recording each call."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def a_result(count: int = 1) -> FR24FlightsResult:
    return FR24FlightsResult(
        success=True,
        task_key="PEK",
        task_type="fr24_airport",
        airport_code="PEK",
        flight_type="arrival",
        flights=[
            FlightData(
                flight_number=f"CA{1000 + i}",
                aircraft_type="B738",
                aircraft_registration="B-5678",
                scheduled_time=datetime(2026, 8, 20, 9, 30),
                flight_id=f"id{i}",
            )
            for i in range(count)
        ],
        flights_count=count,
    )


@pytest.fixture
def post(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder(FakeResponse(body={"success": True, "written": 1}))
    monkeypatch.setattr(requests, "post", recorder)
    return recorder


class TestPayload:
    def test_the_token_and_endpoint_are_sent(self, post: Recorder) -> None:
        FR24AirportApiSink(BASE_URL, TOKEN).on_success(None, a_result())  # type: ignore[arg-type]

        call = post.calls[0]
        assert call["url"] == f"{BASE_URL}/api/ingest/flight-schedules"
        assert call["headers"]["X-Ingest-Token"] == TOKEN

    def test_a_trailing_slash_on_the_base_url_does_not_double_up(self, post: Recorder) -> None:
        FR24AirportApiSink(f"{BASE_URL}/", TOKEN).on_success(None, a_result())  # type: ignore[arg-type]

        assert post.calls[0]["url"] == f"{BASE_URL}/api/ingest/flight-schedules"

    def test_the_body_is_what_the_route_validates(self, post: Recorder) -> None:
        """Checked against the route's own model rather than a copy of its field
        names, so a rename on either side fails here."""
        from src.web.routes.ingest import IngestBatch

        FR24AirportApiSink(BASE_URL, TOKEN, flight_type_hint="arrival").on_success(
            None,  # type: ignore[arg-type]
            a_result(),
        )

        batch = IngestBatch.model_validate(post.calls[0]["json"])
        assert batch.airport_code == "PEK"
        assert batch.flight_type_hint == "arrival"
        assert batch.flights[0].flight_number == "CA1000"
        assert batch.flights[0].scheduled_time == datetime(2026, 8, 20, 9, 30)

    def test_an_empty_board_sends_nothing(self, post: Recorder) -> None:
        FR24AirportApiSink(BASE_URL, TOKEN).on_success(None, a_result(count=0))  # type: ignore[arg-type]

        assert post.calls == []


class TestBatching:
    def test_a_board_larger_than_one_batch_is_split(self, post: Recorder) -> None:
        FR24AirportApiSink(BASE_URL, TOKEN, batch_size=2).on_success(None, a_result(count=5))  # type: ignore[arg-type]

        assert [len(call["json"]["flights"]) for call in post.calls] == [2, 2, 1]

    def test_every_flight_is_sent_exactly_once(self, post: Recorder) -> None:
        FR24AirportApiSink(BASE_URL, TOKEN, batch_size=2).on_success(None, a_result(count=5))  # type: ignore[arg-type]

        sent = [f["flight_id"] for call in post.calls for f in call["json"]["flights"]]
        assert sent == ["id0", "id1", "id2", "id3", "id4"]


class TestFailuresAreLoud:
    def test_a_refused_batch_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests, "post", Recorder(FakeResponse(status_code=401, text="nope")))

        with pytest.raises(APIError) as caught:
            FR24AirportApiSink(BASE_URL, TOKEN).on_success(None, a_result())  # type: ignore[arg-type]

        assert caught.value.status_code == 401

    def test_a_transport_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(url: str, **kwargs: Any) -> FakeResponse:
            raise requests.ConnectionError("no route to host")

        monkeypatch.setattr(requests, "post", explode)

        with pytest.raises(APIError):
            FR24AirportApiSink(BASE_URL, TOKEN).on_success(None, a_result())  # type: ignore[arg-type]

    def test_a_success_false_body_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            requests, "post", Recorder(FakeResponse(body={"success": False, "error": "x"}))
        )

        with pytest.raises(APIError):
            FR24AirportApiSink(BASE_URL, TOKEN).on_success(None, a_result())  # type: ignore[arg-type]

    def test_a_non_json_body_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(requests, "post", Recorder(FakeResponse(body=_NOT_JSON, text="<html>")))

        with pytest.raises(APIError):
            FR24AirportApiSink(BASE_URL, TOKEN).on_success(None, a_result())  # type: ignore[arg-type]

    def test_a_later_batch_failing_still_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            requests,
            "post",
            Recorder(
                FakeResponse(body={"success": True, "written": 2}),
                FakeResponse(status_code=500),
            ),
        )

        with pytest.raises(APIError):
            FR24AirportApiSink(BASE_URL, TOKEN, batch_size=2).on_success(None, a_result(count=4))  # type: ignore[arg-type]


class TestScraperMainWiring:
    def _configs(self) -> dict[str, tuple[type, dict[str, Any]]]:
        return {"fr24_airport": (object, {})}

    def test_an_ingest_target_replaces_the_database_sink(self) -> None:
        config = {"scraper": {"ingest": {"base_url": BASE_URL, "token": TOKEN}}}

        sinks = _build_sinks_and_augment_configs(self._configs(), "sqlite://", config)

        assert isinstance(sinks["fr24_airport"], FR24AirportApiSink)

    def test_the_api_sink_is_built_even_with_no_database(self) -> None:
        """--no-db passes an empty URL; this sink is the reason that combination
        is useful at all."""
        config = {"scraper": {"ingest": {"base_url": BASE_URL, "token": TOKEN}}}

        sinks = _build_sinks_and_augment_configs(self._configs(), "", config)

        assert isinstance(sinks["fr24_airport"], FR24AirportApiSink)

    def test_without_an_ingest_target_the_database_sink_is_used(self) -> None:
        from src.scraper.sinks.fr24_airport_sink import FR24AirportSink

        sinks = _build_sinks_and_augment_configs(self._configs(), "sqlite://", {})

        assert isinstance(sinks["fr24_airport"], FR24AirportSink)

    def test_no_database_and_no_ingest_target_builds_nothing(self) -> None:
        assert _build_sinks_and_augment_configs(self._configs(), "", {}) == {}

    def test_a_base_url_without_a_token_is_not_an_ingest_target(self) -> None:
        """The endpoint would refuse every request, so scraping into it silently
        would lose the whole board."""
        config = {"scraper": {"ingest": {"base_url": BASE_URL, "token": ""}}}

        assert _build_ingest_target(config) is None

    def test_the_hint_reaches_the_sink(self) -> None:
        config = {"scraper": {"ingest": {"base_url": BASE_URL, "token": TOKEN}}}
        configs: dict[str, tuple[type, dict[str, Any]]] = {"fr24_arrivals": (object, {})}

        sinks = _build_sinks_and_augment_configs(configs, "", config)

        assert sinks["fr24_arrivals"].flight_type_hint == "arrival"
