#!/bin/bash
#
# Create the GCP-side infrastructure the gcp deploy target needs
#
# This is the first script to run for DEPLOY_TARGET=gcp. It creates only the
# minimal surface the plan allows — two GCS buckets, one service account, one
# firewall rule, and optionally one static IP. No Cloud SQL, no load balancer,
# no managed instance group, no Secret Manager.
#
# Every step is idempotent: existing resources are reported and left alone
# rather than recreated. That matters because the target project may host
# unrelated workloads, and a re-run must never disturb them.
#
# Port 5432 is never opened. The VM's Postgres listens on 127.0.0.1 only, so
# the database is reachable from the app processes on the same host and from
# nowhere else. The script asserts this at the end.
#
# Prerequisites:
#   - gcloud authenticated against an account with project-level admin rights
#   - Billing enabled on the project
#
# Usage:
#   ./scripts/gcp/create-infra.sh [options]
#
# Options:
#   --project ID           GCP project (default: gcloud config value)
#   --region REGION        Region for the buckets and static IP (default: us-west1)
#   --assets-bucket NAME   Public bucket for images and web assets
#                          (default: <project>-flight-matrix-assets)
#   --private-bucket NAME  Private bucket for the env file and DB dumps
#                          (default: <project>-flight-matrix-private)
#   --service-account NAME Service account id the VM runs as, 6-30 chars
#                          (default: flight-matrix-vm)
#   --network NAME         VPC network for the firewall rule (default: default)
#   --tag TAG              Network tag the firewall rule targets, and which
#                          create-vm.sh must apply (default: flight-matrix)
#   --no-public-assets     Keep the assets bucket private. Breaks browser image
#                          loading unless something else fronts the bucket.
#   --nearline-after DAYS  Lifecycle age at which archival image prefixes move
#                          to Nearline. Mirrors the existing S3 90-day
#                          transition to STANDARD_IA. 0 disables (default: 90)
#   --reserve-ip           Reserve a static external IP. Costs money while
#                          unattached, so this is opt-in: reserve it when you
#                          are ready to create the VM and point DNS at it.
#   --ip-name NAME         Static IP name (default: flight-matrix-ip)
#   --skip-apis            Do not touch service enablement
#   --yes                  Skip confirmation prompts
#   --dry-run              Print every command, change nothing
#
# Examples:
#   # See exactly what would be created
#   ./scripts/gcp/create-infra.sh --dry-run
#
#   # Buckets, service account and firewall, no static IP yet
#   ./scripts/gcp/create-infra.sh
#
#   # Later, when DNS is ready for certbot
#   ./scripts/gcp/create-infra.sh --reserve-ip
#

set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="us-west1"
ASSETS_BUCKET=""
PRIVATE_BUCKET=""
SA_NAME="flight-matrix-vm"
NETWORK="default"
NET_TAG="flight-matrix"
PUBLIC_ASSETS=true
NEARLINE_AFTER=90
RESERVE_IP=false
IP_NAME="flight-matrix-ip"
SKIP_APIS=false
ASSUME_YES=false
DRY_RUN=false

REQUIRED_APIS=(
    compute.googleapis.com
    storage.googleapis.com
    storagetransfer.googleapis.com
)

# Prefixes that are written once and read rarely. jetphotos_images is 97% of
# the corpus at ~449 KiB average, so Nearline is where the money is; the
# thumbnails prefix is deliberately excluded because it is the hot read path
# and only ~21 GiB, where a retrieval fee would cost more than it saves.
ARCHIVAL_PREFIXES=(
    "data/jetphotos_images/"
    "data/airport_data_raw/"
    "data/planespotters_raw/"
    "data/xiaohongshu_screenshots/"
)

while [[ $# -gt 0 ]]; do
    case $1 in
        --project) PROJECT="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --assets-bucket) ASSETS_BUCKET="$2"; shift 2 ;;
        --private-bucket) PRIVATE_BUCKET="$2"; shift 2 ;;
        --service-account) SA_NAME="$2"; shift 2 ;;
        --network) NETWORK="$2"; shift 2 ;;
        --tag) NET_TAG="$2"; shift 2 ;;
        --no-public-assets) PUBLIC_ASSETS=false; shift ;;
        --nearline-after) NEARLINE_AFTER="$2"; shift 2 ;;
        --reserve-ip) RESERVE_IP=true; shift ;;
        --ip-name) IP_NAME="$2"; shift 2 ;;
        --skip-apis) SKIP_APIS=true; shift ;;
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

