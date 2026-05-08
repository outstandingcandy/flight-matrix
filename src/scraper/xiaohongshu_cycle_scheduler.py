"""
Xiaohongshu Closed-Loop Cycle Scheduler.

Orchestrates the complete aircraft discovery workflow:
1. Get trending aircraft from aircraft_attention_aggregate
2. Search Xiaohongshu to discover authors
3. Scrape author notes
4. Analyze notes to extract aircraft registrations
5. Update attention aggregate -> close the loop

Usage:
    # Start new cycle
    uv run python -m src.scraper.xiaohongshu_cycle_scheduler --config config/config.yaml

    # Resume specific cycle
    uv run python -m src.scraper.xiaohongshu_cycle_scheduler --resume cycle_2026-03-04

    # Show cycle status
    uv run python -m src.scraper.xiaohongshu_cycle_scheduler --status

    # Run single phase (debug)
    uv run python -m src.scraper.xiaohongshu_cycle_scheduler --step search_authors
"""

import argparse
import json
import logging
import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("scraper.xiaohongshu_cycle")


class CyclePhase(str, Enum):
    """Cycle phases."""

    GET_TRENDING_AIRCRAFT = "get_trending_aircraft"
    SEARCH_AUTHORS = "search_authors"
    SCRAPE_NOTES = "scrape_notes"
    ANALYZE_NOTES = "analyze_notes"
    COMPLETE = "complete"


