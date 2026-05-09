# Flight Matrix

Real-time aircraft tracking and analysis system. Ingests ADS-B data, enriches it
with airline/airport/photo metadata scraped from public sources, applies
configurable filters, and emails AI-generated summary reports when matching
flights are observed.

> **Status:** early open-source release. APIs and internal structure may change.
> See [CHANGELOG.md](CHANGELOG.md).

English · [中文](README.zh-CN.md)

---

## What it does

- **Track** — polls ADS-B Exchange for aircraft positions and stores snapshots.
- **Scrape** — pulls flight schedules (FR24), aircraft photos (JetPhotos,
  Planespotters), airport metadata, and social-media mentions through a
  distributed scraper framework with browser pooling.
- **Filter** — three independent filter engines (snapshot-based, schedule-based,
  registration-match) evaluate rules against incoming data.
- **Report** — generates an HTML digest with maps, aircraft images, and a
  Claude-powered narrative analysis, then delivers via SMTP or AWS SES.
- **Web UI** — Flask app for queries, admin dashboards, and user filter/
  subscription management backed by Cognito auth.

## Architecture

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

Three independent services share one PostgreSQL database. The Flask app runs
locally or on AWS Lambda (via a Mangum ASGI adapter).

More detail: [docs/architecture.md](docs/architecture.md).

## Tech stack

