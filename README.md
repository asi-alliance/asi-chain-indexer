<div align="center">

# ASI Chain: Indexer

[![Status](https://img.shields.io/badge/Status-BETA-FFA500?style=for-the-badge)](https://github.com/asi-alliance/asi-chain-explorer)
[![Version](https://img.shields.io/badge/Version-0.1.0-A8E6A3?style=for-the-badge)](https://github.com/asi-alliance/asi-chain-explorer/releases)
[![License](https://img.shields.io/badge/License-Apache%202.0-1A1A1A?style=for-the-badge)](LICENSE)
[![Docs](https://img.shields.io/badge/Docs-Available-C4F0C1?style=for-the-badge)](https://docs.asichain.io/explorer/usage/)

<h3>Blockchain Indexer Infrastructure for ASI Chain</h3>

Part of the [**Artificial Superintelligence Alliance**](https://superintelligence.io) ecosystem

*Uniting Fetch.ai, SingularityNET and CUDOS*

</div>

---

**ASI Chain Indexer** provides comprehensive blockchain data synchronization and hasura interface for exploring blocks, transactions, validators, and network statistics on the ASI Chain network.

---


A high-performance blockchain indexer for ASI-Chain that synchronizes data from ASI nodes using the node's native gRPC + HTTP API and stores it in PostgreSQL for efficient querying.

## Latest Version

The indexer provides complete automation for blockchain data synchronization:
- Full blockchain sync from genesis (block 0) via the node gRPC/HTTP API
- Automatic Hasura GraphQL relationships setup
- Enhanced ASI transfer detection with Rholang pattern matching
- Comprehensive database schema with single migration
- Balance tracking with bonded/unbonded separation
- DAG-aware block storage (multi-parent support via `block_parents`)
- Pending deploys snapshot from the node's deploy buffers (via `getPendingDeploys` RPC)

## Current Status

✅ **Working Features:**
- **Genesis block processing** with automatic extraction of validator bonds and initial allocations
- **Full blockchain synchronization from block 0** via the node gRPC/HTTP API
- **Enhanced ASI transfer detection** - now supports match-based Rholang patterns
- **Balance state tracking** - separate bonded and unbonded balances per address
- **GraphQL API** via Hasura with automatic bash-based configuration
- Enhanced block metadata extraction (state roots, bonds, validators, justifications)
- PostgreSQL storage with 150-char address fields (supports both ASI addresses and validator keys)
- Deployment extraction with full Rholang code
- Smart contract type classification (ASI transfers, validator ops, etc.)
- ASI transfer extraction with both variable-based and match-based pattern matching
- Address validation supporting 52-56 character ASI addresses
- Validator tracking with full public keys (130+ characters)
- Network consensus monitoring
- Advanced search capabilities (blocks by hash, deployments by ID/deployer)
- Network statistics and analytics
- Prometheus metrics endpoint
- Health and readiness checks
- **Zero-touch deployment** - complete automation with automatic Hasura relationships
- **Complete REST API and GraphQL interface** with working nested queries
- **Pending deploys tracking** - live snapshot of the node's deploy buffers via the `getPendingDeploys` RPC, refreshed every sync cycle

⚠️ **Known Limitations:**
- **Epoch transitions tracking** - Table exists but data not populated (epoch rewards not tracked)
- **Validator rewards** - Not tracked in current implementation

✅ **Recent Improvements:**
- Manual Hasura configuration eliminated - relationships setup automatically
- Comprehensive migration - single `000_comprehensive_initial_schema.sql`
- Data quality improvements - proper NULL handling
- Enhanced error tracking

📊 **Performance:**
- Syncs up to 50 blocks per batch
- Processes blocks from genesis without limitations
- Sub-second block processing time
- Handles complex block metadata including justifications
- **240+ blocks indexed in initial sync**
- **148+ deployments tracked with full metadata**
- **732+ validator bond records maintained**

🔧 **Technical Improvements:**
- Uses the node's native gRPC + HTTP API for blockchain interaction (no external CLI binary)
- Vendor node protos under `protos/`, gRPC stubs generated at build time
- Enhanced database schema for additional data types
- Proper NULL handling in error_message fields
- Multi-stage Docker builds for optimized images

### Stack

- **Python 3.11**: Core programming language
- **asyncio**: Asynchronous processing framework
- **SQLAlchemy 2.0.31**: ORM and database abstraction
- **asyncpg 0.29.0**: PostgreSQL async driver
- **Pydantic 2.7.4**: Configuration and data validation
- **pydantic-settings 2.3.4**: Settings management
- **structlog 24.2.0**: Structured logging
- **prometheus-client 0.20.0**: Metrics exposure
- **aiohttp 3.9.5**: HTTP client
- **click 8.1.7**: CLI interface
- **tenacity 8.5.0**: Retry logic


## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     ASI Chain Node                          │
│                  (RChain-based Network)                     │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ gRPC (40412) / HTTP (40413)
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                  gRPC Node Client                            │
│         (Blockchain Data Extraction Interface)               │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ in-process calls
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                  Python Indexer Service                     │
│  - Block synchronization                                    │
│  - Deployment processing                                    │
│  - Transfer extraction                                      │
│  - Validator tracking                                       │
│  - Network statistics                                       │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ asyncpg/SQLAlchemy
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                   PostgreSQL Database                       │
│  Tables: blocks, block_parents, deployments, transfers,     │
│         pending_deploys, validators, validator_bonds,       │
│         balance_states, network_stats, epoch_transitions,    │
│         block_validators, indexer_state                     │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ Database Connection
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                   Hasura GraphQL Engine                     │
│  - Auto-generated GraphQL API                               │
│  - Views: transaction_history, block_ancestors/descendants  │
│  - SQL functions: get_block_ancestors/descendants, metrics  │
│  - Real-time queries with polling                           │
│  - Query optimization                                       │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ GraphQL (HTTP)
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                  Client Applications                        │
│  - Explorer Frontend                                        │
│  - Your App                                                 │
└─────────────────────────────────────────────────────────────┘
```

## Node gRPC/HTTP API Used

The indexer talks to the node directly via gRPC (primary) and HTTP (fallback for validator list), using vendored protos from `protos/`. Key RPCs:

1. **getBlocksByHeights** - Fetch blocks within a height range (supports large batches)
2. **getBlock** - Get detailed block information including deployments
3. **findDeploy** - Retrieve specific deployment details
4. **getPendingDeploys** - List deploys waiting in the node's buffers (deploy_storage + rejected-recovery), optionally filtered by deployer; snapshot refreshed every sync cycle
5. **getBonds** - Get current validator bonds and stakes
6. **showMainChain** - Verify main chain consistency
7. **status** - Node health check
8. HTTP `/api/validators` - List currently active validators (no gRPC equivalent)

## ⚡ Quick Start
## Requirements

- Docker and Docker Compose (recommended)
- OR Python 3.9+ and PostgreSQL 14+
- Running node (gRPC port & HTTP port)

## Installation

### Recommended Installation

```bash
# Step 1: Create and configure .env file in /indexer directory
cp .env.example .env
# Edit .env with your node configuration if needed

# Step 2: Start the indexer
./deploy.sh

# Check status
curl http://localhost:9090/status | jq .
```

### Docker Installation

```bash
# Clone the repository
git clone <repository-url>

# Manual Docker Compose
docker compose -f docker-compose.yml up -d

# Verify it's working
curl http://localhost:9090/status | jq .
```


**That's it!** The indexer will automatically:
- Generate the gRPC client from `protos/` (during `Dockerfile` build stage)
- Set up PostgreSQL database with the complete schema (DAG-aware)
- Start syncing from the genesis block

After running `./deploy.sh` (which calls `scripts/full-init-hasura.sh`):
- Hasura tables/views/functions will be tracked
- Relationships (FK-based + manual DAG relationships) will be configured
- Public role gets SELECT (`limit: 5000`) on every table/view, plus
  aggregates enabled on `deployments`/`transfers`/`transaction_history_view`
- API will be ready for complex queries

**Access services:**
- Indexer API: http://localhost:9090
- Hasura Console: http://localhost:8080/console
- GraphQL endpoint: http://localhost:8080/v1/graphql

#### Docker Compose Files

1. **docker-compose.yml** (Production)
   - Uses the standard `Dockerfile`
   - Services included:
     - `postgres`: PostgreSQL 14 Alpine (port 5432)
     - `indexer`: Python indexer with gRPC node client (port 9090)
     - `metrics-cron`: Alpine sidecar refreshing `network_metrics_buckets` every 3 min
     - `hasura`: Hasura GraphQL Engine (port 8080)
   - Network: Custom bridge network `indexer-network`
   - Volumes: 
     - `postgres_data`: Persistent database storage
     - `./migrations/000_comprehensive_initial_schema.sql`: Auto-run SQL migrations
   - Health checks configured for all services

> **Debug mode**: run the same `docker-compose.yml` with verbose, human-readable
> logs via env vars —
> `LOG_LEVEL=DEBUG LOG_FORMAT=text docker compose up -d --build`.

### Environment Configuration

Create a `.env` file in the indexer directory:

```bash
cp .env.example .env
```

**Key variables:**
```bash
NODE_HOST=13.251.66.61  # Your ASI Chain observer node
GRPC_PORT=40452         # gRPC port
HTTP_PORT=40453          # HTTP port (used for /api/validators)
DATABASE_URL=postgresql://indexer:indexer_pass@postgres:5432/asichain
```

See `.env.example` for all available options. No pre-built binary is needed —
the gRPC client is generated at Docker build time from `protos/` via
`grpc_tools.protoc` (see `Dockerfile`).

## Configuration

### Switching Between Configurations

```bash
# Edit your .env file with new configuration
vim .env

# Restart indexer to apply changes
docker compose -f docker-compose.yml restart indexer

# Rebuild after code/proto changes
docker compose -f docker-compose.yml build
```

### Environment Variables

### Indexer Environment Variables

| Variable | Description                               | Default |
|----------|-------------------------------------------|---------|
| `NODE_HOST` | ASI Chain node hostname                   | `localhost` |
| `GRPC_PORT` | Node gRPC port for blockchain operations  | `40452` |
| `HTTP_PORT` | Node HTTP port for status queries         | `40453` |
| `NODE_TIMEOUT` | HTTP/gRPC request timeout in seconds      | `30` |
| `DATABASE_URL` | PostgreSQL connection URL                 | `postgresql://indexer:indexer_pass@localhost:5432/asichain` |
| `DATABASE_POOL_SIZE` | Database connection pool size             | `20` |
| `DATABASE_POOL_TIMEOUT` | Database pool timeout in seconds          | `10` |
| `SYNC_INTERVAL` | Seconds between sync cycles               | `5` |
| `BATCH_SIZE` | Number of blocks per batch                | `100` |
| `START_FROM_BLOCK` | Initial block to start indexing           | `0` |
| `MONITORING_PORT` | Prometheus metrics port                   | `9090` |
| `HEALTH_CHECK_INTERVAL` | Health check interval in seconds          | `60` |
| `LOG_LEVEL` | Logging level (DEBUG/INFO/WARNING/ERROR)  | `INFO` |
| `LOG_FORMAT` | Log format (json/text)                    | `json` |
| `ENABLE_ASI_TRANSFER_EXTRACTION` | Extract ASI transfers from deployments    | `true` |
| `ENABLE_PENDING_DEPLOYS_SYNC` | Poll node pending deploys every sync cycle | `true` |
| `ENABLE_METRICS` | Enable Prometheus metrics                 | `true` |
| `ENABLE_HEALTH_CHECK` | Enable health check endpoint              | `true` |
| `HASURA_ADMIN_SECRET` | Hasura admin secret (used by setup scripts) | Empty |

## Database Schema

### Core Tables

- **blocks**: Blockchain blocks with comprehensive metadata
  - Enhanced with JSONB fields for `bonds_map` and `justifications`
  - Tracks finalization status and fault tolerance metrics
  - 150-char proposer field for full validator keys

- **deployments**: Smart contract deployments
  - Full Rholang term storage
  - Automatic type classification
  - Error tracking and status management

- **pending_deploys**: Ephemeral snapshot of the node's deploy buffers
  - Deploys not yet included in any block (no block_hash / FK by design)
  - Fully refreshed (DELETE + INSERT) every sync cycle (`SYNC_INTERVAL`, default 5s)
  - `is_rejected` provenance: fresh (deploy_storage) vs recovering after merge conflict
  - `sig` matches `deployments.deploy_id` once the deploy is included in a block
  - Pre-cap total count in `indexer_state` key `pending_deploys_total_available` (node caps the response at 1000 entries)

- **transfers**: ASI token transfers
  - Supports both ASI addresses (52-57 chars) and validator public keys (130+ chars)
  - Tracks amounts in both dust and ASI (8 decimal precision)
  - Links to deployments and blocks

- **balance_states**: Address balance tracking
  - Separate bonded and unbonded balances
  - Point-in-time balance snapshots per block
  - Supports both validator keys and ASI addresses

- **validators**: Validator registry
  - Full public key storage (up to 200 chars)
  - Status tracking (active/bonded/quarantine/inactive)
  - First/last seen block tracking

- **validator_bonds**: Stake records per block
  - Genesis bonds automatically extracted
  - Links to blocks for historical tracking

- **block_validators**: Block signers/justifications
  - Many-to-many relationship between blocks and validators

- **network_stats**: Network health snapshots
  - Consensus participation rates
  - Active validator counts
  - Quarantine metrics

- **epoch_transitions**: Epoch boundaries
  - Start/end blocks per epoch
  - Active validator counts

- **indexer_state**: Indexer metadata (key-value store)
  - Sync/runtime state: e.g. `pending_deploys_total_available` (pre-cap pending deploy count, updated every sync cycle)

### Views

- **transaction_history_view**: Combined wallet transaction history
  - Pre-joins `deployments` LEFT JOIN `transfers` into one row per transfer (or per deployment if it produced none)
  - Fields: `transfer_id`, `deploy_id`, `block_hash`, `block_number`, `timestamp`, `type` (`'transfer'` | `'not_transfer'`), `deployer_address`, `from_address`, `to_address`, `from_public_key`, `amount_asi`, `status`
  - All `transfers.*` fields are NULL when `type = 'not_transfer'` (deployment produced no transfer)
  - Hasura public SELECT with `limit: 5000` and `allow_aggregations: true`
  - Pagination via GraphQL `where` / `order_by` / `offset` / `limit` (no SQL parameters)

- **block_ancestors_view / block_descendants_view**: DAG traversal result types
  - `block_ancestors_view`: `ancestor_hash`, `ancestor_number`, `depth`
  - `block_descendants_view`: `descendant_hash`, `descendant_number`, `depth`
  - Used as the return type of `get_block_ancestors` / `get_block_descendants` SQL functions

### SQL Functions

- **get_block_ancestors(p_block_hash)**: Recursively returns all ancestor blocks of the given block by walking `block_parents` (DAG traversal)
- **get_block_descendants(p_block_hash)**: Recursively returns all descendant blocks of the given block by walking `block_parents`
- Both are `LANGUAGE sql STABLE` recursive CTEs using `UNION` (deduplicated) and are exposed via Hasura as callable GraphQL functions

## API Endpoints

### Status and Health

```bash
# Detailed sync status
curl http://localhost:9090/status | jq .

# Health check
curl http://localhost:9090/health

# Readiness check
curl http://localhost:9090/ready
```

### Data Endpoints

All existing endpoints continue to work with enhanced data:

```bash
# Blocks with enhanced metadata
curl http://localhost:9090/api/blocks | jq .

# Network statistics
curl http://localhost:9090/api/stats/network | jq .

# Epoch information
curl http://localhost:9090/api/epochs | jq .

# Validator performance
curl http://localhost:9090/api/validators | jq .
```

## Monitoring

The indexer exposes a Prometheus-compatible `/metrics` endpoint on port 9090:

- `indexer_blocks_indexed_total`: Total blocks processed
- `indexer_sync_lag_blocks`: Blocks behind chain head
- `indexer_grpc_requests_total`: gRPC calls made to the node
- `indexer_grpc_errors_total`: gRPC call failures
- `indexer_epoch_transitions_total`: Epoch changes detected
- `indexer_network_health_score`: Network consensus health (0-1)

## Troubleshooting

### Common Issues

1. **gRPC connection refused / unavailable**
   - Verify the observer node is reachable from the indexer container:
     `docker exec asi-indexer python -c "import grpc, asyncio; asyncio.run(grpc.aio.insecure_channel('host:40452').channel_ready())"`
   - Check `NODE_HOST`, `GRPC_PORT`, `HTTP_PORT` in `.env` match the observer node
   - For Linux Docker hosts, use the actual IP address instead of `host.docker.internal`

2. **Cannot connect to node**
   - Verify node is running and ports are accessible
   - Check `NODE_HOST` is set correctly (use `host.docker.internal` for Docker on Mac/Windows)
   - For Linux Docker hosts, use actual IP address instead of `host.docker.internal`

3. **Database schema errors**
   - Run migrations: `docker exec asi-indexer-db psql -U indexer -d asichain < migrations/000_comprehensive_initial_schema.sql`

### Docker-Specific Issues

1. **Build fails**
   - Ensure Docker has at least 2GB RAM allocated
   - Check disk space (gRPC stub generation needs a few hundred MB)
   - Try cleaning Docker cache: `docker system prune -a`
   - Rebuild: `docker compose -f docker-compose.yml build`

2. **Container health checks failing**
   ```bash
   # Check container logs
   docker compose -f docker-compose.yml logs indexer
    
   # Verify all services are running
   docker compose -f docker-compose.yml ps
   
   # Check network connectivity between containers
   docker exec asi-rust-indexer ping postgres
   ```

3. **Permission errors with volumes**
   ```bash
   # Fix PostgreSQL volume permissions
   sudo chown -R 999:999 ./postgres_data
   
   # Or remove and recreate volumes
   docker compose -f docker-compose.yml down -v
   docker compose -f docker-compose.yml up -d
   ```

### Reset and Start Fresh

```bash
# Stop services and remove data
docker compose -f docker-compose.yml down -v

# Start fresh sync from block 0
docker compose -f docker-compose.yml up -d
```

## Performance Characteristics

- **Memory Usage**: ~80MB (indexer) + ~50MB (database)
- **CPU Usage**: <5% during sync, <1% when caught up
- **Sync Performance**: 100 blocks in ~2 seconds
- **Database Growth**: ~100KB per 100 blocks (with enhanced data)
- **gRPC round-trip latency**: 10–50ms per call
- **Full Chain Sync**: Capable of syncing entire blockchain

## Migration from HTTP Indexer

To migrate from the HTTP-based indexer:

1. **Export existing data** (optional):
   ```bash
   docker exec asi-indexer-db pg_dump -U indexer asichain > backup.sql
   ```

2. **Stop old indexer**:
   ```bash
   docker compose down
   ```

3. **Start the gRPC indexer**:
   ```bash
   docker compose -f docker-compose.yml up -d --build
   ./scripts/full-init-hasura.sh
   ```

The indexer will start syncing from block 0 by default (`START_FROM_BLOCK=0`),
building a complete chain history.

## Development

### Project Structure

```
indexer/
├── src/
│   ├── grpc_node_client.py    # Async gRPC client (DeployServiceV1)
│   ├── rust_indexer.py        # Indexer service (legacy name retained; uses gRPC, not Rust CLI)
│   ├── models.py              # Database models (DAG-aware)
│   ├── database.py            # asyncpg/SQLAlchemy sessions
│   ├── main.py                # Entry point / orchestrator
│   ├── monitoring.py          # REST API + Prometheus metrics
│   ├── config.py              # Pydantic settings
│   ├── addr.py                # ASI address derivation
│   ├── cache.py / resilience.py
│   └── grpc_stubs/            # Generated gRPC client (from protos/)
├── migrations/
│   ├── 000_comprehensive_initial_schema.sql  # Current complete schema
│   └── backup_old_migrations/
├── scripts/
│   ├── full-init-hasura.sh               # FULL Hasura setup (recommended)
│   ├── refresh-network-metrics-once.sh   # Sidecar entrypoint for metrics-cron
│   ├── configure-hasura.py               # Legacy Hasura setup (older)
│   ├── configure-hasura.sh               # Legacy Hasura setup (older)
│   ├── setup-hasura-relationships.sh      # Legacy relationship setup
│   ├── fix-hasura-relationships.py        # Legacy relationship fix
│   ├── test-relationships.sh / test-stats.sh
│   └── seed_test_data.py
├── protos/                    # Protobuf definitions for the node's DeployServiceV1
├── examples/
├── Docker Configuration:
│   ├── Dockerfile             # Multi-stage Python image; regenerates gRPC stubs
│   ├── docker-compose.yml     # Production (postgres + indexer + metrics-cron + hasura)
│   └── deploy.sh              # One-command deploy + Hasura init + self-tests
├── Environment Templates:
│   ├── .env.example           # Reference with all options
│   ├── .env.template          # Blank template
│   ├── .env.remote-observer   # Remote observer node sample
│   └── .env.rust              # Legacy
├── Documentation:
│   ├── README.md                 # This file
│   ├── API.md                    # REST API documentation
│   ├── CHANGELOG.md              # Version history
│   ├── DEPLOYMENT.md             # Deployment scenarios (current)
│   ├── DEPLOYMENT_GUIDE.md       # Quick deployment guide
│   ├── DEPLOYMENT_DOCUMENTATION.md  # Comprehensive deployment
│   ├── GRAPHQL_GUIDE.md          # GraphQL usage guide
│   └── GRAPHQL_SCHEMA.md         # Database schema reference
```

### Modifying the gRPC Client

The gRPC stubs are generated at Docker build time from `protos/`. To regenerate
them locally (e.g. after editing a `.proto`):

1. `pip install grpcio-tools==1.83.0`
2. `make protos` (regenerates stubs into `src/grpc_stubs/`)
3. Restart the indexer

If you need to call a new node RPC, add a method to `GrpcNodeClient` in
`src/grpc_node_client.py`, then wire it into `src/rust_indexer.py`.

## License

MIT
