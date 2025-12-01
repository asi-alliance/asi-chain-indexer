#!/bin/bash
set -e

# ============================================================
# FULL HASURA INITIALIZATION FOR ASI-CHAIN INDEXER
#
# This script performs a *complete* setup of Hasura metadata:
#
#  - waits for Hasura to become available
#  - tracks all tables, views, and SQL functions
#  - creates all required object & array relationships
#  - grants read-only permissions to "public" role
#      - SELECT on all tables & views
#      - aggregation allowed (_aggregate)
#      - EXECUTE allowed for SQL functions
#  - runs test queries (admin + public)
#
# REQUIREMENTS (must be set in environment):
#   HASURA_GRAPHQL_DATABASE_URL=...
#   HASURA_GRAPHQL_ADMIN_SECRET=...
#   HASURA_GRAPHQL_UNAUTHORIZED_ROLE=public
#
# After this script completes, the GraphQL API is fully ready
# for public read-only access — *no deprecated scripts needed*.
# ============================================================

# Load .env if present
if [ -f ".env" ]; then
    sed -i 's/\r$//' .env
    set -o allexport
    # shellcheck source=/dev/null
    source .env
    set +o allexport
fi

HASURA_ADMIN_SECRET="${HASURA_ADMIN_SECRET:-myadminsecretkey}"
HASURA_BASE="${HASURA_BASE:-http://localhost:8080}"

# Construct endpoint URLs
HASURA_ENDPOINT="${HASURA_ENDPOINT:-$HASURA_BASE/v1/metadata}"
HASURA_GRAPHQL="${HASURA_GRAPHQL:-$HASURA_BASE/v1/graphql}"
HASURA_SQL="${HASURA_SQL:-$HASURA_BASE/v2/query}"
HASURA_HEALTH="${HASURA_HEALTH:-$HASURA_BASE/healthz}"

echo "Metadata endpoint: $HASURA_ENDPOINT"
echo "SQL endpoint:      $HASURA_SQL"
echo "GraphQL endpoint:  $HASURA_GRAPHQL"
echo "Health endpoint:   $HASURA_HEALTH"

admin_secret="${HASURA_GRAPHQL_ADMIN_SECRET:-$HASURA_ADMIN_SECRET}"

# Wrapper for metadata API
hasura_metadata() {
  local payload="$1"
  if [ -n "$admin_secret" ]; then
    curl -s -X POST "$HASURA_ENDPOINT" \
      -H "Content-Type: application/json" \
      -H "x-hasura-admin-secret: $admin_secret" \
      -d "$payload"
  else
    curl -s -X POST "$HASURA_ENDPOINT" \
      -H "Content-Type: application/json" \
      -d "$payload"
  fi
}

# Wrapper for GraphQL queries
graphql_query() {
  local payload="$1"
  if [ -n "$admin_secret" ]; then
    curl -s -X POST "$HASURA_GRAPHQL" \
      -H "Content-Type: application/json" \
      -H "x-hasura-admin-secret: $admin_secret" \
      -d "$payload"
  else
    curl -s -X POST "$HASURA_GRAPHQL" \
      -H "Content-Type: application/json" \
      -d "$payload"
  fi
}

# ------------------------------------------------------------
# Wait for Hasura to become available
# ------------------------------------------------------------
echo "Waiting for Hasura to be ready..."
until curl -s "$HASURA_HEALTH" >/dev/null 2>&1; do
  sleep 2
done
echo "Hasura is ready."

# ------------------------------------------------------------
# Track base tables
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
  echo "  -> $table"
  hasura_metadata "{
    \"type\": \"pg_track_table\",
    \"args\": {\"source\": \"default\", \"table\": {\"schema\": \"public\", \"name\": \"$table\"}}
  }" >/dev/null 2>&1
done

# ------------------------------------------------------------
# Track views
# ------------------------------------------------------------

VIEWS=(
  "tx_enriched_view"
  "network_metrics_view"
  "network_stats_view"
)

echo "Tracking views..."
for view in "${VIEWS[@]}"; do
  echo "  -> $view"
  hasura_metadata "{
    \"type\": \"pg_track_table\",
    \"args\": {\"source\": \"default\", \"table\": {\"schema\": \"public\", \"name\": \"$view\"}}
  }" >/dev/null 2>&1
done

# ------------------------------------------------------------
# Track SQL functions
# ------------------------------------------------------------

