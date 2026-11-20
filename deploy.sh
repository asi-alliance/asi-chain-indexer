#!/bin/bash
set -e

CLEAN_DEPLOY=false

# --- Parse arguments ---
for arg in "$@"; do
  case $arg in
    --clean)
      CLEAN_DEPLOY=true
      shift
      ;;
  esac
done

# --- Load .env if exists ---
if [ -f ".env" ]; then
    sed -i 's/\r$//' .env
    set -o allexport
    # shellcheck source=/dev/null
    source .env
    set +o allexport
fi

# --- Required environment variables ---
REQUIRED_VARS=(NODE_HOST HTTP_PORT GRPC_PORT HASURA_BASE HASURA_ADMIN_SECRET)

for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        echo "❌ ERROR: Environment variable $var is not set or empty in .env"
        exit 1
    fi
done

# ASI defaults are intentional; legacy installations can override both settings.
export NODE_PROTOCOL_PROFILE="${NODE_PROTOCOL_PROFILE:-uint256}"
export NATIVE_TOKEN_DECIMALS="${NATIVE_TOKEN_DECIMALS:-18}"
case "$NODE_PROTOCOL_PROFILE" in
    classic|uint256) ;;
    *) echo "ERROR: NODE_PROTOCOL_PROFILE must be classic or uint256"; exit 1 ;;
esac
if ! [[ "$NATIVE_TOKEN_DECIMALS" =~ ^(0|[1-9][0-9]?)$ ]] ||
   [ "$NATIVE_TOKEN_DECIMALS" -gt 77 ]; then
    echo "ERROR: NATIVE_TOKEN_DECIMALS must be an integer from 0 to 77"
    exit 1
fi

echo "Required environment variables are set."
echo "Selected node profile: $NODE_PROTOCOL_PROFILE; native decimals: $NATIVE_TOKEN_DECIMALS"
# Validate configuration before any container or volume mutation.
docker compose config --quiet

# --- Optional clean deploy ---
if [ "$CLEAN_DEPLOY" = true ]; then
  echo "⚠️  Performing CLEAN deploy: stopping containers, removing volumes and images..."
  docker compose down -v --remove-orphans || true

  echo "🧹 Removing local images related to the project..."
  images=$(docker images --format "{{.Repository}}" | grep -E "indexer|hasura|postgres" || true)
  if [ -n "$images" ]; then
      echo "$images" | xargs -r docker rmi -f
  else
      echo "No project-related images found to remove."
  fi

  echo "✅ Clean environment ready for fresh build."
  echo
fi

echo "--- Building and starting containers ---"
docker compose up -d --build

echo "--- Waiting for containers to be up ---"

timeout=30
interval=4
elapsed=0

SERVICES=$(docker compose ps --services)

while true; do
  all_ready=true

  for svc in $SERVICES; do
    line=$(docker compose ps "$svc" | awk 'NR==2')
    if [ -z "$line" ]; then
      all_ready=false
      continue
    fi

    # ready if Up or Exit 0
    if echo "$line" | grep -qE "Up|Exit 0"; then
      continue
    else
      all_ready=false
    fi
  done

  if [ "$all_ready" = true ]; then
    echo "✅ All services are up (Up/Exit 0)"
    break
  fi

  elapsed=$((elapsed + interval))
  if [ "$elapsed" -ge "$timeout" ]; then
    echo "⚠️  Timeout waiting for services to be up (>${timeout}s)"
    docker compose ps
    break
  fi

  echo "Waiting for containers to be ready..."
  sleep "$interval"
done

echo "--- Running Hasura configuration script ---"
if [ -f "./scripts/full-init-hasura.sh" ]; then
    chmod +x ./scripts/full-init-hasura.sh
    ./scripts/full-init-hasura.sh
else
    echo "⚠️  ./scripts/full-init-hasura.sh not found, skipping."
fi

echo "--- Running basic Hasura tests (PUBLIC) ---"

