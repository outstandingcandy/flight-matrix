# Architecture

Flight Matrix is composed of three independent services that share a single
PostgreSQL database, plus a Flask web application that reads and writes the
same database. The services can run as separate processes locally or as
separate workloads (Lambda for the web app, EC2 ASG for the scrapers) in
production.

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Track Service  │     │ Report Service  │     │ Scraper Service │
│ (ADS-B → DB)    │     │ (DB → Email)    │     │ (FR24/JetPhotos)│
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 │
                        ┌────────┴────────┐
                        │   PostgreSQL    │
                        └────────┬────────┘
                                 │
                        ┌────────┴────────┐
                        │   Flask Web     │
                        │  (web_app.py)   │
                        └─────────────────┘
```

## Services

### Track service (`src/track_main.py`)

Polls ADS-B Exchange on a fixed cadence, stores position snapshots, and
emits light aggregates (attention aggregates per aircraft). Writes to
`aircraft_snapshots` and refreshes `AircraftStaticInfo` where appropriate.

### Report service (`src/report_main.py`)

Runs the filter pipeline against the database and produces HTML digests with
maps and AI-generated narratives. Three filter modes run in parallel:

- **Mode A — snapshot-based.** SQL filter on recent ADS-B snapshots.
  Engine: `SQLFilterEngine`.
- **Mode B — schedule-based.** FR24 flight schedules for target airports.
  Engine: `ScheduleFilterEngine`.
- **Mode C — registration-match.** Correlates FR24 positions (missing a
  registration number) with ADS-B snapshots using time / distance / altitude
  windows. Engine: `RegistrationMatchFilterEngine`.

Each mode keeps its own cooldowns with distinct key suffixes
(`:schedule`, `:regmatch`) so a flight that matches two modes still generates
one report per mode.

### Scraper service (`src/scraper_main.py`)

A distributed task-queue scraper built on DrissionPage + Chromium with a
browser pool.

```
TaskScheduler ──► TaskQueue (Postgres) ──► ScraperWorker ──► BaseScraper
                         ▲                                       │
                         └──────────── BrowserPool ◄─────────────┘
```

- `TaskScheduler` generates periodic tasks based on airport priorities.
- `TaskQueue` uses `SELECT ... FOR UPDATE SKIP LOCKED` for distributed
  locking.
- `ScraperWorker` pulls tasks, dispatches to the right `BaseScraper[T]`
  subclass, and manages lifecycle (`setup`, `teardown`, `on_success`,
  `on_failure`, `should_retry`).
- `BrowserPool` recycles browsers after N tasks and runs health checks.
- Heartbeat every 30 s; stale tasks re-queued after 60 min.

Local mode uses `LocalTaskProvider` instead of the Postgres queue.

## Service-layer pattern

All business logic lives under `src/services/`. Every service inherits from
`BaseService` which provides transactional session management:

- `self.session_scope()` — context manager that commits on success,
  rolls back on error.
- `self.readonly_session()` — context manager for read-only operations.

Key services today: `AircraftService`, `ReportService`, `SubscriptionService`,
`UserService`, `FilterService`, `NoteAnalysisService`.

Blueprints (`src/web/routes/*`) call services; services call repositories;
repositories own the SQL. Services do not emit SQL directly.

## Data model

Two overlapping groups:

- **Core:** `AircraftSnapshot`, `AircraftStaticInfo`, `Airport`,
  `FlightSchedule`, `ReportCooldown`.
- **Multi-user (SaaS layer):** `User`, `Subscription`, `UserFilter`,
  `UserCooldown`, `UserUsage`, `AircraftAttentionAggregate`.

`AircraftStaticInfo` has `effective_*` properties that merge data from
multiple sources (airport_data `ad_*`, planespotters `ps_*`, jetphotos
`jp_*`).

Multi-user tables require `db.ensure_multi_user_tables_exist()` before first
use.

See [DATABASE.md](DATABASE.md) for full schemas.

## Authentication

Cognito JWT verification. JWKS loaded from `COGNITO_JWKS` env var or fetched
once at startup (no per-request network calls). Groups-based access:
`admins`, `flight-schedules-viewers`. Decorators available on routes:

- `@login_required`
- `@admin_required`
- `@flight_schedules_required`
- `@group_required(['admins'])`

Local dev bypasses auth entirely when `SKIP_AUTH=true` + `STAGE=local`, using
a mock user from `LOCAL_DEV_EMAIL` and `LOCAL_DEV_GROUPS`.

## Configuration

Modular YAML with an includes pattern. `config/config.yaml` is the root file
that includes ~13 sub-configs (`aws.yaml`, `database.yaml`, `api.yaml`,
`email.yaml`, `llm.yaml`, `scraper/*.yaml`). `YAMLConfig` in
`src/utils/yaml_config.py` supports:

- `${ENV_VAR}` interpolation.
- IAM authentication for RDS (`USE_IAM_AUTH=true`, requires SSL bundle at
  `/etc/ssl/certs/rds-ca-bundle.pem`).
- Auto-detection of `AWS_ACCOUNT_ID` via STS.

See [configuration.md](configuration.md) for the env-var reference and
[`config/config_template.yaml`](../config/config_template.yaml) for the full
schema.

## Deployment targets

- **Lambda web app.** `lambda_handler.py` uses the Mangum ASGI adapter
  wrapping Flask WSGI via `asgiref.wsgi.WsgiToAsgi`. `init_app()` runs on
  cold start. Static assets served from CloudFront (URL injected via
  `inject_static_url()` context processor). Code must be synced to
  `lambda_code/` before building; `deploy.sh` handles this.
- **Scraper ASG.** EC2 instances built from `Dockerfile.scraper` (Chromium +
  Xvfb). The ASG is sized via `SCRAPER_MIN_CAPACITY` / `SCRAPER_MAX_CAPACITY`.
- **CDK.** `cdk_app.py` → `infra/unified_stack.py` supports **import mode**
  (default: reuses existing VPC, Aurora, S3, CloudFront; creates Lambda +
  API Gateway + scraper ASG) and **fresh mode** (`FRESH_DEPLOY=true`: creates
  everything from scratch).

See [deployment.md](deployment.md) for the step-by-step deploy procedure.
