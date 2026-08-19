"""
Aircraft information cache management module.

Provides cache management for aircraft static info, supporting:
- Querying cached data
- Saving/updating the cache
- Calculating data freshness
"""

import json
import logging
from datetime import datetime

from sqlalchemy import text

logger = logging.getLogger("aircraft_cache")


def calculate_freshness(updated_at: datetime) -> dict:
    """
    Calculate data freshness.

    Args:
        updated_at: Data update timestamp

    Returns:
        {
            'age_days': int,
            'freshness': 'fresh' | 'good' | 'stale' | 'expired',
            'recommendation': str
        }
    """
    if updated_at is None:
        return {
            "age_days": None,
            "freshness": "unknown",
            "recommendation": "Data has no timestamp, consider refreshing",
        }

    if isinstance(updated_at, str):
        try:
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return {
                "age_days": None,
                "freshness": "unknown",
                "recommendation": "Could not parse timestamp",
            }

    age = datetime.now() - updated_at
    age_days = age.days

    if age_days < 7:
        return {
            "age_days": age_days,
            "freshness": "fresh",
            "recommendation": "Data is very recent, safe to use",
        }
    elif age_days < 30:
        return {
            "age_days": age_days,
            "freshness": "good",
            "recommendation": "Data is reasonably fresh, OK to use for static info",
        }
    elif age_days < 90:
        return {
            "age_days": age_days,
            "freshness": "stale",
            "recommendation": "Data may be outdated, consider refreshing for important analysis",
        }
    else:
        return {
            "age_days": age_days,
            "freshness": "expired",
            "recommendation": "Data is likely outdated, recommend refreshing",
        }