class XiaohongshuCycleScheduler:
    """Xiaohongshu closed-loop cycle scheduler.

    Coordinates the complete aircraft discovery workflow:
    - Phase 1: Get trending aircraft from attention aggregate
    - Phase 2: Search Xiaohongshu for authors posting about those aircraft
    - Phase 3: Scrape notes from discovered authors
    - Phase 4: Analyze notes to extract registrations and update aggregate
    - Phase 5: Complete cycle, wait for next cycle

    State is persisted to database for recovery after failures.
    """

    def __init__(self, config_file: str = "config.yaml") -> None:
        """Initialize the cycle scheduler.

        Args:
            config_file: Path to YAML configuration file.
        """
        self.config_file = config_file
        self.yaml_config = YAMLConfig(config_file)

        # Load cycle configuration
        self._load_config()

        # Initialize database
        self._init_database()

        # Current cycle state
        self._cycle_id: str | None = None
        self._state: dict[str, Any] = {}

        logger.info(
            f"XiaohongshuCycleScheduler initialized "
            f"(top_aircraft={self.top_aircraft_count}, "
            f"top_authors={self.top_author_count})"
        )

    def _load_config(self) -> None:
        """Load cycle scheduler configuration."""
        config = (
            self.yaml_config.config.get("scraper", {})
            .get("scrapers", {})
            .get("xiaohongshu_cycle", {})
        )

        # Phase 1: Trending aircraft
        self.top_aircraft_count = config.get("top_aircraft_count", 10)
        self.min_trending_score = config.get("min_trending_score", 10)

        # Phase 2: Author search
        self.search_max_results = config.get("search_max_results", 10)
        self.search_interval_days = config.get("search_interval_days", 7)

        # Phase 3: Note scraping
        self.top_author_count = config.get("top_author_count", 10)
        self.max_notes_per_author = config.get("max_notes_per_author", 100)
        self.author_scrape_interval_days = config.get("author_scrape_interval_days", 7)

        # Phase 4: Note analysis
        self.analysis_batch_size = config.get("analysis_batch_size", 50)

        # Cycle control
        self.min_cycle_gap = config.get("min_cycle_gap", 604800)  # 7 days

        # Scraper configurations for direct invocation
        self.xhs_config = (
            self.yaml_config.config.get("scraper", {}).get("scrapers", {}).get("xiaohongshu", {})
        )

    def _init_database(self) -> None:
        """Initialize database connection."""
        db_config = self.yaml_config.get_database_config()
        self.database_url = db_config["url"]

        connect_args = {"connect_timeout": 10}
        self.engine = create_engine(
            self.database_url,
            echo=False,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        self._ensure_tables_exist()

    def _ensure_tables_exist(self) -> None:
        """Create cycle state table if it doesn't exist."""
        try:
            with self.engine.connect() as conn:
                conn.execute(
                    text("""
                    CREATE TABLE IF NOT EXISTS xiaohongshu_cycle_state (
                        id BIGSERIAL PRIMARY KEY,
                        cycle_id VARCHAR(50) UNIQUE NOT NULL,
                        status VARCHAR(20) DEFAULT 'running',
                        current_phase VARCHAR(50),

                        -- Phase 1: Trending aircraft
                        target_registrations JSONB,
                        searched_registrations JSONB DEFAULT '[]',

                        -- Phase 2: Author search
                        discovered_authors JSONB DEFAULT '[]',

                        -- Phase 3: Note scraping
                        target_authors JSONB,
                        scraped_authors JSONB DEFAULT '[]',

                        -- Phase 4: Note analysis
                        notes_to_analyze INTEGER DEFAULT 0,
                        notes_analyzed INTEGER DEFAULT 0,

                        -- Statistics
                        total_authors_found INTEGER DEFAULT 0,
                        total_notes_scraped INTEGER DEFAULT 0,
                        total_registrations_found INTEGER DEFAULT 0,

                        -- Timestamps
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                        -- Error info
                        error_message TEXT
                    )
                """)
                )

                # Create index on cycle_id
                conn.execute(
                    text("""
                    CREATE INDEX IF NOT EXISTS idx_xiaohongshu_cycle_state_cycle_id
                    ON xiaohongshu_cycle_state(cycle_id)
                """)
                )

                conn.commit()
                logger.info("Cycle state table ensured")

        except SQLAlchemyError as e:
            logger.error(f"Failed to create cycle state table: {e}")

    def _get_session(self) -> Any:
        """Get a database session."""
        return self.SessionLocal()

    # -------------------------------------------------------------------------
    # State Management
    # -------------------------------------------------------------------------

    def _load_state(self, cycle_id: str) -> dict[str, Any] | None:
        """Load cycle state from database.

        Args:
            cycle_id: Cycle identifier.

        Returns:
            State dictionary or None if not found.
        """
        session = self._get_session()
        try:
            result = session.execute(
                text("""
                    SELECT cycle_id, status, current_phase,
                           target_registrations, searched_registrations,
                           discovered_authors, target_authors, scraped_authors,
                           notes_to_analyze, notes_analyzed,
                           total_authors_found, total_notes_scraped,
                           total_registrations_found, error_message
                    FROM xiaohongshu_cycle_state
                    WHERE cycle_id = :cycle_id
                """),
                {"cycle_id": cycle_id},
            )
            row = result.fetchone()
            if row:
                return {
                    "cycle_id": row[0],
                    "status": row[1],
                    "current_phase": row[2],
                    "target_registrations": row[3] or [],
                    "searched_registrations": row[4] or [],
                    "discovered_authors": row[5] or [],
                    "target_authors": row[6] or [],
                    "scraped_authors": row[7] or [],
                    "notes_to_analyze": row[8] or 0,
                    "notes_analyzed": row[9] or 0,
                    "total_authors_found": row[10] or 0,
                    "total_notes_scraped": row[11] or 0,
                    "total_registrations_found": row[12] or 0,
                    "error_message": row[13],
                }
            return None
        finally:
            session.close()

    def _save_state(self) -> None:
        """Save current state to database."""
        if not self._cycle_id:
            return

        session = self._get_session()
        try:
            session.execute(
                text("""
                    UPDATE xiaohongshu_cycle_state
                    SET status = :status,
                        current_phase = :current_phase,
                        target_registrations = :target_registrations,
                        searched_registrations = :searched_registrations,
                        discovered_authors = :discovered_authors,
                        target_authors = :target_authors,
                        scraped_authors = :scraped_authors,
                        notes_to_analyze = :notes_to_analyze,
                        notes_analyzed = :notes_analyzed,
                        total_authors_found = :total_authors_found,
                        total_notes_scraped = :total_notes_scraped,
                        total_registrations_found = :total_registrations_found,
                        error_message = :error_message,
                        last_updated = :last_updated,
                        completed_at = :completed_at
                    WHERE cycle_id = :cycle_id
                """),
                {
                    "cycle_id": self._cycle_id,
                    "status": self._state.get("status", "running"),
                    "current_phase": self._state.get("current_phase"),
                    "target_registrations": json.dumps(self._state.get("target_registrations", [])),
                    "searched_registrations": json.dumps(
                        self._state.get("searched_registrations", [])
                    ),
                    "discovered_authors": json.dumps(self._state.get("discovered_authors", [])),
                    "target_authors": json.dumps(self._state.get("target_authors", [])),
                    "scraped_authors": json.dumps(self._state.get("scraped_authors", [])),
                    "notes_to_analyze": self._state.get("notes_to_analyze", 0),
                    "notes_analyzed": self._state.get("notes_analyzed", 0),
                    "total_authors_found": self._state.get("total_authors_found", 0),
                    "total_notes_scraped": self._state.get("total_notes_scraped", 0),
                    "total_registrations_found": self._state.get("total_registrations_found", 0),
                    "error_message": self._state.get("error_message"),
                    "last_updated": datetime.now(UTC),
                    "completed_at": self._state.get("completed_at"),
                },
            )
            session.commit()
            logger.debug(f"Saved state for cycle {self._cycle_id}")
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Failed to save state: {e}")
        finally:
            session.close()

    def _create_cycle(self, cycle_id: str) -> bool:
        """Create a new cycle in database.

        Args:
            cycle_id: Unique cycle identifier.

        Returns:
            True if created successfully.
        """
        session = self._get_session()
        try:
            session.execute(
                text("""
                    INSERT INTO xiaohongshu_cycle_state (
                        cycle_id, status, current_phase, started_at
                    ) VALUES (
                        :cycle_id, 'running', :phase, :started_at
                    )
                """),
                {
                    "cycle_id": cycle_id,
                    "phase": CyclePhase.GET_TRENDING_AIRCRAFT.value,
                    "started_at": datetime.now(UTC),
                },
            )
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Failed to create cycle: {e}")
            return False
        finally:
            session.close()

    # -------------------------------------------------------------------------
    # Cycle Execution
    # -------------------------------------------------------------------------

    def run_cycle(self, cycle_id: str | None = None) -> dict[str, Any]:
        """Run or resume a cycle.

        Args:
            cycle_id: Optional cycle ID to resume. Creates new if None.

        Returns:
            Cycle result statistics.
        """
        # Generate or load cycle ID
        if cycle_id:
            self._cycle_id = cycle_id
            state = self._load_state(cycle_id)
            if not state:
                logger.error(f"Cycle {cycle_id} not found")
                return {"success": False, "error": f"Cycle {cycle_id} not found"}
            self._state = state
            logger.info(f"Resuming cycle {cycle_id} from phase {state.get('current_phase')}")
        else:
            self._cycle_id = f"cycle_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
            if not self._create_cycle(self._cycle_id):
                return {"success": False, "error": "Failed to create cycle"}
            self._state = {
                "status": "running",
                "current_phase": CyclePhase.GET_TRENDING_AIRCRAFT.value,
                "target_registrations": [],
                "searched_registrations": [],
                "discovered_authors": [],
                "target_authors": [],
                "scraped_authors": [],
            }
            logger.info(f"Starting new cycle {self._cycle_id}")

        # Execute phases
        try:
            phase = CyclePhase(self._state.get("current_phase"))

            if phase == CyclePhase.GET_TRENDING_AIRCRAFT:
                self._phase_get_trending_aircraft()
                phase = CyclePhase.SEARCH_AUTHORS

            if phase == CyclePhase.SEARCH_AUTHORS:
                self._phase_search_authors()
                phase = CyclePhase.SCRAPE_NOTES

            if phase == CyclePhase.SCRAPE_NOTES:
                self._phase_scrape_notes()
                phase = CyclePhase.ANALYZE_NOTES

            if phase == CyclePhase.ANALYZE_NOTES:
                self._phase_analyze_notes()
                phase = CyclePhase.COMPLETE

            if phase == CyclePhase.COMPLETE:
                self._state["status"] = "completed"
                self._state["current_phase"] = CyclePhase.COMPLETE.value
                self._state["completed_at"] = datetime.now(UTC)
                self._save_state()
                logger.info(f"Cycle {self._cycle_id} completed successfully")

            return {
                "success": True,
                "cycle_id": self._cycle_id,
                "status": self._state.get("status"),
                "total_authors_found": self._state.get("total_authors_found", 0),
                "total_notes_scraped": self._state.get("total_notes_scraped", 0),
                "total_registrations_found": self._state.get("total_registrations_found", 0),
            }

        except Exception as e:
            logger.error(f"Cycle {self._cycle_id} failed: {e}")
            self._state["status"] = "failed"
            self._state["error_message"] = str(e)
            self._save_state()
            return {
                "success": False,
                "cycle_id": self._cycle_id,
                "error": str(e),
            }

    def run_single_phase(self, phase_name: str) -> dict[str, Any]:
        """Run a single phase for debugging.

        Args:
            phase_name: Phase name (e.g., "search_authors").

        Returns:
            Phase execution result.
        """
        # Create a temporary cycle for single phase execution
        self._cycle_id = f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._state = {
            "status": "running",
            "current_phase": phase_name,
            "target_registrations": [],
            "searched_registrations": [],
            "discovered_authors": [],
            "target_authors": [],
            "scraped_authors": [],
        }

        try:
            phase = CyclePhase(phase_name)

            if phase == CyclePhase.GET_TRENDING_AIRCRAFT:
                self._phase_get_trending_aircraft()
            elif phase == CyclePhase.SEARCH_AUTHORS:
                # Need to load target registrations first
                self._phase_get_trending_aircraft()
                self._phase_search_authors()
            elif phase == CyclePhase.SCRAPE_NOTES:
                self._phase_scrape_notes()
            elif phase == CyclePhase.ANALYZE_NOTES:
                self._phase_analyze_notes()

            return {
                "success": True,
                "phase": phase_name,
                "state": self._state,
            }

        except Exception as e:
            logger.error(f"Phase {phase_name} failed: {e}")
            return {
                "success": False,
                "phase": phase_name,
                "error": str(e),
            }

    # -------------------------------------------------------------------------
    # Phase Implementations
    # -------------------------------------------------------------------------

    def _phase_get_trending_aircraft(self) -> None:
        """Phase 1: Get trending aircraft from attention aggregate.

        Applies cross-cycle deduplication: skips registrations searched in recent
        cycles and fills in with the next trending aircraft from the list.
        """
        logger.info("Phase 1: Getting trending aircraft")
        self._state["current_phase"] = CyclePhase.GET_TRENDING_AIRCRAFT.value
        self._save_state()

        # Get recently searched registrations for cross-cycle deduplication
        recently_searched = self._get_recently_searched_registrations(self.search_interval_days)

        session = self._get_session()
        try:
            # Query more than needed to allow for filtering
            # Fetch 50x the target count to ensure we have enough after filtering
            # (most top aircraft may already be searched in recent cycles)
            query_limit = self.top_aircraft_count * 50

            result = session.execute(
                text("""
                    SELECT registration, trending_score
                    FROM aircraft_attention_aggregate
                    WHERE trending_score >= :min_score
                    ORDER BY trending_score DESC
                    LIMIT :query_limit
                """),
                {
                    "min_score": self.min_trending_score,
                    "query_limit": query_limit,
                },
            )

            registrations = []
            skipped_count = 0
            for row in result:
                reg = row[0]
                score = float(row[1]) if row[1] else 0

                # Skip if searched in recent cycles
                if reg in recently_searched:
                    logger.debug(f"Skipping recently searched: {reg} (score={score:.2f})")
                    skipped_count += 1
                    continue

                registrations.append({"registration": reg, "score": score})

                # Stop once we have enough
                if len(registrations) >= self.top_aircraft_count:
                    break

            if skipped_count > 0:
                logger.info(
                    f"Skipped {skipped_count} recently searched registrations, "
                    f"filled with next trending aircraft"
                )

            self._state["target_registrations"] = registrations
            logger.info(f"Phase 1 complete: {len(registrations)} trending aircraft selected")

            # Log top registrations
            for reg in registrations[:5]:
                logger.info(f"  - {reg['registration']}: score={reg['score']:.2f}")

        finally:
            session.close()

        self._save_state()

    def _phase_search_authors(self) -> None:
        """Phase 2: Search Xiaohongshu for authors posting about aircraft."""
        logger.info("Phase 2: Searching for authors")
        self._state["current_phase"] = CyclePhase.SEARCH_AUTHORS.value
        self._save_state()

        # Lazy import to avoid circular imports
        from resilient_scraper.scrapers.xiaohongshu import XiaohongshuSearchAuthorScraper

        from src.scraper.models import ScraperTask

        target_regs = self._state.get("target_registrations", [])
        searched_regs = set(self._state.get("searched_registrations", []))
        # Note: Cross-cycle deduplication is now done in Phase 1 (get_trending_aircraft)
        # searched_regs here only tracks within-cycle progress for resume capability

        # Build scraper config
        scraper_config = {
            **self.xhs_config,
            "database_url": self.database_url,
            "max_results": self.search_max_results,
        }

        scraper = XiaohongshuSearchAuthorScraper(scraper_config)
        scraper.setup()

        # Check if using existing browser (no pool needed)
        use_existing_browser = self.xhs_config.get("use_existing_browser", False)
        browser_pool = None

        if not use_existing_browser:
            # Create browser pool for this phase
            from src.scraper.browser_pool import BrowserPool

            drission_options = self.yaml_config.config.get("scraper", {}).get("drission_page", {})
            browser_pool = BrowserPool(size=1, drission_options=drission_options)
            browser_pool.initialize()
            logger.info("Using BrowserPool for scraping")
        else:
            logger.info("Using existing browser connection")

        try:
            for reg_info in target_regs:
                reg = reg_info["registration"]

                # Skip if already searched in this cycle (for resume)
                if reg in searched_regs:
                    logger.debug(f"Skipping already searched in this cycle: {reg}")
                    continue

                logger.info(f"Searching for authors related to: {reg}")

                # Create task
                task = ScraperTask(
                    id=0,
                    task_type="xiaohongshu_search_author",
                    task_key=reg,
                    payload={"max_results": self.search_max_results},
                )

                browser = None
                try:
                    # Get browser from pool or use existing browser via scraper
                    if browser_pool:
                        browser = browser_pool.acquire(timeout=60)

                    # scraper._prepare_browser() handles existing browser connection
                    result = scraper.scrape(task, browser)

                    if result.success:
                        # Track discovered authors
                        for author in result.authors:
                            self._state.setdefault("discovered_authors", []).append(
                                {
                                    "user_id": author.user_id,
                                    "nickname": author.nickname,
                                    "discovered_from": reg,
                                }
                            )

                        self._state["total_authors_found"] = self._state.get(
                            "total_authors_found", 0
                        ) + len(result.authors)

                        logger.info(f"Found {len(result.authors)} authors for {reg}")
                    else:
                        logger.warning(f"Search failed for {reg}: {result.error}")

                except Exception as e:
                    logger.error(f"Error searching {reg}: {e}")

                finally:
                    if browser_pool and browser:
                        browser_pool.release(browser)

                # Mark as searched (even if failed, to avoid infinite retry)
                searched_regs.add(reg)
                self._state["searched_registrations"] = list(searched_regs)
                self._save_state()

                # Delay between searches
                time.sleep(5)

        finally:
            if browser_pool:
                browser_pool.shutdown()

        logger.info(
            f"Phase 2 complete: {self._state.get('total_authors_found', 0)} "
            f"total authors discovered"
        )

    def _phase_scrape_notes(self) -> None:
        """Phase 3: Scrape notes from discovered authors."""
        logger.info("Phase 3: Scraping author notes")
        self._state["current_phase"] = CyclePhase.SCRAPE_NOTES.value
        self._save_state()

        # Get top authors by discovery count
        target_authors = self._get_top_authors_by_discovery()
        self._state["target_authors"] = target_authors

        scraped_authors = set(self._state.get("scraped_authors", []))

        # Lazy import
        from resilient_scraper.scrapers.xiaohongshu import XiaohongshuScraper

        from src.scraper.models import ScraperTask

        # Build scraper config
        scraper_config = {
            **self.xhs_config,
            "database_url": self.database_url,
            "max_notes": self.max_notes_per_author,
        }

        scraper = XiaohongshuScraper(scraper_config)
        scraper.setup()

        # Check if using existing browser (no pool needed)
        use_existing_browser = self.xhs_config.get("use_existing_browser", False)
        browser_pool = None

        if not use_existing_browser:
            # Create browser pool for this phase
            from src.scraper.browser_pool import BrowserPool

            drission_options = self.yaml_config.config.get("scraper", {}).get("drission_page", {})
            browser_pool = BrowserPool(size=1, drission_options=drission_options)
            browser_pool.initialize()
            logger.info("Using BrowserPool for scraping")
        else:
            logger.info("Using existing browser connection")

        try:
            for author_info in target_authors:
                user_id = author_info["user_id"]
                nickname = author_info.get("nickname") or user_id

                # Skip if already scraped
                if user_id in scraped_authors:
                    logger.debug(f"Skipping already scraped author: {nickname}")
                    continue

                logger.info(f"Scraping notes for author: {nickname}")

                # Create task with nickname in payload for better logging
                task = ScraperTask(
                    id=0,
                    task_type="xiaohongshu",
                    task_key=user_id,
                    payload={
                        "max_notes": self.max_notes_per_author,
                        "nickname": nickname,
                    },
                )

                browser = None
                try:
                    # Get browser from pool or use existing browser via scraper
                    if browser_pool:
                        browser = browser_pool.acquire(timeout=60)

                    # scraper._prepare_browser() handles existing browser connection
                    result = scraper.scrape(task, browser)

                    if result.success:
                        notes_count = len(result.notes) if result.notes else 0
                        self._state["total_notes_scraped"] = (
                            self._state.get("total_notes_scraped", 0) + notes_count
                        )
                        logger.info(f"Scraped {notes_count} notes from {nickname}")
                    else:
                        logger.warning(f"Failed to scrape {nickname}: {result.error}")

                except Exception as e:
                    logger.error(f"Error scraping {nickname}: {e}")

                finally:
                    if browser_pool and browser:
                        browser_pool.release(browser)

                # Mark as scraped
                scraped_authors.add(user_id)
                self._state["scraped_authors"] = list(scraped_authors)
                self._save_state()

                # Delay between authors
                time.sleep(10)

        finally:
            if browser_pool:
                browser_pool.shutdown()

        logger.info(f"Phase 3 complete: {self._state.get('total_notes_scraped', 0)} notes scraped")

    def _phase_analyze_notes(self) -> None:
        """Phase 4: Analyze notes to extract registrations."""
        logger.info("Phase 4: Analyzing notes")
        self._state["current_phase"] = CyclePhase.ANALYZE_NOTES.value
        self._save_state()

        # Lazy import
        from src.services.note_analysis_service import NoteAnalysisService

        service = NoteAnalysisService(self.config_file)

        # Get count of pending notes
        pending_stats = service.get_stats()
        pending_count = pending_stats.get("pending_notes", 0)
        self._state["notes_to_analyze"] = pending_count

        logger.info(f"Found {pending_count} notes to analyze")

        total_analyzed = 0
        total_registrations: set[str] = set()

        while True:
            # Process batch
            result = service.process_batch(
                source_type="xiaohongshu",
                limit=self.analysis_batch_size,
            )

            processed = result.get("processed", 0)
            if processed == 0:
                break

            total_analyzed += result.get("success", 0)
            total_registrations.update(str(r) for r in range(result.get("registrations_found", 0)))

            self._state["notes_analyzed"] = total_analyzed
            self._save_state()

            logger.info(
                f"Analyzed batch: {result.get('success', 0)} successful, "
                f"{result.get('registrations_found', 0)} registrations found"
            )

            # Short delay between batches
            time.sleep(1)

        self._state["total_registrations_found"] = len(total_registrations)

        logger.info(
            f"Phase 4 complete: {total_analyzed} notes analyzed, "
            f"{len(total_registrations)} unique registrations found"
        )

    def _get_top_authors_by_discovery(self) -> list[dict[str, Any]]:
        """Get authors with highest discovery count.

        Returns:
            List of author dictionaries sorted by discovery frequency.
        """
        session = self._get_session()
        try:
            # Select top authors by discovery count
            # Note deduplication is handled during scraping (skip_existing_notes)
            result = session.execute(
                text("""
                    SELECT
                        a.user_id,
                        a.nickname,
                        a.follower_count,
                        a.note_count,
                        COALESCE(array_length(a.discovered_from_registrations, 1), 0)
                            AS discovery_count
                    FROM xiaohongshu_authors a
                    WHERE a.discovered_from_registrations IS NOT NULL
                      AND array_length(a.discovered_from_registrations, 1) > 0
                    ORDER BY
                        discovery_count DESC,
                        a.follower_count DESC NULLS LAST
                    LIMIT :top_count
                """),
                {"top_count": self.top_author_count},
            )

            authors = []
            for row in result:
                authors.append(
                    {
                        "user_id": row[0],
                        "nickname": row[1],
                        "follower_count": row[2],
                        "note_count": row[3],
                        "discovery_count": row[4],
                    }
                )

            logger.info(f"Selected {len(authors)} top authors by discovery count")
            for author in authors[:5]:
                logger.info(
                    f"  - {author['nickname']} ({author['user_id']}): "
                    f"discovered {author['discovery_count']} times"
                )

            return authors

        finally:
            session.close()

    def _get_recently_searched_registrations(self, days: int) -> set[str]:
        """Get registrations that have been searched in recent cycles.

        Args:
            days: Number of days to look back.

        Returns:
            Set of registration numbers that were searched recently.
        """
        session = self._get_session()
        try:
            result = session.execute(
                text("""
                    SELECT searched_registrations
                    FROM xiaohongshu_cycle_state
                    WHERE started_at > NOW() - make_interval(days => :days)
                      AND searched_registrations IS NOT NULL
                      AND jsonb_array_length(searched_registrations) > 0
                """),
                {"days": days},
            )

            all_searched: set[str] = set()
            for row in result:
                searched_list = row[0] or []
                all_searched.update(searched_list)

            logger.info(f"Found {len(all_searched)} registrations searched in the last {days} days")
            return all_searched

        except Exception as e:
            logger.warning(f"Failed to get recently searched registrations: {e}")
            return set()
        finally:
            session.close()

    # -------------------------------------------------------------------------
    # Status Methods
    # -------------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Get status of all cycles.

        Returns:
            Status dictionary with cycle information.
        """
        session = self._get_session()
        try:
            # Get recent cycles
            result = session.execute(
                text("""
                    SELECT cycle_id, status, current_phase,
                           jsonb_array_length(target_registrations) as target_count,
                           jsonb_array_length(searched_registrations) as searched_count,
                           total_authors_found, total_notes_scraped,
                           total_registrations_found, started_at, completed_at
                    FROM xiaohongshu_cycle_state
                    ORDER BY started_at DESC
                    LIMIT 10
                """),
            )

            cycles = []
            for row in result:
                cycles.append(
                    {
                        "cycle_id": row[0],
                        "status": row[1],
                        "current_phase": row[2],
                        "target_registrations": row[3] or 0,
                        "searched_registrations": row[4] or 0,
                        "total_authors_found": row[5] or 0,
                        "total_notes_scraped": row[6] or 0,
                        "total_registrations_found": row[7] or 0,
                        "started_at": row[8].isoformat() if row[8] else None,
                        "completed_at": row[9].isoformat() if row[9] else None,
                    }
                )

            # Get last completed cycle
            last_completed = None
            for cycle in cycles:
                if cycle["status"] == "completed":
                    last_completed = cycle
                    break

            return {
                "cycles": cycles,
                "last_completed": last_completed,
                "running_cycle": next((c for c in cycles if c["status"] == "running"), None),
            }

        finally:
            session.close()


# =============================================================================
# CLI Interface
# =============================================================================


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Xiaohongshu closed-loop cycle scheduler")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Configuration file path",
    )
    parser.add_argument(
        "--resume",
        metavar="CYCLE_ID",
        help="Resume a specific cycle by ID",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show cycle status",
    )
    parser.add_argument(
        "--step",
        metavar="PHASE",
        choices=[p.value for p in CyclePhase],
        help="Run a single phase (for debugging)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Initialize scheduler
    scheduler = XiaohongshuCycleScheduler(args.config)

    if args.status:
        # Show status
        status = scheduler.get_status()
        print("\n=== Xiaohongshu Cycle Status ===\n")

        if status.get("running_cycle"):
            running = status["running_cycle"]
            print(f"Running: {running['cycle_id']}")
            print(f"  Phase: {running['current_phase']}")
            print(
                f"  Progress: {running['searched_registrations']}/"
                f"{running['target_registrations']} registrations searched"
            )
            print(f"  Authors found: {running['total_authors_found']}")
            print(f"  Notes scraped: {running['total_notes_scraped']}")
            print()

        if status.get("last_completed"):
            last = status["last_completed"]
            print(f"Last completed: {last['cycle_id']}")
            print(f"  Completed at: {last['completed_at']}")
            print(f"  Authors found: {last['total_authors_found']}")
            print(f"  Notes scraped: {last['total_notes_scraped']}")
            print(f"  Registrations found: {last['total_registrations_found']}")
            print()

        print("Recent cycles:")
        for cycle in status.get("cycles", [])[:5]:
            print(f"  - {cycle['cycle_id']}: {cycle['status']} (phase: {cycle['current_phase']})")

    elif args.step:
        # Run single phase
        print(f"\nRunning single phase: {args.step}\n")
        result = scheduler.run_single_phase(args.step)
        print(f"\nResult: {json.dumps(result, indent=2, default=str)}")

    else:
        # Run or resume cycle
        result = scheduler.run_cycle(args.resume)
        print("\n=== Cycle Result ===\n")
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