FUNCTIONS=(
  "get_transactions_by_blocks"
  "get_network_metrics"
)

echo "Tracking SQL functions..."
for fn in "${FUNCTIONS[@]}"; do
  echo "  -> $fn"
  hasura_metadata "{
    \"type\": \"pg_track_function\",
    \"args\": {\"source\": \"default\", \"function\": {\"schema\": \"public\", \"name\": \"$fn\"}}
  }" >/dev/null 2>&1
done

# ------------------------------------------------------------
# Object relationships
# ------------------------------------------------------------

declare -A OBJECT_RELATIONS=(
  # Deployments / Transfers / Blocks
  ["deployments.block"]="blocks:block_number:block_number"
  ["transfers.block"]="blocks:block_number:block_number"
  ["transfers.deployment"]="deployments:deploy_id:deploy_id"

  # Validators & bonding
  ["validator_bonds.block"]="blocks:block_number:block_number"
  ["validator_bonds.validator"]="validators:validator_public_key:public_key"
  ["block_validators.block"]="blocks:block_hash:block_hash"
  ["block_validators.validator"]="validators:validator_public_key:public_key"
  ["transfers.sender_validator"]="validators:from_public_key:public_key"

  # Balance states
  ["balance_states.block"]="blocks:block_number:block_number"

  # Network stats
  ["network_stats.block"]="blocks:block_number:block_number"
)

echo "Creating object relationships..."
for key in "${!OBJECT_RELATIONS[@]}"; do
  table="${key%%.*}"
  rel="${key##*.}"
  IFS=":" read -r remote_table local_col remote_col <<< "${OBJECT_RELATIONS[$key]}"

  echo "  -> $table.$rel -> $remote_table"
  hasura_metadata "{
    \"type\": \"pg_create_object_relationship\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"name\": \"$rel\",
      \"using\": {
        \"manual_configuration\": {
          \"remote_table\": {\"schema\": \"public\", \"name\": \"$remote_table\"},
          \"column_mapping\": {\"$local_col\": \"$remote_col\"}
        }
      }
    }
  }" >/dev/null 2>&1
done

# ------------------------------------------------------------
# Array relationships
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

  echo "  -> $table.$rel -> $remote_table[]"
  hasura_metadata "{
    \"type\": \"pg_create_array_relationship\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"name\": \"$rel\",
      \"using\": {
        \"manual_configuration\": {
          \"remote_table\": {\"schema\": \"public\", \"name\": \"$remote_table\"},
          \"column_mapping\": {\"$local_col\": \"$remote_col\"}
        }
      }
    }
  }" >/dev/null 2>&1
done

# ------------------------------------------------------------
# Permissions: public SELECT + aggregations
# ------------------------------------------------------------

ALL_TABLES_AND_VIEWS=( "${TABLES[@]}" "${VIEWS[@]}" )

echo "Granting public SELECT permissions..."
for table in "${ALL_TABLES_AND_VIEWS[@]}"; do
  echo "  -> $table"
  hasura_metadata "{
    \"type\": \"pg_create_select_permission\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"role\": \"public\",
      \"permission\": {
        \"columns\": \"*\",
        \"filter\": {},
        \"allow_aggregations\": true
      }
    }
  }" >/dev/null 2>&1
done

# ------------------------------------------------------------
# Permissions: allow public to execute SQL functions
# ------------------------------------------------------------

echo "Granting public EXECUTE permissions on SQL functions..."
for fn in "${FUNCTIONS[@]}"; do
  echo "  -> $fn"
  hasura_metadata "{
    \"type\": \"pg_create_function_permission\",
    \"args\": {
      \"source\": \"default\",
      \"function\": {\"schema\": \"public\", \"name\": \"$fn\"},
      \"role\": \"public\"
    }
  }" >/dev/null 2>&1
done

# ------------------------------------------------------------
# Test queries (admin & public)
# ------------------------------------------------------------

echo "Running test query as admin..."
graphql_query '{"query":"{ blocks(limit:1, order_by:{block_number:desc}) { block_number block_hash } }"}'

echo -e "\nRunning test query as PUBLIC..."
curl -s -X POST "$HASURA_GRAPHQL" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ blocks(limit:1) { block_number block_hash } }"}'

echo -e "\n\n🎉 Hasura FULL initialization completed successfully!"
exit 0
