#!/bin/bash
#
# Migrate Flight Matrix object storage from AWS S3 to Google Cloud Storage
#
# Object keys are preserved byte-for-byte. This is not cosmetic: the database
# stores relative keys like "data/jetphotos_images/N703PA_full_1769150936.jpg"
# and src/storage/base.py resolves them against whichever provider is active.
# Adding or rewriting a key prefix here would orphan every image row.
#
# Two transfer methods, chosen by object count:
#
#   sts     Storage Transfer Service. Runs on Google's infrastructure, so no
#           bytes cross this machine. Resumable, parallel, and the only sane
#           option past ~100k objects. Requires AWS credentials to be readable
#           by Google (see --auth below).
#   rsync   `gcloud storage rsync`, streaming through this machine. Fine for
#           the small static/ prefixes; it will not finish in reasonable time
#           on the multi-million-object data/ prefix.
#
# Prerequisites:
#   - gcloud authenticated, with the target project set
#   - storagetransfer.googleapis.com enabled (--method sts)
#   - The destination GCS bucket already created by scripts/gcp/create-infra.sh
#   - AWS CLI configured for read access to the source bucket
#
# Usage:
#   ./scripts/gcp/migrate-objects-to-gcp.sh --source-bucket NAME --dest-bucket NAME [options]
#
# Options:
#   --source-bucket NAME   S3 bucket to read from (required)
#   --dest-bucket NAME     GCS bucket to write to (required)
#   --project ID           GCP project (default: gcloud config value)
#   --method sts|rsync     Transfer mechanism (default: sts)
#   --prefix P             Only migrate this key prefix. Repeatable. Default is
#                          the application prefixes only — see DEFAULT_PREFIXES
#                          below for what is excluded and why.
#   --all-prefixes         Migrate the whole bucket. Do NOT combine this with a
#                          public destination: it would also copy exports/,
#                          backup/, debug/ and the source tarball.
#   --auth accesskey|federated
#                          How Storage Transfer Service reads S3.
#                          accesskey: uses AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
#                            from the environment. Simple, but hands a
#                            long-lived credential to Google.
#                          federated: uses --aws-role-arn, no secret leaves AWS.
#                            Preferred where it is available at all -- but see
#                            the note below, because in some AWS accounts it is
#                            not. Print the required trust policy with
#                            --show-trust-policy.
#   --aws-role-arn ARN     Role for --auth federated
#   --show-trust-policy    Print the IAM role + trust policy that federated auth
#                          needs, then exit. Creates nothing.
#   --delete-extras        Delete destination objects missing from the source.
#                          Off by default: this migration is additive.
#   --exclude PREFIX       Skip this key prefix. Repeatable. Applies to both
#                          methods. Use it when a broad --prefix contains
#                          something that must not become public.
#   --no-clobber           Never replace an object that already exists at the
#                          destination. Required when a second source bucket
#                          feeds a destination the first one already filled --
#                          see the comment on the rsync branch.
#   --allow-public-sensitive
#                          Permit a SENSITIVE_PREFIXES entry to be copied into a
#                          world-readable bucket. Refused by default.
#   --wait                 Poll until the transfer job finishes
#   --dry-run              Show what would run, change nothing
#
# A note on --auth federated, learned the hard way:
#
# Federated auth requires an IAM role whose trust policy names
# Principal.Federated = accounts.google.com. In an Amazon-internal AWS account
# that is a policy violation by construction -- the account forbids roles
# assumable by principals outside Amazon -- and the Palisade/Epoxy automation
# will detect the role and rewrite its trust policy from Allow to Deny within
# minutes, opening an AppSec ticket in the process. This happened to account
# 683638520402 on 2026-08-16 (Talos 58f8a016-4136-4801-bcb1-7d6849a4c65e).
#
# Do not re-create the role and flip it back; it will simply be re-mitigated.
# Handing Google a long-lived access key instead is worse, not better. In such an
# account the answer is --method rsync, which streams through a machine that
# already holds legitimate AWS credentials so Google never gets AWS access at
# all. That is viable for tens of thousands of objects; it is not viable for
# millions, which is the real constraint to plan around.
#
# Examples:
#   # Inspect what federated auth would require, before touching IAM
#   ./scripts/gcp/migrate-objects-to-gcp.sh --show-trust-policy
#
#   # Millions of objects, server-side. Needs a role that federated auth can
#   # assume, so read the --auth note above first: this exact invocation is what
#   # tripped Palisade in account 683638520402, and the role it names is gone.
#   ./scripts/gcp/migrate-objects-to-gcp.sh \
#       --source-bucket flight-matrix-static-us-east-1-683638520402 \
#       --dest-bucket outstandingcandy-flight-matrix-assets \
#       --auth federated --aws-role-arn arn:aws:iam::ACCOUNT:role/ROLE \
#       --wait
#
#   # Just the web assets, streamed locally
#   ./scripts/gcp/migrate-objects-to-gcp.sh \
#       --source-bucket flight-matrix-static-us-east-1-683638520402 \
#       --dest-bucket outstandingcandy-flight-matrix-assets \
#       --method rsync --prefix static/ --prefix web_static/ --prefix js/
#
#   # Backfill the older us-west-2 bucket into a destination us-east-1 already
#   # filled. No AWS role, nothing overwritten, login screenshots left behind.
#   ./scripts/gcp/migrate-objects-to-gcp.sh \
#       --source-bucket flight-matrix-static-683638520402 \
#       --dest-bucket outstandingcandy-flight-matrix-assets \
#       --method rsync --no-clobber \
#       --prefix data/ --prefix static/ \
#       --exclude data/xiaohongshu_screenshots/
#

