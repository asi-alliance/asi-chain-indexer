# ASI Chain Indexer

The indexer reads ASI Chain node gRPC and HTTP APIs, stores blocks and native-token
activity in PostgreSQL, and exposes data through Hasura GraphQL and an aiohttp API.
It uses SQLAlchemy ORM and asyncpg directly; no external node CLI is required.

This README describes the integration branch, `dev`. Use the files at your selected
revision as the configuration and dependency references. A feature PR or a published
image may contain different behavior; verify its source before deployment.

## Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│ ASI Chain observer node                                      │
│ Blocks, deploys, validator observations and replay reports   │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       │ gRPC + HTTP (configured node ports)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ Node API client                                              │
│ gRPC block/deploy reads; HTTP validators and replay traces   │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       │ in-process calls
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ Python indexer service                                       │
│ Block synchronization and deploy processing                  │
│ Native call/result correlation and successful transfers      │
│ Validator observations, network statistics and sync cursor   │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       │ asyncpg / SQLAlchemy
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ PostgreSQL database                                          │
│ blocks, block_parents, deployments, pending_deploys          │
│ transfers, native_transfer_calls, native_transfer_results    │
│ validators, validator_bonds, block_validators                │
│ balance_states, network_stats, epoch_transitions             │
│ indexer_state                                                │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       │ tracked tables, views and SQL functions
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ Hasura GraphQL engine                                        │
│ DAG relationships, exact-amount queries and live updates     │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       │ GraphQL over HTTP / WebSocket
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ Client applications                                          │
│ Explorer, wallet and other consumers                         │
└──────────────────────────────────────────────────────────────┘
```

The indexer also exposes its own HTTP data, status and Prometheus endpoints on
port 9090. The metrics sidecar refreshes PostgreSQL network metric buckets; it
does not sit between the indexer and Hasura. See [API and monitoring](#api-and-monitoring)
for these paths and the metadata requirements for lifecycle queries.

For runtime versions, dependency pins and lock maintenance, see
[DEPENDENCIES.md](DEPENDENCIES.md).

The service stores blocks by hash and retains ordered parent edges, deploys,
validator observations, pending deploy snapshots, exact amounts, native call/result
history and a sync cursor. Successful native movements come from replay reports,
including calls completed by a later deploy. Pending/rejected calls remain separate
from successful ledger totals. Hasura exposes relationships, SQL views and live
queries; the metrics sidecar refreshes network metric buckets every three minutes.

## Node compatibility and exact amounts

Configure a read-only observer with the gRPC service described by [protos/](protos/),
HTTP APIs, retained replay state and phase-marked `/reporting/trace` responses for
the standard SystemVault contract. Transfer extraction requires these reports;
missing, mismatched or unsupported reports cause retry rather than source-regex
fallback. A reachable node alone does not establish compatibility.

Raw token amounts are checked Python integers stored as uint256-range
`NUMERIC(78,0)`. Human-readable amounts use exact decimal strings. Keep
`HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES=true` and preserve strings in consumers.

`NATIVE_TOKEN_DECIMALS` defaults to **8** and must match node token metadata.
Set **18** only for a chain declaring 18 decimals. Startup checks this setting;
legacy nodes without token metadata support the eight-decimal profile. Each row
retains its recorded precision; changing the setting does not rescale historical
balances or change the chain denomination.

Select the wire contract explicitly with `NODE_PROTOCOL_PROFILE`:

| Profile | Pinned node schema | Monetary wire fields |
|---|---|---|
| `classic` (default) | [#896 at 92903a1](https://github.com/F1R3FLY-io/f1r3node-rust/commit/92903a1d81c49fa0a035fb7ee729002e38428280) | Legacy int64 stakes/prices; exact transfer negotiation and authoritative `amountRaw` |
| `uint256` | [merged #881 at 6233501](https://github.com/F1R3FLY-io/f1r3node-rust/commit/6233501064492d0cac9c7ebc598daf70c501a215) | Decimal-string stakes/prices/`amountRaw`; 32-byte big-endian pending deploy prices; no exact-transfer flags |

This includes the exact-transfer and replay-history hardening from
[#22](https://github.com/asi-alliance/asi-chain-indexer/pull/22). Missing or malformed
compact transfer amounts stay unknown; replay remains authoritative for the ledger.
Signed rejected attempts are preserved, including requests outside uint256.
`GRPC_MAX_RECEIVE_MESSAGE_BYTES` bounds responses (default 64 MiB, range 1–2147483647).

For a uint256 image and local Compose installation:

```bash
# Set NODE_PROTOCOL_PROFILE=uint256 in .env alongside the node's actual precision.
docker compose build indexer
# After the reviewed database/metadata upgrade:
docker compose up -d --force-recreate indexer
```

For a standalone build use `docker build --build-arg NODE_PROTOCOL_PROFILE=uint256 .`.
For local Python use `NODE_PROTOCOL_PROFILE=uint256 make protos PYTHON=python` and
run with the same environment setting. The image retains its build profile as the
runtime default. An environment/binding mismatch fails immediately. Monetary
messages reject incompatible wire fields on every read, including after a node
schema switch or reconnect; rejected blocks cannot advance the sync cursor.
Select by source revision, not by a guessed version or token precision.
See [schema provenance](protos/README.md).

Migration 004 widens included and pending `phlo_price` to checked `NUMERIC(78,0)`.
Apply it once with writers stopped; then reload Hasura metadata so prices become
`numeric` GraphQL strings. Consumers with typed `bigint` price filters must adapt.
The migration preserves historical values and per-row precision. Heights, timestamps,
phlo limits and gas counts retain their existing bounded types.

Validator refreshes read finalized stakes over gRPC independently of HTTP activity
membership. When HTTP membership is unavailable, stakes and recorded precision
still refresh while existing activity statuses are preserved. New validators
start as `bonded`; a successful HTTP response can update their activity status.
Stored validators that are not in the finalized bond set become `inactive`. If such
a validator bonds again, the next refresh sets `active` or `bonded` (`bonded` when
HTTP membership is unavailable). A failed finalized-block read, or a finalized
block without bonds, is not treated as membership evidence: stored statuses and
stakes are kept unchanged until a later refresh reads a non-empty bond set.

`python analyze_transfers.py` reads successful ledger transfers, including
`genesis_mint` and `genesis_bond` records, through Hasura
using `HASURA_URL` (default `http://localhost:8080/v1/graphql`) and an optional
`HASURA_ADMIN_SECRET`. It paginates by transfer ID, rejects GraphQL errors, and
computes exact totals and net flows from `amount_dust` and each row's recorded
`token_decimals`. Detailed output retains fractional units; averages use exact
fractions. These net flows exclude fees and are not wallet balance snapshots.

