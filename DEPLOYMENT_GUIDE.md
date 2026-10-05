# ASI-Chain Indexer Quick Deployment Guide

**Version**: 2.2.0 (dag_support) | **Updated**: August 2026

> **Note.** This is a quick-reference companion to the full
> [`DEPLOYMENT.md`](./DEPLOYMENT.md). For complete scenarios (production
> overrides, Kubernetes, ECS, Cloud Run, monitoring, backup, security, hardening),
> consult that document. The present guide only walks through the recommended
> local/Docker deployment path.

## Overview

The ASI-Chain Indexer is a Python asyncio service that synchronizes ASI Chain
blockchain data into PostgreSQL and exposes it through a Hasura GraphQL API.
The indexer talks to an ASI Chain observer node **via gRPC** (the legacy
Rust-CLI-based client was removed — `feat: remove rust-cli`). No external
binary is needed: the gRPC client is generated at Docker build time from
`protos/`.

The schema is **DAG-aware**: `blocks` primary key is `block_hash`,
`block_number` is not unique, and block parents live in the `block_parents`
junction table.

## Architecture

```
┌─────────────────┐       gRPC (DeployServiceV1)        ┌─────────────────┐
│  ASI Node       │ ←─────────────────────────────────→ │  Indexer        │
│  (gRPC/HTTP)    │                                    │ (Python/asyncio)│
└─────────────────┘                                    └────────┬────────┘
                                                                │
                                                                ▼
                                                       ┌─────────────────┐
                                                       │   PostgreSQL    │
                                                       │   (indexed)    │
                                                       └────────┬────────┘
                                                                │
                                                                ▼
                                                       ┌─────────────────┐
                                                       │ Hasura GraphQL  │
                                                       │   (configured)  │
                                                       └─────────────────┘
```

## Prerequisites

1. **ASI blockchain observer node**
   - Observer nodes expose gRPC port `40452` and HTTP port `40453` (default).
     Both ports must be reachable from the indexer container.
   - Example remote observer node: `13.251.66.61`.

2. **Docker and Docker Compose**
   - Docker Engine 20.10+
   - Docker Compose v2+

3. **System Requirements**
   - 2GB+ RAM
   - 5GB+ disk space
   - Network access to the ASI node

No pre-built binary is required.

## Quick Start

### Step 1: Configure Environment

```bash
cd /path/to/asi-chain-indexer

# Copy and edit .env (use .env, NOT .env.local or .env.observer)
cp .env.example .env

# Edit .env to point at your ASI Chain observer node:
#   NODE_HOST=13.251.66.61
#   GRPC_PORT=40452
#   HTTP_PORT=40453
#   DATABASE_URL=postgresql://indexer:indexer_pass@postgres:5432/asichain
#   HASURA_ADMIN_SECRET=<a-strong-random-secret>
vim .env
```

### Step 2: Deploy

The recommended entrypoint is the bundled `deploy.sh` — it builds containers,
waits for health, then runs `scripts/full-init-hasura.sh` (tracking, relationships,
public-role permissions with the aggregate split) and finishes with built-in
self-tests:

```bash
./deploy.sh

# Monitor indexer
docker compose logs -f indexer

# Check service health
docker compose ps
```

If you prefer the manual path:

```bash
docker compose up -d --build
./scripts/full-init-hasura.sh
```

**What you get:**
- ✅ Indexer deployment (Python + generated gRPC client)
- ✅ DAG-aware PostgreSQL schema (`blocks` keyed by `block_hash`)
- ✅ Sidecar `metrics-cron` refreshing `network_metrics_buckets` every 3 min
- ✅ Real-time blockchain synchronization starting from the genesis block
- ✅ Working REST API (port 9090) and GraphQL API (port 8080)
- ✅ Pre-configured `public` role: SELECT (`limit: 5000`) on every tracked
  table/view, with aggregates enabled on `deployments` / `transfers` /
  `transaction_history_view`

### Step 3 (Nothing more)

The `./deploy.sh` wrapper already runs Hasura configuration. You can immediately
test complex nested queries:

```graphql
{
  blocks(limit: 5) {
    block_number
    block_hash
    parent_links {
      parent_block { block_hash block_number }
    }
    deployments { deploy_id deployment_type }
    validator_bonds { stake }
  }
}
```

## Environment Variables

