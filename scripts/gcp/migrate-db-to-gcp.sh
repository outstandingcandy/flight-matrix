#!/bin/bash
#
# Migrate the Flight Matrix database from Aurora PostgreSQL to the GCP host
#
# The Aurora cluster has PubliclyAccessible=False, so pg_dump cannot run from a
# laptop and no security-group change makes it possible. It runs on an in-VPC EC2
# host via SSM Session Manager instead, which needs no SSH key. The dump then
# travels to the GCP host and is restored into the Postgres container there.
#
# Three steps, runnable independently so a failure does not restart the chain:
#
#   dump      pg_dump -Fd on an in-VPC host, plus per-table source row counts
#             and (optionally) recent-window slices of the largest tables
#   transfer  move the bundle to the GCP host
#   restore   rebuild the schema from the dump and load the data, in sections
#
# Four things this procedure gets wrong if done the obvious way. Each is enforced
# below rather than left to the operator:
#
#   1. Restoring into a database the application has already touched yields the
#      APPLICATION's schema, not production's. SQLAlchemy create_all runs at
#      startup and builds tables from the models, which lag the columns added by
#      scripts/migrate_add_*.py. pg_restore's CREATE TABLE against an existing
#      table is only an ignored "already exists", so the COPY that follows lands
#      on the wrong structure -- or fails on a column production has and the
#      models do not. The restore step therefore drops the schema first and
#      asserts the table count afterwards.
#   2. The dump's size is not the restore's size. Indexes are not dumped, they
#      are rebuilt: a 26 GB Aurora volume compresses to a ~3.4 GB dump and then
#      re-expands to ~26 GB on restore. Size the destination against the source
#      database, never against the archive.
#   3. AWS-RunShellScript runs the remote script under sh, not bash, so
#      `set -o pipefail` aborts with "Illegal option". The remote scripts here
#      start with a #!/bin/bash line for that reason.
#   4. A dump of this database contains personal data (users, subscriptions,
#      user_filters, user_usage, user_cooldowns). The s3gcs channel refuses to
#      stage it in a bucket that is publicly readable.
#
# Prerequisites:
#   - AWS CLI configured; the dump host registered with SSM (PingStatus Online)
#     and able to reach Aurora on 5432
#   - The source password readable by that host from SSM Parameter Store
#     (default parameter: /flight-matrix/db-password)
#   - gcloud authenticated; the GCP host provisioned by
#     scripts/gcp/provision-existing-host.sh
#   - For --channel scp: an SSH identity on the dump host that the GCP host
#     accepts. Not automated here on purpose -- the GCP host may be shared, and
#     editing another tenant's authorized_keys or instance metadata is not
#     something a migration script should do unattended. Mint a throwaway key
#     and revoke it afterwards:
#
#       # on the dump host
#       ssh-keygen -t ed25519 -f /root/.ssh/fm-migrate -N '' -C fm-migrate
#       # on the GCP host, appending only (never rewrite instance metadata)
#       cp -n ~/.ssh/authorized_keys ~/.ssh/authorized_keys.pre-fm-migrate
#       echo '<the public key>' >> ~/.ssh/authorized_keys
#       # afterwards, matching on the comment so no other key can be hit
#       grep -v fm-migrate ~/.ssh/authorized_keys > k && mv k ~/.ssh/authorized_keys
#
# Usage:
#   ./scripts/gcp/migrate-db-to-gcp.sh --step all --label 20260817 [options]
#
# Options:
#   --step dump|transfer|restore|all   Which stage to run (default: all)
#   --label LABEL          Label shared by all three steps. Required: the steps
#                          agree on artefact names through it and nothing is
#                          derived from the clock.
#
#   Source side
#   --dump-instance ID     EC2 instance to run pg_dump on. Must be SSM-Online and
#                          able to reach Aurora. Default: the first running
#                          instance tagged Name=scraper-worker-prod
#   --aurora-host HOST     Aurora writer endpoint (default: discovered from RDS)
#   --db-name NAME         Source database (default: aircraft_data)
#   --db-user USER         Source user (default: aircraft_admin)
#   --db-password-param P  SSM Parameter Store name holding the source password
#                          (default: /flight-matrix/db-password)
#   --work-dir DIR         Where the dump is written on the dump host
#                          (default: /data/flight-matrix-dump). Must NOT be on a
#                          nearly-full root filesystem of a shared host.
#   --dump-jobs N          pg_dump parallelism (default: 4)
#   --slice T:COL:DAYS     Restore only the last DAYS days of table T, windowed on
#                          column COL. Repeatable. Use when the destination cannot
#                          hold the whole database. The window is anchored on the
#                          table's own max(COL), not on today, because a stalled
#                          pipeline makes "last 7 days of wall-clock time" empty.
#   --discard-source-dump  Delete the dump from the dump host once transferred.
#                          Default is to keep it: it is the only copy that is not
#                          on the destination.
#
#   Transfer
#   --channel scp|s3gcs    scp (default) goes host to host. s3gcs stages the
#                          bundle in S3 and streams it to GCS; it verifies both
#                          buckets block public read before writing.
#   --scp-dest USER@HOST:DIR  Destination for --channel scp
#   --scp-identity PATH    SSH private key on the dump host (default:
#                          /root/.ssh/fm-migrate)
#   --s3-bucket NAME       Staging bucket for --channel s3gcs (default: $S3_BUCKET_NAME)
#   --gcs-bucket NAME      Private GCS bucket for --channel s3gcs
#
#   Destination side
#   --vm NAME              GCP host name (default: redpanda)
#   --vm-zone ZONE         Its zone (default: us-west1-b)
#   --project ID           GCP project (default: gcloud config value)
#   --dest-dir DIR         Where the bundle lands (default: /var/tmp/fm-migrate)
#   --db-container NAME    Postgres container on the GCP host (default:
#                          flight-matrix-db). Pass "" if Postgres runs on the host.
#   --target-db NAME       Database to restore into (default: aircraft_data)
#   --target-user USER     Postgres superuser there (default: aircraft_admin)
#   --web-service NAME     systemd unit stopped for the restore
#                          (default: flight-matrix-web)
#   --jobs N               pg_restore parallelism (default: 2)
#   --keep-bundle          Keep the extracted bundle on the destination. Default
#                          is to delete it: it carries personal data and the
#                          source still has the original.
#
#   --yes                  Skip confirmation prompts
#   --dry-run              Print every command and remote script, change nothing
#
# Examples:
#   L=20260817
#   ./scripts/gcp/migrate-db-to-gcp.sh --step dump --label $L \
#       --dump-instance i-0dc7112f988ab9522 --work-dir /data/flight-matrix-dump \
#       --slice aircraft_snapshots:snapshot_time:7 \
#       --slice aircraft_realtime_positions:fr24_timestamp:7
#   ./scripts/gcp/migrate-db-to-gcp.sh --step transfer --label $L \
#       --dump-instance i-0dc7112f988ab9522 \
#       --scp-dest tangjiee@136.109.216.214:/var/tmp/fm-migrate
#   ./scripts/gcp/migrate-db-to-gcp.sh --step restore --label $L --vm redpanda
#

