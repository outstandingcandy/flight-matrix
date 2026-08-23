# Configuration

All configuration flows through two layers:

1. **YAML files** under `config/` — the schema and non-sensitive defaults.
   `config/config.yaml` is the entry point; it includes the sub-configs.
2. **Environment variables** (via `.env` or the shell) — secrets and
   environment-specific values. Interpolated into YAML via `${VAR}` syntax.

The template in [`config/config_template.yaml`](../config/config_template.yaml)
documents every YAML key. [`.env.example`](../.env.example) documents every
environment variable.

## Environment variables

### Core

| Variable        | Purpose                                              | Default     |
|-----------------|------------------------------------------------------|-------------|
| `ENVIRONMENT`   | Deployment stage: `local` / `dev` / `prod`           | `local`     |
| `STAGE`         | Parallel to `ENVIRONMENT`, used by auth              | `local`     |
| `DEPLOY_TARGET` | Cloud target: `aws` / `gcp` / `local`. Selects the   | `local`     |
|                 | default auth / storage / LLM providers (see          |             |
|                 | `config/deploy.yaml`, `src/core/deploy_target.py`).  |             |
| `AWS_REGION`    | AWS region for all AWS SDK calls                     | `us-east-1` |

### Local development

| Variable            | Purpose                                       |
|---------------------|-----------------------------------------------|
| `SKIP_AUTH`         | `true` bypasses Cognito and uses a mock user  |
| `LOCAL_DEV_EMAIL`   | Email of the mock user                        |
| `LOCAL_DEV_GROUPS`  | Comma-separated Cognito group list            |

### Database

| Variable       | Purpose                                                     |
|----------------|-------------------------------------------------------------|
| `DATABASE_URL` | SQLAlchemy URL. `sqlite:///aircraft_data.db` for local dev. |
| `DB_USERNAME`  | Used when composing a URL from parts                        |
| `DB_NAME`      | ""                                                          |
| `DB_PASSWORD`  | ""                                                          |
| `DB_HOST`      | ""                                                          |
| `DB_ENDPOINT`  | Aurora writer endpoint (production)                         |
| `USE_IAM_AUTH` | `true` to use RDS IAM auth; requires SSL bundle             |
| `DB_IAM_USER`  | IAM user for RDS (default: `scraper_iam`)                   |

### AWS infrastructure (import-mode CDK deploys)

| Variable                      | Purpose                                      |
|-------------------------------|----------------------------------------------|
| `AWS_ACCOUNT_ID`              | Required for ECR + CDK; auto-detected via STS|
| `VPC_ID`                      | Existing VPC                                 |
| `DB_SECURITY_GROUP_ID`        | DB SG to attach Lambda / workers to          |
| `SERVICES_SECURITY_GROUP_ID`  | Shared services SG                           |
| `PRIVATE_SUBNET_IDS`          | Comma-separated                              |
| `ISOLATED_SUBNET_IDS`         | ""                                           |
| `PUBLIC_SUBNET_IDS`           | ""                                           |
| `S3_BUCKET_NAME`              | Static-assets bucket                         |
| `CLOUDFRONT_DISTRIBUTION_ID`  | CDN distribution for static assets           |
| `CLOUDFRONT_DOMAIN`           | CDN domain for image URL generation          |

### Authentication

`auth.provider` in `config/auth.yaml` resolves from `DEPLOY_TARGET` unless
overridden: `aws` → Cognito, `gcp` → Google, `local` → none (`SKIP_AUTH=true`).

#### Cognito (AWS target)

| Variable                 | Purpose                                         |
|--------------------------|-------------------------------------------------|
| `COGNITO_USER_POOL_ID`   | User pool                                       |
| `COGNITO_CLIENT_ID`      | App client                                      |
| `COGNITO_CLIENT_SECRET`  | App client secret                               |
| `COGNITO_DOMAIN`         | Hosted-UI domain                                |
| `COGNITO_CALLBACK_URL`   | OAuth callback URL                              |
| `COGNITO_LOGOUT_URL`     | Logout redirect URL                             |
| `COGNITO_JWKS`           | JWKS JSON string; fetched at startup if unset   |
| `FLASK_SECRET_KEY`       | Flask session secret; auto-generated if unset   |

#### Google OAuth (GCP target)

