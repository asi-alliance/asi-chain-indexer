#!/bin/bash
set -e

# ============================================================
# FULL HASURA INITIALIZATION FOR ASI-CHAIN INDEXER
# ============================================================

# Load .env if present
if [ -f ".env" ]; then
    sed -i 's/\r$//' .env
    set -o allexport
    source .env
    set +o allexport
fi

HASURA_ADMIN_SECRET="${HASURA_ADMIN_SECRET:-myadminsecretkey}"
HASURA_BASE="${HASURA_BASE:-http://localhost:8080}"

HASURA_ENDPOINT="${HASURA_BASE}/v1/metadata"
HASURA_GRAPHQL="${HASURA_BASE}/v1/graphql"
HASURA_SQL="${HASURA_BASE}/v2/query"
HASURA_HEALTH="${HASURA_BASE}/healthz"

echo "Metadata endpoint: $HASURA_ENDPOINT"
echo "SQL endpoint:      $HASURA_SQL"
echo "GraphQL endpoint:  $HASURA_GRAPHQL"
echo "Health endpoint:   $HASURA_HEALTH"

admin_secret="${HASURA_GRAPHQL_ADMIN_SECRET:-$HASURA_ADMIN_SECRET}"

# Wrapper for metadata API
hasura_metadata() {
  local payload="$1"
  curl -s -X POST "$HASURA_ENDPOINT" \
    -H "Content-Type: application/json" \
    -H "x-hasura-admin-secret: $admin_secret" \
    -d "$payload"
}

# Wrapper for GraphQL queries
graphql_query() {
  local payload="$1"
  curl -s -X POST "$HASURA_GRAPHQL" \
    -H "Content-Type: application/json" \
    -H "x-hasura-admin-secret: $admin_secret" \
    -d "$payload"
}

# ------------------------------------------------------------
# Wait for Hasura
# ------------------------------------------------------------
echo "Waiting for Hasura to be ready..."
until curl -s "$HASURA_HEALTH" >/dev/null 2>&1; do
  sleep 2
done
echo "Hasura is ready."

# ------------------------------------------------------------
# TRACK TABLES
# ------------------------------------------------------------
TABLES=(
  "blocks"
  "deployments"
  "transfers"
  "validators"
  "validator_bonds"
  "block_validators"
  "balance_states"
  "epoch_transitions"
  "network_stats"
  "indexer_state"
)

echo "Tracking tables..."
for table in "${TABLES[@]}"; do
  hasura_metadata "{
    \"type\": \"pg_track_table\",
    \"args\": {\"source\": \"default\", \"table\": {\"schema\": \"public\", \"name\": \"$table\"}}
  }" >/dev/null
done

# ------------------------------------------------------------
# TRACK VIEWS
# ------------------------------------------------------------
VIEWS=(
  "tx_enriched_view"
  "network_metrics_view"
  "network_stats_view"
)

echo "Tracking views..."
for view in "${VIEWS[@]}"; do
  hasura_metadata "{
    \"type\": \"pg_track_table\",
    \"args\": {\"source\": \"default\", \"table\": {\"schema\": \"public\", \"name\": \"$view\"}}
  }" >/dev/null
done

# ------------------------------------------------------------
# TRACK SQL FUNCTIONS
# ------------------------------------------------------------
FUNCTIONS=(
  "get_transactions_by_blocks"
  "get_network_metrics"
)

echo "Tracking SQL functions..."
for fn in "${FUNCTIONS[@]}"; do
  hasura_metadata "{
    \"type\": \"pg_track_function\",
    \"args\": {\"source\": \"default\", \"function\": {\"schema\": \"public\", \"name\": \"$fn\"}}
  }" >/dev/null
done

# ------------------------------------------------------------
# OBJECT RELATIONS
# ------------------------------------------------------------
declare -A OBJECT_RELATIONS=(
  ["deployments.block"]="blocks:block_number:block_number"
  ["transfers.block"]="blocks:block_number:block_number"
  ["transfers.deployment"]="deployments:deploy_id:deploy_id"

  ["validator_bonds.block"]="blocks:block_number:block_number"
  ["validator_bonds.validator"]="validators:validator_public_key:public_key"

  ["block_validators.block"]="blocks:block_hash:block_hash"
  ["block_validators.validator"]="validators:validator_public_key:public_key"

  ["transfers.sender_validator"]="validators:from_public_key:public_key"

  ["balance_states.block"]="blocks:block_number:block_number"
  ["network_stats.block"]="blocks:block_number:block_number"
)

