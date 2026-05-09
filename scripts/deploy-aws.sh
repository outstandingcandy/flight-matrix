#!/usr/bin/env bash
#
# Flight Matrix — AWS one-command deploy wrapper.
#
# Wraps ./deploy.sh with up-front checks and interactive .env bootstrap so
# a first-time deploy just works. Reasonable defaults; only prompts for the
# values the user actually has to pick.
#
# What it does:
#   1. Verify aws-cli, cdk, docker, uv are installed.
#   2. Confirm AWS identity (account, region).
#   3. Create or audit .env; fill in the required keys interactively.
#   4. Bootstrap CDK in the target region if needed.
#   5. Hand off to the chosen ./deploy.sh subcommand.
#
# Read docs/deployment.md for the full picture.
#
# Usage:
#   ./scripts/deploy-aws.sh              # full deploy (interactive)
#   ./scripts/deploy-aws.sh deploy       # full deploy (alias for default)
#   ./scripts/deploy-aws.sh update       # Lambda + scraper + static files
#   ./scripts/deploy-aws.sh webapp       # webapp only (Lambda + S3 + invalidation)
#   ./scripts/deploy-aws.sh status       # stack outputs + health summary
#   ./scripts/deploy-aws.sh destroy      # tear down (double-confirm)
#   ./scripts/deploy-aws.sh --check      # preflight only; no deploy
#   ./scripts/deploy-aws.sh --help

set -euo pipefail

if [[ -t 1 ]]; then
    RED=$'\033[31m' GREEN=$'\033[32m' YELLOW=$'\033[33m' BLUE=$'\033[34m' CYAN=$'\033[36m'
    BOLD=$'\033[1m' RESET=$'\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' RESET=''
fi

step() { printf "${BLUE}==>${RESET} ${BOLD}%s${RESET}\n" "$*"; }
ok()   { printf "    ${GREEN}✓${RESET} %s\n" "$*"; }
warn() { printf "    ${YELLOW}!${RESET} %s\n" "$*"; }
fail() { printf "    ${RED}✗${RESET} %s\n" "$*" >&2; }
die()  { fail "$*"; exit 1; }
ask()  { local prompt="$1" default="${2:-}" reply
    if [[ -n $default ]]; then
        read -r -p "    $prompt [$default]: " reply || true
        echo "${reply:-$default}"
    else
        read -r -p "    $prompt: " reply || true
        echo "$reply"
    fi
}

# --- Arg parsing ------------------------------------------------------------
CHECK_ONLY=0
SUBCOMMAND=deploy
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        -h|--help)
            sed -n 's/^# //;s/^#//;/^Usage/q;p' "$0" | head -30
            exit 0
            ;;
        deploy|update|webapp|webapp-env|scraper|status|destroy|synth|diff|fetch-jwks|create-user)
            SUBCOMMAND="$1"
            ;;
        -*) die "unknown flag: $1 (use --help)" ;;
        *) die "unknown subcommand: $1 (use --help)" ;;
    esac
    shift
done

# --- Project root -----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"
# Production config lives in .env.prod. Legacy single-file .env is still
# accepted if it exists and .env.prod doesn't (warn the operator).
ENV_FILE="$PROJECT_ROOT/.env.prod"
if [[ ! -f $ENV_FILE ]] && [[ -f "$PROJECT_ROOT/.env" ]]; then
    warn "Using legacy .env — rename to .env.prod to make the dev/prod split explicit."
    ENV_FILE="$PROJECT_ROOT/.env"
fi

# --- 1. Prerequisites -------------------------------------------------------
step "Checking AWS prerequisites"

missing=()
for tool in aws docker python3; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done

# cdk is optional at this point — it can be installed via npm after the fact.
if ! command -v cdk >/dev/null 2>&1; then
    warn "aws-cdk not found. Install with: npm install -g aws-cdk"
    warn "Skipping CDK checks for now."
    HAS_CDK=0
else
    HAS_CDK=1
    ok "cdk ($(cdk --version 2>/dev/null | head -1))"
fi

if command -v uv >/dev/null 2>&1; then
    ok "uv ($(uv --version | awk '{print $2}'))"
else
    warn "uv not found (needed by deploy.sh). Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    missing+=("uv")