# gcloud pushes --filter expressions to the Compute API, which rejects
# predicates on allowed[].ports outright. Fetch the rule list once and filter
# locally. Doing this server-side and swallowing the error would turn the 5432
# assertion below into a silent pass, which is the one outcome worth avoiding.
_FIREWALL_JSON=""
firewall_json() {
    if [[ -z "$_FIREWALL_JSON" ]]; then
        _FIREWALL_JSON=$(gcloud compute firewall-rules list --project="$PROJECT" \
            --format=json) || die "could not list firewall rules in $PROJECT"
    fi
    printf '%s' "$_FIREWALL_JSON"
}

# Names of enabled INGRESS rules that permit TCP on a port.
#
# Args: PORT [NETWORK] [--internet-only] [--with-sources]
#   NETWORK          restrict to this network ("" for any)
#   --internet-only   only rules reachable from outside RFC1918 space
#   --with-sources    append the source ranges to each name
list_ingress_rules_for_port() {
    local port="$1" network="${2:-}"
    shift $(( $# > 1 ? 2 : 1 ))
    local internet_only=false with_sources=false
    for flag in "$@"; do
        case "$flag" in
            --internet-only) internet_only=true ;;
            --with-sources) with_sources=true ;;
        esac
    done

    firewall_json | PORT="$port" NETWORK="$network" \
        INTERNET_ONLY="$internet_only" WITH_SOURCES="$with_sources" \
        python3 -c '
import ipaddress, json, os, sys

port = int(os.environ["PORT"])
network = os.environ["NETWORK"]
internet_only = os.environ["INTERNET_ONLY"] == "true"
with_sources = os.environ["WITH_SOURCES"] == "true"


def covers(allowed):
    for entry in allowed:
        if entry.get("IPProtocol") not in ("tcp", "all"):
            continue
        ports = entry.get("ports")
        if not ports:
            return True  # absent "ports" means every port
        for spec in ports:
            if "-" in spec:
                low, high = spec.split("-", 1)
                if int(low) <= port <= int(high):
                    return True
            elif int(spec) == port:
                return True
    return False


def from_internet(ranges):
    for entry in ranges or []:
        try:
            if not ipaddress.ip_network(entry, strict=False).is_private:
                return True
        except ValueError:
            continue
    return False


for rule in json.load(sys.stdin):
    if rule.get("disabled") or rule.get("direction") != "INGRESS":
        continue
    if network and rule.get("network", "").rsplit("/", 1)[-1] != network:
        continue
    if not covers(rule.get("allowed", [])):
        continue
    sources = rule.get("sourceRanges", [])
    if internet_only and not from_internet(sources):
        continue
    label = rule["name"]
    if with_sources:
        label += "(" + ",".join(sources) + ")"
    print(label)
'
}