set -euo pipefail

STEP="all"
LABEL=""
DUMP_INSTANCE=""
AURORA_HOST=""
DB_NAME="aircraft_data"
DB_USER="aircraft_admin"
DB_PASSWORD_PARAM="/flight-matrix/db-password"
WORK_DIR="/data/flight-matrix-dump"
DUMP_JOBS=4
SLICES=()
DISCARD_SOURCE_DUMP=false
CHANNEL="scp"
SCP_DEST=""
SCP_IDENTITY="/root/.ssh/fm-migrate"
S3_BUCKET="${S3_BUCKET_NAME:-}"
GCS_BUCKET=""
VM_NAME="redpanda"
VM_ZONE="us-west1-b"
DEST_DIR="/var/tmp/fm-migrate"
DB_CONTAINER="flight-matrix-db"
TARGET_DB="aircraft_data"
TARGET_USER="aircraft_admin"
WEB_SERVICE="flight-matrix-web"
JOBS=2
KEEP_BUNDLE=false
ASSUME_YES=false
DRY_RUN=false
AWS_REGION_OPT="${AWS_REGION:-us-east-1}"
PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --step) STEP="$2"; shift 2 ;;
        --label|--dump-label) LABEL="$2"; shift 2 ;;
        --dump-instance) DUMP_INSTANCE="$2"; shift 2 ;;
        --aurora-host) AURORA_HOST="$2"; shift 2 ;;
        --db-name) DB_NAME="$2"; shift 2 ;;
        --db-user) DB_USER="$2"; shift 2 ;;
        --db-password-param) DB_PASSWORD_PARAM="$2"; shift 2 ;;
        --work-dir) WORK_DIR="$2"; shift 2 ;;
        --dump-jobs) DUMP_JOBS="$2"; shift 2 ;;
        --slice) SLICES+=("$2"); shift 2 ;;
        --discard-source-dump) DISCARD_SOURCE_DUMP=true; shift ;;
        --channel) CHANNEL="$2"; shift 2 ;;
        --scp-dest) SCP_DEST="$2"; shift 2 ;;
        --scp-identity) SCP_IDENTITY="$2"; shift 2 ;;
        --s3-bucket) S3_BUCKET="$2"; shift 2 ;;
        --gcs-bucket) GCS_BUCKET="$2"; shift 2 ;;
        --vm) VM_NAME="$2"; shift 2 ;;
        --vm-zone) VM_ZONE="$2"; shift 2 ;;
        --dest-dir) DEST_DIR="$2"; shift 2 ;;
        --db-container) DB_CONTAINER="$2"; shift 2 ;;
        --target-db) TARGET_DB="$2"; shift 2 ;;
        --target-user) TARGET_USER="$2"; shift 2 ;;
        --web-service) WEB_SERVICE="$2"; shift 2 ;;
        --jobs) JOBS="$2"; shift 2 ;;
        --keep-bundle) KEEP_BUNDLE=true; shift ;;
        --region) AWS_REGION_OPT="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --yes) ASSUME_YES=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        # Print the header block, however long it is, rather than a hard-coded
        # line range that silently truncates when the header grows.
        -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

confirm() {
    [[ "$ASSUME_YES" == true ]] && return 0
    [[ "$DRY_RUN" == true ]] && return 0
    printf '%s [y/N] ' "$1"
    read -r reply
    [[ "$reply" == "y" || "$reply" == "Y" ]]
}

# The remote scripts below are rendered through a temp file rather than assigned
# with script=$(cat <<REMOTE ...). bash 3.2 -- still /bin/bash on macOS, which is
# where this script is run from -- scans a command substitution for its closing
# paren with naive quote tracking, so a single apostrophe in the heredoc body
# ("the table's own max") makes it read to end of file and refuse to parse the
# whole script. A plain redirection keeps the heredoc out of $( ).
TMPFILES=()
cleanup_tmpfiles() {
    [[ ${#TMPFILES[@]} -gt 0 ]] && rm -f "${TMPFILES[@]+"${TMPFILES[@]}"}"
    return 0
}
trap cleanup_tmpfiles EXIT
new_tmp() {
    local f
    f=$(mktemp "${TMPDIR:-/tmp}/fm-migrate.XXXXXX") || die "cannot create a temp file"
    TMPFILES+=("$f")
    printf '%s' "$f"
}

case "$STEP" in
    dump|transfer|restore|all) ;;
    *) die "--step must be dump, transfer, restore or all" ;;
esac
case "$CHANNEL" in
    scp|s3gcs) ;;
    *) die "--channel must be scp or s3gcs" ;;