echo "Creating object relationships..."
for key in "${!OBJECT_RELATIONS[@]}"; do
  table="${key%%.*}"
  rel="${key##*.}"
  IFS=":" read -r remote_table local_col remote_col <<< "${OBJECT_RELATIONS[$key]}"

  hasura_metadata "{
    \"type\": \"pg_create_object_relationship\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"name\": \"$rel\",
      \"using\": {\"manual_configuration\": {
        \"remote_table\": {\"schema\": \"public\", \"name\": \"$remote_table\"},
        \"column_mapping\": {\"$local_col\": \"$remote_col\"}
      }}
    }
  }" >/dev/null
done

# ------------------------------------------------------------
# ARRAY RELATIONS
# ------------------------------------------------------------
declare -A ARRAY_RELATIONS=(
  ["blocks.deployments"]="deployments:block_number:block_number"
  ["blocks.transfers"]="transfers:block_number:block_number"
  ["blocks.validator_bonds"]="validator_bonds:block_number:block_number"
  ["blocks.balance_states"]="balance_states:block_number:block_number"
  ["blocks.block_validators"]="block_validators:block_hash:block_hash"
  ["blocks.network_stats"]="network_stats:block_number:block_number"

  ["deployments.transfers"]="transfers:deploy_id:deploy_id"

  ["validators.validator_bonds"]="validator_bonds:public_key:validator_public_key"
  ["validators.block_validators"]="block_validators:public_key:validator_public_key"
  ["validators.transfers_sent"]="transfers:public_key:from_public_key"
)

echo "Creating array relationships..."
for key in "${!ARRAY_RELATIONS[@]}"; do
  table="${key%%.*}"
  rel="${key##*.}"
  IFS=":" read -r remote_table local_col remote_col <<< "${ARRAY_RELATIONS[$key]}"

  hasura_metadata "{
    \"type\": \"pg_create_array_relationship\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"name\": \"$rel\",
      \"using\": {\"manual_configuration\": {
        \"remote_table\": {\"schema\": \"public\", \"name\": \"$remote_table\"},
        \"column_mapping\": {\"$local_col\": \"$remote_col\"}
      }}
    }
  }" >/dev/null
done

# ------------------------------------------------------------
# PUBLIC PERMISSIONS
# ------------------------------------------------------------
echo "Granting public SELECT permissions..."
ALL_TABLES_AND_VIEWS=( "${TABLES[@]}" "${VIEWS[@]}" )

for table in "${ALL_TABLES_AND_VIEWS[@]}"; do
  hasura_metadata "{
    \"type\": \"pg_create_select_permission\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"role\": \"public\",
      \"permission\": {\"columns\": \"*\", \"filter\": {}, \"allow_aggregations\": true}
    }
  }" >/dev/null
done

echo "Granting public EXECUTE permissions on SQL functions..."
for fn in "${FUNCTIONS[@]}"; do
  hasura_metadata "{
    \"type\": \"pg_create_function_permission\",
    \"args\": {
      \"source\": \"default\",
      \"function\": {\"schema\": \"public\", \"name\": \"$fn\"},
      \"role\": \"public\"
    }
  }" >/dev/null
done

# ------------------------------------------------------------
# Test queries
# ------------------------------------------------------------
echo "Running test query as admin..."
graphql_query '{"query":"{ blocks(limit:1, order_by:{block_number:desc}) { block_number block_hash } }"}'

echo -e "\nRunning test query as PUBLIC..."
curl -s -X POST "$HASURA_GRAPHQL" -H "Content-Type: application/json" \
  -d '{"query":"{ blocks(limit:1) { block_number block_hash } }"}'

# ------------------------------------------------------------
# Pre-Warm Metrics
# ------------------------------------------------------------
echo -e "\nPre-warming network_metrics_buckets..."

if [ -z "$DATABASE_LOCAL_URL" ]; then
  echo "⚠️ DATABASE_LOCAL_URL not set → skipping pre-warm"
elif [ ! -f "./scripts/refresh-network-metrics-once.sh" ]; then
  echo "⚠️ ./scripts/refresh-network-metrics-once.sh not found → skipping pre-warm"
else
  chmod +x ./scripts/refresh-network-metrics-once.sh
  if ./scripts/refresh-network-metrics-once.sh --db_url "$DATABASE_LOCAL_URL"; then
    echo "✅ Pre-warm OK"
  else
    echo "❌ Failed to pre-warm metrics buckets"
  fi
fi

echo -e "\n🎉 Hasura FULL initialization completed successfully!"
exit 0
