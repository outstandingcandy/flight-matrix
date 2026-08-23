# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository. Human
contributors: [README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md), and
the longer architecture / configuration / deployment notes under `docs/` are
authoritative.

## Stack

- **Python 3.11+** with `uv` as package manager.
- **Ruff** for lint + format; **mypy** in strict mode for type checking.
- **pytest + pytest-asyncio** for tests; SQLite in-memory by default (override
  with `TEST_DATABASE_URL`).
- **PostgreSQL** (Aurora Serverless) in production; **SQLite** for local.
- **DrissionPage + Chromium** for the scraper service.

## Common commands

```bash
# Lint and format
uv run ruff check src/
uv run ruff format src/

# Type-check
uv run mypy src/

# Tests
uv run pytest tests/
uv run pytest tests/scraper/test_jetphotos.py -v

# Run the web app locally (auth bypassed)
uv run python web_app.py --skip-auth

# Run the scraper locally against one source
uv run python src/scraper_main.py --local --debug --scrapers fr24_flights

# Sync static assets to the CDN
aws s3 sync web_static/ "s3://${S3_BUCKET_NAME}/static/" --delete
```

## Architecture (short form)

Three independent services sharing one PostgreSQL database, plus a Flask web
app. See [docs/architecture.md](docs/architecture.md) for the full picture.

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ Track       │   │ Report      │   │ Scraper     │
│ (ADS-B → DB)│   │ (DB → Email)│   │ (FR24/…)    │
└──────┬──────┘   └──────┬──────┘   └──────┬──────┘
       │                 │                 │
       └─────────────────┼─────────────────┘
                         │
                   ┌─────┴─────┐
                   │ Postgres  │
                   └─────┬─────┘
                         │
                   ┌─────┴─────┐
                   │ Flask Web │
                   └───────────┘
```

Three of the nine services in `src/services/` (`FilterService`,
`SubscriptionService`, `UserService`) extend `BaseService` and use its
`session_scope()` / `readonly_session()` for transactional DB access; the rest
manage sessions themselves. Prefer `BaseService` in new code. Repositories live
alongside the models in `src/data/` (`snapshot_repo.py`, `cooldown_repo.py`,
`flight_schedule_repo.py`).

Most routes are still defined directly in `web_app.py`; auth and the scraper
ingest endpoint have been extracted to blueprints under `src/web/routes/`
(`auth.py`, `ingest.py`).

## Configuration

- Modular YAML under `config/` (root: `config/config.yaml` via `includes`).
- `YAMLConfig` interpolates `${ENV_VAR}` lazily; see
  [docs/configuration.md](docs/configuration.md) for every env var.
- Cognito JWT auth with `COGNITO_JWKS` env var (offline verification).
  `SKIP_AUTH=true` + `STAGE=local` bypasses auth and uses a mock user.

## Deployment

CDK via `cdk_app.py` → `infra/unified_stack.py`. Default is *import mode*
(reuses existing VPC, Aurora, S3, CloudFront). Set `FRESH_DEPLOY=true` to
create everything from scratch. Driver script: `./deploy.sh`. See
[docs/deployment.md](docs/deployment.md).

## Coding guidelines

- **Type hints** mandatory on all public function arguments and return values.
- **Docstrings** Google-style on public APIs, English only.
- **Logging** — `structlog` (or `logging`). No `print()` in `src/`.
- **Errors** — catch specific exceptions; use `src/core/exceptions.py`.
- **Imports** — absolute only (`from src.services.aircraft_service import ...`).
- **Async** — `async/await` for I/O-bound code.
- **Data validation** — Pydantic v2.

## Constraints

- Do not add `Co-Authored-By: Claude` to commit messages.
- Do not deploy directly after making changes — the user decides when to
  deploy.
- No placeholder code or TODOs in final output.

## Scraper guidelines

- Simulate real user behaviour (clicks, scrolls, delays).
- Save page HTML and screenshots on error for debugging.
- Set `cloudflare_protected = True` for Cloudflare-protected sites.
