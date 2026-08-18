#!/bin/bash
#
# Create the single GCE VM that hosts every flight-matrix service
#
# The plan puts Postgres, Web, Track, Report, Scraper, Xvfb and Chromium on one
# machine. That is a deliberate cost trade, and it has one consequence worth
# stating up front: Chromium will happily take every spare gigabyte, so the
# memory budget below is not advisory. bootstrap-vm.sh applies it.
#
# This script only creates the instance. Host preparation happens in
# bootstrap-vm.sh, which is passed as the startup script and runs on first boot;
# application code is shipped separately by deploy-app.sh. The split matters:
# re-running deploy-app.sh must never risk re-partitioning swap or re-tuning
# Postgres.
#
# Prerequisites:
#   - scripts/gcp/create-infra.sh has run (buckets, service account, firewall)
#   - The service account exists and holds objectAdmin on both buckets
#
# Usage:
#   ./scripts/gcp/create-vm.sh [options]
#
# Options:
#   --project ID           GCP project (default: gcloud config value)
#   --zone ZONE            Zone for the instance (default: us-west1-b)
#   --name NAME            Instance name (default: flight-matrix)
#   --machine-type TYPE    (default: e2-standard-4 — 4 vCPU / 16 GB)
#   --disk-size GB         Boot disk size (default: 100)
#   --disk-type TYPE       (default: pd-balanced)
#   --service-account SA   Full SA email the VM runs as
#                          (default: flight-matrix-vm@<project>.iam.gserviceaccount.com)
#   --tag TAG              Network tag, must match create-infra.sh (default: flight-matrix)
#   --network NAME         VPC network (default: default)
#   --assets-bucket NAME   Passed to the VM as GCS_ASSETS_BUCKET
#                          (default: <project>-flight-matrix-assets)
#   --private-bucket NAME  Passed as GCS_PRIVATE_BUCKET, and the source of
#                          /etc/flight-matrix/env
#                          (default: <project>-flight-matrix-private)
#   --ip-name NAME         Attach this reserved static IP instead of an ephemeral
#                          one. Reserve it with create-infra.sh --reserve-ip.
#   --swap-size GB         Swap file size (default: 4)
#   --domain HOST          Hostname for nginx/certbot. Without it, bootstrap
#                          configures nginx on port 80 only and skips certbot.
#   --image-family FAMILY  (default: ubuntu-2404-lts-amd64)
#   --image-project PROJ   (default: ubuntu-os-cloud)
#   --preemptible          Create as Spot. Cheap, but it will be stopped without
#                          warning; Postgres lives on this disk, so only use it
#                          for a throwaway trial.
#   --yes                  Skip confirmation prompts
#   --dry-run              Print every command, change nothing
#
# Examples:
#   # See what would be created, and what it costs, without creating it
#   ./scripts/gcp/create-vm.sh --dry-run
#
#   # The real thing, with a domain so certbot can run
#   ./scripts/gcp/create-vm.sh --ip-name flight-matrix-ip --domain fm.example.com
#

set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
ZONE="us-west1-b"
VM_NAME="flight-matrix"
MACHINE_TYPE="e2-standard-4"
DISK_SIZE=100
DISK_TYPE="pd-balanced"
SERVICE_ACCOUNT=""
NET_TAG="flight-matrix"
NETWORK="default"
ASSETS_BUCKET=""
PRIVATE_BUCKET=""
IP_NAME=""
SWAP_SIZE=4
DOMAIN=""
IMAGE_FAMILY="ubuntu-2404-lts-amd64"
IMAGE_PROJECT="ubuntu-os-cloud"
PREEMPTIBLE=false
ASSUME_YES=false
DRY_RUN=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP="$SCRIPT_DIR/bootstrap-vm.sh"

while [[ $# -gt 0 ]]; do
    case $1 in
        --project) PROJECT="$2"; shift 2 ;;
        --zone) ZONE="$2"; shift 2 ;;
        --name) VM_NAME="$2"; shift 2 ;;
        --machine-type) MACHINE_TYPE="$2"; shift 2 ;;
        --disk-size) DISK_SIZE="$2"; shift 2 ;;
        --disk-type) DISK_TYPE="$2"; shift 2 ;;
        --service-account) SERVICE_ACCOUNT="$2"; shift 2 ;;
        --tag) NET_TAG="$2"; shift 2 ;;
        --network) NETWORK="$2"; shift 2 ;;
        --assets-bucket) ASSETS_BUCKET="$2"; shift 2 ;;
        --private-bucket) PRIVATE_BUCKET="$2"; shift 2 ;;
        --ip-name) IP_NAME="$2"; shift 2 ;;
        --swap-size) SWAP_SIZE="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --image-family) IMAGE_FAMILY="$2"; shift 2 ;;
        --image-project) IMAGE_PROJECT="$2"; shift 2 ;;
        --preemptible) PREEMPTIBLE=true; shift ;;
        --yes) ASSUME_YES=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) sed -n '2,62p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run() {
    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY-RUN:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