set -euo pipefail

SOURCE_BUCKET=""
DEST_BUCKET=""
PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
METHOD="sts"
AUTH="federated"
AWS_ROLE_ARN=""
SHOW_TRUST_POLICY=false
STS_AGENT=""
DELETE_EXTRAS=false
ALLOW_PUBLIC_SENSITIVE=false
NO_CLOBBER=false
WAIT=false
DRY_RUN=false
ALL_PREFIXES=false
PREFIXES=()
EXCLUDES=()

# Prefixes that must never become world-readable.
#
# data/xiaohongshu_screenshots/ is not an obvious member of this list, so it is
# worth stating why: despite living under data/ alongside the image prefixes the
# application serves, it holds *login* screenshots. See the docstring at
# lib/resilient-scraper/src/resilient_scraper/scrapers/xiaohongshu/scraper.py:49
# and the login_alert_email / wait_for_login options next to it — the captures
# are the login page and QR code an operator scans, so they can contain the
# account identity and a scannable session. Note that it is a *sub*prefix of the
# default data/ scope, which is exactly why the check below has to look inside
# broad prefixes rather than only compare strings.
SENSITIVE_PREFIXES=(
    "exports/"
    "backup/"
    "debug/"
    "deploy/"
    "scripts/"
    "data/xiaohongshu_screenshots/"
)

# Prefixes the running application actually reads, and which are therefore safe
# to land in the world-readable assets bucket. Everything else is excluded
# deliberately, because create-infra.sh grants allUsers:objectViewer there:
#
#   exports/             a 951 MiB full-dataset CSV. No code path reads it, and
#                        a public copy would hand out the entire corpus in one
#                        request. Send it to the private bucket instead.
#   backup/              a pg_dump that migrate-db-to-gcp.sh supersedes.
#   debug/               scraper failure screenshots and captcha images.
#   deploy/, scripts/    deployment artifacts, present only in the older
#                        us-west-2 bucket. Not application data; the code they
#                        contain is in git, and the GCP path uses scripts/gcp/.
#   scraper-code.tar.gz  the application source, sitting at the bucket root.
#
# data/ is listed below even though data/xiaohongshu_screenshots/ is sensitive.
# That is deliberate: the screenshots prefix does not exist in the us-east-1
# bucket at all, so blanket-excluding data/ would be wrong for the common case.
# The guard resolves this per bucket by probing the source, and points at
# --exclude when it finds the prefix really is present.
#
# Move any of them across explicitly, and only into the private bucket:
#   --dest-bucket <project>-flight-matrix-private --prefix exports/
DEFAULT_PREFIXES=("data/" "js/" "static/" "web_static/")

