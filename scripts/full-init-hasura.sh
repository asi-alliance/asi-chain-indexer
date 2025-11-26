#!/bin/bash
set -e

# Load .env if exists
if [ -f ".env" ]; then
    sed -i 's/\r$//' .env
    set -o allexport
    source .env
    set +o allexport
fi

HASURA_ADMIN_SECRET="${HASURA_ADMIN_SECRET:-myadminsecretkey}"
HASURA_BASE="${HASURA_BASE:-http://localhost:8080}"

# Build dependent URLs dynamically
HASURA_ENDPOINT="${HASURA_ENDPOINT:-$HASURA_BASE/v1/metadata}"
HASURA_GRAPHQL="${HASURA_GRAPHQL:-$HASURA_BASE/v1/graphql}"
HASURA_SQL="${HASURA_SQL:-$HASURA_BASE/v2/query}"

#until curl -s "$HASURA_GRAPHQL" > /dev/null; do
#  echo "Waiting for Hasura GraphQL endpoint..."
#  sleep 2
#done

echo "Metadata endpoint: $HASURA_ENDPOINT"
echo "SQL endpoint: $HASURA_SQL"
echo "GraphQL endpoint: $HASURA_GRAPHQL"

admin_secret="${HASURA_GRAPHQL_ADMIN_SECRET:-$HASURA_ADMIN_SECRET}"

hasura_api() {
  if [ -n "$admin_secret" ]; then
    curl -s -X POST "$HASURA_ENDPOINT" \
      -H "Content-Type: application/json" \
      -H "x-hasura-admin-secret: $admin_secret" \
      -d "$1"
  else
    curl -s -X POST "$HASURA_ENDPOINT" \
      -H "Content-Type: application/json" \
      -d "$1"
  fi
}

graphql_query() {
  if [ -n "$admin_secret" ]; then
    curl -s -X POST "$HASURA_GRAPHQL" \
      -H "Content-Type: application/json" \
      -H "x-hasura-admin-secret: $admin_secret" \
      -d "$1"
  else
    curl -s -X POST "$HASURA_GRAPHQL" \
      -H "Content-Type: application/json" \
      -d "$1"
  fi
}

echo "Waiting for Hasura to be ready..."
until curl -s "$HASURA_ENDPOINT" > /dev/null; do
  sleep 2
done
echo "Hasura is ready."

###########################################
# TRACK TABLES
###########################################

TABLES=(
  "blocks"
  "deployments"
  "transfers"
  "validators"
  "validator_bonds"
  "balance_states"
  "network_stats"
  "epoch_transitions"
)

for table in "${TABLES[@]}"; do
  echo "Tracking table: $table"
  hasura_api "{
    \"type\": \"pg_track_table\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"}
    }
  }" >/dev/null 2>&1
done

###########################################
# TRACK VIEWS
###########################################

VIEWS=(
  "network_metrics_view"
  "tx_enriched_view"
)

for view in "${VIEWS[@]}"; do
  echo "Tracking view: $view"
  hasura_api "{
    \"type\": \"pg_track_table\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$view\"}
    }
  }" >/dev/null 2>&1
done

###########################################
# TRACK FUNCTIONS
###########################################

FUNCTIONS=(
  "get_transactions_by_blocks"
  "get_network_metrics"
)

for fn in "${FUNCTIONS[@]}"; do
  echo "Tracking SQL function: $fn"
  hasura_api "{
    \"type\": \"pg_track_function\",
    \"args\": {
      \"source\": \"default\",
      \"function\": {\"schema\": \"public\", \"name\": \"$fn\"}
    }
  }" >/dev/null 2>&1
done

###########################################
# OBJECT RELATIONSHIPS
###########################################

declare -A OBJECT_RELATIONS=(
  ["deployments.block"]="blocks:block_number:block_number"
  ["transfers.block"]="blocks:block_number:block_number"
  ["transfers.sender_validator"]="validators:public_key:from_address"
  ["transfers.receiver_validator"]="validators:public_key:to_address"
  ["validator_bonds.validator"]="validators:public_key:validator"
  ["balance_states.validator"]="validators:public_key:validator"
  ["epoch_transitions.block"]="blocks:block_number:block_number"
)

for key in "${!OBJECT_RELATIONS[@]}"; do
  table="${key%%.*}"
  rel="${key##*.}"
  IFS=":" read -r remote_table local_col remote_col <<< "${OBJECT_RELATIONS[$key]}"

  echo "Creating object relationship: $table.$rel"
  hasura_api "{
    \"type\": \"pg_create_object_relationship\",
    \"args\": {
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"name\": \"$rel\",
      \"source\": \"default\",
      \"using\": {
        \"manual_configuration\": {
          \"remote_table\": {\"schema\": \"public\", \"name\": \"$remote_table\"},
          \"column_mapping\": {\"$local_col\": \"$remote_col\"}
        }
      }
    }
  }" >/dev/null 2>&1
done

###########################################
# ARRAY RELATIONSHIPS
###########################################

declare -A ARRAY_RELATIONS=(
  ["blocks.deployments"]="deployments:block_number:block_number"
  ["blocks.transfers"]="transfers:block_number:block_number"
  ["blocks.epoch_transitions"]="epoch_transitions:block_number:block_number"
  ["validators.transfers_sent"]="transfers:from_address:public_key"
  ["validators.transfers_received"]="transfers:to_address:public_key"
  ["validators.validator_bonds"]="validator_bonds:validator:public_key"
  ["validators.balance_states"]="balance_states:validator:public_key"
)

for key in "${!ARRAY_RELATIONS[@]}"; do
  table="${key%%.*}"
  relname="${key##*.}"
  IFS=":" read -r remote_table local_col remote_col <<< "${ARRAY_RELATIONS[$key]}"

  echo "Creating array relationship: $table.$relname"
  hasura_api "{
    \"type\": \"pg_create_array_relationship\",
    \"args\": {
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"name\": \"$relname\",
      \"source\": \"default\",
      \"using\": {
        \"manual_configuration\": {
          \"remote_table\": {\"schema\": \"public\", \"name\": \"$remote_table\"},
          \"column_mapping\": {\"$local_col\": \"$remote_col\"}
        }
      }
    }
  }" >/dev/null 2>&1
done

###########################################
# PUBLIC PERMISSIONS — READ ONLY
###########################################

ALL_TABLES=( "${TABLES[@]}" "${VIEWS[@]}" )

for table in "${ALL_TABLES[@]}"; do
  echo "Granting public SELECT on $table"
  hasura_api "{
    \"type\": \"pg_set_table_select_permissions\",
    \"args\": {
      \"source\": \"default\",
      \"table\": {\"schema\": \"public\", \"name\": \"$table\"},
      \"role\": \"public\",
      \"permission\": {\"columns\": \"*\", \"filter\": {}}
    }
  }" >/dev/null 2>&1
done

echo "Allowing public to EXECUTE SQL functions..."
for fn in "${FUNCTIONS[@]}"; do
  hasura_api "{
    \"type\": \"pg_set_function_permissions\",
    \"args\": {
      \"source\": \"default\",
      \"function\": {\"schema\": \"public\", \"name\": \"$fn\"},
      \"role\": \"public\",
      \"permissions\": {\"execute\": true}
    }
  }" >/dev/null 2>&1
done

###########################################
# TEST QUERY
###########################################

echo "Running test GraphQL query..."
graphql_query '{
  "query": "{ blocks(limit:1){ block_number block_hash deployments { deploy_id } } }"
}'

echo -e "\n🎉 Hasura initialization completed successfully!"
exit 0
