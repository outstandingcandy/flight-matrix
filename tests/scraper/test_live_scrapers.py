"""End-to-end live scraping tests for every non-submodule scraper.

These tests hit real third-party websites (airport-data.com, JetPhotos,
flightradar24.com, globe.adsbexchange.com) with production configuration — a
non-headless Chromium driven by DrissionPage.

They're marked `integration` and skipped by default. To run them:

    pytest -m integration tests/scraper/test_live_scrapers.py -v

Requirements for a green run:

  - A way to show a real (non-headless) browser window. On Linux that
    means Xvfb and a display (`:55` by default, override with `$DISPLAY`);
    the display fixture auto-starts Xvfb if it isn't already up. On macOS
    and Windows the browser window is native, so no virtual display is
    involved and the fixture is a no-op — do NOT skip there, or these
    tests silently cover nothing on a developer's laptop.
  - Chromium or Chrome installed (DrissionPage auto-detects).
  - Network access to airport-data.com, jetphotos.com,
    flightradar24.com, and globe.adsbexchange.com. Cloudflare sometimes
    throws a challenge that takes a few extra seconds to pass — we give
    each test a generous per-scraper timeout.

Why these tests exist: it's easy to refactor the scraper framework and
not notice until production that a specific site's DOM shifted or the
project's Cloudflare bypass stopped working. A live smoke per scraper
catches this in CI (if CI can show a browser window).

These tests are slow (~5 minutes for the full suite — the ADSBx test
spends 40s waiting on the live feed by design) and network-flaky by
nature; CI should schedule them nightly rather than on every PR.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest
from resilient_scraper.models import ScraperTask
from resilient_scraper.service.browser_pool import BrowserPool
from resilient_scraper.service.config import BrowserSettings

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Xvfb + browser-pool fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def xvfb_display() -> Iterator[str | None]:
    """Ensure a non-headless browser window can be shown.

    Yields the X11 display string (e.g. ":55") on Linux, or ``None`` on
    platforms where a virtual display is neither needed nor available.

    Only X11 needs a display server. On macOS a Chromium window is a native
    Cocoa window and on Windows it is a native Win32 window, so there is
    nothing to start and nothing to point `$DISPLAY` at — gating on Xvfb
    there would skip every test in this module and report a green run that
    exercised no scraper at all.
    """
    if not sys.platform.startswith("linux"):
        yield None
        return

    display = os.environ.get("DISPLAY", ":55")

    # If a display is already up, reuse it.
    display_num = display.lstrip(":").split(".")[0]
    x_socket = f"/tmp/.X11-unix/X{display_num}"
    if os.path.exists(x_socket):
        yield display
        return

    # Otherwise auto-start Xvfb, which the test lifecycle owns.
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb not installed; live scrapers need a virtual display on Linux")

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
def browser_pool(xvfb_display: str | None) -> Iterator[BrowserPool]:
    """A single-slot, non-headless browser pool on the test's display.

    `xvfb_display` is None off Linux, where the window is native — leave
    `$DISPLAY` alone there rather than pointing Chromium at an X server
    that doesn't exist.
    """
    if xvfb_display is not None:
        os.environ["DISPLAY"] = xvfb_display
    settings = BrowserSettings(pool=True, size=1, max_tasks_per_browser=50, headless=False)
    pool = BrowserPool(settings)
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
        from resilient_scraper.scrapers.aviation.airport_data import AirportDataScraper

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
        # N703PA has been worn by two airframes, so the page lists both a 1999
        # Cessna 208B and a 1959 Boeing 707-331. Assert the Cessna's own numbers
        # rather than mere presence: the interesting failure is a record that
        # blends the two, which "is not empty" happily accepts.
        assert ac.manufacturer == "Cessna"
        assert ac.model == "208B", "model should not carry the cell's 'Search all' link"
        assert ac.year_built == 1999, "year must come from the Cessna, not the Boeing"
        assert ac.engines == 1, "engine count must come from the Cessna, not the Boeing"
        assert ac.seats == 12, "seat count must come from the Cessna, not the Boeing"


# ---------------------------------------------------------------------------
# JetPhotos — cloudflare-protected
# ---------------------------------------------------------------------------


class TestJetPhotosScraper:
    def test_known_registration_has_photos(self, browser, tmp_path) -> None:
        """N703PA has been photographed enough times to be a stable fixture."""
        from resilient_scraper.scrapers.aviation.jetphotos import JetPhotosScraper

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
        from resilient_scraper.scrapers.aviation.fr24_airport import FR24AirportArrivalsScraper

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
        from resilient_scraper.scrapers.aviation.fr24_airport import FR24AirportDeparturesScraper

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
        from resilient_scraper.scrapers.aviation.fr24_aircraft import FR24AircraftScraper

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
        from resilient_scraper.scrapers.aviation.fr24_map import FR24MapScraper

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


# ---------------------------------------------------------------------------
# ADS-B Exchange globe — cloudflare-protected, feed captured via a JS hook
# ---------------------------------------------------------------------------


class TestADSBxMapScraper:
    """The one scraper whose data never touches the DOM.

    ADSBx serves its live feed as binCraft/zstd bytes that only the site's own
    XHR is allowed to fetch, so this scraper wraps `window.processAircraft` and
    reads the rows tar1090 hands that function. Three things can break without
    raising: the hook can fail to install into a late-loading bundle, the page
    can rename the `now` global the position timestamps are derived from, and
    Cloudflare can serve a challenge instead of the map. All three produce
    `success=True` with an empty aircraft list, so asserting the status is not
    enough here — the assertions below are on the rows themselves.

    Run against `europe_central` with the military filter off. Production runs
    `military_only: true` (`config/scraper/fr24.yaml`), but military traffic is
    sparse enough that a live assertion on it would flake; the filter and the
    `dbFlags & 1` test behind it are covered deterministically by the
    submodule's `tests/test_adsbx_map_parsing.py`. What needs a real browser is
    the hook, and any traffic proves the hook.

    One scrape, many assertions, on purpose. Every check below wants the same
    request with the same configuration, and each `browser` fixture builds and
    tears down its own `BrowserPool` on a fixed debug port — splitting these
    across two tests doubled a 90-second network wait for no extra coverage and
    raced the first pool's shutdown for the port.
    """

    # Busiest airspace on the planet — there is never nothing here.
    CENTER_LAT = 50.0
    CENTER_LON = 10.0
    ZOOM = 5

    def test_busy_region_yields_parsed_aircraft(self, browser) -> None:
        from resilient_scraper.scrapers.aviation.adsbx_map import ADSBxMapScraper

        scraper = ADSBxMapScraper(
            {
                "sync_to_database": False,
                "military_only": False,
                # Production waits 60s to catch sparse military traffic; over
                # Europe with the filter off, one or two feed polls is plenty.
                "collect_duration": 25,
                "wait_for_load": 15,
                "save_debug_html": True,
            }
        )
        scraper.setup()
        try:
            result = scraper.scrape(
                ScraperTask(
                    task_type="adsbx_map",
                    task_key="europe_central",
                    payload={
                        "lat": self.CENTER_LAT,
                        "lon": self.CENTER_LON,
                        "zoom": self.ZOOM,
                        "dbFlags": 1,
                    },
                ),
                browser=browser,
            )
        finally:
            scraper.teardown()

        assert result.success is True

        # The hook installed and the feed arrived. An empty list here is the
        # failure this whole test exists to catch.
        aircraft = result.aircraft or []
        assert len(aircraft) >= 1, (
            "no aircraft over central Europe — the processAircraft hook did not "
            "install, or Cloudflare served a challenge instead of the map"
        )
        assert result.aircraft_count == len(aircraft)

        # Read off the page, not computed by us: if tar1090 renames its `now`
        # global this goes None and every position timestamp goes with it.
        assert result.feed_generated_at is not None, (
            "the feed clock was not found on the page; position timestamps would all be None"
        )
        assert result.scraped_at is not None

        # Dedup held over a live collection window, where tar1090 calls the hook
        # once per aircraft per feed poll — tens of thousands of calls for these.
        hexes = [a.hex for a in aircraft]
        assert len(set(hexes)) == len(hexes), "the same airframe was returned more than once"
        assert all(h == h.lower() for h in hexes)

        # `military_count` counts the feed, not the filtered output.
        assert result.military_count == sum(1 for a in aircraft if a.mil)
        assert result.military_count <= result.aircraft_count

        # The request echoed back, so a mis-parsed payload can't pass silently.
        assert result.center_lat == self.CENTER_LAT
        assert result.center_lon == self.CENTER_LON
        assert result.zoom_level == self.ZOOM

        # The URL's coordinates must actually steer the map. tar1090 serves
        # whatever is in the current viewport, so if lat/lon were ignored — or
        # reset to the site's default world view — the scrape would still return
        # plenty of aircraft, just not the region the task asked for, and every
        # region in the rotation would report the same airspace.
        positioned = [a for a in aircraft if a.latitude is not None and a.longitude is not None]
        assert len(positioned) >= 1, "not one aircraft carried a position"

        # Generous on purpose: the viewport at zoom 5 spans ~6°, so 25° leaves
        # room for edge-of-screen traffic and stale rows while still failing if
        # we were served the default world view.
        near = [
            a
            for a in positioned
            if abs(a.latitude - self.CENTER_LAT) <= 25 and abs(a.longitude - self.CENTER_LON) <= 25
        ]
        assert len(near) * 2 >= len(positioned), (
            f"only {len(near)} of {len(positioned)} positions are near "
            f"({self.CENTER_LAT}, {self.CENTER_LON}) — the map was not steered there"
        )