HASURA_BASE="${HASURA_BASE:-http://localhost:8080}"
HASURA_URL="${HASURA_URL:-$HASURA_BASE/v1/graphql}"

echo "Checking Hasura availability at $HASURA_URL..."
status_code=$(curl -s -o /dev/null -w "%{http_code}" "$HASURA_URL" || echo "000")

if echo "$status_code" | grep -qE "200|400"; then
    echo "✅ Hasura endpoint reachable! (HTTP $status_code)"
else
    echo "⚠️  Hasura not responding at $HASURA_URL (HTTP $status_code)"
    echo "--- Done (skipping tests) ---"
    exit 0
fi

# ------------------------------------------------------------
# PUBLIC select should PASS
# ------------------------------------------------------------
PUBLIC_SELECT_QUERY='{"query":"{ blocks(limit:1, order_by:{block_number:desc}) { block_number block_hash } }"}'

echo "▶ PUBLIC SELECT test (should PASS, no admin secret)..."
select_resp=$(curl -s -X POST "$HASURA_URL" \
  -H "Content-Type: application/json" \
  -d "$PUBLIC_SELECT_QUERY")

# Fail if GraphQL returned errors
if echo "$select_resp" | grep -q '"errors"'; then
  echo "❌ PUBLIC SELECT failed (expected success). Response:"
  echo "$select_resp"
  exit 1
fi

echo "✅ PUBLIC SELECT passed."
command -v jq >/dev/null 2>&1 && echo "$select_resp" | jq . || echo "$select_resp"

# ------------------------------------------------------------
# PUBLIC aggregate tests
#   * blocks_aggregate       -> should FAIL (allow_aggregations=false)
#   * deployments_aggregate  -> should SUCCEED (allow_aggregations=true, used by explorer frontend)
#   * transfers_aggregate    -> should SUCCEED (allow_aggregations=true, used by explorer frontend)
# ------------------------------------------------------------
PUBLIC_AGG_BLOCKS_QUERY='{"query":"{ blocks_aggregate { aggregate { count } } }"}'

echo "▶ PUBLIC AGGREGATE test on blocks (should FAIL, allow_aggregations=false)..."
agg_resp=$(curl -s -X POST "$HASURA_URL" \
  -H "Content-Type: application/json" \
  -d "$PUBLIC_AGG_BLOCKS_QUERY")

# We EXPECT errors here. If no errors -> fail the deploy check.
if echo "$agg_resp" | grep -q '"errors"'; then
  echo "✅ PUBLIC AGGREGATE on blocks correctly rejected."
  command -v jq >/dev/null 2>&1 && echo "$agg_resp" | jq . || echo "$agg_resp"
else
  echo "❌ PUBLIC AGGREGATE on blocks unexpectedly succeeded (expected errors). Response:"
  echo "$agg_resp"
  exit 1
fi

PUBLIC_AGG_TX_QUERY='{"query":"{ deployments_aggregate { aggregate { count } } transfers_aggregate { aggregate { count } } }"}'

echo "▶ PUBLIC AGGREGATE test on deployments + transfers (should SUCCEED, allow_aggregations=true)..."
tx_agg_resp=$(curl -s -X POST "$HASURA_URL" \
  -H "Content-Type: application/json" \
  -d "$PUBLIC_AGG_TX_QUERY")

# We EXPECT success here (these two tables have allow_aggregations=true so the
# explorer's /transactions counters work).
if echo "$tx_agg_resp" | grep -q '"errors"'; then
  echo "❌ PUBLIC AGGREGATE on deployments/transfers failed (expected success). Response:"
  echo "$tx_agg_resp"
  exit 1
else
  echo "✅ PUBLIC AGGREGATE on deployments + transfers passed."
  command -v jq >/dev/null 2>&1 && echo "$tx_agg_resp" | jq . || echo "$tx_agg_resp"
fi

echo "--- Done! ---"