fi

if (( ${#missing[@]} > 0 )); then
    die "missing tools: ${missing[*]}"
fi
ok "aws ($(aws --version 2>&1 | awk '{print $1}'))"
ok "docker ($(docker --version | awk '{print $3}' | tr -d ,))"

# --- 2. AWS identity --------------------------------------------------------
step "Verifying AWS credentials"

if ! AWS_IDENTITY=$(aws sts get-caller-identity --output json 2>/dev/null); then
    die "aws sts get-caller-identity failed. Run 'aws configure' or set AWS env vars."
fi
ACCOUNT_ID=$(echo "$AWS_IDENTITY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Account"])')
ARN=$(echo "$AWS_IDENTITY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Arn"])')
CURRENT_REGION=$(aws configure get region 2>/dev/null || echo "")
ok "account: $ACCOUNT_ID"
ok "identity: $ARN"
ok "region (profile default): ${CURRENT_REGION:-<unset>}"

# --- 3. .env bootstrap ------------------------------------------------------
# Read-only subcommands only need the file to exist; they skip the
# interactive prompts, the secret generation, and the fresh-deploy check.
READ_ONLY=0
case "$SUBCOMMAND" in
    status|synth|diff|fetch-jwks) READ_ONLY=1 ;;
esac

step "Checking .env configuration"

if [[ ! -f $ENV_FILE ]]; then
    if (( READ_ONLY )); then
        die ".env not found. Run ./scripts/deploy-aws.sh deploy first."
    fi
    cp .env.example "$ENV_FILE"
    ok "created .env from .env.example"
fi

# Helper: read a key's current value from .env (empty if missing or blank).
env_get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | sed "s|^$1=||;s|^\"||;s|\"$||" || true; }

# Helper: set or replace a key in .env.
env_set() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        if sed --version >/dev/null 2>&1; then
            sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
        else
            sed -i '' "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
        fi
    else
        printf "%s=%s\n" "$key" "$value" >> "$ENV_FILE"
    fi
}

# Required core values. AWS_ACCOUNT_ID comes from STS so we don't prompt.
env_set AWS_ACCOUNT_ID "$ACCOUNT_ID"
ok "AWS_ACCOUNT_ID set to $ACCOUNT_ID"

region=$(env_get AWS_REGION)
if [[ -z $region ]]; then
    region=$(ask "AWS region" "${CURRENT_REGION:-us-east-1}")
    env_set AWS_REGION "$region"
fi
ok "AWS_REGION=$region"

env_set ENVIRONMENT "$(env_get ENVIRONMENT || echo prod)"
ok "ENVIRONMENT=$(env_get ENVIRONMENT)"

