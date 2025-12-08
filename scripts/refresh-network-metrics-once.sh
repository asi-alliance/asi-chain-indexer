#!/bin/sh
set -e

LOOKBACK_HOURS="${LOOKBACK_HOURS:-168}"
BUCKET_SECONDS="${BUCKET_SECONDS:-300}"

DB_URL=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --db_url)
      DB_URL="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

# fallback to env (for metrics-cron container)
if [ -z "$DB_URL" ]; then
  DB_URL="$DATABASE_URL"
fi

now() {
  date -u +"%Y-%m-%dT%H:%M:%S%z"
}

if [ -z "$DB_URL" ]; then
  echo "[metrics-cron] $(now) ❌ DB URL is not set (neither --db_url nor \$DATABASE_URL)"
  exit 1
fi

echo "[metrics-cron] $(now) Running refresh_network_metrics_buckets(LOOKBACK=${LOOKBACK_HOURS}, BUCKET=${BUCKET_SECONDS})..."

if psql "$DB_URL" -c \
  "SELECT public.refresh_network_metrics_buckets(${LOOKBACK_HOURS}, ${BUCKET_SECONDS});"; then
  echo "[metrics-cron] $(now) ✅ refresh_network_metrics_buckets finished successfully"
else
  echo "[metrics-cron] $(now) ⚠️ refresh_network_metrics_buckets failed"
fi