- **Python 3.11+** with [`uv`](https://docs.astral.sh/uv/) for dependency
  management.
- **Flask** web framework; **SQLAlchemy** ORM; **Pydantic v2** schemas.
- **PostgreSQL** (Aurora Serverless in production) or **SQLite** for local dev.
- **DrissionPage** + Chromium for scraping behind Cloudflare.
- **AWS** (Lambda, CDK, Cognito, SES, S3, CloudFront) for production deployment.
- **Anthropic Claude** via AWS Bedrock for flight-analysis narratives.

## Quick start

```bash
git clone --recurse-submodules https://github.com/outstandingcandy/flight-matrix.git
cd flight-matrix
./scripts/quickstart.sh                           # one-shot local setup
uv run python web_app.py --skip-auth              # then visit http://localhost:5000
```

`quickstart.sh` checks prerequisites (Python 3.11+, `uv`), creates a venv,
installs dependencies, seeds `.env.local` with a generated `FLASK_SECRET_KEY`,
initialises a local SQLite database, and runs the test suite as a smoke
check. Idempotent.

### Local vs production config

Two env files live side by side; `STAGE` selects which one loads:

- `.env.local` — SQLite, auth bypass, no external APIs. Active when
  `STAGE=local`. Created by `quickstart.sh`.
- `.env.prod` — Aurora, Cognito, SES, real API keys. Active when
  `STAGE=prod` or unset. Created by `scripts/deploy-aws.sh`.

`DatabaseManager` refuses to connect to an AWS-hosted database (any host
matching `*.rds.amazonaws.com`) when `STAGE=local`, so leaving a
production URL in the wrong file is a loud startup error rather than a
silent data leak. Override deliberately with `ALLOW_PROD_DB_FROM_LOCAL=1`
if you actually need that (e.g. a debugging SSH tunnel).

To run the full data-ingestion stack (Xvfb + Track + Scraper worker)
from one command:

```bash
./scripts/start-all.sh              # launch everything in the background
./scripts/start-all.sh --status     # see what's up
./scripts/start-all.sh --tail       # follow every log file
./scripts/start-all.sh --stop       # stop everything
```

PIDs and logs live in `./logs/`. Add `--no-track` or `--no-scraper` to
skip a service, `--scrapers fr24_airport,jetphotos` to override which
scrapers run.

To configure which sources to scrape, how often, and how to enqueue
ad-hoc jobs, see [docs/scraping.md](docs/scraping.md). For a full
end-to-end walkthrough that boots the stack, seeds a handful of tasks,
and prints a before/after diff:

```bash
./scripts/demo.sh                            # default demo
./scripts/demo.sh --registration B-1020      # one specific aircraft
```

For manual setup or to understand what `quickstart.sh` is doing, see
[docs/configuration.md](docs/configuration.md).

## One-command AWS deploy

```bash
./scripts/deploy-aws.sh --check                   # preflight: identity, tools, .env
./scripts/deploy-aws.sh                           # guided interactive deploy
```

`deploy-aws.sh` verifies `aws`, `cdk`, `docker`, `uv`; confirms your AWS
identity; auto-populates `.env` with required values (generates a DB
password and Flask secret); and hands off to the low-level `./deploy.sh`.
See [docs/deployment.md](docs/deployment.md).

## Common commands

```bash
# Lint + format
uv run ruff check src/
uv run ruff format src/

# Type-check
uv run mypy src/

# Tests
uv run pytest tests/
uv run pytest tests/scraper/test_jetphotos.py -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## Deployment

Production uses AWS CDK in "import mode" against pre-existing VPC/Aurora/S3.
See [docs/deployment.md](docs/deployment.md).

```bash
./scripts/deploy-aws.sh            # guided: preflight + deploy
./scripts/deploy-aws.sh update     # fast update (Lambda + scraper + static)
./scripts/deploy-aws.sh status     # stack outputs + health summary
./scripts/deploy-aws.sh destroy    # tear down (asks twice)
./scripts/deploy-aws.sh --check    # preflight only
./deploy.sh <subcommand>           # low-level; what deploy-aws.sh wraps
```

## Troubleshooting

### `quickstart.sh` fails at `uv: command not found`
Install `uv` once:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
then re-run `./scripts/quickstart.sh`.

### Tests fail with `ModuleNotFoundError: src`
You're running `pytest` directly instead of via `uv run`. Either activate
the venv (`source .venv/bin/activate && pytest tests/`) or prefix every
command with `uv run`.

### Web app boots but every page 500s
Most likely `DATABASE_URL` isn't set. `quickstart.sh` seeds it to
`sqlite:///aircraft_data.db`; if you wiped `.env`, re-run the quickstart
or add the line manually.

### `/login` redirects to `/login` forever (auth loop)
This means `SKIP_AUTH` is true but Cognito env vars are half-configured.
Either set **all** of `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`,
`COGNITO_CLIENT_SECRET`, `COGNITO_DOMAIN`, `COGNITO_CALLBACK_URL`,
`COGNITO_LOGOUT_URL`, or remove all of them and set `SKIP_AUTH=true`.

### `deploy-aws.sh` says `aws sts get-caller-identity failed`
Your AWS credentials aren't set up. Pick one:
```bash
aws configure                           # interactive profile
export AWS_ACCESS_KEY_ID=…              # env vars
export AWS_SECRET_ACCESS_KEY=…
export AWS_SESSION_TOKEN=…              # if using SSO / STS
```

### CDK deploy fails with `This stack uses assets, so the toolkit stack must be deployed`
Your region isn't bootstrapped. `deploy-aws.sh` handles this automatically,
but if you bypassed it:
```bash
cdk bootstrap aws://<ACCOUNT_ID>/<REGION>
```

### Scraper can't find Chromium
DrissionPage expects a Chromium binary. On Ubuntu:
```bash
sudo apt install chromium-browser
```
On macOS, install Chrome or set `CHROME_PATH` in `.env` to a custom binary.

### A Cloudflare-protected scraper (JetPhotos / FR24) returns CloudflareBlockedError locally
This is almost always a test-harness mistake, not a real block. The
project's anti-scraping strategy depends on DrissionPage running
**non-headless** under an **Xvfb virtual display**. If you wrote your own
snippet with `BrowserPool(drission_options={"headless": True})`, Cloudflare
will reject it.

Use the proven smoke-test script instead:
```bash
./scripts/test-scrapers.sh            # all 3 core sources
./scripts/test-scrapers.sh jetphotos  # just JetPhotos
```
It starts Xvfb on `:55`, keeps `headless=False`, and asserts real data
comes back. See `src/scraper/CLAUDE.md` for the config invariants.

### I committed a secret by accident
Don't push. Rewrite the commit(s) locally before the first push, e.g.
`git reset --soft HEAD~1`, remove the secret, recommit. If it's already
public, rotate the credential immediately — git rewriting after the fact
doesn't help once a bot has scraped the value.

## Project layout

```
flight-matrix/
├── config/                  # Modular YAML configuration (includes pattern)
├── docs/                    # Architecture, configuration, deployment notes
├── infra/                   # CDK stacks
├── lib/resilient-scraper/   # Submodule: standalone scraper service
├── scripts/                 # One-off operational scripts
├── src/
│   ├── aircraft/            # Aircraft-domain helpers
│   ├── analysis/            # AI flight-analysis agent
│   ├── auth/                # Cognito JWT verification
│   ├── core/                # Shared base classes and exceptions
│   ├── data/                # Models and database access
│   ├── geo/                 # Airport distance / geo queries
│   ├── media/               # Image handling
│   ├── notifications/       # Email delivery
│   ├── reporting/           # Report generation + filter engines
│   ├── scraper/             # Distributed scraper framework + sources
│   ├── search/              # Tavily integration
│   ├── services/            # BaseService + domain services
│   └── utils/               # Cross-cutting utilities (being thinned out)
├── tests/                   # pytest suite
├── web_static/              # CSS / JS / images
├── web_templates/           # Jinja2 templates
├── web_app.py               # Flask entry
├── lambda_handler.py        # Mangum ASGI adapter for AWS Lambda
└── cdk_app.py               # CDK entry
```

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development
setup, code style, and the PR workflow.

By participating, you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE).