[[ -n "$PROJECT" ]] || die "--project or a gcloud default project is required"
[[ "$NEARLINE_AFTER" =~ ^[0-9]+$ ]] || die "--nearline-after must be a whole number of days"
# The IAM API enforces 6-30 characters. Catch it here rather than after the
# buckets have already been created.
[[ ${#SA_NAME} -ge 6 && ${#SA_NAME} -le 30 ]] \
    || die "--service-account '$SA_NAME' must be 6-30 characters long (IAM requirement)"
[[ "$SA_NAME" =~ ^[a-z][a-z0-9-]*$ ]] \
    || die "--service-account '$SA_NAME' must be lowercase letters, digits and hyphens, starting with a letter"
[[ -n "$ASSETS_BUCKET" ]] || ASSETS_BUCKET="${PROJECT}-flight-matrix-assets"
[[ -n "$PRIVATE_BUCKET" ]] || PRIVATE_BUCKET="${PROJECT}-flight-matrix-private"

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
log "Preflight"

gcloud projects describe "$PROJECT" --format='value(projectId)' >/dev/null 2>&1 \
    || die "project '$PROJECT' not found, or the active account cannot read it"

billing=$(gcloud billing projects describe "$PROJECT" \
    --format='value(billingEnabled)' 2>/dev/null || true)
[[ "$billing" == "True" ]] \
    || die "billing is not enabled on '$PROJECT'; bucket and VM creation will fail"

log "  project: $PROJECT (billing enabled)"
log "  region:  $REGION"
log "  buckets: gs://$ASSETS_BUCKET (public=$PUBLIC_ASSETS), gs://$PRIVATE_BUCKET (private)"
log "  sa:      $SA_EMAIL"

# The target project may host unrelated workloads. Name them so a re-run of
# this script is visibly scoped to flight-matrix resources only.
existing_buckets=$(gcloud storage ls --project="$PROJECT" 2>/dev/null | tr -d ' ' || true)
if [[ -n "$existing_buckets" ]]; then
    log "  pre-existing buckets in this project (left untouched):"
    while IFS= read -r b; do
        [[ -z "$b" ]] && continue
        case "$b" in
            "gs://$ASSETS_BUCKET/"|"gs://$PRIVATE_BUCKET/") ;;
            *) log "    $b" ;;
        esac
    done <<<"$existing_buckets"
fi

if ! confirm "Create the resources listed above in project $PROJECT?"; then
    die "aborted by user"
fi

# ---------------------------------------------------------------------------
# Service enablement
# ---------------------------------------------------------------------------
if [[ "$SKIP_APIS" == true ]]; then
    log "Skipping service enablement (--skip-apis)"
else
    log "Enabling services"
    enabled=$(gcloud services list --enabled --project="$PROJECT" \
        --format='value(config.name)' 2>/dev/null || true)
    to_enable=()
    for api in "${REQUIRED_APIS[@]}"; do
        if printf '%s\n' "$enabled" | grep -qx "$api"; then
            log "  already enabled: $api"
        else
            to_enable+=("$api")
        fi
    done
    if [[ ${#to_enable[@]} -gt 0 ]]; then
        log "  enabling: ${to_enable[*]}"
        run gcloud services enable "${to_enable[@]}" --project="$PROJECT"
    fi
fi

# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------
create_bucket() {
    local name="$1" pap="$2"
    if gcloud storage buckets describe "gs://$name" --project="$PROJECT" \
        --format='value(name)' >/dev/null 2>&1; then
        log "  exists: gs://$name"
        return 0
    fi
    log "  creating gs://$name"
    run gcloud storage buckets create "gs://$name" \
        --project="$PROJECT" \
        --location="$REGION" \
        --default-storage-class=STANDARD \
        --uniform-bucket-level-access \
        "$pap"
}

log "Buckets"
# Public access prevention must be off on the assets bucket for allUsers to be
# a valid IAM member; it is enforced on the private bucket, which holds the env
# file and database dumps.
if [[ "$PUBLIC_ASSETS" == true ]]; then
    create_bucket "$ASSETS_BUCKET" --no-public-access-prevention
else
    create_bucket "$ASSETS_BUCKET" --public-access-prevention
fi
create_bucket "$PRIVATE_BUCKET" --public-access-prevention

if [[ "$PUBLIC_ASSETS" == true ]]; then
    log "  granting allUsers objectViewer on gs://$ASSETS_BUCKET"
    run gcloud storage buckets add-iam-policy-binding "gs://$ASSETS_BUCKET" \
        --project="$PROJECT" \
        --member=allUsers \
        --role=roles/storage.objectViewer
fi

# ---------------------------------------------------------------------------
# Lifecycle: mirror the S3 90-day transition to a colder class
# ---------------------------------------------------------------------------
if [[ "$NEARLINE_AFTER" -eq 0 ]]; then
    log "Lifecycle: disabled (--nearline-after 0)"
else
    log "Lifecycle: archival prefixes -> Nearline after ${NEARLINE_AFTER} days"
    lifecycle_file=$(mktemp)
    trap 'rm -f "$lifecycle_file"' EXIT
    prefix_json=$(printf '%s\n' "${ARCHIVAL_PREFIXES[@]}" \
        | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')
    cat >"$lifecycle_file" <<EOF
{
  "lifecycle": {
    "rule": [
      {
        "action": { "type": "SetStorageClass", "storageClass": "NEARLINE" },
        "condition": {
          "age": ${NEARLINE_AFTER},
          "matchesPrefix": ${prefix_json},
          "matchesStorageClass": ["STANDARD"]
        }
      }
    ]
  }
}
EOF
    for p in "${ARCHIVAL_PREFIXES[@]}"; do log "    $p"; done
    run gcloud storage buckets update "gs://$ASSETS_BUCKET" \
        --project="$PROJECT" \
        --lifecycle-file="$lifecycle_file"
fi

# ---------------------------------------------------------------------------
# Service account — bucket-scoped only, never project-wide storage admin
# ---------------------------------------------------------------------------
log "Service account"
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" \
    --format='value(email)' >/dev/null 2>&1; then
    log "  exists: $SA_EMAIL"
else
    log "  creating $SA_EMAIL"
    run gcloud iam service-accounts create "$SA_NAME" \
        --project="$PROJECT" \
        --display-name="Flight Matrix VM" \
        --description="Runs the flight-matrix services on the GCE VM"
fi

for b in "$ASSETS_BUCKET" "$PRIVATE_BUCKET"; do
    log "  granting objectAdmin on gs://$b"
    run gcloud storage buckets add-iam-policy-binding "gs://$b" \
        --project="$PROJECT" \
        --member="serviceAccount:$SA_EMAIL" \
        --role=roles/storage.objectAdmin
done

# ---------------------------------------------------------------------------
# Firewall — HTTP/HTTPS to tagged instances only
# ---------------------------------------------------------------------------
log "Firewall"
fw_name="${NET_TAG}-allow-web"
if gcloud compute firewall-rules describe "$fw_name" --project="$PROJECT" \
    --format='value(name)' >/dev/null 2>&1; then
    log "  exists: $fw_name"
else
    log "  creating $fw_name (tcp:80,443 -> tag '$NET_TAG')"
    run gcloud compute firewall-rules create "$fw_name" \
        --project="$PROJECT" \
        --network="$NETWORK" \
        --direction=INGRESS \
        --action=ALLOW \
        --rules=tcp:80,tcp:443 \
        --source-ranges=0.0.0.0/0 \
        --target-tags="$NET_TAG" \
        --description="flight-matrix web (nginx + certbot)"
fi

# SSH is normally already covered by the auto-created default-allow-ssh rule.
# Report which rule provides it rather than adding a redundant one.
ssh_rules=$(list_ingress_rules_for_port 22 "$NETWORK" | tr '\n' ' ')
if [[ -n "${ssh_rules// /}" ]]; then
    log "  ssh (22) already allowed by: ${ssh_rules% }"
else
    log "  creating ${NET_TAG}-allow-ssh (tcp:22 -> tag '$NET_TAG')"
    run gcloud compute firewall-rules create "${NET_TAG}-allow-ssh" \
        --project="$PROJECT" \
        --network="$NETWORK" \
        --direction=INGRESS \
        --action=ALLOW \
        --rules=tcp:22 \
        --source-ranges=0.0.0.0/0 \
        --target-tags="$NET_TAG" \
        --description="flight-matrix ssh"
fi

# ---------------------------------------------------------------------------
# Static IP (opt-in)
# ---------------------------------------------------------------------------
if [[ "$RESERVE_IP" == true ]]; then
    log "Static IP"
    if gcloud compute addresses describe "$IP_NAME" --region="$REGION" \
        --project="$PROJECT" --format='value(name)' >/dev/null 2>&1; then
        log "  exists: $IP_NAME"
    else
        log "  reserving $IP_NAME in $REGION"
        run gcloud compute addresses create "$IP_NAME" \
            --project="$PROJECT" --region="$REGION"
    fi
    reserved_ip=$(gcloud compute addresses describe "$IP_NAME" --region="$REGION" \
        --project="$PROJECT" --format='value(address)' 2>/dev/null || true)
    [[ -n "$reserved_ip" ]] && log "  address: $reserved_ip — point the DNS A record here before certbot"
else
    log "Static IP: skipped (pass --reserve-ip when you are ready to create the VM)"
fi

# ---------------------------------------------------------------------------
# Assert the database port is not exposed
# ---------------------------------------------------------------------------
log "Verifying Postgres is not reachable from the internet"
pg_rules=$(list_ingress_rules_for_port 5432 "" --internet-only --with-sources | tr '\n' ' ')
if [[ -n "${pg_rules// /}" ]]; then
    printf 'WARNING: an ingress rule exposes 5432: %s\n' "${pg_rules% }" >&2
    printf 'WARNING: the gcp target expects Postgres bound to 127.0.0.1 only. Review this.\n' >&2
else
    log "  no ingress rule opens 5432"
fi

# ---------------------------------------------------------------------------
# Output the environment block
# ---------------------------------------------------------------------------
cat <<EOF

Infrastructure ready. Add these to /etc/flight-matrix/env on the VM (the file
that bootstrap-vm.sh pulls from gs://$PRIVATE_BUCKET/env):

  DEPLOY_TARGET=gcp
  GOOGLE_CLOUD_PROJECT=$PROJECT
  GCS_ASSETS_BUCKET=$ASSETS_BUCKET
  GCS_PRIVATE_BUCKET=$PRIVATE_BUCKET

STATIC_BASE_URL is optional: src/storage/factory.py derives
https://storage.googleapis.com/$ASSETS_BUCKET from GCS_ASSETS_BUCKET. Set it
only to front the bucket with a different host.

Next steps:
  1. Migrate objects (does not need the VM, and is the long pole):
       ./scripts/gcp/migrate-objects-to-gcp.sh --show-trust-policy
       ./scripts/gcp/migrate-objects-to-gcp.sh \\
           --source-bucket <s3-bucket> --dest-bucket $ASSETS_BUCKET --wait
  2. Create the VM, then migrate the database:
       ./scripts/gcp/create-vm.sh --tag $NET_TAG --service-account $SA_EMAIL
       ./scripts/gcp/migrate-db-to-gcp.sh --step all --dump-label <label> \\
           --gcs-bucket $PRIVATE_BUCKET
EOF

log "Done"
