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


A high-performance blockchain indexer for ASI-Chain that synchronizes data from ASI nodes using the Rust CLI client and stores it in PostgreSQL for efficient querying.

## Latest Version

The indexer provides complete automation for blockchain data synchronization:
- Full blockchain sync from genesis (block 0) using Rust CLI
- Automatic Hasura GraphQL relationships setup
- Enhanced ASI transfer detection with Rholang pattern matching
- Comprehensive database schema with single migration
- Balance tracking with bonded/unbonded separation

## Current Status

✅ **Working Features:**
- **Genesis block processing** with automatic extraction of validator bonds and initial allocations
- **Full blockchain synchronization from block 0** using Rust CLI
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
- Uses native Rust CLI for blockchain interaction
- Cross-compiled from macOS ARM64 to Linux x86_64 in Docker
- Enhanced database schema for additional data types
- Removed dependency on limited HTTP APIs
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
                      │ gRPC/HTTP
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                    Rust CLI Client                          │
│         (Blockchain Data Extraction Interface)              │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ Command Execution
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
│  Tables: blocks, deployments, transfers, validators,        │
│          validator_bonds, balance_states, network_stats     │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ Database Connection
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                   Hasura GraphQL Engine                     │
│  - Auto-generated GraphQL API                               │
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

## Rust CLI Commands Used

The indexer leverages these Rust CLI commands for comprehensive data extraction:

1. **last-finalized-block** - Get the latest finalized block information
2. **get-blocks-by-height** - Fetch blocks within a height range (supports large batches)
3. **blocks** - Get detailed block information including deployments
4. **get-deploy** - Retrieve specific deployment details
5. **bonds** - Get current validator bonds and stakes
6. **active-validators** - List currently active validators
7. **epoch-info** - Get epoch transitions and timing
8. **network-consensus** - Monitor network health and participation
9. **show-main-chain** - Verify main chain consistency

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
- Build Rust CLI from source (10-15 min first time, cached after)
- Set up PostgreSQL database with complete schema
- Start syncing from genesis block

After running the Hasura configuration scripts in Step 3:
- GraphQL relationships will be configured
- API will be ready for complex queries

**Access services:**
- Indexer API: http://localhost:9090
- Hasura Console: http://localhost:8080/console
- GraphQL endpoint: http://localhost:8080/v1/graphql

#### Docker Compose Files

1. **docker-compose.yml** (Production)
   - Uses Dockerfile by default
   - Services included:
     - `postgres`: PostgreSQL 14 Alpine (port 5432)
     - `rust-indexer`: Python indexer with Rust CLI (port 9090)
     - `hasura`: Hasura GraphQL Engine (port 8080)
   - Network: Custom bridge network `indexer-network`
   - Volumes: 
     - `postgres_data`: Persistent database storage
     - `./migrations:/docker-entrypoint-initdb.d`: Auto-run SQL migrations
   - Health checks configured for all services

2. **docker-compose.debug.yml** (Debug)
   - Same as production but with full dependency installation in runtime

### Environment Configuration

Create a `.env` file in the indexer directory:

```bash
cp .env.example .env
```

**Key variables:**
```bash
NODE_HOST=13.251.66.61  # Your ASI Chain node
GRPC_PORT=40452         # gRPC port
HTTP_PORT=40453         # HTTP port
DATABASE_URL=postgresql://indexer:indexer_pass@postgres:5432/asichain
```

See `.env.example` for all available options.

### Building Rust CLI (Optional)

If you need to build the Rust CLI for a different platform:

```bash
# Clone rust-client repository
cd ../rust-client

# For Linux (cross-compilation from macOS)
rustup target add x86_64-unknown-linux-musl
brew install filosottile/musl-cross/musl-cross
CC=x86_64-linux-musl-gcc cargo build --release --target x86_64-unknown-linux-musl

# Copy to indexer
cp target/x86_64-unknown-linux-musl/release/node_cli ../indexer/node_cli_linux
```

## Configuration

### Switching Between Configurations

```bash
# Edit your .env file with new configuration
vim .env

# Restart indexer to apply changes
docker compose -f docker-compose.yml restart rust-indexer

# Use pre-compiled binary instead of building from source
# 1. Edit docker-compose.yml
# 2. Change: dockerfile: indexer/Dockerfile.rust-builder
#    To: dockerfile: indexer/Dockerfile.rust-simple
# 3. Ensure node_cli_linux exists in indexer directory
# 4. Rebuild: docker compose -f docker-compose.yml build
```

### Environment Variables

### Indexer Environment Variables