esac
[[ -n "$LABEL" ]] || die "--label is required so the three steps agree on artefact names"
[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "--label may contain only letters, digits, dot, underscore and dash"

for spec in "${SLICES[@]+"${SLICES[@]}"}"; do
    [[ "$spec" =~ ^[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*:[0-9]+$ ]] \
        || die "--slice must be TABLE:COLUMN:DAYS (got '$spec')"
done

BUNDLE="flight-matrix-${LABEL}.tar"
S3_KEY="backup/migration/$BUNDLE"

log "Label: $LABEL   bundle: $BUNDLE   channel: $CHANNEL"

# ---------------------------------------------------------------------------
# Resolving the dump host is needed by both the dump and transfer steps
# ---------------------------------------------------------------------------
resolve_dump_host() {
    if [[ -z "$DUMP_INSTANCE" ]]; then
        DUMP_INSTANCE=$(aws ec2 describe-instances --region "$AWS_REGION_OPT" \
            --filters "Name=tag:Name,Values=scraper-worker-prod" \
                      "Name=instance-state-name,Values=running" \
            --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)
        [[ -n "$DUMP_INSTANCE" && "$DUMP_INSTANCE" != "None" ]] \
            || die "no running scraper-worker-prod found; pass --dump-instance"
    fi

    local ping
    ping=$(aws ssm describe-instance-information --region "$AWS_REGION_OPT" \
        --filters "Key=InstanceIds,Values=$DUMP_INSTANCE" \
        --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)
    [[ "$ping" == "Online" ]] \
        || die "$DUMP_INSTANCE is not SSM-Online (status: ${ping:-none}); it cannot run the dump"
    log "Dump host: $DUMP_INSTANCE (SSM Online)"
}

# Run a bash script on the dump host through SSM and stream back its output.
# Waits rather than fire-and-forget: a dump that fails halfway must not be
# followed by a transfer of a truncated archive.
run_on_dump_host() {
    local script="$1" comment="$2" timeout="${3:-7200}"

    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY-RUN: aws ssm send-command --instance-ids %s (%s)\n' "$DUMP_INSTANCE" "$comment"
        printf 'DRY-RUN: remote script:\n%s\n' "$script"
        return 0
    fi

    local cmd_id state
    cmd_id=$(aws ssm send-command --region "$AWS_REGION_OPT" \
        --instance-ids "$DUMP_INSTANCE" \
        --document-name AWS-RunShellScript \
        --comment "$comment" \
        --timeout-seconds "$timeout" \
        --parameters "$(printf '%s' "$script" \
            | python3 -c 'import json,sys; print(json.dumps({"commands": sys.stdin.read().splitlines()}))')" \
        --query 'Command.CommandId' --output text)
    log "  SSM command: $cmd_id"

    while true; do
        state=$(aws ssm get-command-invocation --region "$AWS_REGION_OPT" \
            --command-id "$cmd_id" --instance-id "$DUMP_INSTANCE" \
            --query Status --output text 2>/dev/null || echo Pending)
        case "$state" in
            Success) break ;;
            Failed|Cancelled|TimedOut)
                aws ssm get-command-invocation --region "$AWS_REGION_OPT" \
                    --command-id "$cmd_id" --instance-id "$DUMP_INSTANCE" \
                    --query StandardOutputContent --output text >&2 || true
                aws ssm get-command-invocation --region "$AWS_REGION_OPT" \
                    --command-id "$cmd_id" --instance-id "$DUMP_INSTANCE" \
                    --query StandardErrorContent --output text >&2 || true
                die "$comment failed on $DUMP_INSTANCE (status $state)"
                ;;
            *) log "  $state"; sleep 20 ;;
        esac
    done

    aws ssm get-command-invocation --region "$AWS_REGION_OPT" \
        --command-id "$cmd_id" --instance-id "$DUMP_INSTANCE" \
        --query StandardOutputContent --output text
}

# The preamble every source-side script shares. The password is fetched by the
# instance itself into this process's environment: no .pgpass is written, so
# nothing is left behind on a host the migration does not own, and the value
# never becomes a process argument -- psql and pg_dump read PGPASSWORD from the
# environment. --with-decryption is a no-op for a String parameter and required
# for a SecureString one, so it covers both.
pg_preamble() {
    cat <<PREAMBLE
#!/bin/bash
set -uo pipefail
export PGHOST='$AURORA_HOST' PGPORT=5432 PGUSER='$DB_USER' PGDATABASE='$DB_NAME'
export PGCONNECT_TIMEOUT=15
PGPASSWORD=\$(aws ssm get-parameter --name '$DB_PASSWORD_PARAM' --region '$AWS_REGION_OPT' \\
    --with-decryption --query Parameter.Value --output text) || {
  echo "could not read $DB_PASSWORD_PARAM from SSM Parameter Store" >&2; exit 1; }
export PGPASSWORD
command -v pg_dump >/dev/null || {
  (apt-get update -qq && apt-get install -y -qq postgresql-client) >/dev/null 2>&1 ||
  (yum install -y -q postgresql16 || yum install -y -q postgresql) >/dev/null 2>&1; }
command -v pg_dump >/dev/null || { echo "no postgresql-client on this host" >&2; exit 1; }
PREAMBLE
}

