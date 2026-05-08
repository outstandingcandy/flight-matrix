"""
FR24 airport local task source.

Provides tasks by reading airport list from configuration file.
Each airport becomes a task to scrape arrivals or departures.
"""

import logging
from typing import Any

from src.scraper.local_task_source import BaseTaskSource
from src.scraper.models import ScraperTask

logger = logging.getLogger("scraper.sources.fr24_airport")


class FR24AirportTaskSource(BaseTaskSource):
    """Local task source for FR24 airport arrivals/departures scraper.

    Reads airport list from config and cycles through them.
    Unlike JetPhotos, FR24 tasks are ephemeral - we scrape each airport
    repeatedly to get updated flight data.

    Attributes:
        task_type: "fr24_arrivals", "fr24_departures", or "fr24_airport".
        airports: List of airport codes to scrape.
        max_clicks: Maximum "Load more" clicks per airport.
    """

    def __init__(
        self,
        task_type: str,
        config: dict[str, Any],
        database_url: str,
    ) -> None:
        """Initialize the FR24 task source.

        Args:
            task_type: "fr24_arrivals", "fr24_departures", or "fr24_airport".
            config: Full configuration dictionary.
            database_url: Database URL (stored for potential future use).
        """
        if task_type not in ("fr24_arrivals", "fr24_departures", "fr24_airport"):
            raise ValueError(f"Invalid task_type: {task_type}")

        super().__init__(task_type, max_attempts=3)
        self.database_url = database_url

        # Get configuration for this task type
        scraper_config = config.get("scraper", {}).get("scrapers", {}).get(task_type, {})
        self.airports: list[str] = [
            airport.upper().strip()
            for airport in scraper_config.get("airports", [])
            if airport and airport.strip()
        ]
        self.max_clicks = scraper_config.get("max_load_more_clicks", 10)
        self.load_more_delay = scraper_config.get("load_more_delay", 2.0)

        # Cycling state (source-specific)
        self._current_index = 0
        self._airport_to_task: dict[str, int] = {}  # airport -> task_id
        self._cycle_count = 0  # How many complete cycles through airport list

        logger.info(
            f"FR24AirportTaskSource({task_type}) initialized with {len(self.airports)} airports: "
            f"{self.airports}, max_clicks={self.max_clicks}"
        )

    def get_pending_tasks(self, limit: int = 10) -> list[ScraperTask]:
        """Get pending tasks by cycling through airport list.

        Each call returns the next batch of airports to scrape.
        When we reach the end of the list, we start over.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of ScraperTask objects.
        """
        if not self.airports:
            logger.warning(f"No airports configured for {self._task_type}")
            return []

        tasks = []
        with self._lock:
            for _ in range(limit):
                # Check if we've completed a cycle
                if self._current_index >= len(self.airports):
                    self._current_index = 0
                    self._cycle_count += 1
                    logger.info(
                        f"FR24AirportTaskSource({self._task_type}) completed cycle "
                        f"{self._cycle_count}, starting new cycle"
                    )
                    # Don't immediately start a new cycle - let the worker rest
                    break

                airport = self.airports[self._current_index]
                self._current_index += 1

                # Skip if already being processed
                if airport in self._airport_to_task:
                    continue

                # Create task using base class helper
                task = self._create_task(
                    task_key=airport,
                    payload={
                        "max_clicks": self.max_clicks,
                        "load_more_delay": self.load_more_delay,
                    },
                )

                self._airport_to_task[airport] = task.id
                tasks.append(task)

        if tasks:
            logger.info(
                f"FR24AirportTaskSource({self._task_type}) returned {len(tasks)} tasks: "
                f"{[t.task_key for t in tasks]}"
            )

        return tasks

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        """Hook called after task is marked completed.

        Cleans up airport-to-task mapping and logs flight count.

        Args:
            task: The completed task.
            result: Optional result data.
        """
        with self._lock:
            self._airport_to_task.pop(task.task_key, None)

        flights_count = 0
        if result:
            flights_count = result.get("flights_count", 0)
        logger.info(f"Task {task.id} ({task.task_key}) completed, scraped {flights_count} flights")

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        """Hook called after task is marked failed.

        Cleans up airport-to-task mapping so the airport can be retried
        on the next cycle.

        Args:
            task: The failed task.
            error: Error message.
            retry: Whether to allow retry (ignored for FR24).
        """
        with self._lock:
            self._airport_to_task.pop(task.task_key, None)

        logger.warning(f"Task {task.id} ({task.task_key}) failed: {error}")

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        """Hook called after task is marked no-data.

        Cleans up airport-to-task mapping so the airport can be retried
        on the next cycle.

        Args:
            task: The task with no data.
            reason: Reason why there's no data.
        """
        with self._lock:
            self._airport_to_task.pop(task.task_key, None)

        logger.info(f"Task {task.id} ({task.task_key}) no data: {reason}")

    def get_stats(self) -> dict[str, Any]:
        """Get source statistics.

        Returns:
            Dictionary with common and FR24-specific statistics.
        """
        stats = super().get_stats()
        with self._lock:
            remaining = len(self.airports) - self._current_index
        stats.update(
            {
                "total_airports": len(self.airports),
                "remaining_in_cycle": remaining,
                "cycles_completed": self._cycle_count,
            }
        )
        return stats