while [[ $# -gt 0 ]]; do
    case $1 in
        --source-bucket) SOURCE_BUCKET="$2"; shift 2 ;;
        --dest-bucket) DEST_BUCKET="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --method) METHOD="$2"; shift 2 ;;
        --prefix) PREFIXES+=("$2"); shift 2 ;;
        --all-prefixes) ALL_PREFIXES=true; shift ;;
        --auth) AUTH="$2"; shift 2 ;;
        --aws-role-arn) AWS_ROLE_ARN="$2"; shift 2 ;;
        --show-trust-policy) SHOW_TRUST_POLICY=true; shift ;;
        --delete-extras) DELETE_EXTRAS=true; shift ;;
        --exclude) EXCLUDES+=("$2"); shift 2 ;;
        --no-clobber) NO_CLOBBER=true; shift ;;
        --allow-public-sensitive) ALLOW_PUBLIC_SENSITIVE=true; shift ;;
        --wait) WAIT=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) sed -n '2,112p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run() {
    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY-RUN: %s\n' "$*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Federated auth is the recommended path, so make its setup discoverable
# without this script ever writing to IAM itself.
# ---------------------------------------------------------------------------
if [[ "$SHOW_TRUST_POLICY" == true ]]; then
    [[ -n "$PROJECT" ]] || die "--project or a gcloud default project is required"

    # AWS matches the "sub" claim of the Google-issued OIDC token, which is the
    # numeric subjectId of the Storage Transfer service agent. It is NOT the
    # agent's email, and NOT the GCS service agent that
    # `gcloud storage service-agent` returns — that is a different account
    # entirely. Only googleServiceAccounts.get is authoritative here.
    agent_json=$(curl -s --max-time 30 \
        -H "Authorization: Bearer $(gcloud auth print-access-token 2>/dev/null)" \
        -H "x-goog-user-project: $PROJECT" \
        "https://storagetransfer.googleapis.com/v1/googleServiceAccounts/$PROJECT" \
        2>/dev/null || true)
    subject=$(printf '%s' "$agent_json" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("subjectId", ""))
except ValueError:
    pass
' 2>/dev/null || true)
    agent_email=$(printf '%s' "$agent_json" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("accountEmail", ""))
except ValueError:
    pass
' 2>/dev/null || true)

    if [[ -z "$subject" ]]; then
        die "could not read the Storage Transfer service agent for '$PROJECT'.
  Ensure storagetransfer.googleapis.com is enabled, then retry:
    gcloud services enable storagetransfer.googleapis.com --project=$PROJECT
  API response was: ${agent_json:-<empty>}"
    fi

    policy_bucket="${SOURCE_BUCKET:-SOURCE_BUCKET}"

    cat <<EOF
Federated auth lets Storage Transfer Service read S3 without a long-lived
AWS key. Create this role in the SOURCE AWS account, then pass its ARN via
--aws-role-arn.

Storage Transfer service agent for project "$PROJECT":
  email      $agent_email
  subjectId  $subject   <-- this is what AWS matches on

Trust policy (trust-policy.json):
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Federated": "accounts.google.com" },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": { "accounts.google.com:sub": "$subject" }
      }
    }
  ]
}

Permission policy — read-only on the source bucket only:
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::$policy_bucket"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::$policy_bucket/*"
    }
  ]
}

Commands to create it (review the account first — these are IAM writes):
  aws sts get-caller-identity
  aws iam create-role --role-name gcs-transfer \\
      --assume-role-policy-document file://trust-policy.json
  aws iam put-role-policy --role-name gcs-transfer \\
      --policy-name read-source-bucket \\
      --policy-document file://permission-policy.json
EOF
    exit 0
fi

[[ -n "$SOURCE_BUCKET" ]] || die "--source-bucket is required"
[[ -n "$DEST_BUCKET" ]] || die "--dest-bucket is required"
[[ -n "$PROJECT" ]] || die "--project or a gcloud default project is required"