if (( ! READ_ONLY )); then
    # DB password — generate a strong one if missing or too short.
    db_password=$(env_get DB_PASSWORD)
    if [[ ${#db_password} -lt 16 ]]; then
        if [[ -n $db_password ]]; then
            warn "DB_PASSWORD is shorter than 16 chars — regenerating"
        fi
        db_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
        env_set DB_PASSWORD "$db_password"
        ok "generated a 32-char DB_PASSWORD — stored in .env"
    else
        ok "DB_PASSWORD is set (${#db_password} chars)"
    fi

    # Flask secret — always ensure it's present and long.
    flask_secret=$(env_get FLASK_SECRET_KEY)
    if [[ ${#flask_secret} -lt 32 ]]; then
        flask_secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
        env_set FLASK_SECRET_KEY "$flask_secret"
        ok "generated FLASK_SECRET_KEY"
    else
        ok "FLASK_SECRET_KEY is set"
    fi

    # Optional but commonly needed.
    missing_optional=()
    for key in TAVILY_API_KEY ADSB_API_KEY; do
        if [[ -z $(env_get "$key") ]]; then
            missing_optional+=("$key")
        fi
    done
    if (( ${#missing_optional[@]} > 0 )); then
        warn "optional API keys not set: ${missing_optional[*]}"
        warn "  TAVILY_API_KEY  — web search for AI analysis (optional)"
        warn "  ADSB_API_KEY    — ADS-B Exchange via RapidAPI (required for track service)"
        warn "edit .env to fill them in later"
    fi

    # Import-mode IDs — if any are blank we assume fresh deploy.
    # Only relevant for the initial `deploy`; update / webapp / scraper run
    # against an already-provisioned stack.
    if [[ $SUBCOMMAND == "deploy" ]]; then
        missing_infra=()
        for key in VPC_ID S3_BUCKET_NAME; do
            if [[ -z $(env_get "$key") ]]; then
                missing_infra+=("$key")
            fi
        done

        if (( ${#missing_infra[@]} > 0 )); then
            warn "infra IDs not set: ${missing_infra[*]}"
            if (( ! CHECK_ONLY )); then
                reply=$(ask "Run a FRESH deploy (create all infra from scratch)? [y/N]" "N")
                case "$reply" in
                    y|Y|yes|YES) env_set FRESH_DEPLOY true ; ok "FRESH_DEPLOY=true";;
                    *) die "Set VPC_ID, S3_BUCKET_NAME (and related subnet/SG IDs) in .env for import-mode deploy, or rerun and answer y for fresh deploy.";;
                esac
            fi
        fi
    fi
fi

# --- 4. CDK bootstrap (idempotent, skip for read-only) ---------------------
if (( HAS_CDK )) && (( ! READ_ONLY )) && [[ $SUBCOMMAND == "deploy" ]]; then
    step "Checking CDK bootstrap"
    if aws cloudformation describe-stacks --stack-name CDKToolkit --region "$region" >/dev/null 2>&1; then
        ok "CDKToolkit already bootstrapped in $region"
    else
        if (( CHECK_ONLY )); then
            warn "CDKToolkit missing in $region — run: cdk bootstrap aws://$ACCOUNT_ID/$region"
        else
            step "Bootstrapping CDK in $region (one-time)"
            cdk bootstrap "aws://$ACCOUNT_ID/$region"
            ok "CDK bootstrapped"
        fi
    fi
fi

# --- 5. Done / hand off -----------------------------------------------------
echo
if (( CHECK_ONLY )); then
    printf "${BOLD}Preflight OK.${RESET} To deploy for real, run:\n"
    echo "    ./scripts/deploy-aws.sh           # full deploy"
    echo "    ./scripts/deploy-aws.sh update    # fast update"
    echo "    ./scripts/deploy-aws.sh status    # stack outputs"
    exit 0
fi

# Confirmation blurb depends on the subcommand.
case "$SUBCOMMAND" in
    status|synth|diff|fetch-jwks)
        # Read-only — no confirmation needed.
        exec ./deploy.sh "$SUBCOMMAND"
        ;;
    destroy)
        cat <<EOF

${BOLD}${RED}⚠  ABOUT TO DESTROY ALL AWS RESOURCES${RESET}

Account : $ACCOUNT_ID
Region  : $region
Env     : $(env_get ENVIRONMENT)

This deletes the Lambda, CloudFront, scraper ASG, and — if this was a
FRESH_DEPLOY stack — the Aurora cluster and S3 bucket. ${YELLOW}Data will be lost.${RESET}
EOF
        reply=$(ask "Type 'destroy' to confirm" "")
        [[ $reply == "destroy" ]] || die "aborted by user"
        reply=$(ask "Are you absolutely sure? [y/N]" "N")
        case "$reply" in
            y|Y|yes|YES) ;;
            *) die "aborted by user" ;;
        esac
        exec ./deploy.sh destroy
        ;;
    deploy|update|webapp|webapp-env|scraper|create-user)
        action_verb="${SUBCOMMAND^^}"
        cat <<EOF

${BOLD}About to run ${CYAN}./deploy.sh $SUBCOMMAND${RESET}${BOLD}.${RESET}

Account : $ACCOUNT_ID
Region  : $region
Env     : $(env_get ENVIRONMENT)
Mode    : $([[ $(env_get FRESH_DEPLOY) = true ]] && echo "fresh" || echo "import")

This will modify AWS resources and ${YELLOW}may incur charges${RESET}.
EOF
        reply=$(ask "Proceed with $action_verb? [y/N]" "N")
        case "$reply" in
            y|Y|yes|YES) ;;
            *) die "aborted by user" ;;
        esac
        exec ./deploy.sh "$SUBCOMMAND"
        ;;
esac
