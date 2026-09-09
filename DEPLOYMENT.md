# ASI-Chain Indexer Deployment Guide

**Version**: 2.2.0 (dag_support) | **Updated**: August 2026

This guide covers deployment scenarios for the ASI-Chain Indexer. The indexer
talks to an ASI Chain node **over gRPC** (the previous Rust-CLI based client has
been removed — `feat: remove rust-cli`) and follows the **DAG-aware** schema:
`blocks` primary key is `block_hash`, `block_number` is not unique, parents
live in the `block_parents` junction table.

## ✨ v2.2.0 Features (Latest — DAG Support)

- **gRPC node client**: streams blocks via `getBlocksByHeights`, fetches
  `lastFinalizedBlock`, `bonds`, etc. directly from the node's
  `DeployServiceV1` gRPC API — no external binary, no shell-out.
- **DAG-aware schema**: `blocks` keyed by `block_hash`, multi-parent blocks via
  `block_parents`, `get_block_ancestors` / `get_block_descendants` traversal
  functions.
- **Pre-aggregated metrics**: `network_metrics_buckets` table +
  `get_network_metrics()` SQL function (hybrid fast/slow path), refreshed by
  a sidecar `metrics-cron` container (every 3 min).
- **Wallet history view**: `transaction_history_view` (deployments LEFT JOIN
  transfers), Hasura-tracked with public aggregate enabled.
- **Zero-Touch Deployment**: `./deploy.sh` builds containers, waits for health,
  then runs `scripts/full-init-hasura.sh` to track tables/views/functions,
  create relationships, and grant public-role permissions.
- **Hasura permission split**: public SELECT with `limit: 5000` on every
  tracked table/view; **aggregates only enabled for `deployments`,
  `transfers`, `transaction_history_view`** (`allow_aggregations: true`);
  all other tables keep `allow_aggregations: false`.
- **Network-Agnostic Genesis Processing**: automatic validator bond and ASI
  allocation extraction.
- **Balance State Tracking**: separate bonded and unbonded balances per address.
- **Address Validation**: supports 53–56 character ASI addresses.

## Key Capabilities

- **Genesis Data Extraction**: automatically processes validator bonds from block 0
- **ASI Balance Tracking**: monitors bonded vs unbonded balances for all addresses
- **GraphQL API**: query all data via Hasura at http://localhost:8080
- **Advanced Transfer Detection**: handles both variable (`@fromAddr`) and match-based Rholang patterns
- **Full Validator Keys**: supports 130+ character validator public keys
- **Comprehensive Metrics**: Prometheus-compatible monitoring endpoint on port 9090
- **Idempotent Hasura Setup**: `full-init-hasura.sh` drops-and-recreates
  permissions so it can flip flags safely on re-runs

## Table of Contents