Before release, validate a pinned fresh-state #881 observer with a funded wide
transfer through ingestion, SQL and GraphQL. Protocol fixtures and database tests
do not replace that live acceptance. Switching this setting does not migrate node
state, authorize a chain reset, or select the cost-accounting protocol.

A pending deploy disappearing from a node buffer is not proof of successful
execution or finality. Block heights can have multiple DAG siblings; preserve every
block hash and ordered parent link. [DAG.md](DAG.md) contains a dated research
snapshot: its old branch-status table is historical, not a current feature inventory.

### Classic exact-transfer compatibility and replay history

The historical `classic` profile requests `exactTransferAmounts=true` on every
full-block read, including finalized bonds and deploy lookup. It preserves the
legacy integer `amount`; use canonical signed `amountRaw` text whenever known.
Without `amountRaw`, legacy values convert directly to text except ambiguous `-1`,
which remains unknown. A malformed exact field also becomes `None`, with a warning
containing only its character count, and never falls back to the legacy amount.
The `uint256` profile uses field 6 directly and sends no exact-transfer flags.

Unknown compact amounts do not bypass replay processing. Non-genesis blocks with
deploys still require valid phase-marked reports before ledger writes and cursor
advancement. Old nodes can omit BigInt transfers from compact summaries; source
terms and compact summaries never replace execution-derived ledger movements.
Signed rejected requests retain every digit through persistence and later result
correlation, including values above Python's 4,300-digit conversion limit, without
changing that limit or rounding with the Decimal context. There is no size
maximum: the stored `requested_amount` stays canonical decimal text. The binary
to decimal conversion is subquadratic (about 0.8 s for 2,000,000 digits) and runs
in a worker process for values above 2^16 bits, so `/health` and metrics keep
answering while such a block is indexed. Later blocks compare the stored text and
never convert it back. Successful movements still require a positive uint256 amount.

