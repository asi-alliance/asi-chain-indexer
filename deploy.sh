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

# --- Load .env if exists ---
if [ -f ".env" ]; then
    sed -i 's/\r$//' .env
    set -o allexport
    source .env
    set +o allexport
fi

# --- Required environment variables ---
REQUIRED_VARS=(NODE_HOST HTTP_PORT GRPC_PORT HASURA_URL HASURA_ADMIN_SECRET)

for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        echo "❌ ERROR: Environment variable $var is not set or empty in .env"
        exit 1
    fi
done

echo "✅ All required environment variables loaded from .env:"
for var in "${REQUIRED_VARS[@]}"; do
    echo "   • $var=${!var}"
done

echo "--- Building and starting containers ---"
docker compose up -d --build

echo "--- Waiting for containers to be healthy ---"

timeout=120
interval=5
elapsed=0

while true; do
    total_count=$(docker compose ps --services | wc -l)
    healthy_count=$(docker compose ps | grep -c "(healthy)")

    if [ "$healthy_count" -eq "$total_count" ] && [ "$total_count" -gt 0 ]; then
        echo "✅ All containers are healthy!"
        break
    fi

    echo "Waiting for containers to be ready..."
    sleep 5
done


echo "--- Running Hasura configuration script ---"
if [ -f "./scripts/configure-hasura.sh" ]; then
    chmod +x ./scripts/configure-hasura.sh
    ./scripts/configure-hasura.sh
else
    echo "⚠️  ./scripts/configure-hasura.sh not found, skipping."
fi

echo "--- Running setup script ---"
if [ -f "./scripts/setup-hasura-relationships.sh" ]; then
    chmod +x ./scripts/setup-hasura-relationships.sh
    ./scripts/setup-hasura-relationships.sh
else
    echo "⚠️  ./scripts/setup-hasura-relationships.sh not found, skipping."
fi

echo "--- Running basic Hasura test ---"

HASURA_URL=${HASURA_URL:-http://localhost:8080/v1/graphql}
ADMIN_SECRET=${HASURA_ADMIN_SECRET:-myadminsecretkey}

echo "Checking Hasura availability at $HASURA_URL..."
if curl -s -o /dev/null -w "%{http_code}" "$HASURA_URL" | grep -qE "200|400"; then
    echo "✅ Hasura endpoint reachable!"
else
    echo "⚠️  Hasura not responding at $HASURA_URL"
    echo "--- Done (skipping test) ---"
    exit 0
fi

TEST_QUERY='{"query":"{ blocks_aggregate { aggregate { count } } }"}'

echo "Sending test query to Hasura..."
response=$(curl -s -X POST "$HASURA_URL" \
  -H "Content-Type: application/json" \
  -H "x-hasura-admin-secret: $ADMIN_SECRET" \
  -d "$TEST_QUERY" || echo "")

if echo "$response" | grep -q "aggregate"; then
    echo "✅ Hasura GraphQL endpoint responded successfully!"
    if command -v jq >/dev/null 2>&1; then
        echo "$response" | jq .
    else
        echo "$response"
    fi
else
    echo "⚠️  Hasura test failed or no data returned."
    echo "Response:"
    echo "$response"
fi

echo "--- Done! ---"
