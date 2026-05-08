"""
JetPhotos local task source.

Provides tasks by polling the aircraft_static_info table for registrations
that need images downloaded.
"""

import logging
import re
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.scraper.local_task_source import BaseTaskSource
from src.scraper.models import ScraperTask

logger = logging.getLogger("scraper.sources.jetphotos")


class JetPhotosTaskSource(BaseTaskSource):
    """Local task source for JetPhotos scraper.

    Polls aircraft_static_info table for registrations where
    images_downloaded is NULL or false.

    Attributes:
        task_type: Always "jetphotos".
        max_images: Maximum images to download per aircraft.
        batch_size: Number of registrations to fetch per poll.
    """

    # Regex pattern for valid aircraft registrations
    # Supports: XX-XXXXX, NXXXXX, XX+XX (German military like 10+01)
    VALID_REG_PATTERN = re.compile(r"^[A-Z0-9]{1,2}[-+][A-Z0-9]{2,5}$|^N\d{1,5}[A-Z]{0,2}$")

    def __init__(
        self,
        database_url: str,
        config: dict[str, Any],
    ) -> None:
        """Initialize the JetPhotos task source.

        Args:
            database_url: PostgreSQL connection URL.
            config: Full configuration dictionary.
        """
        super().__init__(task_type="jetphotos", max_attempts=3)
        self.database_url = database_url

        # Get max_images from image_download config
        image_config = config.get("image_download", {})
        self.max_images = image_config.get("max_images_per_aircraft", 3)
        self.batch_size = 100

        self.engine = create_engine(database_url, echo=False, pool_pre_ping=True)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        # Source-specific mapping
        self._registration_to_task: dict[str, int] = {}  # registration -> task_id

        logger.info(
            f"JetPhotosTaskSource initialized (max_images={self.max_images}, "
            f"batch_size={self.batch_size})"
        )

    def _get_session(self) -> Any:
        """Get a database session."""
        return self.SessionLocal()

    def _is_valid_registration(self, registration: str) -> bool:
        """Check if registration is valid (filters out test data and garbage).

        Args:
            registration: Aircraft registration string.

        Returns:
            True if registration appears to be valid.
        """
        if not registration or len(registration) < 2 or len(registration) > 10:
            return False

        reg_upper = registration.strip().upper()

        # Filter out known test/invalid patterns
        invalid_patterns = [
            "TEST",
            "BLOCKED",
            "UNKNOWN",
            "NONE",
            "N/A",
            "00000",
            "XXXXX",
        ]
        for pattern in invalid_patterns:
            if pattern in reg_upper:
                return False

        # Must match common registration patterns
        return bool(self.VALID_REG_PATTERN.match(reg_upper))

    def _get_pending_registrations(self, limit: int) -> list[str]:
        """Get registrations that need images downloaded.

        Args:
            limit: Maximum registrations to return.

        Returns:
            List of registration strings.
        """
        session = self._get_session()
        try:
            result = session.execute(
                text(
                    """
                    SELECT registration
                    FROM aircraft_static_info
                    WHERE registration IS NOT NULL
                    AND registration != ''
                    AND (images_downloaded IS NULL OR images_downloaded = false)
                    ORDER BY last_updated DESC
                    LIMIT :limit
                """
                ),
                {"limit": limit * 2},  # Fetch extra to account for filtering
            )

            registrations = []
            for row in result:
                reg = row[0]
                if self._is_valid_registration(reg):
                    registrations.append(reg.strip().upper())
                    if len(registrations) >= limit:
                        break

            return registrations

        except Exception as e:
            logger.error(f"Error fetching pending registrations: {e}")
            return []
        finally:
            session.close()

    def get_pending_tasks(self, limit: int = 10) -> list[ScraperTask]:
        """Get pending tasks by fetching from aircraft_static_info.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of ScraperTask objects.
        """
        # Get pending registrations
        registrations = self._get_pending_registrations(limit + len(self._active_tasks))

        tasks: list[ScraperTask] = []
        with self._lock:
            for reg in registrations:
                if len(tasks) >= limit:
                    break

                # Skip if already being processed
                if reg in self._registration_to_task:
                    continue

                # Create task
                task = self._create_task(
                    task_key=reg,
                    payload={"max_images": self.max_images},
                )

                self._registration_to_task[reg] = task.id
                tasks.append(task)

        if tasks:
            logger.info(
                f"JetPhotosTaskSource returned {len(tasks)} tasks: {[t.task_key for t in tasks]}"
            )

        return tasks

    def _update_images_downloaded(self, registration: str, downloaded: bool) -> None:
        """Update images_downloaded flag in aircraft_static_info.

        Args:
            registration: Aircraft registration.
            downloaded: Whether images were downloaded.
        """
        session = self._get_session()
        try:
            session.execute(
                text(
                    """
                    UPDATE aircraft_static_info
                    SET images_downloaded = :downloaded,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE registration = :registration
                """
                ),
                {"registration": registration, "downloaded": downloaded},
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating images_downloaded for {registration}: {e}")
        finally:
            session.close()

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        """Clean up registration mapping and update database on completion."""
        with self._lock:
            if task.task_key:
                self._registration_to_task.pop(task.task_key, None)

        # Update database
        self._update_images_downloaded(task.task_key, downloaded=True)
        logger.info(f"Task {task.id} ({task.task_key}) completed")

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        """Clean up registration mapping and handle retry logic on failure."""
        with self._lock:
            if task.task_key:
                self._registration_to_task.pop(task.task_key, None)

        # Check attempts from the task that was tracked
        attempts = task.attempts if task.attempts else 1

        if retry and attempts < 3:
            # Keep images_downloaded = false to allow retry on next poll
            logger.warning(
                f"Task {task.id} ({task.task_key}) failed (attempt {attempts}/3): {error}"
            )
        else:
            # Max retries exceeded - mark as downloaded to stop retrying
            self._update_images_downloaded(task.task_key, downloaded=True)
            logger.error(
                f"Task {task.id} ({task.task_key}) permanently failed "
                f"after {attempts} attempts: {error}"
            )

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        """Clean up registration mapping and mark as downloaded on no-data."""
        with self._lock:
            if task.task_key:
                self._registration_to_task.pop(task.task_key, None)

        # Still mark as downloaded to avoid retrying
        self._update_images_downloaded(task.task_key, downloaded=True)
        logger.info(f"Task {task.id} ({task.task_key}) marked as no_data: {reason}")

    def get_stats(self) -> dict[str, Any]:
        """Get source statistics.

        Returns:
            Dictionary with statistics.
        """
        # Count pending in database
        session = self._get_session()
        try:
            result = session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM aircraft_static_info
                    WHERE registration IS NOT NULL
                    AND registration != ''
                    AND (images_downloaded IS NULL OR images_downloaded = false)
                """
                )
            )
            pending_count = result.fetchone()[0]
        except Exception as e:
            logger.error(f"Error getting pending count: {e}")
            pending_count = 0
        finally:
            session.close()

        stats = super().get_stats()
        stats["total_pending"] = pending_count
        return stats
