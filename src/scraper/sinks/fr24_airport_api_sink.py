"""Sink that posts FR24 airport boards to the ingest API instead of the database.

Counterpart of :class:`src.scraper.sinks.fr24_airport_sink.FR24AirportSink`, for
the case where the scraper and the database are not on the same host. The
`fr24_airport` scraper needs a real Chromium (the site is Cloudflare-protected),
so it runs on a workstation; Aurora listens on the web host's loopback only. The
rows travel over HTTPS to `POST /api/ingest/flight-schedules`, which performs the
identical upsert.

Both sinks build their payload from the same `FlightData` model, so nothing here
reshapes the rows — the route validates the field names it already has.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.fr24_airport.models import FR24FlightsResult

from src.core.exceptions import APIError

logger = logging.getLogger("scraper.sinks.fr24_airport_api")

# Kept under the route's own 500-row limit, so a large hub's board is split
# rather than rejected.
DEFAULT_BATCH_SIZE = 200
DEFAULT_TIMEOUT = 60


class FR24AirportApiSink:
    """Post scraped arrival/departure rows to the ingest API.

    Args:
        base_url: Root URL of the web app, e.g. `https://example.org:8443`.
        token: Shared secret for the `X-Ingest-Token` header.
        flight_type_hint: `"arrival"` or `"departure"`, forwarded so the server
            can fill in a type the page did not state.
        batch_size: Rows per request.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        flight_type_hint: str = "",
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.endpoint = f"{base_url.rstrip('/')}/api/ingest/flight-schedules"
        self.token = token
        self.flight_type_hint = flight_type_hint
        self.batch_size = max(1, batch_size)
        self.timeout = timeout

    def on_success(self, task: ScraperTask, result: FR24FlightsResult) -> None:
        """Send every scraped row, in batches.

        Raises:
            APIError: If a batch is refused or the request fails. `bind_sink`
                catches it and logs it with a traceback; that is deliberate —
                a dropped board should leave a loud record rather than a silent
                zero-row scrape.
        """
        if not result.flights:
            return

        flights = [flight.model_dump(mode="json") for flight in result.flights]
        written = 0
        for start in range(0, len(flights), self.batch_size):
            batch = flights[start : start + self.batch_size]
            written += self._post(result.airport_code, batch)

        logger.info(f"[{result.airport_code}] Ingested {written}/{len(flights)} flights via API")

    def _post(self, airport_code: str, flights: list[dict[str, Any]]) -> int:
        """Post one batch and return how many rows the server stored.

        Args:
            airport_code: Code the board was scraped for.
            flights: Serialised `FlightData` rows.

        Returns:
            The server's `written` count.

        Raises:
            APIError: On a transport failure, a non-200 status, or a body that
                is not the expected JSON object.
        """
        payload = {
            "airport_code": airport_code,
            "flight_type_hint": self.flight_type_hint,
            "flights": flights,
        }
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers={"X-Ingest-Token": self.token},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise APIError(f"Ingest request for {airport_code} failed: {e}") from e

        if response.status_code != 200:
            # The body can carry validation details, which are ours and useful;
            # truncated because a rejected batch could otherwise echo pages of it.
            raise APIError(
                f"Ingest of {len(flights)} flights for {airport_code} was refused",
                status_code=response.status_code,
                response=response.text[:500],
            )

        try:
            body = response.json()
        except ValueError as e:
            raise APIError(f"Ingest response for {airport_code} was not JSON") from e

        if not isinstance(body, dict) or not body.get("success"):
            raise APIError(f"Ingest of {airport_code} reported failure", response=str(body)[:500])

        written = body.get("written", 0)
        return int(written) if isinstance(written, int) else 0

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        pass
