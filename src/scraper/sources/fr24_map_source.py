"""
FR24 Map global task source.

Generates tasks to scrape aircraft positions from FR24 map view,
covering the entire globe or specific regions.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from src.scraper.local_task_source import BaseTaskSource
from src.scraper.models import ScraperTask

logger = logging.getLogger("scraper.sources.fr24_map")

# Global coverage regions with center coordinates
# Divided into high-traffic and low-traffic areas
GLOBAL_REGIONS = {
    # East Asia (high traffic)
    "east_asia_north": {
        "lat": 40.0,
        "lon": 116.0,
        "zoom": 5,
        "priority": 1,
    },  # Beijing, Korea, Japan
    "east_asia_south": {
        "lat": 25.0,
        "lon": 114.0,
        "zoom": 5,
        "priority": 1,
    },  # South China, Taiwan, HK
    "japan": {"lat": 36.0, "lon": 138.0, "zoom": 5, "priority": 1},  # Japan
    "korea": {"lat": 36.5, "lon": 127.5, "zoom": 6, "priority": 1},  # Korea
    # Southeast Asia (high traffic)
    "southeast_asia_north": {
        "lat": 15.0,
        "lon": 105.0,
        "zoom": 5,
        "priority": 1,
    },  # Thailand, Vietnam
    "southeast_asia_south": {
        "lat": 2.0,
        "lon": 110.0,
        "zoom": 5,
        "priority": 1,
    },  # Singapore, Indonesia
    "philippines": {"lat": 12.0, "lon": 122.0, "zoom": 5, "priority": 2},  # Philippines
    # South Asia (high traffic)
    "india_north": {"lat": 28.0, "lon": 77.0, "zoom": 5, "priority": 1},  # North India
    "india_south": {"lat": 15.0, "lon": 78.0, "zoom": 5, "priority": 1},  # South India
    "india_west": {"lat": 20.0, "lon": 72.0, "zoom": 5, "priority": 2},  # West India
    # Middle East (high traffic)
    "middle_east_gulf": {"lat": 25.0, "lon": 54.0, "zoom": 5, "priority": 1},  # UAE, Qatar
    "middle_east_west": {"lat": 32.0, "lon": 36.0, "zoom": 5, "priority": 2},  # Israel, Jordan
    "turkey": {"lat": 39.0, "lon": 32.0, "zoom": 5, "priority": 1},  # Turkey
    # Europe (high traffic)
    "europe_west": {"lat": 48.0, "lon": 2.0, "zoom": 5, "priority": 1},  # France, Benelux
    "europe_central": {"lat": 50.0, "lon": 10.0, "zoom": 5, "priority": 1},  # Germany
    "europe_south": {"lat": 42.0, "lon": 12.0, "zoom": 5, "priority": 1},  # Italy, Spain
    "europe_north": {"lat": 58.0, "lon": 12.0, "zoom": 5, "priority": 2},  # Scandinavia
    "uk_ireland": {"lat": 53.0, "lon": -2.0, "zoom": 5, "priority": 1},  # UK, Ireland
    "europe_east": {"lat": 50.0, "lon": 20.0, "zoom": 5, "priority": 2},  # Poland, Czech
    "russia_west": {"lat": 56.0, "lon": 38.0, "zoom": 5, "priority": 2},  # Moscow region
    # North America (high traffic)
    "usa_northeast": {"lat": 41.0, "lon": -74.0, "zoom": 5, "priority": 1},  # NYC, Boston
    "usa_southeast": {"lat": 30.0, "lon": -82.0, "zoom": 5, "priority": 1},  # Florida, Atlanta
    "usa_midwest": {"lat": 42.0, "lon": -88.0, "zoom": 5, "priority": 1},  # Chicago
    "usa_southwest": {"lat": 34.0, "lon": -118.0, "zoom": 5, "priority": 1},  # LA, Phoenix
    "usa_northwest": {"lat": 47.0, "lon": -122.0, "zoom": 5, "priority": 2},  # Seattle
    "usa_texas": {"lat": 30.0, "lon": -97.0, "zoom": 5, "priority": 1},  # Texas
    "canada_east": {"lat": 45.0, "lon": -75.0, "zoom": 5, "priority": 2},  # Toronto, Montreal
    "canada_west": {"lat": 49.0, "lon": -123.0, "zoom": 5, "priority": 2},  # Vancouver
    "mexico": {"lat": 20.0, "lon": -100.0, "zoom": 5, "priority": 2},  # Mexico
    # South America
    "brazil_south": {"lat": -23.0, "lon": -46.0, "zoom": 5, "priority": 2},  # Sao Paulo
    "brazil_north": {"lat": -8.0, "lon": -40.0, "zoom": 5, "priority": 3},  # Northeast Brazil
    "argentina": {"lat": -34.0, "lon": -58.0, "zoom": 5, "priority": 2},  # Buenos Aires
    "colombia": {"lat": 5.0, "lon": -74.0, "zoom": 5, "priority": 3},  # Colombia
    "chile": {"lat": -33.0, "lon": -70.0, "zoom": 5, "priority": 3},  # Chile
    # Africa
    "africa_north": {"lat": 30.0, "lon": 10.0, "zoom": 4, "priority": 2},  # North Africa
    "africa_west": {"lat": 8.0, "lon": 0.0, "zoom": 4, "priority": 3},  # West Africa
    "africa_east": {"lat": -2.0, "lon": 38.0, "zoom": 5, "priority": 2},  # Kenya, Ethiopia
    "africa_south": {"lat": -26.0, "lon": 28.0, "zoom": 5, "priority": 2},  # South Africa
    # Oceania
    "australia_east": {"lat": -33.0, "lon": 151.0, "zoom": 5, "priority": 2},  # Sydney
    "australia_west": {"lat": -32.0, "lon": 116.0, "zoom": 5, "priority": 3},  # Perth
    "new_zealand": {"lat": -41.0, "lon": 175.0, "zoom": 5, "priority": 3},  # New Zealand
    # Central Asia / Russia
    "central_asia": {"lat": 42.0, "lon": 65.0, "zoom": 4, "priority": 3},  # Kazakhstan
    "russia_siberia": {"lat": 55.0, "lon": 85.0, "zoom": 4, "priority": 3},  # Siberia
    "russia_far_east": {"lat": 50.0, "lon": 130.0, "zoom": 4, "priority": 3},  # Far East
    # Oceans / Remote (low priority, large zoom for coverage)
    "pacific_north": {"lat": 30.0, "lon": -160.0, "zoom": 3, "priority": 4},  # North Pacific
    "pacific_south": {"lat": -15.0, "lon": -150.0, "zoom": 3, "priority": 4},  # South Pacific
    "atlantic_north": {"lat": 45.0, "lon": -40.0, "zoom": 3, "priority": 3},  # North Atlantic
    "atlantic_south": {"lat": -20.0, "lon": -20.0, "zoom": 3, "priority": 4},  # South Atlantic
    "indian_ocean": {"lat": -10.0, "lon": 70.0, "zoom": 3, "priority": 4},  # Indian Ocean
    # Polar regions (very low priority)
    "arctic": {"lat": 75.0, "lon": 0.0, "zoom": 3, "priority": 5},  # Arctic
}


class FR24MapTaskSource(BaseTaskSource):
    """Task source for FR24 map global coverage.

    Generates tasks to scrape aircraft positions from different regions
    of the world. Cycles through regions based on priority.

    Attributes:
        regions: Dictionary of region name -> config.
        interval_seconds: Minimum seconds between scraping same region.
    """

    def __init__(
        self,
        config: dict[str, Any],
        database_url: str,
    ) -> None:
        """Initialize the FR24 map task source.

        Args:
            config: Full configuration dictionary.
            database_url: Database URL.
        """
        super().__init__(task_type="fr24_map", max_attempts=3)
        self.database_url = database_url

        # Get configuration
        scraper_config = config.get("scraper", {}).get("scrapers", {}).get("fr24_map", {})

        # Global coverage mode vs custom regions mode
        # If global_coverage is true OR no custom regions defined, use GLOBAL_REGIONS
        self.global_coverage = scraper_config.get("global_coverage", False)
        custom_regions = scraper_config.get("regions", [])

        # Region selection for global mode
        self.enabled_regions: list[str] = scraper_config.get("enabled_regions", [])
        self.max_priority = scraper_config.get("max_priority", 3)
        self.include_oceans = scraper_config.get("include_oceans", False)

        # Build active regions list
        if self.global_coverage or not custom_regions:
            # Use predefined global regions
            self.regions = self._build_active_regions()
        else:
            # Use custom regions from config
            self.regions = self._build_custom_regions(custom_regions)

        # Timing - support both interval_seconds and min_cycle_gap
        self.interval_seconds = scraper_config.get(
            "interval_seconds",
            scraper_config.get("min_cycle_gap", 600),  # fallback to min_cycle_gap
        )

        # Source-specific state
        self._current_index = 0
        self._region_to_task: dict[str, int] = {}
        self._last_scraped: dict[str, datetime] = {}
        self._cycle_count = 0

        logger.info(
            f"FR24MapTaskSource initialized with {len(self.regions)} regions "
            f"(max_priority={self.max_priority}, interval={self.interval_seconds}s)"
        )

    def _build_active_regions(self) -> dict[str, dict[str, Any]]:
        """Build the list of active regions based on configuration.

        Returns:
            Dictionary of region name -> config.
        """
        active = {}

        for name, region_config in GLOBAL_REGIONS.items():
            # Skip if specific regions are configured and this isn't one
            if self.enabled_regions and name not in self.enabled_regions:
                continue

            # Skip if priority is too low
            if region_config.get("priority", 1) > self.max_priority:
                continue

            # Skip ocean regions if not enabled
            if not self.include_oceans and "ocean" in name.lower():
                continue
            if not self.include_oceans and "pacific" in name.lower():
                continue
            if not self.include_oceans and "atlantic" in name.lower():
                continue
            if not self.include_oceans and "arctic" in name.lower():
                continue

            active[name] = region_config

        # Sort by priority
        sorted_regions = dict(sorted(active.items(), key=lambda x: x[1].get("priority", 1)))

        return sorted_regions

    def _build_custom_regions(
        self, custom_regions: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Build regions from custom config format.

        Args:
            custom_regions: List of region configs from YAML.

        Returns:
            Dictionary of region name -> config.
        """
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
        """Get pending tasks by cycling through regions.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of ScraperTask objects.
        """
        if not self.regions:
            logger.warning("No regions configured for fr24_map")
            return []

        tasks: list[ScraperTask] = []
        region_names = list(self.regions.keys())
        now = datetime.now(UTC)

        with self._lock:
            checked = 0
            while len(tasks) < limit and checked < len(region_names):
                # Wrap around
                if self._current_index >= len(region_names):
                    self._current_index = 0
                    self._cycle_count += 1
                    logger.info(f"FR24MapTaskSource completed cycle {self._cycle_count}")

                region_name = region_names[self._current_index]
                self._current_index += 1
                checked += 1

                # Skip if already being processed
                if region_name in self._region_to_task:
                    continue

                # Skip if scraped too recently
                last_time = self._last_scraped.get(region_name)
                if last_time:
                    elapsed = (now - last_time).total_seconds()
                    if elapsed < self.interval_seconds:
                        continue

                # Get region config
                region_config = self.regions[region_name]

                # Create task
                task = self._create_task(
                    task_key=region_name,
                    payload={
                        "lat": region_config["lat"],
                        "lon": region_config["lon"],
                        "zoom": region_config.get("zoom", 5),
                    },
                )

                self._region_to_task[region_name] = task.id
                tasks.append(task)

        if tasks:
            logger.info(
                f"FR24MapTaskSource returned {len(tasks)} tasks: {[t.task_key for t in tasks]}"
            )

        return tasks

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        """Clean up region mapping and update last_scraped on completion."""
        with self._lock:
            if task.task_key:
                self._region_to_task.pop(task.task_key, None)
                self._last_scraped[task.task_key] = datetime.now(UTC)

        aircraft_count = 0
        if result:
            aircraft_count = result.get("aircraft_count", 0)
        logger.info(f"Task {task.id} ({task.task_key}) completed, found {aircraft_count} aircraft")

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        """Clean up region mapping on failure."""
        with self._lock:
            if task.task_key:
                self._region_to_task.pop(task.task_key, None)

        logger.warning(f"Task {task.id} ({task.task_key}) failed: {error}")

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        """Clean up region mapping and update last_scraped on no-data."""
        with self._lock:
            if task.task_key:
                self._region_to_task.pop(task.task_key, None)
                # Still update last_scraped to avoid immediate retry
                self._last_scraped[task.task_key] = datetime.now(UTC)

        logger.info(f"Task {task.id} ({task.task_key}) no data: {reason}")

    def get_stats(self) -> dict[str, Any]:
        """Get source statistics.

        Returns:
            Dictionary with statistics.
        """
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
        """Get list of all configured regions.

        Returns:
            List of region configs with names.
        """
        return [{"name": name, **config} for name, config in self.regions.items()]