| Variable | Description                               | Default |
|----------|-------------------------------------------|---------|
| `NODE_HOST` | ASI Chain node hostname                   | `localhost` |
| `GRPC_PORT` | Node gRPC port for blockchain operations  | `40412` |
| `HTTP_PORT` | Node HTTP port for status queries         | `40413` |
| `NODE_URL` | RChain node HTTP API endpoint             | `http://localhost:40453` |
| `NODE_TIMEOUT` | HTTP request timeout in seconds           | `30` |
| `RUST_CLI_PATH` | Path to Rust CLI executable               | `/rust-client/target/release/node_cli` |
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
| `ENABLE_METRICS` | Enable Prometheus metrics                 | `true` |
| `ENABLE_HEALTH_CHECK` | Enable health check endpoint              | `true` |
| `HASURA_ADMIN_SECRET` | Hasura admin secret (not used by indexer) | Empty |

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

- **indexer_state**: Indexer metadata (⚠️ Not implemented)
  - Intended for key-value store for indexer state
  - Currently not created in schema

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

The Rust indexer provides enhanced metrics:

- `indexer_blocks_indexed_total`: Total blocks processed
- `indexer_sync_lag_blocks`: Blocks behind chain head
- `indexer_cli_commands_total`: CLI commands executed
- `indexer_cli_errors_total`: CLI command failures
- `indexer_epoch_transitions_total`: Epoch changes detected
- `indexer_network_health_score`: Network consensus health (0-1)

## Troubleshooting

### Common Issues

1. **CLI binary not found**
   - Ensure `node_cli_linux` is in the indexer directory
   - Check binary has execute permissions: `chmod +x node_cli_linux`

2. **Cannot connect to node**
   - Verify node is running and ports are accessible
   - Check `NODE_HOST` is set correctly (use `host.docker.internal` for Docker on Mac/Windows)
   - For Linux Docker hosts, use actual IP address instead of `host.docker.internal`

3. **Database schema errors**
   - Run migrations: `docker exec asi-indexer-db psql -U indexer -d asichain < migrations/000_comprehensive_initial_schema.sql`

### Docker-Specific Issues

1. **Build fails with Dockerfile.rust-builder**
   - Ensure Docker has at least 8GB RAM allocated
   - Check disk space (need ~20GB free for Rust compilation)
   - Try cleaning Docker cache: `docker system prune -a`
   - Switch to pre-compiled binary method (Dockerfile.rust-simple)

2. **Container health checks failing**
   ```bash
   # Check container logs
   docker compose -f docker-compose.yml logs rust-indexer
   
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
- **CLI Command Latency**: 10-50ms per command
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

3. **Start Rust indexer**:
   ```bash
   docker compose -f docker-compose.rust.yml up -d
   ```

The Rust indexer will start syncing from block 0 by default, building a complete chain history.

## Development

### Project Structure

```
indexer/
├── src/
│   ├── rust_cli_client.py    # Rust CLI wrapper with bond detection fix
│   ├── rust_indexer.py        # Enhanced indexer with NULL handling
│   ├── models.py              # Database models (10 tables)
│   ├── main.py                # Entry point with CLI detection
│   └── monitoring.py          # REST API and metrics endpoints
├── migrations/
│   ├── 000_comprehensive_initial_schema.sql  # Complete schema
│   ├── 001_initial_schema.sql               # Legacy
│   └── 002_add_enhanced_tables.sql          # Legacy
├── scripts/
│   ├── full-init-hasura.sh           # FULL Hasura setup
│   ├── setup-hasura-relationships.sh # Relationship configuration
│   └── test-relationships.sh         # GraphQL tests
├── examples/
│   └── graphql-queries.md           # Sample GraphQL queries
├── Docker Configuration:
│   ├── Dockerfile                   
│   ├── docker-compose.yml          
├── Environment Templates:
│   ├── .env                         # Active configuration
│   ├── .env.remote-observer        
│   ├── .env.rust                    
│   ├── .env.template                # Blank template
│   └── .env.example                 # Reference with all options
├── Documentation:
│   ├── README.md                    # This file
│   ├── API.md                       # REST API documentation
│   ├── CHANGELOG.md                 # Version history
│   ├── DEPLOYMENT.md                # Deployment scenarios
│   ├── DEPLOYMENT_GUIDE.md          # Quick deployment guide
│   ├── DEPLOYMENT_DOCUMENTATION.md  # Comprehensive deployment
│   ├── GRAPHQL_GUIDE.md             # GraphQL usage guide
│   └── GRAPHQL_SCHEMA.md            # Database schema reference
└── node_cli_linux                   # Pre-compiled Rust CLI (optional)
```

### Adding New CLI Commands

To add support for new CLI commands:

1. Add method to `RustCLIClient` in `rust_cli_client.py`
2. Parse command output (text or JSON)
3. Update indexer logic in `rust_indexer.py`
4. Add database models if needed
5. Create migration for schema changes

## License

MIT