- [Quick Start](#quick-start)
- [Development Deployment](#development-deployment)
- [Production Deployment](#production-deployment)
- [Multi-Node Setup](#multi-node-setup)
- [Cloud Deployment](#cloud-deployment)
- [Monitoring Setup](#monitoring-setup)
- [Backup and Recovery](#backup-and-recovery)
- [Troubleshooting](#troubleshooting)
- [Security Considerations](#security-considerations)
- [Maintenance](#maintenance)

## Quick Start

The fastest path is the bundled `deploy.sh`. It loads `.env`, builds and
starts the containers, waits for health, then runs
`scripts/full-init-hasura.sh` (which tracks tables/views/functions, creates
relationships, and grants public-role permissions) plus a built-in self-test.

```bash
# Clone repository
git clone <repository-url>
cd indexer

# Step 1: Create and configure .env file
cp .env.example .env
# Edit .env with your node configuration (NODE_HOST, GRPC_PORT, HTTP_PORT,
# DATABASE_URL, HASURA_ADMIN_SECRET, ...)

# Step 2: Deploy (builds images, starts containers, configures Hasura, runs self-tests)
./deploy.sh

# Verify deployment
curl http://localhost:9090/status | jq .
curl -s http://localhost:8080/v1/graphql -H "Content-Type: application/json" \
  -d '{"query":"{ blocks(limit:1, order_by:{block_number:desc}) { block_number block_hash } }"}' | jq .
```

**Services started (see `docker-compose.yml`):**
- `postgres` (port 5432) — schema applied from
  `migrations/000_comprehensive_initial_schema.sql`
- `indexer` (port 9090) — Python indexer, gRPC node client, monitoring endpoints
- `metrics-cron` — Alpine sidecar running
  `scripts/refresh-network-metrics-once.sh` every 3 min via `dcron` to upsert
  `network_metrics_buckets`
- `hasura` (port 8080) — Hasura GraphQL Engine v2.36.0, unauthorized role `public`

**Note:** Step 2 (the `./deploy.sh` wrapper) is the recommended entrypoint.
If you prefer to run `docker compose up -d` manually, you MUST afterwards run
`./scripts/full-init-hasura.sh` to configure Hasura (tracking, relationships,
public permissions) — otherwise the explorer frontend will get
`validation-failed` on aggregate queries.

## Node Connectivity

The indexer connects to an ASI Chain observer node via two channels:

| Channel | Default port | Used for |
|---|---|---|
| gRPC (`DeployServiceV1`) | `GRPC_PORT` (default `40452`) | Block streaming, last finalized block, bonds, deploy info |
| HTTP | `HTTP_PORT` (default `40453`) | `/api/validators` (active validators — no gRPC RPC exposes this) |

Both are configured via `NODE_HOST`, `GRPC_PORT`, `HTTP_PORT` env vars (see
`.env.example`). No binary needs to be present on the indexer host — the gRPC
client is generated at Docker build time from `protos/` (see `Dockerfile`
stage 1: `grpc_tools.protoc`).

## Development Deployment

For local development without Docker Compose:

```bash
# Create Python environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# (Optional) Regenerate gRPC stubs from protos/ into src/grpc_stubs/
pip install grpcio-tools==1.83.0
mkdir -p src/grpc_stubs/scalapb
python -m grpc_tools.protoc \
    -I protos \
    --python_out=src/grpc_stubs --grpc_python_out=src/grpc_stubs \
    protos/DeployServiceV1.proto \
    protos/DeployServiceCommon.proto \
    protos/CasperMessage.proto \
    protos/RhoTypes.proto \
    protos/ServiceError.proto \
    protos/scalapb/scalapb.proto

# Start PostgreSQL with migrations
docker run -d \
  --name postgres-dev \
  -e POSTGRES_USER=indexer \
  -e POSTGRES_PASSWORD=indexer_pass \
  -e POSTGRES_DB=asichain \
  -p 5432:5432 \
  -v $(pwd)/migrations:/docker-entrypoint-initdb.d \
  postgres:14-alpine

# Configure environment for remote node
export NODE_HOST=13.251.66.61   # observer host
export GRPC_PORT=40452          # observer gRPC port
export HTTP_PORT=40453          # observer HTTP port
export DATABASE_URL=postgresql://indexer:indexer_pass@localhost:5432/asichain

# Run indexer
python -m src.main

# (In a separate terminal) configure Hasura
./scripts/full-init-hasura.sh
```

## Production Deployment

### Docker Compose Production

The repository's `docker-compose.yml` is already production-shaped. To
harden secrets, create a `docker-compose.prod.yml` override:

```yaml
version: '3.8'

services:
  indexer:
    environment:
      - NODE_HOST=${NODE_HOST}
      - GRPC_PORT=${GRPC_PORT:-40452}
      - HTTP_PORT=${HTTP_PORT:-40453}
      - DATABASE_URL=postgresql://indexer:${DB_PASSWORD}@postgres:5432/asichain
      - BATCH_SIZE=50
      - START_FROM_BLOCK=${START_FROM_BLOCK:-0}
      - LOG_LEVEL=INFO
      - LOG_FORMAT=json
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9090/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  postgres:
    image: postgres:14-alpine
    environment:
      - POSTGRES_USER=indexer
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_DB=asichain
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./migrations/000_comprehensive_initial_schema.sql:/docker-entrypoint-initdb.d/000_comprehensive_initial_schema.sql
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U indexer"]
      interval: 10s
      timeout: 5s
      retries: 5

  metrics-cron:
    image: alpine:3.19
    environment:
      DATABASE_URL: postgresql://indexer:${DB_PASSWORD}@postgres:5432/asichain
      LOOKBACK_HOURS: 168
      BUCKET_SECONDS: 300
    volumes:
      - ./scripts:/scripts:ro
    restart: unless-stopped
    entrypoint: >
      sh -c '
        set -e
        apk add --no-cache postgresql-client dcron >/dev/null 2>&1
        echo "*/3 * * * * /scripts/refresh-network-metrics-once.sh >> /proc/1/fd/1 2>&1" | crontab -
        crond -f -l 2
      '

  hasura:
    image: hasura/graphql-engine:v2.36.0
    ports:
      - "8080:8080"
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      HASURA_GRAPHQL_DATABASE_URL: postgresql://indexer:${DB_PASSWORD}@postgres:5432/asichain
      HASURA_GRAPHQL_ENABLE_CONSOLE: "true"
      HASURA_GRAPHQL_ADMIN_SECRET: ${HASURA_ADMIN_SECRET}
      HASURA_GRAPHQL_UNAUTHORIZED_ROLE: "public"
      HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES: "true"
    restart: unless-stopped

volumes:
  postgres_data:
```

Deploy:

```bash
# Set secure passwords
export DB_PASSWORD=$(openssl rand -base64 32)
export HASURA_ADMIN_SECRET=$(openssl rand -base64 32)

# Deploy
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# Configure Hasura (tracking, relationships, public permissions)
./scripts/full-init-hasura.sh
```

### Kubernetes Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: asi-indexer
spec:
  replicas: 1
  selector:
    matchLabels:
      app: asi-indexer
  template:
    metadata:
      labels:
        app: asi-indexer
    spec:
      containers:
      - name: indexer
        image: asi-indexer:v2.2.0
        ports:
        - containerPort: 9090
        env:
        - name: NODE_HOST
          value: "observer-node-service"
        - name: GRPC_PORT
          value: "40452"
        - name: HTTP_PORT
          value: "40453"
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: indexer-db-secret
              key: connection-string
        - name: BATCH_SIZE
          value: "100"
        livenessProbe:
          httpGet:
            path: /health
            port: 9090
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 9090
          initialDelaySeconds: 5
          periodSeconds: 5
        resources:
          requests:
            memory: "256Mi"
            cpu: "200m"
          limits:
            memory: "512Mi"
            cpu: "1000m"
```

## Multi-Node Setup

Index multiple networks or nodes:

```bash
# Mainnet configuration
cat > .env.mainnet << 'EOF'
NODE_HOST=mainnet.example.com
GRPC_PORT=40452
HTTP_PORT=40453
DATABASE_URL=postgresql://indexer:pass@postgres-mainnet:5432/mainnet
MONITORING_PORT=9091
EOF

# Testnet configuration
cat > .env.testnet << 'EOF'
NODE_HOST=testnet.example.com
GRPC_PORT=40452
HTTP_PORT=40453
DATABASE_URL=postgresql://indexer:pass@postgres-testnet:5432/testnet
MONITORING_PORT=9092
EOF

# Start multiple indexers (separate Compose projects)
docker compose --env-file .env.mainnet -p mainnet up -d --build
docker compose --env-file .env.testnet  -p testnet up -d --build

# Configure Hasura for each (point HASURA_BASE at the right instance)
HASURA_BASE=http://mainnet-hasura:8080 ./scripts/full-init-hasura.sh
HASURA_BASE=http://testnet-hasura:8080 ./scripts/full-init-hasura.sh
```

## Cloud Deployment

### AWS ECS with Fargate

```json
{
  "family": "asi-indexer",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "containerDefinitions": [
    {
      "name": "indexer",
      "image": "your-ecr-repo/asi-indexer:v2.2.0",
      "essential": true,
      "environment": [
        {"name": "NODE_HOST", "value": "your-observer-node"},
        {"name": "GRPC_PORT", "value": "40452"},
        {"name": "HTTP_PORT", "value": "40453"},
        {"name": "BATCH_SIZE", "value": "50"}
      ],
      "secrets": [
        {
          "name": "DATABASE_URL",
          "valueFrom": "arn:aws:secretsmanager:region:account:secret:db-url"
        }
      ],
      "portMappings": [
        {"containerPort": 9090}
      ],
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -f http://localhost:9090/health"],
        "interval": 30,
        "timeout": 5,
        "retries": 3
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/asi-indexer",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

### Google Cloud Run

```bash
# Build and push
gcloud builds submit --tag gcr.io/PROJECT-ID/asi-indexer:v2.2.0

# Deploy with proper resources
gcloud run deploy asi-indexer \
  --image gcr.io/PROJECT-ID/asi-indexer:v2.2.0 \
  --platform managed \
  --region us-central1 \
  --memory 1Gi \
  --cpu 2 \
  --timeout 900 \
  --set-env-vars="NODE_HOST=your-observer-node,GRPC_PORT=40452,HTTP_PORT=40453,BATCH_SIZE=50" \
  --set-secrets="DATABASE_URL=db-connection:latest" \
  --allow-unauthenticated
```

> Cloud Run is a request-scoped runtime. The indexer's long-running sync loop
> needs `--timeout` set to the maximum and `--min-instances=1` to avoid
> cold-start reset — for production prefer ECS Fargate or a VM.

## Monitoring Setup

### Prometheus scrape config

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'asi-indexer'
    static_configs:
      - targets: ['asi-indexer:9090']
    scrape_interval: 15s
```

### Grafana Dashboard

Key metrics to monitor (all exposed on the indexer's `/metrics` endpoint):

- `indexer_blocks_indexed_total` — total blocks processed
- `indexer_sync_lag_blocks` — blocks behind chain tip
- `indexer_grpc_requests_total` — gRPC calls made to the node
- `indexer_grpc_errors_total` — gRPC call failures
- `indexer_epoch_transitions_total` — epoch changes
- `indexer_network_health_score` — network consensus health

### Alerting Rules

```yaml
groups:
  - name: indexer
    rules:
      - alert: GrpcErrorRate
        expr: rate(indexer_grpc_errors_total[5m]) / rate(indexer_grpc_requests_total[5m]) > 0.1
        for: 5m
        annotations:
          summary: "High gRPC error rate"

      - alert: SlowSync
        expr: rate(indexer_blocks_indexed_total[5m]) < 1
        for: 10m
        annotations:
          summary: "Indexer sync rate too slow"

      - alert: NetworkUnhealthy
        expr: indexer_network_health_score < 0.5
        for: 5m
        annotations:
          summary: "Network consensus unhealthy"
```

## Backup and Recovery

### Automated Backups

```bash
#!/bin/bash
# backup-indexer.sh

BACKUP_DIR=/backups
DB_NAME=asichain
CONTAINER=asi-indexer-db

# Create timestamped backup
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
docker exec "$CONTAINER" pg_dump -U indexer "$DB_NAME" | gzip > "$BACKUP_DIR/backup_$TIMESTAMP.sql.gz"

# Keep last 7 days
find "$BACKUP_DIR" -name "backup_*.sql.gz" -mtime +7 -delete
```

### Recovery Process

```bash
# Restore database
gunzip -c backup_20250806_120000.sql.gz | docker exec -i asi-indexer-db psql -U indexer asichain

# Update last indexed block if needed
docker exec asi-indexer-db psql -U indexer -d asichain \
  -c "UPDATE indexer_state SET value = '1000' WHERE key = 'last_indexed_block'"

# Restart indexer
docker restart asi-indexer
```

## Troubleshooting

### Common Issues

1. **gRPC connection refused / unavailable**
   ```bash
   # Verify the observer node is reachable from the indexer container
   docker exec asi-indexer python -c "import grpc; grpc.aio.insecure_channel('host:40452').close()"

   # Check the node's gRPC port from the host
   nc -vz <NODE_HOST> <GRPC_PORT>

   # Inspect indexer logs for gRPC errors
   docker logs asi-indexer 2>&1 | grep -i grpc
   ```
   If gRPC is unreachable, ensure `NODE_HOST`, `GRPC_PORT`, `HTTP_PORT` in `.env`
   match the observer node and that the Docker host can route to them
   (`extra_hosts: "host.docker.internal:host-gateway"` is set in `docker-compose.yml`).

2. **GraphQL aggregate returns `validation-failed` for public role**
   ```bash
   # Anonymous aggregate query test
   curl -s http://localhost:8080/v1/graphql -H "Content-Type: application/json" \
     -d '{"query":"{ deployments_aggregate { aggregate { count } } }"}' | jq .
   ```
   If this returns `field 'deployments_aggregate' not found in type: 'query_root'`,
   the Hasura public role is missing the `allow_aggregations: true` permission
   for `deployments` / `transfers`. Re-run `./scripts/full-init-hasura.sh` — it
   now drops-and-recreates the public SELECT permission with the correct
   `allow_aggregations` flag (see `AGGREGATE_ENABLED_TABLES` in that script).
   This is the root cause of the "0 Deployments / 0 Transfers / 0 Total"
   counters on the explorer's `/transactions` page.

3. **Schema Migration Errors**
   ```bash
   # Manually apply migrations
   docker exec -i asi-indexer-db psql -U indexer -d asichain < migrations/000_comprehensive_initial_schema.sql

   # Check schema
   docker exec asi-indexer-db psql -U indexer -d asichain -c "\dt"
   ```

4. **Slow Sync Performance**
   ```bash
   # Check current batch size
   docker exec asi-indexer env | grep BATCH_SIZE

   # Monitor indexing rate
   docker logs asi-indexer 2>&1 | grep -i "block"
   ```

5. **Foreign Key Constraint Errors**
   ```sql
   -- Drop problematic constraint (rare — manual edits only)
   ALTER TABLE validator_bonds
   DROP CONSTRAINT IF EXISTS validator_bonds_validator_public_key_fkey;
   ```

### Performance Tuning

```bash
# Environment optimizations (in .env or docker-compose override)
BATCH_SIZE=200              # Larger batches for faster sync
SYNC_INTERVAL=2             # More frequent checks
DATABASE_POOL_SIZE=20       # More connections
NODE_TIMEOUT=60             # Longer timeout for large batches / slow nodes

# PostgreSQL tuning (postgresql.conf)
shared_buffers = 512MB
work_mem = 8MB
maintenance_work_mem = 128MB
effective_cache_size = 2GB
```

### Debug Mode

```bash
# Enable debug logging
docker run -e LOG_LEVEL=DEBUG -e LOG_FORMAT=text -e NODE_HOST=... asi-indexer

# Watch gRPC round-trips and indexer decisions
docker logs -f asi-indexer

# Check slow database queries
docker exec asi-indexer-db psql -U indexer -d asichain \
  -c "SELECT query, calls FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10"

# Inspect pre-aggregated metrics buckets
docker exec asi-indexer-db psql -U indexer -d asichain \
  -c "SELECT MIN(bucket_start), MAX(bucket_end), COUNT(*) FROM network_metrics_buckets"
```

## Security Considerations

1. **Hasura admin secret**
   - Always set `HASURA_GRAPHQL_ADMIN_SECRET` to a strong random value in production.
   - The `public` (unauthorized) role may only SELECT with `limit: 5000`; only
     `deployments`, `transfers`, and `transaction_history_view` additionally
     allow aggregates.

2. **Network Security**
   - Use private networks for node communication (`NODE_HOST`, gRPC, HTTP).
   - Enable TLS for external connections.
   - Restrict gRPC / HTTP ports with firewall rules so only the indexer can reach them.

3. **Database Security**
   - Use strong passwords.
   - Enable SSL connections.
   - Regular security updates.
   - Limit connection sources.

4. **Container Security**
   - Run as non-root user (the `Dockerfile` already creates and switches to the
     `indexer` user, UID 1000).
   - Use minimal base images.
   - Regular vulnerability scanning.
   - Resource limits enforced.

## Maintenance

### Regular Tasks

```bash
# Weekly: vacuum database
docker exec asi-indexer-db psql -U indexer -d asichain -c "VACUUM ANALYZE"

# Monthly: update statistics
docker exec asi-indexer-db psql -U indexer -d asichain -c "ANALYZE"

# Quarterly: reindex
docker exec asi-indexer-db psql -U indexer -d asichain -c "REINDEX DATABASE asichain"
```

### Monitoring Checklist

- [ ] Sync lag < 10 blocks
- [ ] gRPC error rate < 1%
- [ ] Database size growth normal
- [ ] API response times < 100ms
- [ ] Network health score > 0.8
- [ ] No missing blocks in recent range
- [ ] `network_metrics_buckets` hydration up to date
      (verify: `SELECT MAX(bucket_end) FROM network_metrics_buckets` is recent)

## Support

For issues specific to the indexer:
1. Check logs: `docker logs asi-indexer`
2. Verify node connectivity: `docker exec asi-indexer python -c "import grpc, asyncio; asyncio.run(grpc.aio.insecure_channel('host:40452').channel_ready())"`
3. Review enhanced schema: `docker exec asi-indexer-db psql -U indexer -d asichain -c "\d+"`
4. Re-apply Hasura setup if permissions drift: `./scripts/full-init-hasura.sh`
5. Check GitHub issues for similar problems