Classic #896 acceptance must use its pinned historical node: compare the legacy
range error with the exact response for the same wide transfer. Current #881
acceptance uses the explicit `uint256` profile and migration 004 instead. Neither
fixture suite establishes funded acceptance against a running node. The separate
cost-accounting schema also requires its own selected source and reconciliation.

## Fresh local installation

These commands initialize a **new, disposable local installation**. Existing
installations must use the [upgrade procedure below](#existing-installations).
Prerequisites are Docker with Linux containers, Docker Compose, Bash and curl;
the Hasura setup script also uses standard Unix utilities. Containers need access
to their image/package repositories during builds and sidecar initialization.

```bash
git clone --branch dev https://github.com/asi-alliance/asi-chain-indexer.git
cd asi-chain-indexer
cp .env.example .env
```

Edit `.env` before starting. `.env.example` and `.env.template` use the
database credentials of the local Compose file. For the unmodified local
Compose file, use this shape:

```dotenv
NODE_HOST=observer.example.org
GRPC_PORT=40401
HTTP_PORT=40403
DATABASE_URL=postgresql://indexer:indexer_pass@postgres:5432/asichain
HASURA_ADMIN_SECRET=replace-with-a-long-random-secret
HASURA_BASE=http://localhost:8080
NATIVE_TOKEN_DECIMALS=8
```

Replace the observer hostname and verify the node's actual host port mappings.
The node's standard external gRPC/HTTP container ports are 40401/40403; the indexer's
legacy Python defaults and `.env.example` still use 40452/40453. Set both ports
explicitly for your node. Inside the indexer container, `localhost` refers to
that container. Use a reachable observer address or the appropriate host gateway.

The checked-in Compose file is a local example: database credentials are also
hard-coded in PostgreSQL and Hasura settings, and host ports 5432/8080/9090 are
published. Before remote use, review credentials, bind addresses, firewall rules,
Hasura public permissions and console exposure together. Keep admin secrets and
Mattermost webhook URLs out of browser assets, Git and logs.

```bash
docker compose config --quiet
docker compose up -d --wait --wait-timeout 180 postgres hasura metrics-cron
bash scripts/full-init-hasura.sh
docker compose up -d --build indexer
docker compose ps
curl -fsS http://localhost:9090/status
```

PostgreSQL runs SQL migrations 000–007 only when initializing an empty volume.
The Hasura script changes metadata and permissions; use it for a fresh installation,
not as a read-only probe or a replacement for reviewing existing custom metadata.
[deploy.sh](deploy.sh) wraps startup and metadata initialization, but currently
prints required environment values, including the Hasura secret. Review it before
use; avoid its destructive `--clean` option for retained data.

| Local service | Address / storage |
|---|---|
| Indexer HTTP API | `http://localhost:9090` |
| Hasura GraphQL | `http://localhost:8080/v1/graphql` |
| Hasura console | `http://localhost:8080/console` |
| PostgreSQL | host port `5432`; Compose service `postgres:5432` |
| PostgreSQL volume | `postgres18_data` mounted at `/var/lib/postgresql` |

The volume is scoped to the Compose project. PostgreSQL 18's default PGDATA here
is `/var/lib/postgresql/18/docker`. The metrics sidecar has no healthcheck;
container startup is not proof of successful indexing or metric refresh.

## Existing installations

Read [POSTGRES_UPGRADE.md](POSTGRES_UPGRADE.md) and the
[migration and history-repair guide](migrations/README.md) before rollout.

- Back up the actual database and Hasura metadata, stop writers and rehearse
  the upgrade. PostgreSQL 14 data requires dump/restore into a new PostgreSQL 18.6
  cluster; retain the old volume and matching images for rollback.
- On an empty database, `python -m src.migrate upgrade` applies tracked revisions
  000–007. For an existing unversioned SQL schema, inspect its actual level, use
  `python -m src.migrate adopt --revision REVISION` once, selecting its actual
  revision from `000`, `001`, `002`, `003`, `004`, `005`, `006` or `007`, then run
  `python -m src.migrate upgrade`. A database already tracked at
  003, 004, 005 or 006 upgrades to 007. Compose restarts and SQLAlchemy `create_all()` do not upgrade
  existing schemas. See the migration guide for validation and backup requirements.
- Populated databases marked for native-transfer backfill must complete
  `python -m src.reindex_transfers` with writes stopped. Do not manually clear
  the repair flag. Repair preserves blocks, the sync cursor and recorded precision.
- Review/reload Hasura metadata, then validate actual consumer queries,
  subscriptions, exact balances, cursor progress and metrics before resuming access.

Do not use `docker compose down -v`, `deploy.sh --clean`, `make docker-reset`
or `python -m src.main --reset` as an upgrade procedure. These can delete data;
ORM table recreation also does not replace the SQL views, functions and triggers.

## Configuration

[.env.example](.env.example), [src/config.py](src/config.py) and
[docker-compose.yml](docker-compose.yml) define different layers: Python defaults,
environment values and the variables actually passed into containers. For example,
`BATCH_SIZE` defaults to 100 in Python and 50 in Compose and both templates.
`.env.example` lists the Python-only settings that Compose does not pass to the
container, for example `MONITORING_PORT` and `DATABASE_POOL_TIMEOUT`.

Important settings include `NODE_HOST`, `GRPC_PORT`, `HTTP_PORT`,
`NODE_TIMEOUT`, `GRPC_MAX_RECEIVE_MESSAGE_BYTES`, `DATABASE_URL`, `DATABASE_POOL_SIZE`, `SYNC_INTERVAL`,
`BATCH_SIZE`, `START_FROM_BLOCK`, `NATIVE_TOKEN_DECIMALS`,
`ENABLE_ASI_TRANSFER_EXTRACTION` and `ENABLE_PENDING_DEPLOYS_SYNC`.
The pending-deploy snapshot represents one node's bounded buffers, not a durable
network-wide queue. `START_FROM_BLOCK` is an initial-history boundary, not a
command to discard the existing cursor or reindex stored history.

Mattermost alerts are disabled by default. Enabling `ALERTS_ENABLED` requires
`MATTERMOST_WEBHOOK_URL`; see the template for throttling and failure thresholds.
Never log or publish webhook values. Configuration is loaded at process startup.
After editing Compose environment values, recreate the affected service:

```bash
docker compose up -d --force-recreate indexer
# After code/protobuf/dependency changes:
docker compose up -d --build indexer
```

A plain `docker compose restart` does not replace the container's environment.

## API and monitoring

| Endpoint | Behavior |
|---|---|
| `GET /health` | Basic HTTP-process response; does not check dependencies or sync progress |
| `GET /readiness` (alias `/ready`) | Checks a database query and a last-finalized-block read; returns 200 or 503 |
| `GET /status` | Database/cursor statistics and node observations; inspect the response content |
| `GET /metrics` | Prometheus exposition with request-time dynamic updates |
| `GET /api/blocks`, `/api/blocks/{block_number}`, `/api/blocks/search` | Block reads/search; a height is not a unique DAG identity |
| `GET /api/deployments`, `/api/deployments/{deploy_id}`, `/api/deployments/search` | Deploy reads/search |
| `GET /api/transfers`, `/api/address/{address}/transfers` | Successful ledger movements |
| `GET /api/validators`, `/api/stats/network` | Stored validator/network data |

Routes are defined in [src/monitoring.py](src/monitoring.py); there is no indexer
`/api/epochs` route. Hasura provides DAG relationships. Migration 003 defines the read-only
`native_transfer_lifecycle` SQL view with initiation/completion references and
pending, completed, rejected or coalesced outcomes. `full-init-hasura.sh` tracks
this view with public read access (no aggregations) and its relationships:
`initiation_deployment`, `completion_deployment`, `initiation_block`,
`completion_block`, `transfer`, and `deployments.native_transfer_calls` /
`deployments.completed_native_transfers`. Both array relationships return
lifecycle rows, that is call outcomes: a fork can record more than one outcome
for one call. The `native_transfer_calls` name comes from the older
`scripts/configure-hasura.py`. The raw call, result and alias tables stay
untracked. The older `scripts/configure-hasura.py` has hard-coded
connection/secret values and an additional `requests` dependency; do not run it
unchanged. Count movements from the transfer ledger, not lifecycle-row counts.

Exported metrics include `indexer_blocks_indexed_total`,
`indexer_deployments_indexed_total`, `indexer_transfers_extracted_total`,
`indexer_sync_lag_blocks`, `indexer_last_block_height`,
`indexer_sync_duration_seconds`, `indexer_node_request_duration_seconds` and
`indexer_http_request_duration_seconds`. The last one records the serving time of
each HTTP request. Its `endpoint` label is the route template, for example
`/api/address/{address}/transfers`, never the raw path. Unknown routes share
the `unmatched` value.
Health/readiness/metrics success alone does not prove continuing finalization;
compare fresh observations over time. Requests to status/readiness/metrics may
query the node; they are not an independent fleet monitor.

## Development and validation

Use CPython 3.14.10 on Linux, or the Docker targets. With a matching interpreter:

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
make protos PYTHON=python
python -W error::DeprecationWarning -m pytest tests/ -m "not integration" --strict-markers
python -m pip check
```

The generated `src/grpc_stubs/` directory is ignored. Regenerate it with the
locked generator after protobuf changes; do not hand-edit generated files.
To run the same unit suite in the test image:

```bash
docker build --target test -t asi-indexer-tests:local .
docker run --rm asi-indexer-tests:local python -W error::DeprecationWarning -m pytest tests/ -m "not integration" --strict-markers
```

Tests marked `integration` require disposable services. Supply
`TEST_DATABASE_URL` with CREATEDB permission, plus `TEST_HASURA_URL` and
`TEST_HASURA_ADMIN_SECRET` for an initialized Hasura test stack. Some tests
create/drop a random database; Hasura tests also write fixtures into the configured
source database. Never point these settings at retained application data.

[Database CI](.github/workflows/database-compatibility.yml) is the reference for
production/test builds, dependency isolation, metadata initialization, the full
suite, SQL amount assertions and metrics-client checks. [Unit CI](.github/workflows/tests.yml)
generates bindings and coverage. A run without external settings can skip integration
tests; inspect skips before claiming full validation. Black, Flake8 and mypy are
available as development tools; repository-wide findings are not currently CI gates.

For diagnostics, start with `docker compose ps`, bounded service logs and the
response bodies from status/readiness. For schema failures, identify the applied
migration and use the migration guide; rerunning migration 000 or deleting a volume
is not a repair. Existing documentation does not establish current performance:
measure the selected node, data set and source revision before sizing a deployment.

## Source map

| Area | Files |
|---|---|
| Node reads and synchronization | [grpc_node_client.py](src/grpc_node_client.py), [block_indexer.py](src/block_indexer.py) |
| Exact amounts and native execution | [amounts.py](src/amounts.py), [transfer_reports.py](src/transfer_reports.py), [native_transfers.py](src/native_transfers.py) |
| ORM, SQL and history repair | [models.py](src/models.py), [database.py](src/database.py), [migrations/](migrations/), [reindex_transfers.py](src/reindex_transfers.py) |
| Service and observability | [main.py](src/main.py), [monitoring.py](src/monitoring.py), [alerts.py](src/alerts.py), [config.py](src/config.py) |
| Contributor instructions | [AGENTS.md](AGENTS.md) |

Protocol/SQL/configuration files and implementation at the selected revision take
precedence over older architecture/API guides. Licensed under [Apache 2.0](LICENSE).