class AircraftCacheManager:
    """Aircraft information cache manager"""

    def __init__(self, database_manager):
        """
        Initialize the cache manager.

        Args:
            database_manager: DatabaseManager instance
        """
        self.db = database_manager
        self._ensure_table_exists()
        self._ensure_image_columns_exist()
        self._ensure_cache_columns_exist()
        logger.info("AircraftCacheManager initialized")

    def _ensure_table_exists(self):
        """Ensure the cache table exists"""
        session = self.db.get_session()
        try:
            session.execute(text("SELECT 1 FROM aircraft_static_info LIMIT 1"))
            logger.debug("aircraft_static_info table exists")
        except Exception:
            logger.info("Creating aircraft_static_info table...")
            session.execute(
                text("""
            CREATE TABLE aircraft_static_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registration VARCHAR(20),
                hex VARCHAR(6),

                owner VARCHAR(500),
                operator VARCHAR(500),
                aircraft_model VARCHAR(100),
                manufacturer VARCHAR(100),
                serial_number VARCHAR(50),
                year_built INTEGER,
                country VARCHAR(50),

                previous_owners TEXT,
                summary TEXT,

                is_military BOOLEAN DEFAULT 0,
                is_government BOOLEAN DEFAULT 0,
                is_vip BOOLEAN DEFAULT 0,
                tags TEXT,

                raw_search_results TEXT,

                -- Image status
                images_downloaded BOOLEAN DEFAULT 0,
                images_updated_at DATETIME,

                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_accessed DATETIME,
                hit_count INTEGER DEFAULT 0,
                data_source VARCHAR(50),

                UNIQUE(registration)
            )
            """)
            )
            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_cache_registration ON aircraft_static_info(registration)"
                )
            )
            session.execute(
                text("CREATE INDEX IF NOT EXISTS idx_cache_hex ON aircraft_static_info(hex)")
            )
            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_cache_updated ON aircraft_static_info(updated_at)"
                )
            )
            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_cache_images ON aircraft_static_info(images_downloaded)"
                )
            )
            session.commit()
            logger.info("aircraft_static_info table created")
        finally:
            session.close()

    def _ensure_image_columns_exist(self) -> None:
        """Ensure image status columns exist (migration for existing tables)"""
        session = self.db.get_session()
        try:
            # Check if images_downloaded column already exists
            try:
                session.execute(text("SELECT images_downloaded FROM aircraft_static_info LIMIT 1"))
                return  # Column already exists
            except Exception:
                pass

            # Add image status columns
            logger.info("Adding image status columns to aircraft_static_info table...")

            columns_to_add = [
                ("images_downloaded", "BOOLEAN DEFAULT false"),
                ("images_updated_at", "TIMESTAMP"),
            ]

            for col_name, col_type in columns_to_add:
                try:
                    session.execute(
                        text(f"ALTER TABLE aircraft_static_info ADD COLUMN {col_name} {col_type}")
                    )
                    logger.info(f"Added column {col_name}")
                except Exception as e:
                    logger.debug(f"Column {col_name} might already exist: {e}")

            try:
                session.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_cache_images ON aircraft_static_info(images_downloaded)"
                    )
                )
            except Exception:
                pass

            session.commit()
            logger.info("Image status columns migration complete")
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding image status columns: {e}")
        finally:
            session.close()

    def _ensure_cache_columns_exist(self) -> None:
        """Ensure cache statistics columns exist (hit_count, last_accessed)"""
        session = self.db.get_session()
        try:
            # Check if hit_count column already exists
            try:
                session.execute(text("SELECT hit_count FROM aircraft_static_info LIMIT 1"))
                return  # Column already exists
            except Exception:
                pass

            logger.info("Adding cache columns to aircraft_static_info table...")

            columns_to_add = [
                ("hit_count", "INTEGER DEFAULT 0"),
                ("last_accessed", "TIMESTAMP"),
            ]

            for col_name, col_type in columns_to_add:
                try:
                    session.execute(
                        text(f"ALTER TABLE aircraft_static_info ADD COLUMN {col_name} {col_type}")
                    )
                    logger.info(f"Added column {col_name}")
                except Exception as e:
                    logger.debug(f"Column {col_name} might already exist: {e}")

            session.commit()
            logger.info("Cache columns migration complete")
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding cache columns: {e}")
        finally:
            session.close()

    def get_cached_info(self, registration: str = None, hex_code: str = None) -> dict:
        """
        Query cached aircraft information.

        Args:
            registration: Aircraft registration
            hex_code: ICAO hex code

        Returns:
            {
                'found': bool,
                'data': {...} or None,
                'metadata': {
                    'updated_at': str,
                    'age_days': int,
                    'freshness': str,
                    'recommendation': str,
                    'hit_count': int
                }
            }
        """
        if not registration and not hex_code:
            return {
                "found": False,
                "message": "At least one of registration or hex_code is required",
                "data": None,
                "metadata": None,
            }

        session = self.db.get_session()
        try:
            # Build query
            if registration:
                result = session.execute(
                    text("SELECT * FROM aircraft_static_info WHERE registration = :reg"),
                    {"reg": registration},
                ).fetchone()
            else:
                result = session.execute(
                    text("SELECT * FROM aircraft_static_info WHERE hex = :hex"), {"hex": hex_code}
                ).fetchone()

            if not result:
                return {
                    "found": False,
                    "message": f"No cached data for {registration or hex_code}",
                    "data": None,
                    "metadata": None,
                }

            # Update access statistics
            if registration:
                session.execute(
                    text("""
                    UPDATE aircraft_static_info
                    SET last_accessed = CURRENT_TIMESTAMP,
                        hit_count = COALESCE(hit_count, 0) + 1
                    WHERE registration = :reg
                """),
                    {"reg": registration},
                )
                session.commit()

            # Convert to dict
            columns = [
                "id",
                "registration",
                "hex",
                "owner",
                "operator",
                "aircraft_model",
                "manufacturer",
                "serial_number",
                "year_built",
                "country",
                "previous_owners",
                "summary",
                "is_military",
                "is_government",
                "is_vip",
                "tags",
                "raw_search_results",
                "created_at",
                "updated_at",
                "last_accessed",
                "hit_count",
                "data_source",
            ]
            data = dict(zip(columns, result))

            # Parse JSON fields
            for json_field in ["previous_owners", "tags", "raw_search_results"]:
                if data.get(json_field):
                    try:
                        data[json_field] = json.loads(data[json_field])
                    except (ValueError, TypeError):
                        # Leave the field as-is; downstream callers handle strings.
                        pass

            # Calculate freshness
            freshness_info = calculate_freshness(data.get("updated_at"))

            return {
                "found": True,
                "data": {
                    "registration": data.get("registration"),
                    "hex": data.get("hex"),
                    "owner": data.get("owner"),
                    "operator": data.get("operator"),
                    "aircraft_model": data.get("aircraft_model"),
                    "manufacturer": data.get("manufacturer"),
                    "country": data.get("country"),
                    "is_military": bool(data.get("is_military")),
                    "is_government": bool(data.get("is_government")),
                    "is_vip": bool(data.get("is_vip")),
                    "summary": data.get("summary"),
                    "tags": data.get("tags", []),
                },
                "metadata": {
                    "updated_at": str(data.get("updated_at")) if data.get("updated_at") else None,
                    "created_at": str(data.get("created_at")) if data.get("created_at") else None,
                    "hit_count": data.get("hit_count", 0),
                    "data_source": data.get("data_source", "unknown"),
                    **freshness_info,
                },
            }

        except Exception as e:
            logger.error(f"Error getting cached info: {e}")
            return {
                "found": False,
                "message": f"Database error: {e!s}",
                "data": None,
                "metadata": None,
            }
        finally:
            session.close()

    def save_info(
        self,
        registration: str,
        hex_code: str = None,
        owner: str = None,
        operator: str = None,
        aircraft_model: str = None,
        manufacturer: str = None,
        country: str = None,
        is_military: bool = False,
        is_government: bool = False,
        is_vip: bool = False,
        summary: str = None,
        tags: list = None,
        data_source: str = "agent",
    ) -> dict:
        """
        Save or update aircraft information in the cache.

        Args:
            registration: Aircraft registration (required)
            hex_code: ICAO hex code
            owner: Owner
            operator: Operator
            aircraft_model: Aircraft model
            manufacturer: Manufacturer
            country: Country of registration
            is_military: Whether military
            is_government: Whether government
            is_vip: Whether VIP
            summary: Background summary
            tags: List of tags
            data_source: Data source

        Returns:
            {'success': bool, 'message': str, 'action': 'created' | 'updated'}
        """
        if not registration:
            return {"success": False, "message": "Registration is required", "action": None}

        session = self.db.get_session()
        try:
            # Check if it already exists
            existing = session.execute(
                text("SELECT id FROM aircraft_static_info WHERE registration = :reg"),
                {"reg": registration},
            ).fetchone()

            # Serialize tags
            tags_json = json.dumps(tags, ensure_ascii=False) if tags else None

            if existing:
                # Update
                session.execute(
                    text("""
                    UPDATE aircraft_static_info SET
                        hex = COALESCE(:hex, hex),
                        owner = COALESCE(:owner, owner),
                        operator = COALESCE(:operator, operator),
                        aircraft_model = COALESCE(:aircraft_model, aircraft_model),
                        manufacturer = COALESCE(:manufacturer, manufacturer),
                        country = COALESCE(:country, country),
                        is_military = :is_military,
                        is_government = :is_government,
                        is_vip = :is_vip,
                        summary = COALESCE(:summary, summary),
                        tags = COALESCE(:tags, tags),
                        updated_at = CURRENT_TIMESTAMP,
                        -- Also bumped because the OpenSearch aircraft index
                        -- resyncs off `last_updated`; a write that leaves it
                        -- alone is a write search never sees.
                        last_updated = CURRENT_TIMESTAMP,
                        data_source = :data_source
                    WHERE registration = :registration
                """),
                    {
                        "registration": registration,
                        "hex": hex_code,
                        "owner": owner,
                        "operator": operator,
                        "aircraft_model": aircraft_model,
                        "manufacturer": manufacturer,
                        "country": country,
                        "is_military": is_military,
                        "is_government": is_government,
                        "is_vip": is_vip,
                        "summary": summary,
                        "tags": tags_json,
                        "data_source": data_source,
                    },
                )
                action = "updated"
                message = f"Updated cache for {registration}"
            else:
                # Insert
                session.execute(
                    text("""
                    INSERT INTO aircraft_static_info
                    (registration, hex, owner, operator, aircraft_model, manufacturer,
                     country, is_military, is_government, is_vip, summary, tags, data_source,
                     last_updated)
                    VALUES
                    (:registration, :hex, :owner, :operator, :aircraft_model, :manufacturer,
                     :country, :is_military, :is_government, :is_vip, :summary, :tags,
                     :data_source, CURRENT_TIMESTAMP)
                """),
                    {
                        "registration": registration,
                        "hex": hex_code,
                        "owner": owner,
                        "operator": operator,
                        "aircraft_model": aircraft_model,
                        "manufacturer": manufacturer,
                        "country": country,
                        "is_military": is_military,
                        "is_government": is_government,
                        "is_vip": is_vip,
                        "summary": summary,
                        "tags": tags_json,
                        "data_source": data_source,
                    },
                )
                action = "created"
                message = f"Created cache entry for {registration}"

            session.commit()
            logger.info(message)

            return {"success": True, "message": message, "action": action}

        except Exception as e:
            session.rollback()
            logger.error(f"Error saving aircraft info: {e}")
            return {"success": False, "message": f"Database error: {e!s}", "action": None}
        finally:
            session.close()

    def get_statistics(self) -> dict:
        """Get cache statistics"""
        session = self.db.get_session()
        try:
            total = session.execute(text("SELECT COUNT(*) FROM aircraft_static_info")).scalar() or 0
            military = (
                session.execute(
                    text("SELECT COUNT(*) FROM aircraft_static_info WHERE is_military = 1")
                ).scalar()
                or 0
            )
            government = (
                session.execute(
                    text("SELECT COUNT(*) FROM aircraft_static_info WHERE is_government = 1")
                ).scalar()
                or 0
            )
            recent = (
                session.execute(
                    text("""
                SELECT COUNT(*) FROM aircraft_static_info
                WHERE updated_at >= datetime('now', '-30 days')
            """)
                ).scalar()
                or 0
            )

            return {
                "total_cached": total,
                "military_aircraft": military,
                "government_aircraft": government,
                "recently_updated_30d": recent,
            }
        except Exception as e:
            logger.error(f"Error getting cache statistics: {e}")
            return {}
        finally:
            session.close()


# Global instance
_cache_manager: AircraftCacheManager | None = None


def get_cache_manager(database_manager=None) -> AircraftCacheManager | None:
    """Get the global cache manager instance"""
    global _cache_manager
    if _cache_manager is None and database_manager:
        _cache_manager = AircraftCacheManager(database_manager)
    return _cache_manager