confirm() {
    [[ "$ASSUME_YES" == true || "$DRY_RUN" == true ]] && return 0
    printf '%s [y/N] ' "$1"
    read -r reply
    [[ "$reply" == "y" || "$reply" == "Y" ]]
}

[[ -n "$PROJECT" ]] || die "--project or a gcloud default project is required"
[[ "$DISK_SIZE" =~ ^[0-9]+$ ]] || die "--disk-size must be a whole number of GB"
[[ "$SWAP_SIZE" =~ ^[0-9]+$ ]] || die "--swap-size must be a whole number of GB"
[[ -f "$BOOTSTRAP" ]] || die "$BOOTSTRAP not found; it is required as the startup script"

[[ -n "$ASSETS_BUCKET" ]] || ASSETS_BUCKET="${PROJECT}-flight-matrix-assets"
[[ -n "$PRIVATE_BUCKET" ]] || PRIVATE_BUCKET="${PROJECT}-flight-matrix-private"
[[ -n "$SERVICE_ACCOUNT" ]] || SERVICE_ACCOUNT="flight-matrix-vm@${PROJECT}.iam.gserviceaccount.com"
REGION="${ZONE%-*}"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
log "Preflight"

if gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --project="$PROJECT" \
    --format='value(name)' >/dev/null 2>&1; then
    log "  instance '$VM_NAME' already exists in $ZONE — nothing to do"
    gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --project="$PROJECT" \
        --format='table(name,status,machineType.basename(),networkInterfaces[0].accessConfigs[0].natIP)'
    exit 0
fi

gcloud iam service-accounts describe "$SERVICE_ACCOUNT" --project="$PROJECT" \
    --format='value(email)' >/dev/null 2>&1 \
    || die "service account '$SERVICE_ACCOUNT' not found. Run scripts/gcp/create-infra.sh first"

for b in "$ASSETS_BUCKET" "$PRIVATE_BUCKET"; do
    gcloud storage buckets describe "gs://$b" --project="$PROJECT" >/dev/null 2>&1 \
        || die "gs://$b does not exist. Run scripts/gcp/create-infra.sh first"
done

# The firewall rule targets a network tag. If no rule matches the tag we are
# about to apply, the VM will boot unreachable on 80/443 and the failure will
# look like an nginx problem an hour later.
#
# Filtered client-side on purpose: gcloud pushes --filter to the Compute API,
# which rejects `targetTags:VALUE` outright. Combined with error swallowing that
# turns this check into a warning that always fires, which is worse than no
# check at all — so the list failing is fatal.
tag_rules=$(gcloud compute firewall-rules list --project="$PROJECT" --format=json \
    | TAG="$NET_TAG" python3 -c '
import json, os, sys

tag = os.environ["TAG"]
for rule in json.load(sys.stdin):
    if rule.get("disabled") or rule.get("direction") != "INGRESS":
        continue
    if tag in (rule.get("targetTags") or []):
        print(rule["name"])
') || die "could not list firewall rules in $PROJECT"
if [[ -z "${tag_rules//[[:space:]]/}" ]]; then
    log "  WARNING: no firewall rule targets tag '$NET_TAG'; 80/443 will be closed"
else
    log "  firewall rules targeting '$NET_TAG': $(printf '%s' "$tag_rules" | tr '\n' ' ')"
fi

# An env file is what makes the VM useful. Say so now rather than after boot.
if gcloud storage objects describe "gs://$PRIVATE_BUCKET/env" --project="$PROJECT" \
    --format='value(name)' >/dev/null 2>&1; then
    log "  env file: gs://$PRIVATE_BUCKET/env present"
else
    log "  WARNING: gs://$PRIVATE_BUCKET/env is missing."
    log "           bootstrap-vm.sh will write a placeholder and leave the app units"
    log "           disabled. Upload the real file before running deploy-app.sh:"
    log "             gcloud storage cp .env.prod gs://$PRIVATE_BUCKET/env"
fi

ADDRESS_ARG=()
if [[ -n "$IP_NAME" ]]; then
    ip_value=$(gcloud compute addresses describe "$IP_NAME" --region="$REGION" \
        --project="$PROJECT" --format='value(address)' 2>/dev/null || true)
    [[ -n "$ip_value" ]] \
        || die "static IP '$IP_NAME' not found in $REGION. Reserve it with:
  ./scripts/gcp/create-infra.sh --reserve-ip --ip-name $IP_NAME --region $REGION"
    ADDRESS_ARG=(--address="$IP_NAME")
    log "  static IP: $IP_NAME ($ip_value)"
else
    log "  static IP: none — an ephemeral address will be assigned, and it changes"
    log "             on every stop/start. Certbot needs a stable one."
fi

if [[ -n "$DOMAIN" ]]; then
    log "  domain: $DOMAIN — bootstrap will run certbot"
    if [[ -n "$IP_NAME" ]]; then
        resolved=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)
        if [[ -z "$resolved" ]]; then
            log "  WARNING: $DOMAIN does not resolve yet; certbot will fail on this boot"
        elif [[ "$resolved" != "$ip_value" ]]; then
            log "  WARNING: $DOMAIN resolves to $resolved, not $ip_value; certbot will fail"
        else
            log "  DNS check: $DOMAIN -> $resolved matches the reserved IP"
        fi
    fi
