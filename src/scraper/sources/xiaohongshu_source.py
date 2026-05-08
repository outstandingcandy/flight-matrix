"""
Xiaohongshu local task sources.

Provides tasks by polling database tables for authors that need scraping.
"""

import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.scraper.local_task_source import BaseTaskSource
from src.scraper.models import ScraperTask

logger = logging.getLogger("scraper.sources.xiaohongshu")


class XiaohongshuAuthorSource(BaseTaskSource):
    """Local task source for Xiaohongshu note scraper.

    Polls xiaohongshu_authors table for authors whose notes
    haven't been scraped recently.

    Attributes:
        task_type: Always "xiaohongshu".
        scrape_interval_days: Days between re-scraping author's notes.
        batch_size: Number of authors to fetch per poll.
    """

    def __init__(
        self,
        database_url: str,
        config: dict[str, Any],
    ) -> None:
        """Initialize the Xiaohongshu author task source.

        Args:
            database_url: PostgreSQL connection URL.
            config: Full configuration dictionary.
        """
        super().__init__(task_type="xiaohongshu", max_attempts=3)

        self.database_url = database_url

        # Get config from scraper section
        xhs_config = config.get("scraper", {}).get("scrapers", {}).get("xiaohongshu", {})
        self.scrape_interval_days = xhs_config.get("scrape_interval_days", 7)
        self.batch_size = xhs_config.get("batch_size", 50)
        self.max_notes = xhs_config.get("max_notes", 50)

        self.engine = create_engine(database_url, echo=False, pool_pre_ping=True)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        # Source-specific mapping: user_id -> task_id
        self._user_id_to_task: dict[str, int] = {}

        logger.info(
            f"XiaohongshuAuthorSource initialized "
            f"(scrape_interval={self.scrape_interval_days}d, batch_size={self.batch_size})"
        )

    def _get_session(self) -> Any:
        """Get a database session."""
        return self.SessionLocal()

    def _get_pending_authors(self, limit: int) -> list[dict[str, Any]]:
        """Get authors that need their notes scraped.

        Excludes authors who have been scraped recently (within scrape_interval_days).

        Args:
            limit: Maximum authors to return.

        Returns:
            List of author dictionaries with user_id, red_id, and nickname.
        """
        session = self._get_session()
        try:
            result = session.execute(
                text("""
                    SELECT a.user_id, a.red_id, a.nickname
                    FROM xiaohongshu_authors a
                    WHERE a.user_id NOT IN (
                        SELECT DISTINCT author_id
                        FROM xiaohongshu_notes
                        WHERE scraped_at > NOW() - make_interval(days => :interval_days)
                        AND author_id IS NOT NULL
                    )
                    ORDER BY a.follower_count DESC NULLS LAST, a.scraped_at ASC
                    LIMIT :limit
                """),
                {"interval_days": self.scrape_interval_days, "limit": limit * 2},
            )

            authors = []
            for row in result:
                authors.append(
                    {
                        "user_id": row[0],
                        "red_id": row[1],
                        "nickname": row[2],
                    }
                )
                if len(authors) >= limit:
                    break

            return authors

        except Exception as e:
            logger.error(f"Error fetching pending authors: {e}")
            return []
        finally:
            session.close()

    def get_pending_tasks(self, limit: int = 10) -> list[ScraperTask]:
        """Get pending tasks by fetching from xiaohongshu_authors.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of ScraperTask objects.
        """
        # Get pending authors
        authors = self._get_pending_authors(limit + len(self._active_tasks))

        tasks: list[ScraperTask] = []
        with self._lock:
            for author in authors:
                if len(tasks) >= limit:
                    break

                user_id = author["user_id"]

                # Skip if already being processed
                if user_id in self._user_id_to_task:
                    continue

                # Create task using base class helper
                task = self._create_task(
                    task_key=user_id,
                    payload={
                        "max_notes": self.max_notes,
                        "red_id": author.get("red_id"),
                        "nickname": author.get("nickname"),
                    },
                )

                self._user_id_to_task[user_id] = task.id
                tasks.append(task)

        if tasks:
            logger.info(
                f"XiaohongshuAuthorSource returned {len(tasks)} tasks: "
                f"{[t.task_key for t in tasks]}"
            )

        return tasks

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        """Clean up user_id mapping after task completion."""
        with self._lock:
            if task.task_key:
                self._user_id_to_task.pop(task.task_key, None)
        logger.info(f"Task {task.id} ({task.task_key}) completed")

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        """Clean up user_id mapping after task failure."""
        with self._lock:
            if task.task_key:
                self._user_id_to_task.pop(task.task_key, None)

        if retry and task.attempts < self._max_attempts:
            logger.warning(
                f"Task {task.id} ({task.task_key}) failed "
                f"(attempt {task.attempts}/{self._max_attempts}): {error}"
            )
        else:
            logger.error(
                f"Task {task.id} ({task.task_key}) permanently failed "
                f"after {task.attempts} attempts: {error}"
            )

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        """Clean up user_id mapping after no-data."""
        with self._lock:
            if task.task_key:
                self._user_id_to_task.pop(task.task_key, None)
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
                text("""
                    SELECT COUNT(*)
                    FROM xiaohongshu_authors a
                    WHERE a.user_id NOT IN (
                        SELECT DISTINCT author_id
                        FROM xiaohongshu_notes
                        WHERE scraped_at > NOW() - make_interval(days => :interval_days)
                        AND author_id IS NOT NULL
                    )
                """),
                {"interval_days": self.scrape_interval_days},
            )
            pending_count = result.fetchone()[0]
        except Exception as e:
            logger.error(f"Error getting pending count: {e}")
            pending_count = 0
        finally:
            session.close()

        stats = super().get_stats()
        stats.update(
            {
                "total_pending": pending_count,
                "total_processing": stats.pop("active"),
                "total_completed": stats.pop("completed"),
                "total_no_data": stats.pop("no_data"),
                "total_failed": stats.pop("failed"),
            }
        )
        return stats