The full list is in [`DEPLOYMENT.md` → Environment Variables](./DEPLOYMENT.md#environment-variables).
The critical ones:

```env
NODE_HOST=13.251.66.61                 # observer node hostname
GRPC_PORT=40452                        # gRPC port (DeployServiceV1)
HTTP_PORT=40453                        # HTTP port (/api/validators)
DATABASE_URL=postgresql://indexer:indexer_pass@postgres:5432/asichain
HASURA_ADMIN_SECRET=<strong-random>
SYNC_INTERVAL=5
BATCH_SIZE=50
START_FROM_BLOCK=0                     # 0 = genesis
LOG_LEVEL=INFO
LOG_FORMAT=json
```

## Service Endpoints

### REST API (port 9090)

```bash
curl http://localhost:9090/status | jq .    # indexer status
curl http://localhost:9090/health           # health check
curl http://localhost:9090/metrics          # Prometheus metrics
curl 'http://localhost:9090/api/blocks?limit=10' | jq .
```

### GraphQL API (port 8080)

- **GraphQL endpoint**: `http://localhost:8080/v1/graphql`
- **Hasura Console**: `http://localhost:8080/console`
- Anonymous `public` role may SELECT (limit 5000) any tracked table/view;
  aggregates are only allowed on `deployments`, `transfers`,
  `transaction_history_view`. Other aggregates require the admin secret.

```bash
# Anonymous query (public role)
curl -X POST http://localhost:8080/v1/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "{ blocks(limit: 5) { block_number block_hash timestamp } }"}'

# Public aggregate query (deployments + transfers)
curl -X POST http://localhost:8080/v1/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "{ deployments_aggregate { aggregate { count } } transfers_aggregate { aggregate { count } } }"}'
```

### PostgreSQL (port 5432)

```bash
psql -h localhost -U indexer -d asichain
# password: indexer_pass (override via .env / docker-compose override)
```

## Docker Configuration

The repository ships a single multi-stage `Dockerfile` that:

1. Installs Python dependencies and `grpcio-tools`.
2. Regenerates the gRPC stubs from `protos/` into `src/grpc_stubs/`.
3. Builds a slim Python 3.11 runtime image that runs `python -m src.main`.

Compose file:

- **`docker-compose.yml`** — production. Services:
  - `postgres` (port 5432)
  - `indexer` (port 9090)
  - `metrics-cron` (Alpine sidecar, every 3 min refreshes `network_metrics_buckets`)
  - `hasura` (port 8080)

> **Debug mode**: run the same `docker-compose.yml` with verbose, human-readable
> logs via env vars —
> `LOG_LEVEL=DEBUG LOG_FORMAT=text docker compose up -d --build`.

There is no `Dockerfile.rust-builder`, `Dockerfile.rust-simple`,
`Dockerfile.build-cli`, `Dockerfile.debug`, or `Dockerfile.local-cli` — those
legacy images have been removed together with the Rust CLI client. There is
also no `docker-compose.debug.yml` — debug mode is selected via env vars (see
above).

## Local Development (without Docker Compose)

```bash
# 1. Create Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. (Optional) Regenerate gRPC stubs from protos/
pip install grpcio-tools==1.83.0
make protos

# 3. Start PostgreSQL with migrations
docker run -d \
  --name indexer-db \
  -e POSTGRES_DB=asichain \
  -e POSTGRES_USER=indexer \
  -e POSTGRES_PASSWORD=indexer_pass \
  -p 5432:5432 \
  -v "$(pwd)/migrations/000_comprehensive_initial_schema.sql:/docker-entrypoint-initdb.d/000_comprehensive_initial_schema.sql" \
  postgres:14-alpine

# 4. Configure environment
export NODE_HOST=13.251.66.61
export GRPC_PORT=40452
export HTTP_PORT=40453
export DATABASE_URL=postgresql://indexer:indexer_pass@localhost:5432/asichain

# 5. Run indexer
python -m src.main

# 6. (Separate terminal) Configure Hasura
./scripts/full-init-hasura.sh
```

## Successful Deployment Verification

```bash
# All containers healthy
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Indexer caught up
curl -s http://localhost:9090/status | jq .

# Database growing
docker exec asi-indexer-db psql -U indexer -d asichain -c "
  SELECT
    (SELECT COUNT(*) FROM blocks)        AS blocks,
    (SELECT COUNT(*) FROM deployments)   AS deployments,
    (SELECT COUNT(*) FROM transfers)     AS transfers,
    (SELECT COUNT(*) FROM validators)    AS validators;
"

# GraphQL endpoints reachable
curl -s http://localhost:8080/v1/graphql \
  -H "Content-Type: application/json" \
  -d '{"query":"{ blocks(limit:1) { block_number block_hash } }"}' | jq .
```

Healthy indicators:
- All containers show `healthy` status
- Indexer logs show `Block indexed` messages
- `indexer.sync_percentage` approaches 100%
- `network_metrics_buckets` has recent `bucket_end`:
  `SELECT MAX(bucket_end) FROM network_metrics_buckets`

## Monitoring and Maintenance

### View logs

```bash
docker compose logs -f indexer
docker compose logs -f hasura
docker compose logs -f metrics-cron
```

### Restart / Reset

```bash
# Restart all services
docker compose restart

# Restart just the indexer
docker compose restart indexer

# Stop and start fresh (wipes Postgres volume)
docker compose down -v
docker compose up -d --build
./scripts/full-init-hasura.sh
```

## Troubleshooting

### 1. gRPC connection refused

**Symptom**: indexer logs show “Failed to get blocks by height … RpcError”.

**Solutions**:
- Verify the observer node is reachable:
  `docker exec asi-indexer python -c "import grpc, asyncio; asyncio.run(grpc.aio.insecure_channel('host:40452').channel_ready())"`
- Check `NODE_HOST`, `GRPC_PORT`, `HTTP_PORT` in `.env`
- On Docker Desktop use `host.docker.internal`; on Linux use the host IP or
  `172.17.0.1`
- Inspect gRPC errors: `docker logs asi-indexer 2>&1 | grep -i grpc`

### 2. Database connection errors

- `docker ps | grep postgres` — verify Postgres is up
- Verify `DATABASE_URL` matches `docker-compose.yml`
- `docker exec asi-indexer-db psql -U indexer -l` — check the `asichain` DB exists
- Reset: `docker compose down -v && docker compose up -d --build`

### 3. GraphQL aggregate fails with `validation-failed` for anonymous clients

This means a `<table>_aggregate` query was issued against a table where the
public role does not have `allow_aggregations: true`. Re-run
`./scripts/full-init-hasura.sh` — it grants aggregates on `deployments`,
`transfers`, and `transaction_history_view` only. Any other aggregate must be
admin-authenticated or added to `AGGREGATE_ENABLED_TABLES` in that script.

### 4. Hasura shows no tables / no relationships

- Run `./scripts/full-init-hasura.sh` (idempotent — drops-and-recreates permissions)
- Check Hasura logs: `docker logs asi-hasura`
- Verify `HASURA_GRAPHQL_DATABASE_URL` points at the `postgres` service
- Verify `HASURA_GRAPHQL_UNAUTHORIZED_ROLE=public` is set in `docker-compose.yml`

### 5. Duplicate key constraint errors during sync

- Normal — the indexer recovers gracefully and continues. No action required.
- If persistent and `sync_percentage` stalls, reset the DB:
  `docker compose down -v && docker compose up -d --build`

### 6. Slow synchronization

- Increase `BATCH_SIZE` (default 50, max ~100)
- Lower `SYNC_INTERVAL` (e.g. 2)
- Use an observer node (ports 40452/40453) rather than a validator node
- Monitor: `curl http://localhost:9090/status | jq .indexer.sync_percentage`

### Debug Commands

```bash
# Inspect gRPC connectivity from indexer container
docker exec asi-indexer python -c "import grpc, asyncio; asyncio.run(grpc.aio.insecure_channel('$NODE_HOST:$GRPC_PORT').channel_ready())"

# Test node HTTP endpoint (/api/validators)
docker exec asi-indexer curl -v http://host.docker.internal:$HTTP_PORT/api/validators

# Database readiness
docker exec asi-indexer-db pg_isready -U indexer -d asichain

# View effective env
docker exec asi-indexer env | grep -E "NODE_|DATABASE_|GRPC_|HTTP_"

# Check image labels
docker image inspect asi-indexer:latest | jq '.[0].Config.Labels'

# Verify metrics buckets are being hydrated
docker exec asi-indexer-db psql -U indexer -d asichain \
  -c "SELECT MIN(bucket_start), MAX(bucket_end), COUNT(*) FROM network_metrics_buckets"
```

## Performance Tuning

```env
INDEXER:
BATCH_SIZE=100                # Larger batches for faster sync (more RAM)
SYNC_INTERVAL=2               # More frequent checks
DATABASE_POOL_SIZE=30         # More connections
NODE_TIMEOUT=60               # Longer timeout for slow nodes

POSTGRES (postgresql.conf):
shared_buffers = 512MB
work_mem = 8MB
maintenance_work_mem = 128MB
effective_cache_size = 2GB

OPTIONAL INDEXES (already present in the migration):
CREATE INDEX IF NOT EXISTS idx_transfers_timestamp ON transfers (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_blocks_proposer      ON blocks (proposer);
CREATE INDEX IF NOT EXISTS idx_deployments_type     ON deployments (deployment_type);
```

### Docker Resource Limits

```yaml
# In a docker-compose override:
services:
  indexer:
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 2G
        reservations:
          cpus: '1.0'
          memory: 1G
```

## Security Considerations

1. **Change default secrets** — set a strong `HASURA_ADMIN_SECRET` and a strong
   Postgres password in production (override via env / docker-compose override).
2. **Network security** — restrict gRPC / HTTP ports with firewall rules so
   only the indexer can reach them; use VPN for remote node connections.
3. **Access control** — Hasura `public` role is read-only with `limit: 5000`;
   aggregates are restricted to `deployments`, `transfers`,
   `transaction_history_view`.
4. **Container security** — the `Dockerfile` runs as the `indexer` user
   (UID 1000); keep images patched; scan regularly.

## Support and Resources

- **Full deployment guide**: [`DEPLOYMENT.md`](./DEPLOYMENT.md)
- **GraphQL schema reference**: [`GRAPHQL_SCHEMA.md`](./GRAPHQL_SCHEMA.md)
- **GraphQL usage guide**: [`GRAPHQL_GUIDE.md`](./GRAPHQL_GUIDE.md)
- **REST API documentation**: [`API.md`](./API.md)
- **Changelog**: [`CHANGELOG.md`](./CHANGELOG.md)
- **Logs location** (in container): structured JSON to stdout — `docker logs asi-indexer`
- **Configuration** (in container): `/app/.env`

## License

MIT License — see the `LICENSE` file for details.