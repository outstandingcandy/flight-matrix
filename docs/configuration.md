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

| Variable       | Purpose                                   | Default       |
|----------------|-------------------------------------------|---------------|
| `ENVIRONMENT`  | Deployment stage: `local` / `dev` / `prod`| `local`       |
| `AWS_REGION`   | AWS region for all AWS SDK calls          | `us-east-1`   |
| `STAGE`        | Parallel to `ENVIRONMENT`, used by auth   | `local`       |

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

### Cognito authentication (production)

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

| Variable       | Purpose                                                    |
|----------------|------------------------------------------------------------|
| `APP_DOMAIN`   | Custom domain; required because API Gateway does not      |
|                | forward a custom `Host` header.                            |

## YAML configuration

`config/config.yaml` is small; it just enumerates includes. Each include is
owned by a domain module:

```
config/
├── config.yaml            # entry (includes)
├── aws.yaml               # AWS region / account overrides
├── database.yaml          # DATABASE_URL + pool settings
├── email.yaml             # SMTP / SES sender + recipients
├── api.yaml               # ADS-B API tuning
├── llm.yaml               # Claude / Bedrock model config
├── recall.yaml            # Aircraft recall / watch lists
├── reporting.yaml         # Filter engine + report body settings
├── subscription.yaml      # Multi-user subscription tiers
├── templates.yaml         # Email templates
├── aircraft_types.yaml    # ICAO type enrichment
└── scraper/
    ├── base.yaml          # Scraper framework defaults
    ├── fr24.yaml          # FR24 scraper settings
    ├── jetphotos.yaml     # JetPhotos + S3 image upload
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
- **Production:** store secrets in **AWS Systems Manager Parameter Store** or
  **AWS Secrets Manager**. `deploy.sh` reads from your shell environment when
  running CDK; the Lambda / scraper ASG read from SSM at start-up.
- Never commit secrets. The pre-commit hook runs
  [`detect-secrets`](https://github.com/Yelp/detect-secrets); CI runs
  [`gitleaks`](https://github.com/gitleaks/gitleaks).
