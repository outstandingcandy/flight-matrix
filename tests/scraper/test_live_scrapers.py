"""End-to-end live scraping tests for every non-submodule scraper.

These tests hit real third-party websites (airport-data.com, JetPhotos,
flightradar24.com) with production configuration (non-headless Chromium
via DrissionPage, backed by an Xvfb virtual display).

They're marked `integration` and skipped by default. To run them:

    pytest -m integration tests/scraper/test_live_scrapers.py -v

Requirements for a green run:

  - Xvfb installed and a display available (`:55` by default, override
    with `$DISPLAY`). A local display fixture auto-starts Xvfb if the
    display isn't already up.
  - Chromium or Chrome installed (DrissionPage auto-detects).
  - Network access to airport-data.com, jetphotos.com, and
    flightradar24.com. Cloudflare sometimes throws a challenge that
    takes a few extra seconds to pass — we give each test a generous
    per-scraper timeout.

Why these tests exist: it's easy to refactor the scraper framework and
not notice until production that a specific site's DOM shifted or the
project's Cloudflare bypass stopped working. A live smoke per scraper
catches this in CI (if CI runs with Xvfb).

These tests are slow (~4 minutes for the full suite) and network-flaky
by nature; CI should schedule them nightly rather than on every PR.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

from src.scraper.browser_pool import BrowserPool
from src.scraper.models import ScraperTask

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Xvfb + browser-pool fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def xvfb_display() -> Iterator[str]:
    """Ensure an X11 display is available; auto-start Xvfb if not.

    Yields the display string (e.g. ":55"). Skips the whole module if the
    host lacks Xvfb and the tests therefore can't run non-headless.
    """
    display = os.environ.get("DISPLAY", ":55")

    # If a display is already up, reuse it.
    display_num = display.lstrip(":").split(".")[0]
    x_socket = f"/tmp/.X11-unix/X{display_num}"
    if os.path.exists(x_socket):
        yield display
        return

    # Otherwise auto-start Xvfb, which the test lifecycle owns.
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb not installed; live scrapers need a virtual display")

    # Don't call `with subprocess.Popen(...)` — Xvfb is daemon-style, we
    # just detach and kill it in teardown.
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1920x1080x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)  # give Xvfb a chance to bind its socket

    if proc.poll() is not None:
        pytest.skip(f"Xvfb {display} failed to start")

    try:
        yield display
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def browser_pool(xvfb_display: str) -> Iterator[BrowserPool]:
    """A single-slot, non-headless browser pool on the test's Xvfb display."""
    os.environ["DISPLAY"] = xvfb_display
    pool = BrowserPool(pool_size=1, drission_options={"headless": False})
    pool.initialize()
    try:
        yield pool
    finally:
        pool.shutdown()


@pytest.fixture
def browser(browser_pool: BrowserPool):
    """A borrowed browser, released at teardown."""
    b = browser_pool.acquire(timeout=60)
    try:
        yield b
    finally:
        browser_pool.release(b)


# ---------------------------------------------------------------------------
# airport-data.com — NOT cloudflare-protected
# ---------------------------------------------------------------------------


class TestAirportDataScraper:
    def test_aircraft_detail(self, browser) -> None:
        """N703PA is a Cessna 208B; airport-data.com has a stable page."""
        from src.scraper.scrapers.airport_data import AirportDataScraper

        scraper = AirportDataScraper({"sync_to_database": False, "s3_upload": False})
        scraper.setup()
        try:
            result = scraper.scrape(
                ScraperTask(task_type="airport_data", task_key="aircraft:N703PA"),
                browser=browser,
            )
        finally:
            scraper.teardown()

        assert result.success is True
        assert result.scrape_mode == "aircraft"
        assert result.aircraft_count == 1
        assert len(result.aircraft) == 1
        ac = result.aircraft[0]
        # Registration is deterministic.
        assert ac.registration == "N703PA"
        # Model data should populate — the exact string may shift so just check presence.
        assert ac.manufacturer, "manufacturer should not be empty"
        assert ac.model, "model should not be empty"


# ---------------------------------------------------------------------------
# JetPhotos — cloudflare-protected
# ---------------------------------------------------------------------------