| Variable                     | Purpose                                    |
|------------------------------|--------------------------------------------|
| `GOOGLE_OAUTH_CLIENT_ID`     | OAuth 2.0 client ID                        |
| `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth 2.0 client secret                    |

### Object storage

`storage.provider` in `config/deploy.yaml` resolves from `DEPLOY_TARGET`.

| Variable              | Purpose                                          |
|-----------------------|--------------------------------------------------|
| `STATIC_BASE_URL`     | Public URL prefix for static/media assets (CDN,  |
|                       | GCS URL, or empty for local `/static` serving)   |
| `GCS_ASSETS_BUCKET`   | GCS bucket name (gcp target)                     |
| `GOOGLE_CLOUD_PROJECT`| GCP project ID                                   |

### Search (OpenSearch — optional accelerator)

| Variable               | Purpose                                          |
|------------------------|--------------------------------------------------|
| `OPENSEARCH_URL`       | Cluster URL; empty disables full-text and falls back to SQL LIKE |
| `OPENSEARCH_USERNAME`  | Empty when the security plugin is off            |
| `OPENSEARCH_PASSWORD`  | Empty when the security plugin is off            |

### Scraper ingest (workstation → web)

| Variable            | Purpose                                             |
|---------------------|-----------------------------------------------------|
| `INGEST_API_TOKEN`  | Shared secret for `POST /api/ingest/*`. If unset, the endpoint returns 503. |

### GCP shared-host deployment (Docker Compose)

| Variable          | Purpose                                                       |
|-------------------|---------------------------------------------------------------|
| `CERT_DOMAIN`     | Domain name whose certbot cert Caddy reads from `/etc/letsencrypt/live/<domain>/` |
| `CADDY_HOST_PORT` | Host port Caddy publishes (default `8443`; use e.g. `8444` for a bypass verify) |

### Third-party APIs

| Variable          | Purpose                                          |
|-------------------|--------------------------------------------------|
| `TAVILY_API_KEY`  | Web search used by the AI flight-analysis agent  |
| `ADSB_API_KEY`    | RapidAPI key for ADS-B Exchange                  |

### Email

| Variable                | Purpose                                        |
|-------------------------|------------------------------------------------|
| `SENDER_EMAIL`          | SMTP sender (Gmail)                            |
| `AWS_SES_SENDER_EMAIL`  | SES sender (production)                        |
| `RECIPIENTS`            | Comma-separated list                           |
| `GMAIL_APP_PASSWORD`    | Gmail app password for SMTP mode               |

### Web app

| Variable     | Purpose                                                                     |
|--------------|-----------------------------------------------------------------------------|
| `APP_DOMAIN` | Custom domain — required on the AWS target because API Gateway does not forward a custom `Host` header. |

## YAML configuration

`config/config.yaml` is small; it just enumerates includes. Each include is
owned by a domain module:

```
config/
├── config.yaml            # entry (includes)
├── aws.yaml               # AWS region / account overrides
├── deploy.yaml            # DEPLOY_TARGET + storage provider (s3/gcs/local)
├── auth.yaml              # auth.provider (cognito/google/none), Cognito + Google OAuth
├── database.yaml          # DATABASE_URL + pool settings
├── email.yaml             # SMTP / SES sender + recipients
├── api.yaml               # ADS-B API tuning + ingest_token
├── llm.yaml               # LLM providers (Bedrock / Gemini) and model config
├── recall.yaml            # Aircraft recall / watch lists
├── reporting.yaml         # Filter engine + report body settings
├── search.yaml            # OpenSearch accelerator (optional; SQL fallback)
├── subscription.yaml      # Multi-user subscription tiers
├── templates.yaml         # Email templates
├── aircraft_types.yaml    # ICAO type enrichment
└── scraper/
    ├── base.yaml          # Scraper framework defaults
    ├── fr24.yaml          # FR24 scraper settings
    ├── jetphotos.yaml     # JetPhotos + object storage upload
    ├── xiaohongshu.yaml   # Xiaohongshu source
    └── other.yaml         # Remaining sources
```

For how the scraper YAMLs translate into actual download tasks — and
how to enqueue ad-hoc jobs during development — see
[scraping.md](scraping.md).

Values containing `${VAR}` are resolved via `YAMLConfig._resolve_env_vars()`
lazily at read time. Anything else is a literal.

## Secrets management

- **Local development:** use `.env`. Git-ignored.
- **AWS target:** store secrets in **AWS Systems Manager Parameter Store** or
  **AWS Secrets Manager**. `deploy.sh` reads from your shell environment when
  running CDK; the Lambda / scraper ASG read from SSM at start-up.
- **GCP shared-host target:** secrets live in `/etc/flight-matrix/env` on the
  host (read by the systemd deployment) and `/etc/flight-matrix/env.docker`
  (a derived copy with `DATABASE_URL` rewritten to the compose `db:5432`
  service — loaded by the `web` container via `env_file:`). Both files are
  root-owned; see `scripts/gcp/deploy-app.sh`.
- Never commit secrets. The pre-commit hook runs
  [`detect-secrets`](https://github.com/Yelp/detect-secrets); CI runs
  [`gitleaks`](https://github.com/gitleaks/gitleaks).
