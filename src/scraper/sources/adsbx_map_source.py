"""
ADS-B Exchange globe task source.

Generates region-cycling tasks to scrape aircraft positions from ADS-B
Exchange.

The region grid is ADSBx's own — deliberately not FR24's. The two feeds have
opposite constraints: FR24 caps a request at roughly 1,500 aircraft
(``config/scraper/fr24.yaml``), so it needs many small windows, while ADSBx
hands us whatever tar1090 has loaded — a single zoom-3 request has measured up
to 11,998 aircraft spanning 85 degrees of latitude and 182 of longitude. Reusing
FR24's 50 small regions here meant scraping the same airspace up to eight times
per cycle and stretching one full pass to about 80 minutes.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from src.scraper.local_task_source import BaseTaskSource
from src.scraper.models import ScraperTask

logger = logging.getLogger("scraper.sources.adsbx_map")

# Six windows — two rows by three columns — covering every latitude that
# carries traffic, with deliberate overlap at every seam.
#
# Sized from measurement, not from the nominal ``180 / 2**zoom`` span. That span
# describes neither what the viewport shows nor what the feed returns: at zoom 3
# it computes to 22.5 degrees, a 1920x1080 viewport shows 337, and a measured
# scrape returned 182. The binding limit is how much tar1090 loads, so the only
# honest source for these numbers is a real request.
#
# Measured at zoom 3, center (50, 10): aircraft spanned latitude -6..73 and
# longitude -81..101.
#
#   longitude: Mercator x is linear in longitude, and the measured span was
#              symmetric about the center, so the reach is +/-91 degrees. Three
#              columns 120 apart therefore overlap by 62 at each seam.
#
#              The six-window probe below confirms this, but only once the
#              antimeridian is accounted for. Both windows centered on longitude
#              0 returned exactly -91.4..91.3, while the four centered on +/-120
#              appear to span -172..180 — which looks like a near-global feed and
#              is not. A window at 120 with +/-91 reach covers 29..180 and
#              -180..-149; taking the raw min and max of those longitudes reads
#              as -173..179 even though nothing between -149 and 29 was returned.
#              The coverage test works on the doubled line for exactly this
#              reason.
#
#   latitude:  Mercator y is not linear in latitude, so a window's reach in
#              degrees depends on where it is centered and rows cannot simply be
#              spaced evenly. Working in y = asinh(tan(lat)): the measurement
#              spans 2.006, of which 0.890 lay north of center. Taking 0.890 as
#              the half-reach is the conservative reading — the southern 1.116
#              may just be where the loaded region ended rather than where the
#              window did.
#
#              Two rows of +/-0.890 in y cover 3.56 against the 3.05 that
#              latitude -60..70 requires. Placing them at y -0.432 and 0.845 —
#              latitude -24 and 44 — covers -60.1..70.2 with 0.49 of y (about
#              27 degrees, from -1.9 to 25.4) overlapping across the equator.
#              Beyond that lie the polar caps, which no route crosses in numbers
#              worth a window.
#
# The redundancy is deliberate: a gap loses aircraft silently and forever, while
# an overlap only costs duplicate rows that retention will expire anyway.
#
# Verified by scraping all six windows back to back with the military filter off.
# Five returned in the first run; north_eurafrica died on a CDP transfer bug since
# fixed in the submodule's _drain_rows and re-measured separately (3 of 3 runs):
#
#   north_americas  11,998    lat -14.8..69.9    lon -172..177  (wrapped)
#   north_eurafrica 11,589-11,670  lat -15.0..72.6    lon  -91.. 91
#   north_asia       2,084    lat -13.1..68.4    lon -173..145  (wrapped)
#   south_americas   5,998    lat -52.9..36.8    lon -172..180  (wrapped)
#   south_africa     4,277    lat -52.6..36.8    lon  -91.. 91
#   south_oceania    1,692    lat -46.2..36.8    lon -172..180  (wrapped)
#
#   About 37,700 rows per pass. The five-window run held 26,049 rows for 16,122
#   distinct aircraft — 1.62x duplication; the six-window figure was not measured
#   in one run, so it is higher than that and lower than the 1.8x the geometry
#   predicts.
#
#   The northern rows reached 0.80..0.87 of y above their center against the
#   0.890 assumed above, so the vertical model holds. The union spans latitude
#   -52.9..72.6 rather than the -60..70 targeted; the southern edge is traffic,
#   not the window, since the southern row's downward reach varied 0.48..1.12 in
#   y between its three windows.
#
#   Every window contributed aircraft no other window saw — 7,040 for
#   north_americas down to 318 for south_oceania — so none of the six is
#   redundant.
ADSBX_REGIONS: dict[str, dict[str, Any]] = {
    "north_americas": {"lat": 44.0, "lon": -120.0, "zoom": 3, "priority": 1},
    "north_eurafrica": {"lat": 44.0, "lon": 0.0, "zoom": 3, "priority": 1},
    "north_asia": {"lat": 44.0, "lon": 120.0, "zoom": 3, "priority": 1},
    "south_americas": {"lat": -24.0, "lon": -120.0, "zoom": 3, "priority": 2},
    "south_africa": {"lat": -24.0, "lon": 0.0, "zoom": 3, "priority": 2},
    "south_oceania": {"lat": -24.0, "lon": 120.0, "zoom": 3, "priority": 2},
}


class ADSBxMapTaskSource(BaseTaskSource):
    """Cycles through :data:`ADSBX_REGIONS` and emits adsbx_map tasks.

    Reads the same YAML keys as fr24_map (``global_coverage``,
    ``enabled_regions``, ``max_priority``,
    ``interval_seconds``/``min_cycle_gap``, ``regions``) so the two sources can
    be tuned with matching knobs, but the default grid is ADSBx's own — see the
    module docstring for why the two cannot share one. ``include_oceans`` is
    accepted and ignored: every window here already spans ocean and land.

    Each emitted task's payload carries ``dbFlags`` so the scraper's tar1090 URL
    filter gets the same bit set the config asked for.
    """

    def __init__(
        self,
        config: dict[str, Any],
        database_url: str,
    ) -> None:
        super().__init__(task_type="adsbx_map", max_attempts=3)
        self.database_url = database_url

        scraper_config = config.get("scraper", {}).get("scrapers", {}).get("adsbx_map", {})

        self.global_coverage = scraper_config.get("global_coverage", False)
        custom_regions = scraper_config.get("regions", [])
        self.enabled_regions: list[str] = scraper_config.get("enabled_regions", [])
        self.max_priority = scraper_config.get("max_priority", 3)
        self.db_flags = int(scraper_config.get("db_flags", 1))

        if self.global_coverage or not custom_regions:
            self.regions = self._build_active_regions()
        else:
            self.regions = self._build_custom_regions(custom_regions)

        self.interval_seconds = scraper_config.get(
            "interval_seconds",
            scraper_config.get("min_cycle_gap", 30),
        )

        self._current_index = 0
        self._region_to_task: dict[str, int] = {}
        self._last_scraped: dict[str, datetime] = {}
        self._cycle_count = 0

        logger.info(
            f"ADSBxMapTaskSource initialized with {len(self.regions)} regions "
            f"(max_priority={self.max_priority}, interval={self.interval_seconds}s, "
            f"dbFlags={self.db_flags})"
        )

    def _build_active_regions(self) -> dict[str, dict[str, Any]]:
        """Select the ADSBx windows this worker should cycle through.

        There is no ocean filter: unlike FR24's grid, every window here spans
        ocean and land alike, so ``include_oceans`` has nothing to exclude.
        """
        active: dict[str, dict[str, Any]] = {}
        for name, region_config in ADSBX_REGIONS.items():
            if self.enabled_regions and name not in self.enabled_regions:
                continue
            if region_config.get("priority", 1) > self.max_priority:
                continue
            active[name] = region_config

        return dict(sorted(active.items(), key=lambda x: x[1].get("priority", 1)))

    def _build_custom_regions(
        self, custom_regions: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        regions: dict[str, dict[str, Any]] = {}
        for region in custom_regions:
            name = region.get("name", f"region_{len(regions)}")
            regions[name] = {
                "lat": region.get("lat", 0),
                "lon": region.get("lon", 0),
                "zoom": region.get("zoom", 5),
                "priority": region.get("priority", 1),
            }
        return regions

    def get_pending_tasks(self, limit: int = 10) -> list[ScraperTask]:
        if not self.regions:
            logger.warning("No regions configured for adsbx_map")
            return []

        tasks: list[ScraperTask] = []
        region_names = list(self.regions.keys())
        now = datetime.now(UTC)

        with self._lock:
            checked = 0
            while len(tasks) < limit and checked < len(region_names):
                if self._current_index >= len(region_names):
                    self._current_index = 0
                    self._cycle_count += 1
                    logger.info(f"ADSBxMapTaskSource completed cycle {self._cycle_count}")

                region_name = region_names[self._current_index]
                self._current_index += 1
                checked += 1

                if region_name in self._region_to_task:
                    continue

                last_time = self._last_scraped.get(region_name)
                if last_time:
                    elapsed = (now - last_time).total_seconds()
                    if elapsed < self.interval_seconds:
                        continue

                region_config = self.regions[region_name]
                task = self._create_task(
                    task_key=region_name,
                    payload={
                        "lat": region_config["lat"],
                        "lon": region_config["lon"],
                        "zoom": region_config.get("zoom", 5),
                        "dbFlags": self.db_flags,
                    },
                )

                if task.id is not None:
                    self._region_to_task[region_name] = task.id
                tasks.append(task)

        if tasks:
            logger.info(
                f"ADSBxMapTaskSource returned {len(tasks)} tasks: {[t.task_key for t in tasks]}"
            )

        return tasks

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        with self._lock:
            if task.task_key:
                self._region_to_task.pop(task.task_key, None)
                self._last_scraped[task.task_key] = datetime.now(UTC)

        mil = 0
        total = 0
        if result:
            mil = result.get("military_count", 0)
            total = result.get("aircraft_count", 0)
        logger.info(
            f"Task {task.id} ({task.task_key}) completed: {mil} military / {total} aircraft"
        )

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        with self._lock:
            if task.task_key:
                self._region_to_task.pop(task.task_key, None)
        logger.warning(f"Task {task.id} ({task.task_key}) failed: {error}")

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        with self._lock:
            if task.task_key:
                self._region_to_task.pop(task.task_key, None)
                self._last_scraped[task.task_key] = datetime.now(UTC)
        logger.info(f"Task {task.id} ({task.task_key}) no data: {reason}")

    def get_stats(self) -> dict[str, Any]:
        stats = super().get_stats()
        with self._lock:
            remaining = len(self.regions) - self._current_index
        stats.update(
            {
                "total_regions": len(self.regions),
                "remaining_in_cycle": remaining,
                "cycles_completed": self._cycle_count,
            }
        )
        return stats

    def get_region_list(self) -> list[dict[str, Any]]:
        return [{"name": name, **cfg} for name, cfg in self.regions.items()]
