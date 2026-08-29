"""
PostgreSQL-backed task queue with distributed locking.

Uses SELECT FOR UPDATE SKIP LOCKED for safe concurrent task claiming.
"""

import logging
import random
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.scraper.models import ScraperTask, TaskStatus, WorkerStatus
from src.web.time_helpers import naive_utc_now

logger = logging.getLogger("scraper.task_queue")


class TaskQueue:
    """PostgreSQL-backed task queue for distributed scraping.

    Features:
        - Atomic task claiming with SELECT FOR UPDATE SKIP LOCKED
        - Worker registration and heartbeat tracking
        - Automatic retry scheduling with exponential backoff
        - Task result storage

    Usage:
        queue = TaskQueue(database_url)
        queue.ensure_tables_exist()

        # Add tasks
        queue.add_task("jetphotos", "N12345", payload={"max_images": 3})

        # Claim and process
        tasks = queue.claim_tasks(worker_id="worker-1", limit=5)
        for task in tasks:
            # process task
            queue.complete_task(task.id, result={"images": [...]})
    """

    def __init__(self, database_url: str) -> None:
        """Initialize the task queue.

        Args:
            database_url: Postgres (prod) or SQLite (local dev) URL.
        """
        self.database_url = database_url
        self.engine = create_engine(database_url, echo=False, pool_pre_ping=True)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.is_postgres = self.engine.dialect.name == "postgresql"
        logger.info(f"TaskQueue initialized with database (dialect={self.engine.dialect.name})")

    def get_session(self) -> Any:
        """Get a database session."""
        return self.SessionLocal()

    def ensure_tables_exist(self) -> None:
        """Create scraper tables if they don't exist.

        DDL is dialect-aware — Postgres gets BIGSERIAL/JSONB/TIMESTAMPTZ, SQLite
        gets INTEGER AUTOINCREMENT/TEXT/DATETIME so integer primary keys
        auto-populate and JSON payloads round-trip correctly on both backends.
        """
        session = self.get_session()
        # Dialect-specific type aliases
        if self.is_postgres:
            pk = "BIGSERIAL PRIMARY KEY"
            json_t = "JSONB"
            json_default = "JSONB NOT NULL DEFAULT '{}'"
            ts = "TIMESTAMPTZ"
            ts_default = "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ts_opt_default = "TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP"
            bigint = "BIGINT"
        else:
            pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
            json_t = "TEXT"
            json_default = "TEXT NOT NULL DEFAULT '{}'"
            ts = "DATETIME"
            ts_default = "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ts_opt_default = "DATETIME DEFAULT CURRENT_TIMESTAMP"
            bigint = "INTEGER"

        try:
            # Check if tables exist
            try:
                session.execute(text("SELECT 1 FROM scraper_tasks LIMIT 1"))
                logger.debug("Scraper tables already exist")
                return
            except Exception:
                pass

            # Create scraper_tasks table
            session.execute(
                text(
                    f"""
                CREATE TABLE IF NOT EXISTS scraper_tasks (
                    id {pk},
                    task_type VARCHAR(50) NOT NULL,
                    task_key VARCHAR(500) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    payload {json_default},
                    claimed_by VARCHAR(100),
                    claimed_at {ts},
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT,
                    result {json_t},
                    scheduled_for {ts_default},
                    created_at {ts_default},
                    completed_at {ts},
                    heartbeat_at {ts}
                )
            """
                )
            )

            # Create indexes
            session.execute(
                text("CREATE INDEX IF NOT EXISTS idx_scraper_tasks_status ON scraper_tasks(status)")
            )
            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_type_key "
                    "ON scraper_tasks(task_type, task_key)"
                )
            )
            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_scheduled "
                    "ON scraper_tasks(scheduled_for)"
                )
            )
            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_priority "
                    "ON scraper_tasks(priority DESC, scheduled_for)"
                )
            )

            # Create scraper_workers table
            session.execute(
                text(
                    f"""
                CREATE TABLE IF NOT EXISTS scraper_workers (
                    id {pk},
                    worker_id VARCHAR(100) UNIQUE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    last_heartbeat {ts_default},
                    tasks_completed INTEGER NOT NULL DEFAULT 0,
                    current_task_id {bigint},
                    started_at {ts_opt_default},
                    metadata {json_default}
                )
            """
                )
            )

            # Create scraper_results table for execution log
            session.execute(
                text(
                    f"""
                CREATE TABLE IF NOT EXISTS scraper_results (
                    id {pk},
                    task_id {bigint} REFERENCES scraper_tasks(id),
                    worker_id VARCHAR(100) NOT NULL,
                    success BOOLEAN NOT NULL,
                    duration_seconds NUMERIC(10, 3),
                    result {json_t},
                    error TEXT,
                    created_at {ts_default}
                )
            """
                )
            )

            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_scraper_results_task "
                    "ON scraper_results(task_id)"
                )
            )

            session.commit()
            logger.info("Scraper tables created successfully")

        except Exception as e:
            session.rollback()
            logger.error(f"Error creating scraper tables: {e}")
            raise
        finally:
            session.close()

    def add_task(
        self,
        task_type: str,
        task_key: str,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        scheduled_for: datetime | None = None,
    ) -> int | None:
        """Add a new task to the queue.

        Args:
            task_type: Type of scraper to use.
            task_key: Unique identifier for the target.
            payload: Additional task data.
            priority: Task priority (higher = more urgent).
            max_attempts: Maximum retry attempts.
            scheduled_for: Earliest processing time.

        Returns:
            Task ID if created, None if duplicate exists.
        """
        session = self.get_session()
        try:
            # Check for existing pending/processing task
            existing = session.execute(
                text(
                    """
                    SELECT id FROM scraper_tasks
                    WHERE task_type = :type AND task_key = :key
                    AND status IN ('pending', 'claimed', 'processing')
                """
                ),
                {"type": task_type, "key": task_key},
            ).fetchone()

            if existing:
                logger.debug(f"Task already exists for {task_type}:{task_key}, skipping")
                return None

            import json

            payload_expr = "CAST(:payload AS jsonb)" if self.is_postgres else ":payload"
            insert_sql = f"""
                INSERT INTO scraper_tasks
                (task_type, task_key, payload, priority, max_attempts, scheduled_for)
                VALUES (:type, :key, {payload_expr}, :priority, :max_attempts, :scheduled)
            """
            params = {
                "type": task_type,
                "key": task_key,
                "payload": json.dumps(payload) if payload else "{}",
                "priority": priority,
                "max_attempts": max_attempts,
                "scheduled": scheduled_for or naive_utc_now(),
            }

            if self.is_postgres:
                result = session.execute(text(insert_sql + " RETURNING id"), params)
                task_id = result.fetchone()[0]
            else:
                # SQLite: RETURNING exists on modern SQLite but not via all drivers.
                # Use lastrowid from the compiled cursor instead.
                cursor_result = session.execute(text(insert_sql), params)
                task_id = cursor_result.lastrowid
            session.commit()
            logger.info(f"Added task {task_id}: {task_type}:{task_key}")
            return task_id

        except Exception as e:
            session.rollback()
            logger.error(f"Error adding task: {e}")
            return None
        finally:
            session.close()

    def add_tasks_bulk(
        self,
        tasks: list[dict[str, Any]],
    ) -> int:
        """Add multiple tasks in bulk.

        Args:
            tasks: List of task dictionaries with keys:
                   task_type, task_key, payload (optional),
                   priority (optional), max_attempts (optional)

        Returns:
            Number of tasks added.
        """
        import json

        session = self.get_session()
        added = 0
        try:
            for task_data in tasks:
                task_type = task_data["task_type"]
                task_key = task_data["task_key"]
                payload = task_data.get("payload", {})
                priority = task_data.get("priority", 0)
                max_attempts = task_data.get("max_attempts", 3)

                # Use INSERT ... WHERE NOT EXISTS
                payload_expr = "CAST(:payload AS jsonb)" if self.is_postgres else ":payload"
                result = session.execute(
                    text(
                        f"""
                        INSERT INTO scraper_tasks
                        (task_type, task_key, payload, priority, max_attempts)
                        SELECT :type, :key, {payload_expr}, :priority, :max_attempts
                        WHERE NOT EXISTS (
                            SELECT 1 FROM scraper_tasks
                            WHERE task_type = :type AND task_key = :key
                            AND status IN ('pending', 'claimed', 'processing')
                        )
                    """
                    ),
                    {
                        "type": task_type,
                        "key": task_key,
                        "payload": json.dumps(payload) if payload else "{}",
                        "priority": priority,
                        "max_attempts": max_attempts,
                    },
                )
                if result.rowcount > 0:
                    added += 1

            session.commit()
            logger.info(f"Bulk added {added} tasks")
            return added

        except Exception as e:
            session.rollback()
            logger.error(f"Error bulk adding tasks: {e}")
            return 0
        finally:
            session.close()

    def claim_tasks(
        self,
        worker_id: str,
        task_types: list[str] | None = None,
        limit: int = 1,
        stale_timeout_minutes: int = 5,
        max_concurrent_by_type: dict[str, int] | None = None,
    ) -> list[ScraperTask]:
        """Atomically claim tasks for processing.

        Uses SELECT FOR UPDATE SKIP LOCKED to prevent duplicate claiming.
        Also resets stale tasks (no heartbeat for stale_timeout_minutes) back to pending.

        Args:
            worker_id: ID of the claiming worker.
            task_types: Optional filter for specific task types.
            limit: Maximum number of tasks to claim.
            stale_timeout_minutes: Reset tasks with no heartbeat for this long.
            max_concurrent_by_type: Optional dict mapping task_type to max concurrent
                workers. -1 means no limit. Task types that have reached their
                limit will be excluded from claiming.

        Returns:
            List of claimed tasks.
        """
        session = self.get_session()
        try:
            # First, reset stale tasks (no heartbeat for stale_timeout_minutes).
            # Compute the cutoff in Python so the SQL is dialect-agnostic —
            # Postgres's `NOW() - INTERVAL` syntax does not work on SQLite.
            stale_cutoff = naive_utc_now() - timedelta(minutes=stale_timeout_minutes)
            stale_result = session.execute(
                text(
                    """
                    UPDATE scraper_tasks
                    SET status = 'pending',
                        claimed_by = NULL,
                        claimed_at = NULL,
                        heartbeat_at = NULL
                    WHERE status IN ('claimed', 'processing')
                    AND heartbeat_at IS NOT NULL
                    AND heartbeat_at < :stale_cutoff
                    """
                ),
                {"stale_cutoff": stale_cutoff},
            )
            if stale_result.rowcount > 0:
                logger.info(f"Reset {stale_result.rowcount} stale tasks (no heartbeat)")

            # Filter out task types that have reached their max concurrent limit
            available_task_types = task_types
            if max_concurrent_by_type and task_types:
                # Get current processing count by task type
                result = session.execute(
                    text(
                        """
                        SELECT task_type, COUNT(*) as count
                        FROM scraper_tasks
                        WHERE status IN ('claimed', 'processing')
                        GROUP BY task_type
                        """
                    )
                )
                processing_counts = {row.task_type: row.count for row in result}

                # Filter task types based on concurrency limits
                available_task_types = []
                for tt in task_types:
                    max_concurrent = max_concurrent_by_type.get(tt, -1)
                    current_count = processing_counts.get(tt, 0)

                    if max_concurrent == -1 or current_count < max_concurrent:
                        available_task_types.append(tt)
                    else:
                        logger.debug(
                            f"Task type {tt} at max concurrency ({current_count}/{max_concurrent})"
                        )

                if not available_task_types:
                    logger.debug("All task types at max concurrency, waiting...")
                    session.commit()
                    return []

            # Build type filter
            type_filter = ""
            params: dict[str, Any] = {
                "worker_id": worker_id,
                "limit": limit,
                "now": naive_utc_now(),
            }

            if available_task_types:
                placeholders = ", ".join([f":type_{i}" for i in range(len(available_task_types))])
                type_filter = f"AND task_type IN ({placeholders})"
                for i, tt in enumerate(available_task_types):
                    params[f"type_{i}"] = tt

            # Atomic claim. Postgres uses `FOR UPDATE SKIP LOCKED` + `UPDATE …
            # WHERE id IN (…) RETURNING …` in a single round-trip. SQLite
            # doesn't support SKIP LOCKED (single-writer anyway) and its
            # RETURNING (since 3.35) can't be used inside the IN subquery
            # pattern safely, so we fall back to a SELECT → UPDATE → SELECT
            # flow in a single transaction.
            tasks = []
            if self.is_postgres:
                result = session.execute(
                    text(
                        f"""
                        UPDATE scraper_tasks
                        SET status = 'claimed',
                            claimed_by = :worker_id,
                            claimed_at = :now,
                            heartbeat_at = :now,
                            attempts = attempts + 1
                        WHERE id IN (
                            SELECT id FROM scraper_tasks
                            WHERE status = 'pending'
                            AND scheduled_for <= :now
                            {type_filter}
                            ORDER BY priority DESC, scheduled_for ASC
                            LIMIT :limit
                            FOR UPDATE SKIP LOCKED
                        )
                        RETURNING id, task_type, task_key, payload, attempts, max_attempts,
                                  priority, created_at, scheduled_for
                    """
                    ),
                    params,
                )
                for row in result:
                    tasks.append(
                        ScraperTask(
                            id=row.id,
                            task_type=row.task_type,
                            task_key=row.task_key,
                            status=TaskStatus.CLAIMED,
                            payload=row.payload or {},
                            attempts=row.attempts,
                            max_attempts=row.max_attempts,
                            priority=row.priority,
                            claimed_by=worker_id,
                            claimed_at=naive_utc_now(),
                            created_at=row.created_at,
                            scheduled_for=row.scheduled_for,
                        )
                    )
            else:
                # SQLite path: pick candidate ids, update them, then read back.
                candidate_rows = session.execute(
                    text(
                        f"""
                        SELECT id FROM scraper_tasks
                        WHERE status = 'pending'
                        AND scheduled_for <= :now
                        {type_filter}
                        ORDER BY priority DESC, scheduled_for ASC
                        LIMIT :limit
                        """
                    ),
                    params,
                ).fetchall()
                candidate_ids = [r.id for r in candidate_rows]

                if candidate_ids:
                    id_placeholders = ", ".join([f":id_{i}" for i in range(len(candidate_ids))])
                    upd_params = {
                        "worker_id": worker_id,
                        "now": params["now"],
                        **{f"id_{i}": cid for i, cid in enumerate(candidate_ids)},
                    }
                    session.execute(
                        text(
                            f"""
                            UPDATE scraper_tasks
                            SET status = 'claimed',
                                claimed_by = :worker_id,
                                claimed_at = :now,
                                heartbeat_at = :now,
                                attempts = attempts + 1
                            WHERE id IN ({id_placeholders})
                            """
                        ),
                        upd_params,
                    )
                    read_rows = session.execute(
                        text(
                            f"""
                            SELECT id, task_type, task_key, payload, attempts, max_attempts,
                                   priority, created_at, scheduled_for
                            FROM scraper_tasks
                            WHERE id IN ({id_placeholders})
                            """
                        ),
                        {f"id_{i}": cid for i, cid in enumerate(candidate_ids)},
                    ).fetchall()
                    # SQLite stores JSON as text — decode payload.
                    import json as _json

                    for row in read_rows:
                        payload = row.payload
                        if isinstance(payload, str):
                            try:
                                payload = _json.loads(payload)
                            except (ValueError, TypeError):
                                payload = {}
                        tasks.append(
                            ScraperTask(
                                id=row.id,
                                task_type=row.task_type,
                                task_key=row.task_key,
                                status=TaskStatus.CLAIMED,
                                payload=payload or {},
                                attempts=row.attempts,
                                max_attempts=row.max_attempts,
                                priority=row.priority,
                                claimed_by=worker_id,
                                claimed_at=naive_utc_now(),
                                created_at=row.created_at,
                                scheduled_for=row.scheduled_for,
                            )
                        )

            session.commit()

            if tasks:
                logger.info(
                    f"Worker {worker_id} claimed {len(tasks)} tasks: {[t.task_key for t in tasks]}"
                )

            return tasks

        except Exception as e:
            session.rollback()
            logger.error(f"Error claiming tasks: {e}")
            return []
        finally:
            session.close()

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
    ) -> None:
        """Update task status.

        Args:
            task_id: ID of the task.
            status: New status.
        """
        session = self.get_session()
        try:
            session.execute(
                text(
                    """
                    UPDATE scraper_tasks
                    SET status = :status,
                        heartbeat_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                """
                ),
                {"id": task_id, "status": status.value},
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating task status: {e}")
        finally:
            session.close()

    def update_heartbeat_at(self, task_id: int) -> None:
        """Update task heartbeat to indicate it's still being processed.

        Should be called periodically while processing a task to prevent
        the task from being considered stale and reset.

        Args:
            task_id: ID of the task.
        """
        session = self.get_session()
        try:
            session.execute(
                text(
                    """
                    UPDATE scraper_tasks
                    SET heartbeat_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                """
                ),
                {"id": task_id},
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating task heartbeat: {e}")
        finally:
            session.close()

    def complete_task(
        self,
        task_id: int,
        result: dict[str, Any] | None = None,
        worker_id: str | None = None,
        duration_seconds: float = 0.0,
    ) -> None:
        """Mark a task as completed.

        Args:
            task_id: ID of the completed task.
            result: Result data to store.
            worker_id: ID of the worker that completed it.
            duration_seconds: Time taken to process.
        """
        import json

        session = self.get_session()
        result_expr = "CAST(:result AS jsonb)" if self.is_postgres else ":result"
        try:
            session.execute(
                text(
                    f"""
                    UPDATE scraper_tasks
                    SET status = 'completed',
                        result = {result_expr},
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                """
                ),
                {
                    "id": task_id,
                    "result": json.dumps(result) if result else "{}",
                },
            )

            # Log to results table
            if worker_id:
                session.execute(
                    text(
                        f"""
                        INSERT INTO scraper_results
                        (task_id, worker_id, success, duration_seconds, result)
                        VALUES (:task_id, :worker_id, true, :duration, {result_expr})
                    """
                    ),
                    {
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "duration": duration_seconds,
                        "result": json.dumps(result) if result else "{}",
                    },
                )

            session.commit()
            logger.info(f"Task {task_id} completed")

        except Exception as e:
            session.rollback()
            logger.error(f"Error completing task: {e}")
        finally:
            session.close()

    def complete_task_no_data(
        self,
        task_id: int,
        reason: str = "No data found",
        worker_id: str | None = None,
        duration_seconds: float = 0.0,
    ) -> None:
        """Mark a task as no_data (target has no data, not a failure).

        This is used when the scrape was successful but the target has no data
        (e.g., no photos on JetPhotos for a registration).

        Args:
            task_id: ID of the task.
            reason: Reason why there's no data.
            worker_id: ID of the worker that processed it.
            duration_seconds: Time taken to process.
        """
        import json

        session = self.get_session()
        result_expr = "CAST(:result AS jsonb)" if self.is_postgres else ":result"
        try:
            result = {"no_data_reason": reason}
            session.execute(
                text(
                    f"""
                    UPDATE scraper_tasks
                    SET status = 'no_data',
                        result = {result_expr},
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                """
                ),
                {
                    "id": task_id,
                    "result": json.dumps(result),
                },
            )

            # Log to results table
            if worker_id:
                session.execute(
                    text(
                        f"""
                        INSERT INTO scraper_results
                        (task_id, worker_id, success, duration_seconds, result)
                        VALUES (:task_id, :worker_id, false, :duration, {result_expr})
                    """
                    ),
                    {
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "duration": duration_seconds,
                        "result": json.dumps(result),
                    },
                )

            session.commit()
            logger.info(f"Task {task_id} marked as no_data: {reason}")

        except Exception as e:
            session.rollback()
            logger.error(f"Error marking task as no_data: {e}")
        finally:
            session.close()

    def fail_task(
        self,
        task_id: int,
        error: str,
        worker_id: str | None = None,
        duration_seconds: float = 0.0,
        retry: bool = True,
    ) -> None:
        """Mark a task as failed.

        Args:
            task_id: ID of the failed task.
            error: Error message.
            worker_id: ID of the worker that processed it.
            duration_seconds: Time taken before failure.
            retry: Whether to schedule a retry.
        """
        session = self.get_session()
        try:
            # Get current attempt count
            task_row = session.execute(
                text("SELECT attempts, max_attempts FROM scraper_tasks WHERE id = :id"),
                {"id": task_id},
            ).fetchone()

            if not task_row:
                logger.warning(f"Task {task_id} not found for failure update")
                return

            attempts, max_attempts = task_row.attempts, task_row.max_attempts

            if retry and attempts < max_attempts:
                # Schedule retry with exponential backoff + jitter
                # Jitter prevents thundering herd when many tasks fail simultaneously
                base_backoff = min(2 ** (attempts - 1) * 5, 60)  # 5, 10, 20, 40, 60
                backoff_minutes = base_backoff * random.uniform(0.5, 1.5)
                retry_time = naive_utc_now() + timedelta(minutes=backoff_minutes)

                session.execute(
                    text(
                        """
                        UPDATE scraper_tasks
                        SET status = 'pending',
                            last_error = :error,
                            scheduled_for = :retry_time,
                            claimed_by = NULL,
                            claimed_at = NULL
                        WHERE id = :id
                    """
                    ),
                    {"id": task_id, "error": error, "retry_time": retry_time},
                )
                logger.info(
                    f"Task {task_id} scheduled for retry at {retry_time} "
                    f"(attempt {attempts}/{max_attempts})"
                )
            else:
                # Mark as permanently failed
                session.execute(
                    text(
                        """
                        UPDATE scraper_tasks
                        SET status = 'failed',
                            last_error = :error,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = :id
                    """
                    ),
                    {"id": task_id, "error": error},
                )
                logger.warning(f"Task {task_id} permanently failed after {attempts} attempts")

            # Log to results table
            if worker_id:
                session.execute(
                    text(
                        """
                        INSERT INTO scraper_results
                        (task_id, worker_id, success, duration_seconds, error)
                        VALUES (:task_id, :worker_id, false, :duration, :error)
                    """
                    ),
                    {
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "duration": duration_seconds,
                        "error": error,
                    },
                )

            session.commit()

        except Exception as e:
            session.rollback()
            logger.error(f"Error failing task: {e}")
        finally:
            session.close()

    def register_worker(
        self,
        worker_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a worker or update its heartbeat.

        Args:
            worker_id: Unique worker identifier.
            metadata: Optional worker metadata.
        """
        import json

        session = self.get_session()
        try:
            metadata_json = json.dumps(metadata) if metadata else "{}"
            # `CAST(... AS jsonb)` is Postgres-only; SQLite stores JSON as
            # TEXT so the raw string works in both places.
            metadata_cast = "CAST(:metadata AS jsonb)" if self.is_postgres else ":metadata"
            session.execute(
                text(
                    f"""
                    INSERT INTO scraper_workers (worker_id, metadata, last_heartbeat)
                    VALUES (:worker_id, {metadata_cast}, CURRENT_TIMESTAMP)
                    ON CONFLICT (worker_id) DO UPDATE SET
                        last_heartbeat = CURRENT_TIMESTAMP,
                        status = 'active',
                        metadata = COALESCE({metadata_cast}, scraper_workers.metadata)
                """
                ),
                {
                    "worker_id": worker_id,
                    "metadata": metadata_json,
                },
            )
            session.commit()
            logger.debug(f"Worker {worker_id} registered/heartbeat")

        except Exception as e:
            session.rollback()
            logger.error(f"Error registering worker: {e}")
        finally:
            session.close()

    def update_worker_heartbeat(
        self,
        worker_id: str,
        status: WorkerStatus = WorkerStatus.ACTIVE,
        current_task_id: int | None = None,
    ) -> None:
        """Update worker heartbeat and status.

        Uses UPSERT to ensure heartbeat is always recorded, even if worker
        record is missing or in an inconsistent state.

        Args:
            worker_id: Worker identifier.
            status: Current worker status.
            current_task_id: ID of task being processed.
        """
        session = self.get_session()
        try:
            # Use UPSERT to ensure heartbeat is always updated
            # This handles cases where worker record might be missing or stale
            result = session.execute(
                text(
                    """
                    INSERT INTO scraper_workers (worker_id, status, current_task_id, last_heartbeat)
                    VALUES (:worker_id, :status, :task_id, CURRENT_TIMESTAMP)
                    ON CONFLICT (worker_id) DO UPDATE SET
                        last_heartbeat = CURRENT_TIMESTAMP,
                        status = :status,
                        current_task_id = :task_id
                    """
                ),
                {
                    "worker_id": worker_id,
                    "status": status.value,
                    "task_id": current_task_id,
                },
            )
            session.commit()

            # Log if we had to insert (worker was missing)
            if result.rowcount == 1:
                logger.debug(f"Worker {worker_id} heartbeat updated")

        except Exception as e:
            session.rollback()
            logger.error(f"Error updating worker heartbeat: {e}")
        finally:
            session.close()

    def increment_worker_completed(self, worker_id: str) -> None:
        """Increment worker's completed task count.

        Args:
            worker_id: Worker identifier.
        """
        session = self.get_session()
        try:
            session.execute(
                text(
                    """
                    UPDATE scraper_workers
                    SET tasks_completed = tasks_completed + 1,
                        current_task_id = NULL
                    WHERE worker_id = :worker_id
                """
                ),
                {"worker_id": worker_id},
            )
            session.commit()

        except Exception as e:
            session.rollback()
            logger.error(f"Error incrementing worker completed count: {e}")
        finally:
            session.close()

    def deactivate_worker(self, worker_id: str) -> None:
        """Mark a worker as stopped.

        Args:
            worker_id: Worker identifier.
        """
        session = self.get_session()
        try:
            session.execute(
                text(
                    """
                    UPDATE scraper_workers
                    SET status = 'stopped',
                        current_task_id = NULL
                    WHERE worker_id = :worker_id
                """
                ),
                {"worker_id": worker_id},
            )
            session.commit()
            logger.info(f"Worker {worker_id} deactivated")

        except Exception as e:
            session.rollback()
            logger.error(f"Error deactivating worker: {e}")
        finally:
            session.close()

    def get_stats(self) -> dict[str, Any]:
        """Get queue statistics (alias for get_queue_stats).

        Returns:
            Dictionary with queue statistics.
        """
        return self.get_queue_stats()

    def get_queue_stats(self) -> dict[str, Any]:
        """Get queue statistics.

        Returns:
            Dictionary with queue statistics.
        """
        session = self.get_session()
        try:
            # Task counts by status
            result = session.execute(
                text(
                    """
                    SELECT status, COUNT(*) as count
                    FROM scraper_tasks
                    GROUP BY status
                """
                )
            )
            status_counts = {row.status: row.count for row in result}

            # Active workers
            result = session.execute(
                text(
                    """
                    SELECT COUNT(*) as count
                    FROM scraper_workers
                    WHERE status = 'active'
                    AND last_heartbeat > NOW() - INTERVAL '5 minutes'
                """
                )
            )
            active_workers = result.fetchone().count

            # Tasks by type
            result = session.execute(
                text(
                    """
                    SELECT task_type, COUNT(*) as count
                    FROM scraper_tasks
                    WHERE status = 'pending'
                    GROUP BY task_type
                """
                )
            )
            pending_by_type = {row.task_type: row.count for row in result}

            return {
                "status_counts": status_counts,
                "active_workers": active_workers,
                "pending_by_type": pending_by_type,
                "total_pending": status_counts.get("pending", 0),
                "total_processing": status_counts.get("claimed", 0)
                + status_counts.get("processing", 0),
                "total_completed": status_counts.get("completed", 0),
                "total_no_data": status_counts.get("no_data", 0),
                "total_failed": status_counts.get("failed", 0),
            }

        except Exception as e:
            logger.error(f"Error getting queue stats: {e}")
            return {}
        finally:
            session.close()

    def cleanup_stale_tasks(self, timeout_minutes: int = 30) -> int:
        """Reset stale claimed tasks back to pending.

        Args:
            timeout_minutes: Consider tasks stale after this many minutes.

        Returns:
            Number of tasks reset.
        """
        session = self.get_session()
        try:
            cutoff = naive_utc_now() - timedelta(minutes=timeout_minutes)
            result = session.execute(
                text(
                    """
                    UPDATE scraper_tasks
                    SET status = 'pending',
                        claimed_by = NULL,
                        claimed_at = NULL
                    WHERE status IN ('claimed', 'processing')
                    AND claimed_at < :cutoff
                    """
                ),
                {"cutoff": cutoff},
            )
            reset_count = result.rowcount
            session.commit()

            if reset_count > 0:
                logger.info(f"Reset {reset_count} stale tasks to pending")

            return reset_count

        except Exception as e:
            session.rollback()
            logger.error(f"Error cleaning up stale tasks: {e}")
            return 0
        finally:
            session.close()

    def cleanup_old_completed(self, days: int = 7) -> int:
        """Remove old completed/failed tasks.

        Args:
            days: Remove tasks completed more than this many days ago.

        Returns:
            Number of tasks removed.
        """
        session = self.get_session()
        try:
            cutoff = naive_utc_now() - timedelta(days=days)
            result = session.execute(
                text(
                    """
                    DELETE FROM scraper_tasks
                    WHERE status IN ('completed', 'failed')
                    AND completed_at < :cutoff
                    """
                ),
                {"cutoff": cutoff},
            )
            deleted_count = result.rowcount
            session.commit()

            if deleted_count > 0:
                logger.info(f"Deleted {deleted_count} old completed/failed tasks")

            return deleted_count

        except Exception as e:
            session.rollback()
            logger.error(f"Error cleaning up old tasks: {e}")
            return 0
        finally:
            session.close()