class TestJetPhotosScraper:
    def test_known_registration_has_photos(self, browser, tmp_path) -> None:
        """N703PA has been photographed enough times to be a stable fixture."""
        from src.scraper.scrapers.jetphotos import JetPhotosScraper

        scraper = JetPhotosScraper(
            {
                "max_pages": 1,
                "max_images_per_aircraft": 1,
                "download_all_images": False,
                "images_dir": str(tmp_path / "jp"),
                "parallel_workers": 1,
                "delay_min": 0.3,
                "delay_max": 0.8,
                "s3_upload": False,
                "sync_to_database": False,
            }
        )
        scraper.setup()
        try:
            result = scraper.scrape(
                ScraperTask(task_type="jetphotos", task_key="N703PA"),
                browser=browser,
            )
        finally:
            scraper.teardown()

        assert result.success is True
        # JetPhotosResult exposes `images_metadata` (list[ImageMetadata]) and
        # `image_paths` (list of local file paths for the images downloaded).
        metadata = result.images_metadata or []
        assert len(metadata) >= 1, "N703PA should have at least one JetPhotos image"
        assert result.image_count >= 1


# ---------------------------------------------------------------------------
# FR24 airport pages — cloudflare-protected, heavy pagination
# ---------------------------------------------------------------------------


class TestFR24AirportScrapers:
    def test_arrivals(self, browser) -> None:
        """JFK has hundreds of daily arrivals — a robust baseline."""
        from src.scraper.scrapers.fr24_airport import FR24AirportArrivalsScraper

        scraper = FR24AirportArrivalsScraper({"max_load_more_clicks": 0, "sync_to_database": False})
        scraper.setup()
        try:
            result = scraper.scrape(
                ScraperTask(task_type="fr24_arrivals", task_key="JFK"),
                browser=browser,
            )
        finally:
            scraper.teardown()

        assert result.success is True
        assert result.airport_code == "JFK"
        # Airport name should mention JFK.
        assert "Kennedy" in (result.airport_name or "")
        flights = result.flights or []
        assert len(flights) >= 10, f"JFK arrivals should yield >=10 flights, got {len(flights)}"

    def test_departures(self, browser) -> None:
        from src.scraper.scrapers.fr24_airport import FR24AirportDeparturesScraper

        scraper = FR24AirportDeparturesScraper(
            {"max_load_more_clicks": 0, "sync_to_database": False}
        )
        scraper.setup()
        try:
            result = scraper.scrape(
                ScraperTask(task_type="fr24_departures", task_key="JFK"),
                browser=browser,
            )
        finally:
            scraper.teardown()

        assert result.success is True
        assert result.airport_code == "JFK"
        assert len(result.flights or []) >= 10


# ---------------------------------------------------------------------------
# FR24 per-aircraft page — cloudflare-protected
# ---------------------------------------------------------------------------


class TestFR24AircraftScraper:
    def test_commercial_aircraft_has_flights(self, browser) -> None:
        """D-AIXA is a Lufthansa A350 that flies daily — FR24 tracks it.

        Small-GA regs (like N703PA, which works for airport-data.com) have no
        FR24 data and will correctly `NoDataFoundError`; the scraper is not
        broken, the test data would be wrong.
        """
        from src.scraper.scrapers.fr24_aircraft import FR24AircraftScraper

        scraper = FR24AircraftScraper({"sync_to_database": False})
        scraper.setup()
        try:
            result = scraper.scrape(
                ScraperTask(task_type="fr24_aircraft", task_key="D-AIXA"),
                browser=browser,
            )
        finally:
            scraper.teardown()

        assert result.success is True
        assert len(result.flights or []) >= 1


# ---------------------------------------------------------------------------
# FR24 map tile — cloudflare-protected but lightweight
# ---------------------------------------------------------------------------


class TestFR24MapScraper:
    def test_nyc_area(self, browser) -> None:
        """NYC area should always have something in the air."""
        from src.scraper.scrapers.fr24_map import FR24MapScraper

        scraper = FR24MapScraper({"sync_to_database": False})
        scraper.setup()
        try:
            result = scraper.scrape(
                ScraperTask(
                    task_type="fr24_map",
                    task_key="nyc",
                    payload={"lat": 40.64, "lon": -73.78, "zoom": 8},
                ),
                browser=browser,
            )
        finally:
            scraper.teardown()

        assert result.success is True
