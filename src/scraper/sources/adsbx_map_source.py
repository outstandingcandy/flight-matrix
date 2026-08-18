"""
ADS-B Exchange globe task source.

Generates region-cycling tasks to scrape military aircraft positions from
ADS-B Exchange. Region centers are shared with the FR24 map source — the
same centers work for both feeds since neither is specific to a provider.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from src.scraper.local_task_source import BaseTaskSource
from src.scraper.models import ScraperTask
from src.scraper.sources.fr24_map_source import GLOBAL_REGIONS

logger = logging.getLogger("scraper.sources.adsbx_map")


class ADSBxMapTaskSource(BaseTaskSource):
    """Cycles through global regions and emits adsbx_map tasks.

    Reads the same YAML keys as fr24_map (``global_coverage``,
    ``enabled_regions``, ``max_priority``, ``include_oceans``,
    ``interval_seconds``/``min_cycle_gap``, ``regions``) so the two sources
    can be tuned with matching knobs. Each emitted task's payload carries
    ``dbFlags=1`` so the scraper queries ADSBx's military-only feed.
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
        self.include_oceans = scraper_config.get("include_oceans", False)
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
        active: dict[str, dict[str, Any]] = {}
        for name, region_config in GLOBAL_REGIONS.items():
            if self.enabled_regions and name not in self.enabled_regions:
                continue
            if region_config.get("priority", 1) > self.max_priority:
                continue
            if not self.include_oceans and any(
                tag in name.lower() for tag in ("ocean", "pacific", "atlantic", "arctic")
            ):
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
