#!/bin/bash
set -e

HASURA="${HASURA_GRAPHQL:-http://localhost:8080/v1/graphql}"
ADMIN_SECRET="${HASURA_ADMIN_SECRET:-myadminsecretkey}"

graphql_admin() {
  curl -s -X POST "$HASURA" \
    -H "Content-Type: application/json" \
    -H "x-hasura-admin-secret: $ADMIN_SECRET" \
    -d "$1"
}

graphql_public() {
  curl -s -X POST "$HASURA" \
    -H "Content-Type: application/json" \
    -d "$1"
}

echo "=============================================="
echo "🔍 TEST 1: network_metrics_view schema available"
echo "=============================================="

graphql_admin '{"query":"{ __type(name: \"network_metrics_view\") { fields { name type { name kind } } } }"}'

echo -e "\n\n=============================================="
echo "🔍 TEST 2: get_network_metrics(admin) — small window"
echo "=============================================="

graphql_admin '{"query":"query { get_network_metrics(args:{p_range_hours:24, p_divisions:8}) { bucket_start avg_block_time_seconds avg_tps deployments_count transfers_count } }"}'

echo -e "\n\n=============================================="
echo "🔍 TEST 3: get_network_metrics(admin) — large window"
echo "=============================================="

graphql_admin '{"query":"query { get_network_metrics(args:{p_range_hours:168, p_divisions:7}) { bucket_start avg_block_time_seconds avg_tps deployments_count transfers_count } }"}'

echo -e "\n\n=============================================="
echo "🔍 TEST 4: get_network_metrics(public)"
echo "=============================================="

graphql_public '{"query":"query { get_network_metrics(args:{p_range_hours:24, p_divisions:8}) { bucket_start avg_block_time_seconds avg_tps deployments_count transfers_count } }"}'

echo -e "\n\n=============================================="
echo "🔍 TEST 5: latest network_stats(admin)"
echo "=============================================="

graphql_admin '{"query":"{ network_stats(limit:1, order_by:{id:desc}) { id total_validators active_validators consensus_status block_number timestamp } }"}'

echo -e "\n\n=============================================="
echo "🔍 TEST 6: latest block(admin)"
echo "=============================================="

graphql_admin '{"query":"{ blocks(limit:1, order_by:{block_number:desc}) { block_number block_hash } }"}'


echo -e "\n\n=============================================="
echo "🔍 TEST 7: PUBLIC role — latest block"
echo "=============================================="

graphql_public '{"query":"{ blocks(limit:1) { block_number block_hash } }"}'

echo -e "\n\n🎉 network_metrics & stats tests completed!"
exit 0