# ---------------------------------------------------------------------------
# Step: dump
# ---------------------------------------------------------------------------
step_dump() {
    log "=== Step 1/3: pg_dump on an in-VPC host ==="

    if [[ -z "$AURORA_HOST" ]]; then
        AURORA_HOST=$(aws rds describe-db-clusters --region "$AWS_REGION_OPT" \
            --query "DBClusters[?DatabaseName=='$DB_NAME'].Endpoint | [0]" --output text 2>/dev/null || true)
        [[ -n "$AURORA_HOST" && "$AURORA_HOST" != "None" ]] \
            || die "could not discover the Aurora endpoint; pass --aurora-host"
    fi
    log "Aurora endpoint: $AURORA_HOST"

    resolve_dump_host

    confirm "Run pg_dump of $DB_NAME on production host $DUMP_INSTANCE into $WORK_DIR?" \
        || die "aborted by user"

    # A quoted array literal, not a newline-delimited string: the remote loop then
    # runs in the current shell instead of a pipeline subshell, so a failing slice
    # can abort the whole dump rather than only its own subshell. The specs are
    # already validated against a strict pattern, so nothing here needs escaping.
    local slice_array="" spec
    for spec in "${SLICES[@]+"${SLICES[@]}"}"; do
        slice_array+=" '$spec'"
    done

    local script sf
    sf=$(new_tmp)
    cat > "$sf" <<REMOTE
WORK='$WORK_DIR'
LABEL='$LABEL'
DUMP="\$WORK/$DB_NAME-\$LABEL"
SLICEDIR="\$WORK/slices-\$LABEL"
STAGE="\$WORK/bundle-\$LABEL"
TAR="\$WORK/$BUNDLE"
mkdir -p "\$WORK" "\$SLICEDIR" "\$STAGE/dump" || exit 1

echo "### connectivity and sizing"
psql -w -Atc 'select 1' >/dev/null || { echo "cannot reach $AURORA_HOST" >&2; exit 1; }
db_kb=\$(psql -w -Atc "select (pg_database_size(current_database())/1024)::bigint")
avail_kb=\$(df -Pk "\$WORK" | awk 'NR==2{print \$4}')
echo "  source database: \$((db_kb/1024)) MB"
echo "  free in \$WORK:   \$((avail_kb/1024)) MB"
# Half the database size, not the expected archive size: the archive cannot be
# predicted before it exists, and half leaves roughly 3x headroom over the ~13%
# ratio a -Z6 dump of this schema actually achieves.
if [ "\$avail_kb" -lt \$((db_kb / 2)) ]; then
  echo "  refusing to dump: need at least \$((db_kb/2/1024)) MB free in \$WORK" >&2
  echo "  pass --work-dir pointing at a filesystem with room; filling a shared" >&2
  echo "  host's root filesystem takes every other tenant down with it." >&2
  exit 1
fi

echo
echo "### per-table source row counts (the acceptance evidence)"
# Exact count(*), not pg_stat_user_tables.n_live_tup: n_live_tup is a statistics
# estimate that ANALYZE moves around, so it cannot decide whether a restore is
# complete. query_to_xml runs one count per table inside a single statement.
psql -w -At -F \$'\t' -c "
select relname,
       (xpath('/row/c/text()',
              query_to_xml('select count(*) as c from public.'||quote_ident(relname),
                           false, true, '')))[1]::text::bigint
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where c.relkind='r' and n.nspname='public' order by relname" > "\$STAGE/counts.tsv" || exit 1
wc -l < "\$STAGE/counts.tsv" | sed 's/^/  tables: /'
cp "\$STAGE/counts.tsv" "\$STAGE/expected.tsv"

echo
echo "### pg_dump --format=directory"
# -Fd, not -Fc: directory is the only format pg_dump can write in parallel, and
# it lands one file per table, which makes the compressed per-table bytes
# readable afterwards. Selective restore by table works from either format.
rm -rf "\$DUMP"
time pg_dump --format=directory --jobs=$DUMP_JOBS --compress=6 \\
  --no-owner --no-privileges --verbose --file="\$DUMP" 2>&1 | tail -5
rc=\${PIPESTATUS[0]}
[ "\$rc" = 0 ] || { echo "pg_dump exited \$rc" >&2; exit 1; }
du -sb "\$DUMP" | awk '{printf "  dump: %s bytes in ", \$1}'
ls "\$DUMP" | wc -l | sed 's/\$/ files/'
pg_restore --list "\$DUMP" > "\$STAGE/full.list" || exit 1
echo "  TOC entries: \$(grep -c '^[0-9]' "\$STAGE/full.list")"

echo
echo "### recent-window slices"
SPECS=($slice_array)
big_oids=""
if [ \${#SPECS[@]} -eq 0 ]; then
  echo "  none requested: every table is restored in full"
else
  for spec in "\${SPECS[@]}"; do
    IFS=: read -r tbl col days <<< "\$spec"
    echo "--- \$tbl: last \$days days by \$col"
    # The window is anchored on the table's own max(col), evaluated server-side
    # inside the same statement that selects the rows. Anchoring on now() gives
    # an empty slice whenever the writing pipeline has stalled, which is exactly
    # when someone reaches for this option.
    cutoff=\$(psql -w -Atc "select (select max(\$col) from public.\$tbl) - interval '\$days days'")
    f="\$SLICEDIR/\$tbl-\${days}d.dat.gz"
    # COPY ... TO STDOUT, because pg_dump cannot filter rows. The default text
    # format pairs with COPY ... FROM STDIN on the restore side and escapes
    # embedded newlines, so one output line is exactly one row and the line count
    # is a valid row count.
    time psql -w -c "\copy (select * from public.\$tbl \\
        where \$col >= (select max(\$col) from public.\$tbl) - interval '\$days days') \\
        to stdout" | gzip -6 > "\$f"
    prc=\${PIPESTATUS[0]}
    # Without this check a failed COPY leaves a valid gzip stream of nothing, the
    # row count below reads 0, and expected.tsv then records 0 as the correct
    # answer -- so the restore step would verify a silently empty table as a match.
    [ "\$prc" = 0 ] || { echo "  COPY TO STDOUT for \$tbl exited \$prc" >&2; exit 1; }
    n=\$(gunzip -c "\$f" | wc -l)
    src=\$(awk -F '\t' -v t="\$tbl" '\$1==t{print \$2}' "\$STAGE/counts.tsv")
    if [ "\$n" = 0 ] && [ "\${src:-0}" != 0 ]; then
      echo "  empty slice, but the source table holds \${src} rows." >&2
      echo "  Check that \$col is the column the rows are actually ordered by." >&2
      exit 1
    fi
    echo "  \$(stat -c %s "\$f") bytes gz, \$n of \${src:-?} rows, cutoff \$cutoff"
    printf '%s\n' "\$cutoff" > "\$SLICEDIR/\$tbl-\${days}d.cutoff"
    # Overwrite this table's expected count with the slice's, so the restore step
    # compares against what it will actually load.
    awk -v t="\$tbl" -v n="\$n" -F '\t' 'BEGIN{OFS="\t"} {if (\$1==t) \$2=n; print}' \\
      "\$STAGE/expected.tsv" > "\$STAGE/expected.tsv.new" && mv "\$STAGE/expected.tsv.new" "\$STAGE/expected.tsv"
  done
  # Resolve the sliced tables' data entries from the TOC by table name, so the
  # exclusion never depends on an OID copied out of an earlier log.
  names=\$(printf '%s\n' "\${SPECS[@]}" | cut -d: -f1 | paste -sd'|' -)
  big_oids=\$(grep -E "; +[0-9]+ +[0-9]+ +TABLE DATA +\S+ +(\$names) " "\$STAGE/full.list" \\
    | sed -E 's/^([0-9]+);.*/\1/')
  echo "  excluded TABLE DATA entries: \$(printf '%s' "\$big_oids" | tr '\n' ' ')"
fi

echo
echo "### assemble the bundle"
cp "\$DUMP/toc.dat" "\$STAGE/dump/"
for f in "\$DUMP"/*.dat.gz; do
  oid=\$(basename "\$f" .dat.gz)
  if [ -n "\$big_oids" ] && printf '%s\n' "\$big_oids" | grep -qx "\$oid"; then
    # A valid empty gzip stream stands in for an excluded table's data file.
    # restore.list already comments those entries out, but if a later invocation
    # forgets -L, pg_restore then loads zero rows instead of failing on a missing
    # file -- and zero rows is correct, because the slice is loaded separately.
    gzip -c < /dev/null > "\$STAGE/dump/\$oid.dat.gz"
  else
    cp "\$f" "\$STAGE/dump/"
  fi
done

# pg_restore -L treats ';' as a comment, so commenting a TABLE DATA line
# suppresses it while every other entry -- schema, indexes, constraints,
# sequence values -- is still taken from the full dump.
awk -v oids="\$big_oids" '
BEGIN { n=split(oids, a, "\n"); for (i=1;i<=n;i++) if (a[i] != "") skip[a[i]]=1 }
{ if (match(\$0, /^[0-9]+;/) && \$0 ~ /TABLE DATA/) {
    id=\$0; sub(/;.*/,"",id); if (id in skip) { print ";" \$0; next } }
  print }' "\$STAGE/full.list" > "\$STAGE/restore.list"