class XiaohongshuRegistrationSource(BaseTaskSource):
    """Task source that generates search tasks from high-attention aircraft registrations.

    Polls note_aircraft_analysis table for registrations with high attention scores
    and creates search author tasks.

    Attributes:
        task_type: Always "xiaohongshu_search_author".
        min_attention_level: Minimum attention level to include (default: "high").
        batch_size: Number of registrations to fetch per poll.
    """

    def __init__(
        self,
        database_url: str,
        config: dict[str, Any],
    ) -> None:
        """Initialize the registration source.

        Args:
            database_url: PostgreSQL connection URL.
            config: Full configuration dictionary.
        """
        super().__init__(task_type="xiaohongshu_search_author", max_attempts=3)

        self.database_url = database_url

        # Get config
        xhs_config = (
            config.get("scraper", {}).get("scrapers", {}).get("xiaohongshu_search_author", {})
        )
        self.min_attention_level = xhs_config.get("min_attention_level", "high")
        self.batch_size = xhs_config.get("batch_size", 20)
        self.max_results = xhs_config.get("max_results", 20)
        self.search_interval_days = xhs_config.get("search_interval_days", 30)

        self.engine = create_engine(database_url, echo=False, pool_pre_ping=True)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        # Source-specific mapping: registration -> task_id
        self._registration_to_task: dict[str, int] = {}

        # Track searched registrations to avoid duplicates within session
        self._searched_registrations: set[str] = set()

        logger.info(
            f"XiaohongshuRegistrationSource initialized "
            f"(attention_level={self.min_attention_level}, batch_size={self.batch_size})"
        )

    def _get_session(self) -> Any:
        """Get a database session."""
        return self.SessionLocal()

    def _get_high_attention_registrations(self, limit: int) -> list[str]:
        """Get high-attention registrations from note analysis.

        Args:
            limit: Maximum registrations to return.

        Returns:
            List of registration strings.
        """
        session = self._get_session()
        try:
            # Get unique registrations from high-attention notes
            result = session.execute(
                text("""
                    SELECT DISTINCT jsonb_array_elements_text(registrations) as registration
                    FROM note_aircraft_analysis
                    WHERE attention_level = :attention_level
                    AND registrations IS NOT NULL
                    AND jsonb_array_length(registrations) > 0
                    ORDER BY registration
                    LIMIT :limit
                """),
                {"attention_level": self.min_attention_level, "limit": limit * 2},
            )

            registrations = []
            for row in result:
                reg = row[0]
                if reg and reg not in self._searched_registrations:
                    registrations.append(reg)
                    if len(registrations) >= limit:
                        break

            return registrations

        except Exception as e:
            logger.error(f"Error fetching high-attention registrations: {e}")
            return []
        finally:
            session.close()

    def get_pending_tasks(self, limit: int = 10) -> list[ScraperTask]:
        """Get pending tasks for high-attention registrations.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of ScraperTask objects.
        """
        registrations = self._get_high_attention_registrations(limit + len(self._active_tasks))

        tasks: list[ScraperTask] = []
        with self._lock:
            for reg in registrations:
                if len(tasks) >= limit:
                    break

                if reg in self._registration_to_task:
                    continue

                # Create task using base class helper
                task = self._create_task(
                    task_key=reg,
                    payload={"max_results": self.max_results},
                )

                self._registration_to_task[reg] = task.id
                self._searched_registrations.add(reg)
                tasks.append(task)

        if tasks:
            logger.info(
                f"XiaohongshuRegistrationSource returned {len(tasks)} tasks: "
                f"{[t.task_key for t in tasks]}"
            )

        return tasks

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        """Clean up registration mapping after task completion."""
        with self._lock:
            if task.task_key:
                self._registration_to_task.pop(task.task_key, None)
        logger.info(f"Task {task.id} ({task.task_key}) completed")

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        """Clean up registration mapping after task failure."""
        with self._lock:
            if task.task_key:
                self._registration_to_task.pop(task.task_key, None)

        if retry and task.attempts < self._max_attempts:
            logger.warning(
                f"Task {task.id} ({task.task_key}) failed "
                f"(attempt {task.attempts}/{self._max_attempts}): {error}"
            )
        else:
            logger.error(f"Task {task.id} ({task.task_key}) permanently failed: {error}")

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        """Clean up registration mapping after no-data."""
        with self._lock:
            if task.task_key:
                self._registration_to_task.pop(task.task_key, None)
        logger.info(f"Task {task.id} ({task.task_key}) marked as no_data: {reason}")

    def get_stats(self) -> dict[str, Any]:
        """Get source statistics."""
        session = self._get_session()
        try:
            result = session.execute(
                text("""
                    SELECT COUNT(DISTINCT jsonb_array_elements_text(registrations))
                    FROM note_aircraft_analysis
                    WHERE attention_level = :attention_level
                    AND registrations IS NOT NULL
                    AND jsonb_array_length(registrations) > 0
                """),
                {"attention_level": self.min_attention_level},
            )
            total_count = result.fetchone()[0]
        except Exception as e:
            logger.error(f"Error getting registration count: {e}")
            total_count = 0
        finally:
            session.close()

        stats = super().get_stats()

        with self._lock:
            searched_count = len(self._searched_registrations)

        stats.update(
            {
                "total_registrations": total_count,
                "total_searched": searched_count,
                "total_processing": stats.pop("active"),
                "total_completed": stats.pop("completed"),
                "total_no_data": stats.pop("no_data"),
                "total_failed": stats.pop("failed"),
            }
        )
        return stats