else
    log "  domain: none — nginx on port 80 only, no certbot"
fi

cat <<EOF

About to create one instance in project $PROJECT:

  name           $VM_NAME
  zone           $ZONE
  machine type   $MACHINE_TYPE$( [[ "$PREEMPTIBLE" == true ]] && printf ' (Spot)' )
  boot disk      ${DISK_SIZE} GB $DISK_TYPE
  swap           ${SWAP_SIZE} GB (file, created by bootstrap-vm.sh)
  service acct   $SERVICE_ACCOUNT
  network tag    $NET_TAG
  scopes         cloud-platform (ADC; no key material on the VM)

This is the first recurring compute cost of the gcp target. A running
e2-standard-4 plus a 100 GB pd-balanced disk is roughly \$110/month on-demand in
us-west1; the disk alone is about \$10 of that. Object storage was already
migrated separately and is unaffected by this instance.

EOF

if ! confirm "Create instance $VM_NAME in $ZONE?"; then
    die "aborted by user"
fi

# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
metadata="deploy-target=gcp"
metadata+=",gcs-assets-bucket=$ASSETS_BUCKET"
metadata+=",gcs-private-bucket=$PRIVATE_BUCKET"
metadata+=",swap-size-gb=$SWAP_SIZE"
metadata+=",fm-domain=$DOMAIN"

create_args=(
    "$VM_NAME"
    --project="$PROJECT"
    --zone="$ZONE"
    --machine-type="$MACHINE_TYPE"
    --image-family="$IMAGE_FAMILY"
    --image-project="$IMAGE_PROJECT"
    --boot-disk-size="${DISK_SIZE}GB"
    --boot-disk-type="$DISK_TYPE"
    --boot-disk-device-name="$VM_NAME"
    --network="$NETWORK"
    --tags="$NET_TAG"
    --service-account="$SERVICE_ACCOUNT"
    --scopes=https://www.googleapis.com/auth/cloud-platform
    --metadata="$metadata"
    --metadata-from-file=startup-script="$BOOTSTRAP"
    --labels=app=flight-matrix
    --description="flight-matrix all-in-one host (DEPLOY_TARGET=gcp)"
)
[[ ${#ADDRESS_ARG[@]} -gt 0 ]] && create_args+=("${ADDRESS_ARG[@]}")

# Spot instances are stopped without warning, and Postgres lives on this disk.
# --no-restart-on-failure is what Spot requires; it is also why this is a trial
# option only.
if [[ "$PREEMPTIBLE" == true ]]; then
    create_args+=(--provisioning-model=SPOT --instance-termination-action=STOP)
fi

log "Creating instance"
run gcloud compute instances create "${create_args[@]}"

if [[ "$DRY_RUN" == true ]]; then
    log "Dry run complete"
    exit 0
fi

nat_ip=$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --project="$PROJECT" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true)

cat <<EOF

Instance created. External IP: ${nat_ip:-unknown}

bootstrap-vm.sh is now running as the startup script. It installs Postgres 16,
Chromium, Xvfb, nginx and the swap file, and it takes several minutes. Follow it:

  gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT \\
      --command='sudo journalctl -u google-startup-scripts -f'

Wait for the line "flight-matrix bootstrap complete", then:

  1. Restore the database (the dump and transfer steps need no VM):
       L=<label>
       ./scripts/gcp/migrate-db-to-gcp.sh --step restore --dump-label \$L \\
           --gcs-bucket $PRIVATE_BUCKET --vm $VM_NAME --vm-zone $ZONE
  2. Ship the application:
       ./scripts/gcp/deploy-app.sh --vm $VM_NAME --vm-zone $ZONE
EOF

log "Done"