mkdir -p "\$STAGE/slices"
cp "\$SLICEDIR"/* "\$STAGE/slices/" 2>/dev/null || true

{
  echo "flight-matrix database migration bundle, label \$LABEL"
  echo "source: $AURORA_HOST database $DB_NAME"
  echo "server: \$(psql -w -Atc 'select version()')"
  echo
  echo "  dump/         pg_dump -Fd archive. Sliced tables carry empty placeholder"
  echo "                files; restore.list comments their TABLE DATA entries."
  echo "  slices/       COPY text-format slices, gzip -6."
  echo "  counts.tsv    exact source row counts at dump time."
  echo "  expected.tsv  counts.tsv with sliced tables replaced by the slice size."
  echo
  echo "Restore order -- indexes last, so they are built once over final data:"
  echo "  1. pg_restore --section=pre-data  -L restore.list dump/"
  echo "  2. pg_restore --section=data      -L restore.list dump/"
  echo "  3. COPY each slices/*.dat.gz into its table"
  echo "  4. pg_restore --section=post-data -L restore.list dump/"
  echo
  echo "Contains personal data: users, subscriptions, user_filters, user_usage,"
  echo "user_cooldowns. Do not stage this bundle in any bucket that is publicly"
  echo "readable or lacks an account-level public-access block."
} > "\$STAGE/MANIFEST.txt"

# -C so the archive holds relative paths: it is extracted into a different
# directory on the destination.
tar -cf "\$TAR" -C "\$STAGE" dump slices counts.tsv expected.tsv restore.list full.list MANIFEST.txt
sha256sum "\$TAR" > "\$TAR.sha256"
rm -rf "\$STAGE"
echo "  bundle: \$(stat -c %s "\$TAR") bytes"
cat "\$TAR.sha256"
echo "BUNDLE_READY \$TAR"
REMOTE
    script=$(pg_preamble; cat "$sf")

    local out
    out=$(run_on_dump_host "$script" "flight-matrix DB dump for GCP migration" 14400)
    printf '%s\n' "$out"
    [[ "$DRY_RUN" == true ]] && return 0
    printf '%s' "$out" | grep -q '^BUNDLE_READY ' \
        || die "the dump step did not report BUNDLE_READY; nothing was transferred"
}

# ---------------------------------------------------------------------------
# Step: transfer
# ---------------------------------------------------------------------------
assert_s3_not_public() {
    local bucket="$1" pab
    aws s3api head-bucket --bucket "$bucket" --region "$AWS_REGION_OPT" >/dev/null 2>&1 \
        || die "s3://$bucket does not exist or is not readable by these credentials"
    pab=$(aws s3api get-public-access-block --bucket "$bucket" --region "$AWS_REGION_OPT" \
        --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' \
        --output text 2>/dev/null || true)
    [[ "$pab" == $'True\tTrue\tTrue\tTrue' ]] \
        || die "s3://$bucket does not block public access (got: ${pab:-none}). The bundle contains
       personal data; stage it only in a bucket with all four public-access
       blocks on. Pass a different --s3-bucket."
}

assert_gcs_not_public() {
    local bucket="$1" public
    public=$(gcloud storage buckets get-iam-policy "gs://$bucket" --project="$PROJECT" \
        --format='value(bindings.members)' 2>/dev/null | tr ';,' '\n\n' \
        | grep -cE '^(allUsers|allAuthenticatedUsers)$' || true)
    [[ "$public" == "0" ]] \
        || die "gs://$bucket grants allUsers/allAuthenticatedUsers. The bundle contains personal
       data; pass a private --gcs-bucket instead."
}

step_transfer() {
    log "=== Step 2/3: move the bundle to the GCP host ==="
    resolve_dump_host

    if [[ "$CHANNEL" == "scp" ]]; then
        [[ -n "$SCP_DEST" ]] || die "--scp-dest USER@HOST:DIR is required for --channel scp"

        local script sf
        sf=$(new_tmp)
        cat > "$sf" <<REMOTE
#!/bin/bash
set -uo pipefail
TAR='$WORK_DIR/$BUNDLE'
[ -r "\$TAR" ] || { echo "\$TAR not found; run --step dump first" >&2; exit 1; }
[ -r '$SCP_IDENTITY' ] || {
  echo "no SSH identity at $SCP_IDENTITY." >&2
  echo "See the Prerequisites block in migrate-db-to-gcp.sh: mint a throwaway key" >&2
  echo "here and append it to the destination's authorized_keys by hand." >&2
  exit 1; }
# accept-new records the host key on first contact and aborts if it later
# changes -- the useful half of host verification without distributing the
# fingerprint in advance. BatchMode so a missing key fails instead of prompting
# into a non-interactive SSM session.
OPTS="-i $SCP_IDENTITY -o StrictHostKeyChecking=accept-new \\
  -o UserKnownHostsFile=$SCP_IDENTITY.known_hosts -o IdentitiesOnly=yes -o BatchMode=yes"
echo "sending \$(stat -c %s "\$TAR") bytes to $SCP_DEST"
dest_host=\$(printf '%s' '$SCP_DEST' | cut -d: -f1)
dest_dir=\$(printf '%s' '$SCP_DEST' | cut -d: -f2-)
ssh \$OPTS "\$dest_host" "mkdir -p '\$dest_dir'" || exit 1
time scp \$OPTS "\$TAR" "\$TAR.sha256" '$SCP_DEST/' || exit 1
echo "verifying on the destination"
# Recomputed against the received file rather than by feeding it the .sha256
# file, whose recorded path is this host's absolute path and does not exist
# there.
expect=\$(cut -d' ' -f1 < "\$TAR.sha256")
actual=\$(ssh \$OPTS "\$dest_host" "sha256sum '\$dest_dir/$BUNDLE' | cut -d' ' -f1") || exit 1
echo "  expect \$expect"
echo "  actual \$actual"
[ "\$expect" = "\$actual" ] || { echo "  CHECKSUM MISMATCH" >&2; exit 1; }
echo "  match"
if [ '$DISCARD_SOURCE_DUMP' = true ]; then
  rm -rf "\$TAR" "\$TAR.sha256" '$WORK_DIR/$DB_NAME-$LABEL' '$WORK_DIR/slices-$LABEL'
  echo "  source dump discarded"
else
  echo "  source dump kept at $WORK_DIR (the only copy that is not on the destination)"
fi
echo "TRANSFER_OK"
REMOTE
        script=$(cat "$sf")

        local out
        out=$(run_on_dump_host "$script" "flight-matrix migration bundle transfer" 7200)
        printf '%s\n' "$out"
        [[ "$DRY_RUN" == true ]] && return 0
        printf '%s' "$out" | grep -q '^TRANSFER_OK' || die "transfer did not verify"
        return 0
    fi

    # s3gcs
    [[ -n "$S3_BUCKET" ]] || die "--s3-bucket (or S3_BUCKET_NAME) is required for --channel s3gcs"
    [[ -n "$GCS_BUCKET" ]] || die "--gcs-bucket is required for --channel s3gcs"
    [[ -n "$PROJECT" ]] || die "--project or a gcloud default project is required"
    assert_s3_not_public "$S3_BUCKET"
    gcloud storage buckets describe "gs://$GCS_BUCKET" --project="$PROJECT" >/dev/null 2>&1 \
        || die "gs://$GCS_BUCKET does not exist. Create it with scripts/gcp/create-infra.sh"
    assert_gcs_not_public "$GCS_BUCKET"
    log "Both buckets block public read"

    local script sf
    sf=$(new_tmp)
    cat > "$sf" <<REMOTE
#!/bin/bash
set -uo pipefail
TAR='$WORK_DIR/$BUNDLE'
[ -r "\$TAR" ] || { echo "\$TAR not found; run --step dump first" >&2; exit 1; }
# The checksum file travels with the archive so the restore step can still verify
# what it unpacks; on this channel nothing else checks the bytes end to end.
aws s3 cp "\$TAR" 's3://$S3_BUCKET/$S3_KEY' --region '$AWS_REGION_OPT' --only-show-errors || exit 1
aws s3 cp "\$TAR.sha256" 's3://$S3_BUCKET/$S3_KEY.sha256' --region '$AWS_REGION_OPT' --only-show-errors || exit 1
echo "UPLOADED s3://$S3_BUCKET/$S3_KEY"
REMOTE
    script=$(cat "$sf")
    run_on_dump_host "$script" "flight-matrix bundle upload to S3" 7200
    [[ "$DRY_RUN" == true ]] && return 0

    local key
    for key in "$S3_KEY" "$S3_KEY.sha256"; do
        log "Streaming s3://$S3_BUCKET/$key -> gs://$GCS_BUCKET/$key"
        aws s3 cp "s3://$S3_BUCKET/$key" - --region "$AWS_REGION_OPT" \
            | gcloud storage cp - "gs://$GCS_BUCKET/$key" --project="$PROJECT"
    done

    log "Fetching it onto $VM_NAME"
    printf '%s\n' \
        "set -euo pipefail" \
        "mkdir -p '$DEST_DIR'" \
        "gcloud storage cp 'gs://$GCS_BUCKET/$S3_KEY' '$DEST_DIR/$BUNDLE'" \
        "gcloud storage cp 'gs://$GCS_BUCKET/$S3_KEY.sha256' '$DEST_DIR/$BUNDLE.sha256'" \
        "ls -l '$DEST_DIR/$BUNDLE'" \
        | gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" --command='bash -s'
}

# ---------------------------------------------------------------------------
# Step: restore
# ---------------------------------------------------------------------------
step_restore() {
    log "=== Step 3/3: rebuild the schema and load the data on $VM_NAME ==="

    [[ -n "$PROJECT" ]] || die "--project or a gcloud default project is required"
    gcloud compute instances describe "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" \
        >/dev/null 2>&1 \
        || die "host '$VM_NAME' not found in $VM_ZONE"
    log "Target: $VM_NAME ($VM_ZONE), database $TARGET_DB as $TARGET_USER"

    cat <<WARN

This drops schema "public" in $TARGET_DB on $VM_NAME and rebuilds every object
from the dump. That is not optional: the application's create_all schema differs
from production column for column, and pg_restore cannot correct an existing
table -- CREATE TABLE against one is an ignored "already exists", so restoring
in place silently keeps the wrong structure.

  --clean --if-exists is not a substitute. It drops only what the dump knows
  about, so a table or index the application created and the dump does not
  contain survives and can still break a COPY.

WARN
    confirm "Drop schema public in $TARGET_DB on $VM_NAME and restore into it?" \
        || die "aborted by user"

    # Two ways Postgres is deployed on the GCP host: in a container (what
    # provision-existing-host.sh builds) or on the host itself. Everything below
    # goes through these two indirections so the body is identical either way.
    local psql_pfx restore_pfx stage_cmd unstage_cmd stage_dir
    if [[ -n "$DB_CONTAINER" ]]; then
        psql_pfx="sudo docker exec -i $DB_CONTAINER psql"
        restore_pfx="sudo docker exec $DB_CONTAINER pg_restore"
        stage_dir="/tmp/fmr"
        # docker cp, because the container has only a named volume: no host path
        # is visible inside it. Only dump/ is copied in; the slices are streamed
        # through stdin and never land in the container.
        stage_cmd="sudo docker exec $DB_CONTAINER rm -rf $stage_dir
sudo docker exec $DB_CONTAINER mkdir -p $stage_dir
sudo docker cp '$DEST_DIR/dump' '$DB_CONTAINER:$stage_dir/dump'
sudo docker cp '$DEST_DIR/restore.list' '$DB_CONTAINER:$stage_dir/restore.list'"
        unstage_cmd="sudo docker exec $DB_CONTAINER rm -rf $stage_dir"
    else
        psql_pfx="sudo -u postgres psql"
        restore_pfx="sudo -u postgres pg_restore"
        stage_dir="$DEST_DIR"
        stage_cmd=":"
        unstage_cmd=":"
    fi

    local remote_script sf
    sf=$(new_tmp)
    cat > "$sf" <<REMOTE
#!/bin/bash
set -uo pipefail
D='$DEST_DIR'
PSQL() { $psql_pfx -U '$TARGET_USER' -d '$TARGET_DB' -v ON_ERROR_STOP=1 "\$@"; }
RESTORE() { $restore_pfx -U '$TARGET_USER' -d '$TARGET_DB' \\
    --no-owner --no-privileges -L '$stage_dir/restore.list' "\$@" '$stage_dir/dump'; }

echo "### 0. unpack"
cd "\$D" || { echo "\$D not found; run --step transfer first" >&2; exit 1; }
if [ -f '$BUNDLE' ]; then
  if [ -r '$BUNDLE.sha256' ]; then
    # Recomputed and compared by hand rather than with sha256sum -c: the recorded
    # path in that file is the dump host's absolute path, which does not exist
    # here, so -c reports "FAILED open or read" on a perfectly good archive.
    want=\$(cut -d' ' -f1 < '$BUNDLE.sha256')
    got=\$(sha256sum '$BUNDLE' | cut -d' ' -f1)
    [ "\$want" = "\$got" ] || { echo "  checksum mismatch: \$want vs \$got" >&2; exit 1; }
    echo "  checksum matches the dump host"
  fi
  tar -xf '$BUNDLE' && rm -f '$BUNDLE' '$BUNDLE.sha256'
  echo "  extracted, archive removed"
fi
[ -d dump ] && [ -r expected.tsv ] || { echo "bundle incomplete in \$D" >&2; exit 1; }
expected_tables=\$(wc -l < expected.tsv)
echo "  expecting \$expected_tables tables"

echo
echo "### 1. stop $WEB_SERVICE"
# It holds pooled connections and runs SQLAlchemy create_all at startup, which is
# what builds the wrong schema. It stays down for the whole restore, not just for
# the drop.
sudo systemctl stop '$WEB_SERVICE' 2>/dev/null && echo "  stopped" || echo "  not running"

echo
echo "### 2. reset schema public"
PSQL -c 'DROP SCHEMA IF EXISTS public CASCADE' >/dev/null || exit 1
PSQL -c 'CREATE SCHEMA public' >/dev/null || exit 1
# initdb leaves public owned by pg_database_owner; the restore runs as
# $TARGET_USER, so hand it the schema rather than leaning on a superuser bypass.
PSQL -c 'ALTER SCHEMA public OWNER TO $TARGET_USER' >/dev/null || exit 1
echo "  empty: \$(PSQL -Atc "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind='r' and n.nspname='public'") tables"
df -h / | tail -1

echo
echo "### 3. stage the archive"
$stage_cmd
echo "  staged"

echo
echo "### 4. pre-data"
RESTORE --section=pre-data 2>&1 | tail -10
n=\$(PSQL -Atc "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind='r' and n.nspname='public'")
echo "  tables created: \$n (expected \$expected_tables)"
[ "\$n" = "\$expected_tables" ] || { echo "ABORT: pre-data did not rebuild the schema" >&2; exit 1; }

echo
echo "### 5. data"
# --exit-on-error: with the schema now coming from the dump there is no
# legitimate error left in this section, so any error means the copy is not
# faithful and continuing would bury it in the log.
time RESTORE --section=data --jobs=$JOBS --exit-on-error 2>&1 | tail -10
rc=\${PIPESTATUS[0]}
[ "\$rc" = 0 ] || { echo "ABORT: data section exited \$rc" >&2; exit 1; }

echo
echo "### 6. slices"
shopt -s nullglob
for f in "\$D"/slices/*.dat.gz; do
  base=\$(basename "\$f" .dat.gz)
  tbl=\${base%-*d}
  # The table must be empty here. If restore.list failed to suppress its
  # TABLE DATA entry, the data section already loaded the full table and this COPY
  # would append a second copy of the window -- duplicating rows rather than
  # failing, since the primary key comes from the dump and would collide only
  # sometimes.
  pre=\$(PSQL -Atc "select count(*) from public.\$tbl")
  [ "\$pre" = 0 ] || { echo "ABORT: public.\$tbl already holds \$pre rows before its slice" >&2; exit 1; }
  echo "--- \$tbl <- \$(stat -c %s "\$f") bytes gz"
  # Piped, so the uncompressed gigabytes never touch a filesystem that may have
  # only a few GB spare.
  time gunzip -c "\$f" | PSQL -c "COPY public.\$tbl FROM STDIN" \\
    || { echo "ABORT: COPY failed for \$tbl" >&2; exit 1; }
done
shopt -u nullglob

echo
echo "### 7. post-data (indexes, constraints, foreign keys)"
time RESTORE --section=post-data --jobs=$JOBS --exit-on-error 2>&1 | tail -20
rc=\${PIPESTATUS[0]}
[ "\$rc" = 0 ] || { echo "ABORT: post-data exited \$rc" >&2; exit 1; }

echo
echo "### 8. ANALYZE"
time PSQL -c ANALYZE >/dev/null && echo "  ok"

echo
echo "### 9. verify row counts against the source"
PSQL -At -F \$'\t' -c "
select relname,
       (xpath('/row/c/text()',
              query_to_xml('select count(*) as c from public.'||quote_ident(relname),
                           false, true, '')))[1]::text::bigint
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where c.relkind='r' and n.nspname='public' order by relname" > "\$D/actual.tsv" || exit 1
# LC_ALL=C on both the sorts and the join: join requires its inputs ordered the
# same way it compares them, and a locale that collates differently from byte
# order makes it silently skip pairs -- which here would read as a mismatch on a
# correct restore. -k1,1 for the same reason: sort must order on the join field,
# not on the whole line.
mismatch=\$(LC_ALL=C join -t\$'\t' -a1 -a2 -e MISSING -o 0,1.2,2.2 \\
    <(LC_ALL=C sort -t\$'\t' -k1,1 expected.tsv) \\
    <(LC_ALL=C sort -t\$'\t' -k1,1 "\$D/actual.tsv") \\
  | awk -F '\t' '\$2 != \$3 { printf "  %s expected %s got %s\n", \$1, \$2, \$3 }')
if [ -n "\$mismatch" ]; then
  echo "\$mismatch"
  echo "ROW COUNTS DO NOT MATCH" >&2
  verdict=1
else
  echo "  all \$expected_tables tables match the source exactly"
  verdict=0
fi

echo
echo "### 10. schema objects"
PSQL -Atc "select 'indexes='||count(*) from pg_indexes where schemaname='public'"
PSQL -Atc "select 'fk_constraints='||count(*) from pg_constraint where contype='f'"
PSQL -Atc "select 'sequences='||count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind='S' and n.nspname='public'"
PSQL -Atc "select 'db_size='||pg_size_pretty(pg_database_size('$TARGET_DB'))"
# A sequence left behind its table's max id makes the application's first insert
# collide on the primary key. --section=pre-data/post-data both carry SEQUENCE
# SET entries, so this is a check, not a repair.
echo "--- sequences behind their column's max value (must be empty) ---"
PSQL -At -c "
select s.relname||' last_value='||pg_sequence_last_value(s.oid)
from pg_class s
join pg_namespace n on n.oid = s.relnamespace and n.nspname='public'
join pg_depend d on d.objid = s.oid and d.deptype = 'a'
join pg_attribute a on a.attrelid = d.refobjid and a.attnum = d.refobjsubid
where s.relkind='S'
  and coalesce(pg_sequence_last_value(s.oid), 0) <
      coalesce((xpath('/row/m/text()', query_to_xml(
        format('select max(%I) as m from %I.%I', a.attname, n.nspname,
               (select relname from pg_class where oid = d.refobjid)),
        false, true, '')))[1]::text::bigint, 0)"

echo
echo "### 11. unstage and restart"
$unstage_cmd
if [ '$KEEP_BUNDLE' = true ]; then
  echo "  bundle kept in \$D (it contains personal data)"
else
  cd / && rm -rf "\$D"
  echo "  bundle removed from \$D"
fi
sudo systemctl start '$WEB_SERVICE' 2>/dev/null || true
for i in \$(seq 1 60); do
  code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/ 2>/dev/null) || true
  if [ -n "\$code" ] && [ "\$code" != 000 ]; then echo "  web answers HTTP \$code"; break; fi
  sleep 1
done
systemctl is-active '$WEB_SERVICE' || true
df -h /
exit \$verdict
REMOTE
    remote_script=$(cat "$sf")

    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY-RUN: gcloud compute ssh %s --zone %s --command "bash -s"\n' "$VM_NAME" "$VM_ZONE"
        printf 'DRY-RUN: remote script:\n%s\n' "$remote_script"
        return 0
    fi

    # Fed through stdin to an explicit bash: the login shell on the GCP host is
    # not guaranteed to be bash, and this script uses bash-only syntax.
    printf '%s\n' "$remote_script" \
        | gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" --command='bash -s' \
        || die "restore failed or row counts did not match; the database on $VM_NAME is NOT a
       faithful copy. Fix the cause and re-run --step restore: it rebuilds from
       scratch, so a second attempt is safe."

    log "Restore verified against the source row counts captured at dump time."
}

case "$STEP" in
    dump) step_dump ;;
    transfer) step_transfer ;;
    restore) step_restore ;;
    all) step_dump; step_transfer; step_restore ;;
esac

log "Done (step: $STEP)"