if [[ "$ALL_PREFIXES" == true ]]; then
    [[ ${#PREFIXES[@]} -eq 0 ]] || die "--all-prefixes and --prefix are mutually exclusive"
elif [[ ${#PREFIXES[@]} -eq 0 ]]; then
    PREFIXES=("${DEFAULT_PREFIXES[@]}")
fi

# ---------------------------------------------------------------------------
# Preflight. Failing here is much cheaper than failing 2 TB into a transfer.
# ---------------------------------------------------------------------------
log "Preflight"

aws s3api head-bucket --bucket "$SOURCE_BUCKET" >/dev/null 2>&1 \
    || die "cannot read s3://$SOURCE_BUCKET — check AWS credentials and bucket name"

src_region=$(aws s3api get-bucket-location --bucket "$SOURCE_BUCKET" \
    --query LocationConstraint --output text 2>/dev/null)
[[ "$src_region" == "None" || -z "$src_region" ]] && src_region="us-east-1"
log "  source: s3://$SOURCE_BUCKET ($src_region)"

gcloud storage buckets describe "gs://$DEST_BUCKET" --project="$PROJECT" >/dev/null 2>&1 \
    || die "gs://$DEST_BUCKET does not exist. Create it first with scripts/gcp/create-infra.sh"
log "  dest:   gs://$DEST_BUCKET (project $PROJECT)"

if [[ "$ALL_PREFIXES" == true ]]; then
    log "  scope:  entire bucket"
else
    log "  scope:  ${PREFIXES[*]}"
fi

# create-infra.sh grants allUsers:objectViewer on the assets bucket, so a wrong
# --prefix here is silent data exposure rather than a visible error. Check the
# destination's actual IAM policy instead of assuming which bucket this is.
dest_visibility=$(gcloud storage buckets get-iam-policy "gs://$DEST_BUCKET" \
    --project="$PROJECT" --format=json 2>/dev/null | python3 -c '
import json, sys

anonymous = {"allUsers", "allAuthenticatedUsers"}
try:
    policy = json.load(sys.stdin)
except ValueError:
    print("unknown")
    raise SystemExit
for binding in policy.get("bindings", []):
    if anonymous & set(binding.get("members", [])):
        print("public")
        raise SystemExit
print("private")
' || true)

case "$dest_visibility" in
    public) log "  dest is WORLD-READABLE (allUsers/allAuthenticatedUsers bound)" ;;
    private) log "  dest is private" ;;
    *) log "  WARNING: could not read the destination IAM policy; assuming public" ;;
esac

# Does this prefix hold at least one object in the source bucket? Used to decide
# whether a sensitive subprefix is a real hazard here or merely hypothetical.
# --max-items 1 keeps it to a single cheap request, not a bucket walk.
source_prefix_has_objects() {
    local prefix="$1" found
    found=$(aws s3api list-objects-v2 --bucket "$SOURCE_BUCKET" --prefix "$prefix" \
        --max-items 1 --query 'Contents[0].Key' --output text 2>/dev/null || printf 'None')
    [[ -n "$found" && "$found" != "None" ]]
}

if [[ "$dest_visibility" != "private" && "$ALLOW_PUBLIC_SENSITIVE" != true ]]; then
    offenders=()
    contained=()
    if [[ "$ALL_PREFIXES" == true ]]; then
        offenders+=("--all-prefixes")
    else
        for p in "${PREFIXES[@]}"; do
            for s in "${SENSITIVE_PREFIXES[@]}"; do
                if [[ "$p" == "$s"* ]]; then
                    # The requested prefix sits inside a sensitive one, i.e. the
                    # caller asked for sensitive content directly. Always refuse.
                    offenders+=("$p")
                elif [[ "$s" == "$p"* ]]; then
                    # A sensitive prefix sits inside the requested one. Only a
                    # hazard if it actually exists here — data/ is a legitimate
                    # default scope, and failing on it unconditionally would make
                    # every normal run abort with a warning that is usually false.
                    # Already excluded is also not a hazard, otherwise following
                    # this check's own advice would still abort.
                    # Length-guarded because bash 3.2 (what macOS ships) treats
                    # "${arr[@]}" on an empty array as unset under `set -u`.
                    already_excluded=false
                    if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
                        for e in "${EXCLUDES[@]}"; do
                            if [[ "$s" == "$e"* ]]; then
                                already_excluded=true
                                break
                            fi
                        done
                    fi
                    if [[ "$already_excluded" == false ]] && source_prefix_has_objects "$s"; then
                        contained+=("$s")
                    fi
                fi
            done
        done
    fi

    if [[ ${#contained[@]} -gt 0 && ${#offenders[@]} -eq 0 ]]; then
        excludes=""
        for c in "${contained[@]}"; do
            excludes+=" --exclude ${c}"
        done
        die "the requested scope contains sensitive prefixes that exist in
s3://$SOURCE_BUCKET and would become world-readable in gs://$DEST_BUCKET:
    ${contained[*]}
  Exclude them and re-run:
   ${excludes}
  Or migrate them to the private bucket on their own:
    --dest-bucket ${PROJECT}-flight-matrix-private --prefix ${contained[0]}
  Pass --allow-public-sensitive only if publishing them is genuinely intended."
    fi

    if [[ ${#offenders[@]} -gt 0 ]]; then
        die "refusing to copy ${offenders[*]} into world-readable gs://$DEST_BUCKET.
  exports/ is a full-dataset CSV, backup/ is a database dump, debug/ holds
  captcha screenshots, data/xiaohongshu_screenshots/ holds login screenshots,
  and deploy/ and scripts/ are deployment artifacts. Send them to the private
  bucket instead:
    --dest-bucket ${PROJECT}-flight-matrix-private --prefix ${offenders[0]}
  Pass --allow-public-sensitive only if publishing them is genuinely intended."
    fi
fi

case "$METHOD" in
    sts)
        gcloud services list --enabled --project="$PROJECT" 2>/dev/null \
            | grep -q '^storagetransfer\.googleapis\.com' \
            || die "storagetransfer.googleapis.com is not enabled. Enable it with:
  gcloud services enable storagetransfer.googleapis.com --project=$PROJECT"

        # The Storage Transfer service agent needs bucket-level roles on the sink
        # or job creation fails with FAILED_PRECONDITION. Per
        # cloud.google.com/storage-transfer/docs/iam-cloud the minimum is:
        #   legacyBucketWriter  read bucket metadata, list, write objects
        #   objectViewer        read existing objects, so --overwrite-when's
        #                       default of "different" can skip matches. This is
        #                       what makes a second run over an overlapping
        #                       bucket nearly free instead of re-copying 1.1 TiB.
        # Granted here rather than in create-infra.sh: it is only needed while a
        # migration is running, and the revoke commands are printed at the end.
        agent_email=$(curl -s --max-time 30 \
            -H "Authorization: Bearer $(gcloud auth print-access-token 2>/dev/null)" \
            -H "x-goog-user-project: $PROJECT" \
            "https://storagetransfer.googleapis.com/v1/googleServiceAccounts/$PROJECT" \
            2>/dev/null | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("accountEmail", ""))
except ValueError:
    pass
' 2>/dev/null || true)
        [[ -n "$agent_email" ]] \
            || die "could not resolve the Storage Transfer service agent for '$PROJECT'"
        STS_AGENT="$agent_email"
        log "  service agent: $STS_AGENT"

        existing_roles=$(gcloud storage buckets get-iam-policy "gs://$DEST_BUCKET" \
            --project="$PROJECT" --format=json 2>/dev/null \
            | MEMBER="serviceAccount:$STS_AGENT" python3 -c '
import json, os, sys

member = os.environ["MEMBER"]
try:
    policy = json.load(sys.stdin)
except ValueError:
    raise SystemExit
for binding in policy.get("bindings", []):
    if member in binding.get("members", []):
        print(binding["role"])
' || true)

        for role in roles/storage.legacyBucketWriter roles/storage.objectViewer; do
            if printf '%s\n' "$existing_roles" | grep -qx "$role"; then
                log "  sink access: $role already granted"
            else
                log "  sink access: granting $role on gs://$DEST_BUCKET"
                run gcloud storage buckets add-iam-policy-binding "gs://$DEST_BUCKET" \
                    --project="$PROJECT" \
                    --member="serviceAccount:$STS_AGENT" \
                    --role="$role"
            fi
        done

        case "$AUTH" in
            federated)
                [[ -n "$AWS_ROLE_ARN" ]] \
                    || die "--auth federated needs --aws-role-arn. See --show-trust-policy"
                ;;
            accesskey)
                [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]] \
                    || die "--auth accesskey needs AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the environment"
                log "  WARNING: handing a long-lived AWS key to Google. --auth federated avoids this."
                ;;
            *) die "--auth must be 'accesskey' or 'federated'" ;;
        esac
        ;;
    rsync) ;;
    *) die "--method must be 'sts' or 'rsync'" ;;
esac

# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------
if [[ "$METHOD" == "rsync" ]]; then
    log "Transferring with gcloud storage rsync (streams through this machine)"
    rsync_flags=(--recursive)
    [[ "$DELETE_EXTRAS" == true ]] && rsync_flags+=(--delete-unmatched-destination-objects)

    # --no-clobber matters when two source buckets feed one destination, which is
    # the situation this migration is in: the us-west-2 bucket overlaps the
    # already-migrated us-east-1 one on data/jetphotos_images/ and
    # data/xiaohongshu_images/. rsync's default is "make the destination match the
    # source", so a key present in both with a differing size gets *overwritten*
    # by whichever bucket runs second. us-east-1 is the bucket the application
    # currently writes to, so letting the older west copy win is a silent data
    # regression. With -n the run is purely additive: fill the gaps, touch nothing
    # that already arrived.
    if [[ "$NO_CLOBBER" == true ]]; then
        rsync_flags+=(--no-clobber)
        log "  --no-clobber: existing destination objects will be skipped, not overwritten"
    fi

    # gcloud takes --exclude as a regex, so anchor the prefix to the start of the
    # key. Relative to the rsync source, which is the prefix itself when the run
    # is scoped, so the pattern is applied per invocation below.
    if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
        for e in "${EXCLUDES[@]}"; do
            log "  excluding $e"
        done
    fi

    build_excludes() {
        # $1 is the prefix this invocation is rooted at; keys the regex sees are
        # relative to it, so strip that root from each exclude pattern.
        local root="$1" pattern rel joined=""
        [[ ${#EXCLUDES[@]} -eq 0 ]] && { printf ''; return 0; }
        for pattern in "${EXCLUDES[@]}"; do
            rel="$pattern"
            if [[ -n "$root" && "$pattern" == "$root"* ]]; then
                rel="${pattern#"$root"}"
            elif [[ -n "$root" && "$pattern" != "$root"* ]]; then
                # Not inside this prefix; it cannot match anything here.
                continue
            fi
            [[ -n "$joined" ]] && joined+="|"
            joined+="^${rel}"
        done
        printf '%s' "$joined"
    }

    if [[ "$ALL_PREFIXES" == true ]]; then
        ex=$(build_excludes "")
        all_flags=("${rsync_flags[@]}")
        [[ -n "$ex" ]] && all_flags+=(--exclude="$ex")
        run gcloud storage rsync "s3://$SOURCE_BUCKET" "gs://$DEST_BUCKET" \
            --project="$PROJECT" "${all_flags[@]}"
    else
        for p in "${PREFIXES[@]}"; do
            log "  prefix $p"
            ex=$(build_excludes "$p")
            p_flags=("${rsync_flags[@]}")
            [[ -n "$ex" ]] && p_flags+=(--exclude="$ex")
            run gcloud storage rsync "s3://$SOURCE_BUCKET/$p" "gs://$DEST_BUCKET/$p" \
                --project="$PROJECT" "${p_flags[@]}"
        done
    fi
    log "rsync complete"
    exit 0
fi

log "Creating a Storage Transfer Service job (runs on Google infrastructure)"

# SOURCE and DESTINATION are positional, not flags. --name must be unique per
# job, so it carries the source bucket: the two source buckets are migrated as
# two separate jobs into the same destination.
sts_args=(
    "s3://$SOURCE_BUCKET"
    "gs://$DEST_BUCKET"
    --project="$PROJECT"
    --name="fm-$SOURCE_BUCKET"
    --description="flight-matrix S3->GCS migration of s3://$SOURCE_BUCKET"
)

# Both auth modes travel in --source-creds-file; gcloud has no --source-role-arn
# flag. Federated sends only the role ARN, so nothing secret is written, but the
# file is still mode 600 because the access-key branch shares this path.
creds=$(mktemp)
trap 'rm -f "$creds"' EXIT
chmod 600 "$creds"
if [[ "$AUTH" == "federated" ]]; then
    cat >"$creds" <<EOF
{"roleArn": "$AWS_ROLE_ARN"}
EOF
else
    cat >"$creds" <<EOF
{"accessKeyId": "$AWS_ACCESS_KEY_ID", "secretAccessKey": "$AWS_SECRET_ACCESS_KEY"}
EOF
fi
sts_args+=(--source-creds-file="$creds")

if [[ "$ALL_PREFIXES" != true ]]; then
    sts_args+=(--include-prefixes="$(IFS=,; echo "${PREFIXES[*]}")")
fi

# STS takes literal prefixes here, not the regex gcloud storage rsync wants, so
# --exclude values pass through unmodified. Wired up on both paths deliberately:
# an --exclude that silently applied to only one --method would be worse than no
# flag at all, since the excluded prefix is by definition something that must not
# reach a public bucket.
if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
    sts_args+=(--exclude-prefixes="$(IFS=,; echo "${EXCLUDES[*]}")")
    log "  excluding: ${EXCLUDES[*]}"
fi

# STS skips objects whose name and size already match, so a re-run over an
# overlapping bucket is cheap. --no-clobber tightens that to "never replace an
# existing object at all", which is what makes a second source bucket additive
# rather than authoritative.
if [[ "$NO_CLOBBER" == true ]]; then
    sts_args+=(--overwrite-when=never)
    log "  --no-clobber: --overwrite-when=never; existing destination objects are left alone"
fi

# Default is additive: never let a transfer delete production images.
if [[ "$DELETE_EXTRAS" == true ]]; then
    log "  WARNING: --delete-extras will remove destination objects absent from the source"
    sts_args+=(--delete-from=destination-if-unique)
fi

if [[ "$DRY_RUN" == true ]]; then
    printf 'DRY-RUN: gcloud transfer jobs create'
    printf ' %q' "${sts_args[@]}"
    printf '\n'
    exit 0
fi

job_output=$(gcloud transfer jobs create "${sts_args[@]}" --format=json)
job_name=$(printf '%s' "$job_output" | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')
log "Job created: $job_name"

if [[ "$WAIT" != true ]]; then
    log "Monitor with:  gcloud transfer jobs monitor $job_name --project=$PROJECT"
    exit 0
fi

log "Waiting for completion (Ctrl-C is safe; the job keeps running server-side)"
while true; do
    status=$(gcloud transfer operations list --job-names="$job_name" --project="$PROJECT" \
        --format='value(metadata.status)' --limit=1 2>/dev/null || true)
    case "$status" in
        SUCCESS)
            log "Transfer finished"
            break
            ;;
        FAILED|ABORTED)
            die "transfer ended with status $status — inspect with:
  gcloud transfer operations list --job-names=$job_name --project=$PROJECT"
            ;;
        *)
            log "  status: ${status:-PENDING}"
            sleep 60
            ;;
    esac
done

log "Verifying object counts"
src_count=$(aws s3 ls "s3://$SOURCE_BUCKET/" --recursive --summarize 2>/dev/null \
    | awk '/Total Objects:/ {print $3}')
dst_count=$(gcloud storage ls "gs://$DEST_BUCKET/**" --project="$PROJECT" 2>/dev/null | wc -l | tr -d ' ')
log "  source objects: ${src_count:-unknown}"
log "  dest objects:   ${dst_count:-unknown}"
log "A prefix-scoped run is expected to show fewer destination objects than the whole source bucket."

if [[ -n "$STS_AGENT" ]]; then
    cat <<EOF

Once every source bucket has been migrated, revoke the transfer agent's write
access to the sink — it is not needed by the running application:

  gcloud storage buckets remove-iam-policy-binding gs://$DEST_BUCKET \\
      --project=$PROJECT \\
      --member=serviceAccount:$STS_AGENT \\
      --role=roles/storage.legacyBucketWriter
  gcloud storage buckets remove-iam-policy-binding gs://$DEST_BUCKET \\
      --project=$PROJECT \\
      --member=serviceAccount:$STS_AGENT \\
      --role=roles/storage.objectViewer

The AWS-side role can go too:

  aws iam delete-role-policy --role-name gcs-transfer --policy-name read-flight-matrix-buckets
  aws iam delete-role --role-name gcs-transfer
EOF
fi
