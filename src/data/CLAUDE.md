# src/data/

Database layer: SQLAlchemy ORM models plus the connection manager and
repositories that own all SQL in the application.

## Files

| File | Purpose |
|------|---------|
| `models.py` | SQLAlchemy declarative models (one Python class per table) |
| `db_manager.py` | `DatabaseManager` — engine + session factory; facade over the repositories |
| `schema.py` | Raw-SQL DDL helpers (AUTOINCREMENT-aware SQLite, multi-user table bootstrap) |
| `snapshot_repo.py` | `SnapshotRepository` — ADS-B snapshot ingest + queries |
| `cooldown_repo.py` | `CooldownRepository` — report cooldown CRUD + rule evaluation |

Legacy import path `src.utils.database` is a thin re-export shim — new
code should import directly from `src.data`.

## Models (models.py)

### Aircraft Tracking

| Model | Table | Purpose |
|-------|-------|---------|
| `AircraftSnapshot` | `aircraft_snapshots` | Real-time aircraft position snapshots from ADS-B |
| `AircraftStaticInfo` | `aircraft_static_info` | Static aircraft data (registration, type, images) |
| `Flight` | `flights` | Flight records with route information |

### Multi-User System

| Model | Table | Purpose |
|-------|-------|---------|
| `User` | `users` | User accounts |
| `Subscription` | `subscriptions` | User subscription tiers (basic/premium/enterprise) |
| `UserFilter` | `user_filters` | Custom SQL filter rules per user |
| `UserCooldown` | `user_cooldowns` | Report cooldown tracking per user |
| `UserUsage` | `user_usage` | API/report usage tracking |

### Airport Data

| Model | Table | Purpose |
|-------|-------|---------|
| `Airport` | `airports` | Airport metadata (IATA/ICAO codes, location) |
| `FlightSchedule` | `flight_schedules` | Scraped flight schedules |

### Scraper

| Model | Table | Purpose |
|-------|-------|---------|
| `ScraperTaskDB` | `scraper_tasks` | Distributed task queue |
| `AircraftImage` | `aircraft_images` | Downloaded aircraft images metadata |

## Key Indexes

Important indexes for query performance:
- `idx_snapshot_time` - Time-based queries
- `idx_hex_time` - Aircraft history lookups
- `idx_recent_military` - Military aircraft filtering
- `idx_location` - Geographic queries

## Usage

```python
from src.data.models import AircraftSnapshot, User, Base

# Models use SQLAlchemy declarative base
from sqlalchemy.orm import Session

def get_recent_aircraft(session: Session, limit: int = 100):
    return session.query(AircraftSnapshot)\
        .order_by(AircraftSnapshot.snapshot_time.desc())\
        .limit(limit)\
        .all()
```
