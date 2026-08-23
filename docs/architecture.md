# Architecture

Flight Matrix is composed of three independent services that share a single
PostgreSQL database, plus a Flask web application that reads and writes the
same database. The services can run as separate processes locally or as
separate workloads in production — the current target is Docker Compose on a
GCP shared host; the AWS Lambda + ASG path (kept in the repo) is described in
[deployment.md](deployment.md).

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
browser pool. The reusable framework — `ResilientScraper` base class,
`BrowserPool`, `Worker`, Cloudflare / login / alert handling — lives in the
`resilient_scraper` submodule under `lib/resilient-scraper/`. This repo owns
the flight-matrix-specific glue: task queues, scheduler, and the per-scraper
sinks that write results to the database or object storage.

```
TaskScheduler ─► scraper_tasks (Postgres) ─► AsyncTaskQueue ─► Worker ─► ResilientScraper[T]
                                                                │              │
                                                                ▼              ▼
                                                          BrowserPool     Sink.on_success
                                                                          (src/scraper/sinks/*)
```

- `TaskScheduler` (`src/scraper/task_scheduler.py`) generates periodic tasks
  based on airport priorities.
- `TaskQueue` uses `SELECT ... FOR UPDATE SKIP LOCKED` for distributed
  locking. `AsyncTaskQueue` / `CliTaskQueue` / `LocalTaskQueue` adapt it to
  the submodule Worker's async contract.
- The submodule `Worker` pulls tasks, dispatches to the right
  `ResilientScraper[T]` subclass, and manages lifecycle (`setup`,
  `teardown`, retry policy).
- `BrowserPool` recycles browsers after N tasks and runs health checks.
- Heartbeat every 30 s; stale tasks re-queued after 60 min.
- Sinks (`src/scraper/sinks/`) are bound onto each scraper at registration
  time so scrapers stay persistence-agnostic. `fr24_airport_api_sink` posts
  to the web app's `/api/ingest/*` route when the scraper runs off-host
  (workstation Chromium, no direct database access).

Local mode uses `LocalTaskQueue` (backed by `src/scraper/sources/*`) instead
of the Postgres queue.

## Service-layer pattern

All business logic lives under `src/services/`. Every service inherits from
`BaseService` which provides transactional session management:

- `self.session_scope()` — context manager that commits on success,
  rolls back on error.
- `self.readonly_session()` — context manager for read-only operations.

Services today (`src/services/`): `AircraftService`,
`AircraftAnalysisService`, `AirportService`, `FilterService`,
`NoteAnalysisService`, `ReportService`, `SubscriptionService`,
`TrackService`, `UserService`. Only `FilterService`, `SubscriptionService`,
and `UserService` currently extend `BaseService`; the rest manage sessions
themselves and should migrate.

Blueprints (`src/web/routes/*`) call services; services call repositories
(`src/data/*_repo.py`); repositories own the SQL. Services do not emit SQL
directly. Blueprints in use: `auth` (login/logout) and `ingest` (scraper
write path for FR24 airport boards); other routes still live in
`web_app.py` — see [web-blueprints.md](web-blueprints.md).

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

Two targets are supported. Which one an image talks to (Bedrock vs. Gemini,
S3 vs. GCS, etc.) is switched by the `STAGE` / storage-target config, not
by separate code paths — see `src/llm/` and `src/storage/`.

- **GCP shared-host (Docker Compose).** Current production. Three
  containers on a single VM: `db` (postgres:16 on the existing
  `flight-matrix-pgdata` volume), `web` (`Dockerfile.web`: gunicorn serving
  `wsgi:app`, 2 workers × 4 threads), and `caddy` (`Caddyfile`: TLS
  termination + reverse proxy to `web:8000` using host-managed certbot
  certs). See `docker-compose.web.yml`; provisioning and cutover scripts
  live in `scripts/gcp/`. A separate `docker-compose.scraper.yml` runs the
  scraper on the same or a different host; workstation-run Cloudflare-heavy
  scrapers post rows back via `/api/ingest/*`.
- **AWS Lambda + ASG.** Historical path, still fully in the repo.
  `lambda_handler.py` uses the Mangum ASGI adapter wrapping Flask via
  `asgiref.wsgi.WsgiToAsgi`; static assets served from CloudFront (URL
  injected via `inject_static_url()`). Scraper runs on an EC2 Auto Scaling
  Group built from `Dockerfile.scraper` (Chromium + Xvfb), sized via
  `SCRAPER_MIN_CAPACITY` / `SCRAPER_MAX_CAPACITY`.
- **CDK.** `cdk_app.py` → `infra/unified_stack.py` supports **import mode**
  (default: reuses existing VPC, Aurora, S3, CloudFront; creates Lambda +
  API Gateway + scraper ASG) and **fresh mode** (`FRESH_DEPLOY=true`:
  creates everything from scratch). Only relevant to the AWS path.

See [deployment.md](deployment.md) for the step-by-step deploy procedure.
