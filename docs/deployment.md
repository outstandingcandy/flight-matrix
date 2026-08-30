# Deployment

Flight Matrix supports two deployment targets. Pick whichever matches your
infrastructure; the application code is target-agnostic and switches
providers (Bedrock ↔ Gemini, S3 ↔ GCS) through config rather than code.

- **GCP shared-host (Docker Compose).** Current production. See
  [GCP shared-host deployment](#gcp-shared-host-deployment) below.
- **AWS Lambda + ASG (via CDK).** Historical path, still supported. Covered
  by the rest of this document.

For a diagram-level overview of both targets see
[architecture.md § Deployment targets](architecture.md#deployment-targets).

## AWS (CDK) — two modes

- **Import mode (default).** Reuses an existing VPC, Aurora cluster, S3
  bucket, and CloudFront distribution. Creates Lambda (web app) + API
  Gateway + scraper Auto Scaling Group.
- **Fresh mode** (`FRESH_DEPLOY=true`). Creates every resource from scratch.
  Good for a personal sandbox.

## Prerequisites

- AWS credentials with permission to deploy the target stacks.
- `aws` CLI configured (`aws sts get-caller-identity` should work).
- Docker (for Lambda container images and scraper images).
- `uv` and a working Python environment (see
  [CONTRIBUTING.md](../CONTRIBUTING.md)).

Once only: `cdk bootstrap` the target account/region.

## Configure

1. Copy `.env.example` to `.env` and fill in the infrastructure variables —
   in particular `AWS_ACCOUNT_ID`, `S3_BUCKET_NAME`,
   `CLOUDFRONT_DISTRIBUTION_ID`, `VPC_ID`, and the subnet / security-group
   IDs. `deploy.sh` sources `.env` automatically.
2. Store production secrets (Cognito client secret, Gmail app password,
   Tavily API key, ADS-B API key) in AWS Systems Manager under the
   `/flight-matrix/` prefix. The scraper ASG reads these at startup.

## Deploy commands

```bash
# Login to ECR (required before every Lambda image push)
aws ecr-public get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin public.ecr.aws

./deploy.sh deploy          # First deploy, or full update
./deploy.sh update          # Lambda + scraper + S3 static files
./deploy.sh webapp          # Lambda + S3 + CloudFront invalidation only
./deploy.sh webapp-env      # Lambda environment variables only (fast)
./deploy.sh scraper         # Refresh scraper ASG only
./deploy.sh status          # Show stack outputs
./deploy.sh diff            # Show pending CDK changes
./deploy.sh synth           # Render CloudFormation template
./deploy.sh fetch-jwks      # Fetch Cognito JWKS into .env
./deploy.sh create-user     # Create a Cognito user, assign to groups
./deploy.sh destroy         # Tear down (requires confirmation)
```

## Static assets

CSS, JS, and images in `web_static/` are synced to S3 and served via
CloudFront. `deploy.sh` handles the sync and the cache invalidation as part
of `deploy`, `update`, or `webapp`.

You can sync manually during frontend development:

```bash
aws s3 sync web_static/ s3://$S3_BUCKET_NAME/static/ --delete
```

## Lambda

- Entry: `lambda_handler.py` — Mangum ASGI adapter around Flask via
  `asgiref.wsgi.WsgiToAsgi`.
- Image: `Dockerfile.lambda`, built against Lambda Python 3.12 base.
- Cold start: `init_app()` runs once to load config and warm the DB pool.
- Code sync: `deploy.sh` copies the repo into `lambda_code/` (git-ignored)
  before building.

## Scraper ASG

- Instances: EC2 launched from `Dockerfile.scraper` (Chromium + Xvfb).
- Auto Scaling bounds: `SCRAPER_MIN_CAPACITY`, `SCRAPER_MAX_CAPACITY`,
  `SCRAPER_DESIRED_CAPACITY` in `.env`.
- Git clone target: parameterised via the SSM parameter
  `/flight-matrix/scraper/github-repo` (format: `owner/repo`). This lets
  forks run without code changes.

## Local production-database access

For debugging, connect to the production RDS cluster over an SSH tunnel
through a bastion host:

```bash
ssh -i ~/your-key.pem \
    -L 5432:<RDS_ENDPOINT>:5432 \
    -N ubuntu@<BASTION_HOST>
```

Set `DATABASE_URL` in `.env` to `postgresql+psycopg2://...@localhost:5432/...`
and run the web app locally — it will connect through the tunnel.

## Checking the deploy

```bash
./deploy.sh status           # Stack outputs (Lambda ARN, API URL, etc.)
aws logs tail /aws/lambda/flight-matrix-unified-prod --follow --since 5m
```

Smoke-test the web app:

```bash
curl -fsSI https://<your-domain>/healthz || echo "health check failed"
```

## GCP shared-host deployment

The current production model. Three containers on a single VM, managed by
Docker Compose, sharing the compose bridge network. No cloud provider
resources beyond the VM and its persistent volume are required.

### Layout

```
docker-compose.web.yml
├── db      postgres:16-alpine   →  volume flight-matrix-pgdata (external)
├── web     Dockerfile.web       →  gunicorn wsgi:app, 2 workers × 4 threads
└── caddy   caddy:2-alpine       →  Caddyfile, host-managed certbot certs
```

Only `caddy` publishes a host port (`${CADDY_HOST_PORT:-8443}`); `web`
reaches `db` and `caddy` reaches `web` over the compose network by service
name.

### Prerequisites (once per host)

- Docker + Docker Compose plugin.
- An existing certbot certificate for the host's domain at
  `/etc/letsencrypt/live/<domain>/` (Caddy reads it read-only; renewal
  stays with certbot — a deploy-hook reloads Caddy after renewal).
- The `flight-matrix-pgdata` named volume with the existing cluster
  (`aircraft_admin` / `aircraft_data`). Restore from a `pg_dumpall` if
  bootstrapping fresh.
- `/etc/flight-matrix/env.docker` — a copy of the systemd `env` file with
  `DATABASE_URL` rewritten to point at the `db` service (`db:5432`, not
  `127.0.0.1`).
- A `.env` next to the compose file with two non-secret values:
  - `CERT_DOMAIN` — matches the certbot live directory.
  - `CADDY_HOST_PORT` — defaults to `8443`; override to a bypass port
    (e.g. `8444`) when verifying a deploy before cutting traffic.

Provisioning helpers live in `scripts/gcp/`
(`provision-existing-host.sh`, `deploy-app.sh`,
`migrate-db-to-gcp.sh`, `migrate-objects-to-gcp.sh`).

### Deploy / update

```bash
# From the repo root on the host
docker compose -f docker-compose.web.yml build web
docker compose -f docker-compose.web.yml up -d
docker compose -f docker-compose.web.yml ps
docker compose -f docker-compose.web.yml logs --tail=100 web caddy
```

Run one-off admin or migration scripts inside the web container so they
inherit the same config and credentials:

```bash
docker compose -f docker-compose.web.yml exec web \
    python scripts/migrate_backfill_image_airport_icao.py
```

### Cutover from the systemd deployment

The pre-container production ran the web app via
`scripts/systemd/flight-matrix-web.service` on the host, with nginx
terminating TLS and a bare `postgres` on `127.0.0.1:5433`. The compose file
keeps `db` published on `127.0.0.1:5433` **temporarily** so that unit can
keep serving while the compose stack is verified on a bypass port. Once the
systemd unit is stopped for good, remove the `ports:` block from the `db`
service — nothing outside the compose network should need it.

### Scraper

A separate `docker-compose.scraper.yml` runs the scraper worker with its
Chromium + Xvfb image (`Dockerfile.scraper`). It can run on the same host
as the web stack or on a workstation; Cloudflare-heavy scrapers
(`fr24_airport`) typically run on a workstation and post rows back to the
web app via `/api/v1/ingest/flight-schedules` (auth: `api.ingest_token`
shared secret).
