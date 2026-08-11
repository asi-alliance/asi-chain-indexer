# GraphQL Schema Documentation

**Version**: 2.2.0 (dag_support) | **Updated**: August 2026

## Overview

The ASI-Chain indexer provides a GraphQL API through Hasura with automatic relationship configuration, exposing comprehensive blockchain data for querying. This document describes the current schema with 12 tables, 5 views, and 6 SQL functions, available queries, and relationships.

## Available Tables

### blocks
Stores blockchain block data. **Primary key: `block_hash`** (DAG-aware — `block_number` is NOT unique, a fork can reuse a height).

**Fields:**
- `block_hash` (varchar(64), **PRIMARY KEY**, NOT NULL): Block hash
- `block_number` (bigint, NOT NULL): Block height (not unique — DAG may have multiple blocks at the same height)
- `timestamp` (bigint, NOT NULL): Block timestamp (epoch ms)
- `proposer` (varchar(160), NOT NULL): Validator public key who proposed the block
- `state_hash` (varchar(64)): State hash
- `state_root_hash` (varchar(64)): Post-execution state root hash
- `pre_state_hash` (varchar(64)): Pre-execution state hash
- `seq_num` (integer): Sequence number
- `sig` (varchar(200)): Block signature
- `sig_algorithm` (varchar(20)): Signature algorithm
- `shard_id` (varchar(20)): Shard identifier
- `extra_bytes` (text): Extra block data
- `version` (integer): Block version
- `deployment_count` (integer, default 0): Number of deployments in block
- `finalization_status` (varchar(20), NOT NULL): `'finalized'` or `'unfinalized'` (real status from node, no default)
- `bonds_map` (jsonb): Validator bonds at this block
- `justifications` (jsonb): Block justifications
- `fault_tolerance` (numeric(5,4)): Fault tolerance value
- `created_at` (timestamp, NOT NULL, default NOW()): When indexed

> **DAG parents**: this table has no `parent_hash` column. Parents live in the `block_parents` junction table (one block → many parents). Use the `parent_links` array relationship in GraphQL.

### deployments
Stores smart contract deployments.

**Fields:**
- `deploy_id` (varchar(200), **PRIMARY KEY**): Deployment signature / ID
- `block_hash` (varchar(64), NOT NULL, FK → `blocks.block_hash`): Block containing the deployment
- `block_number` (bigint, NOT NULL): Block height (denormalised, no FK)
- `deployer` (varchar(200), NOT NULL): Deployer public key
- `deployer_address` (varchar(150), NOT NULL): ASI address derived from deployer public key
- `term` (text, NOT NULL): Rholang source code
- `timestamp` (bigint, NOT NULL): Deployment timestamp (epoch ms)
- `sig` (varchar(200), NOT NULL): Deploy signature
- `sig_algorithm` (varchar(20), default `'secp256k1'`): Signature algorithm
- `phlo_price` (bigint, default 1): Phlo price
- `phlo_limit` (bigint, default 1000000): Phlo limit
- `phlo_cost` (bigint, default 0): Phlo gas cost
- `valid_after_block_number` (bigint): Valid after block
- `errored` (boolean, default false): Whether deployment failed at execution
- `error_message` (text): Rholang error text (NULL when no error; sourced from node's `systemDeployError`)
- `deployment_type` (varchar(50)): Classification (`asi_transfer` / `validator_operation` / `smart_contract` / ...)
- `seq_num` (integer): Sequence number
- `shard_id` (varchar(20)): Shard ID
- `status` (varchar(20), default `'included'`): Deploy lifecycle status — **currently always `"included"`** in production (no `status` field in node's block-stream `DeployInfo`; the gRPC client hardcodes `"included"`)
- `created_at` (timestamp, NOT NULL, default NOW()): When indexed

### transfers
Stores ASI token transfers.

**Fields:**
- `id` (bigserial, **PRIMARY KEY**): Auto-increment transfer ID
- `deploy_id` (varchar(200), NOT NULL, FK → `deployments.deploy_id`): Related deployment ID
- `block_hash` (varchar(64), NOT NULL, FK → `blocks.block_hash`): Block hash containing the transfer
- `block_number` (bigint, NOT NULL): Block height (denormalised, no FK)
- `from_address` (varchar(150), NOT NULL): Sender ASI address
- `from_public_key` (varchar(150), nullable): Sender public key (NULL when sender is a pure ASI address)
- `to_address` (varchar(150), NOT NULL): Recipient ASI address
- `amount_dust` (bigint, NOT NULL): Amount in dust (smallest unit)
- `amount_asi` (numeric(20,8), NOT NULL): Amount in ASI (8 decimals — NOT bigint)
- `status` (varchar(20), default `'success'`): Transfer status — production values: `'success'` / `'failed'` / `'genesis_mint'` / `'genesis_bond'`
- `timestamp` (bigint, NOT NULL): Transfer timestamp (epoch ms)
- `created_at` (timestamp, NOT NULL, default NOW()): When indexed

### block_parents
DAG junction table tracking all parents of a block (a block may have multiple parents).

**Fields:**
- `block_hash` (varchar(64), NOT NULL, FK → `blocks.block_hash` ON DELETE CASCADE, **composite PK**): Child block hash
- `parent_hash` (varchar(64), NOT NULL, **composite PK**): Parent block hash (no FK — parent may not be indexed yet)
- `parent_index` (integer, NOT NULL, default 0): Order of this parent among the block's parents
- `created_at` (timestamp, NOT NULL, default NOW()): When the link was indexed

### validators
Stores validator information.

**Fields:**
- `public_key` (varchar(200), **PRIMARY KEY**): Validator public key
- `name` (varchar(160)): Validator name (can store full public key up to 160 chars)
- `total_stake` (bigint, default 0): Total staked amount in dust
- `first_seen_block` (bigint): First block where validator appeared
- `last_seen_block` (bigint): Last block where validator was seen
- `status` (varchar(20), default `'bonded'`): Validator status (`active` / `bonded` / `quarantine` / `inactive`)
- `created_at` (timestamp, NOT NULL, default NOW()): When first indexed
- `updated_at` (timestamp, NOT NULL, default NOW()): Last update

### validator_bonds
Stores validator bond/stake records.

**Fields:**
- `id` (bigserial, **PRIMARY KEY**): Unique record ID
- `block_hash` (varchar(64), NOT NULL, FK → `blocks.block_hash` ON DELETE CASCADE): Block hash
- `block_number` (bigint, NOT NULL): Block height (denormalised, no FK)
- `validator_public_key` (varchar(200), NOT NULL): Validator's public key
- `stake` (bigint, NOT NULL): Staked amount in dust
- **UNIQUE** (`block_hash`, `validator_public_key`)

### network_stats
Stores network health statistics.

**Fields:**
- `id` (bigserial, **PRIMARY KEY**): Unique record ID
- `block_number` (bigint, NOT NULL): Block number
- `total_validators` (integer, NOT NULL): Total validator count
- `active_validators` (integer, NOT NULL): Active validator count
- `validators_in_quarantine` (integer, default 0): Quarantined validators
- `consensus_participation` (numeric(5,2), NOT NULL): Participation rate (%)
- `consensus_status` (varchar(20), NOT NULL): Consensus status
- `timestamp` (**timestamp**, NOT NULL, default `CURRENT_TIMESTAMP`): When captured (NOT bigint — SQL timestamp)

### balance_states
Stores address balance snapshots with bonded/unbonded separation.

**Fields:**
- `id` (bigserial, **PRIMARY KEY**): Unique record ID
- `address` (varchar(150), NOT NULL): ASI address or validator public key
- `block_hash` (varchar(64), NOT NULL, FK → `blocks.block_hash` ON DELETE CASCADE): Block hash
- `block_number` (bigint, NOT NULL): Block height (denormalised, no FK)
- `unbonded_balance_dust` (bigint, NOT NULL, default 0): Unbonded balance in dust
- `unbonded_balance_asi` (numeric(20,8), NOT NULL, default 0): Unbonded balance in ASI
- `bonded_balance_dust` (bigint, NOT NULL, default 0): Bonded/staked balance in dust
- `bonded_balance_asi` (numeric(20,8), NOT NULL, default 0): Bonded balance in ASI
- `total_balance_dust` (bigint, **GENERATED ALWAYS AS** (`unbonded_balance_dust + bonded_balance_dust`) STORED): Computed total in dust
- `total_balance_asi` (numeric(20,8), **GENERATED ALWAYS AS** (`unbonded_balance_asi + bonded_balance_asi`) STORED): Computed total in ASI
- `updated_at` (timestamp, NOT NULL, default NOW()): When updated
- **UNIQUE** (`address`, `block_hash`)

### block_validators
Many-to-many relationship between blocks and validators (justifications).

**Fields:**
- `block_hash` (varchar(64), FK → `blocks.block_hash` ON DELETE CASCADE, **composite PK**): Block hash
- `validator_public_key` (varchar(200), **composite PK**): Validator public key who signed/justified

> Note: this table has **no `id`, `block_number`, or `role` columns** — only the two composite-PK columns.

### indexer_state
Stores indexer sync metadata. Populated on migration with initial rows: `last_indexed_block='0'`, `indexer_version='1.0.0'`, `schema_version='000'`.

**Fields:**
- `key` (varchar(50), **PRIMARY KEY**): State key
- `value` (text, NOT NULL): State value
- `updated_at` (timestamp, NOT NULL, default NOW()): Last update

### epoch_transitions
Stores epoch boundary information (⚠️ Not populated by the current indexer).

**Fields:**
- `id` (bigserial, **PRIMARY KEY**): Unique record ID
- `epoch_number` (bigint, **UNIQUE**, NOT NULL): Epoch number (NOT integer — bigint)
- `start_block` (bigint, NOT NULL): First block of epoch
- `end_block` (bigint, NOT NULL): Last block of epoch
- `active_validators` (integer, NOT NULL): Number of active validators
- `quarantine_length` (integer, NOT NULL): Quarantine period length
- `timestamp` (timestamp, NOT NULL, default `CURRENT_TIMESTAMP`): When recorded (NOT `created_at` — the column is `timestamp`)

## Views

### transaction_history_view
Combined wallet transaction history: `deployments LEFT JOIN transfers` — one row per transfer, or one row per deployment that produced no transfer. Tracked in Hasura with public SELECT (`limit: 5000`, `allow_aggregations: true`).

**Fields:**
- `transfer_id` (bigint, nullable): `transfers.id` — NULL when `type = 'not_transfer'`
- `deploy_id` (varchar, NOT NULL): Deployment signature
- `block_hash` (varchar, NOT NULL): Containing block hash
- `block_number` (bigint, NOT NULL): Containing block number
- `timestamp` (bigint, NOT NULL): Deployment timestamp (epoch ms)
- `type` (text, NOT NULL): `'transfer'` | `'not_transfer'`
- `deployer_address` (varchar, NOT NULL): ASI address derived from deployer public key
- `from_address` (varchar, nullable): Sender ASI address — NULL when `type = 'not_transfer'`
- `to_address` (varchar, nullable): Recipient ASI address — NULL when `type = 'not_transfer'`
- `from_public_key` (varchar, nullable): Sender public key — NULL when `type = 'not_transfer'`
- `amount_asi` (numeric(20,8), nullable): Transfer amount in ASI — NULL when `type = 'not_transfer'`
- `status` (varchar, nullable): Transfer status (`'success'` / `'failed'` / ...) — NULL when `type = 'not_transfer'`

**Aggregations:** `transaction_history_view_aggregate { aggregate { count } }` is allowed for public role (e.g. for pagination totals).

### block_ancestors_view
Read-only view that types the return of `get_block_ancestors`.

**Fields:**
- `ancestor_hash` (varchar(64)): Ancestor block hash
- `ancestor_number` (bigint): Ancestor block number
- `depth` (integer): Distance from the queried block

### block_descendants_view
Read-only view that types the return of `get_block_descendants`.

**Fields:**
- `descendant_hash` (varchar(64)): Descendant block hash
- `descendant_number` (bigint): Descendant block number
- `depth` (integer): Distance from the queried block

### network_stats_view
Analytics view over recent blocks (last 100 non-genesis blocks): `total_blocks`, `avg_block_time_seconds`, `earliest_block_time`, `latest_block_time`. Hasura-tracked.

### network_metrics_view
Schema-holder view (composite type) for Hasura that types the return of `get_network_metrics`. Columns: `bucket_start` (timestamptz), `bucket_end` (timestamptz), `avg_block_time_seconds` (numeric), `avg_tps` (numeric), `deployments_count` (bigint), `transfers_count` (bigint). The view itself returns 0 rows (`LIMIT 0`) — use `get_network_metrics()` instead.

## SQL Functions

### get_block_ancestors
`get_block_ancestors(p_block_hash varchar)` → `SETOF block_ancestors_view`. Recursively returns all ancestor blocks of the given block by walking the `block_parents` junction (DAG traversal, `UNION`-deduplicated, no cycle guard on the recursive CTE).

### get_block_descendants
`get_block_descendants(p_block_hash varchar)` → `SETOF block_descendants_view`. Recursively returns all descendant blocks of the given block by walking `block_parents` (symmetric to `get_block_ancestors`).

### get_network_metrics
`get_network_metrics(p_range_hours integer DEFAULT 24, p_divisions integer DEFAULT 7)` → `SETOF network_metrics_view`. **Hybrid**: if `network_metrics_buckets` has data, reads pre-aggregated buckets (fast path); otherwise computes from raw `blocks`/`deployments`/`transfers` (slow fallback). Returns time-bucketed performance metrics.

### refresh_network_metrics_buckets
`refresh_network_metrics_buckets(p_lookback_hours integer DEFAULT 720, p_bucket_seconds integer DEFAULT 600)` → `void`. Cron-friendly incremental refresh of `network_metrics_buckets` (default: 30 days lookback, 10-minute buckets). Upserts new/updated buckets; skips already-aggregated ranges.

These functions are `LANGUAGE sql STABLE` / `LANGUAGE plpgsql` and are tracked in Hasura as callable GraphQL fields — see "Hasura function permissions" for public role access on `get_block_ancestors` / `get_block_descendants` / `get_network_metrics`.

## Triggers (internal, not exposed via GraphQL)

- `update_deployment_count` (AFTER INSERT/DELETE on `deployments`): keeps `blocks.deployment_count` in sync.
- `new_block_notify` (AFTER INSERT on `blocks`): `pg_notify('new_block', ...)` for real-time subscriptions.
- `new_transfer_notify` (AFTER INSERT on `transfers`): `pg_notify('new_transfer', ...)` for real-time subscriptions.

## GraphQL Query Examples

### Get Latest Blocks
```graphql
query GetLatestBlocks {
  blocks(
    limit: 10
    order_by: { block_number: desc }
  ) {
    block_number
    block_hash
    timestamp
    proposer
    deployment_count
  }
}
```

### Get Deployments for a Block
```graphql
query GetBlockDeployments($blockNumber: bigint!) {
  deployments(
    where: { block_number: { _eq: $blockNumber } }
  ) {
    deploy_id
    deployer
    deployment_type
    phlo_cost
    errored
    error_message
  }
}
```

### Get ASI Transfers
```graphql
query GetTransfers {
  transfers(
    limit: 50
    order_by: { created_at: desc }
  ) {
    from_address
    to_address
    amount_asi
    status
    block_number
  }
}
```

### Get Validators
```graphql
query GetValidators {
  validators(
    order_by: { total_stake: desc }
  ) {
    public_key
    name
    status
    total_stake
    first_seen_block
    last_seen_block
  }
}
```

### Get Address Transfers
```graphql
query GetAddressTransfers($address: String!) {
  transfers(
    where: {
      _or: [
        { from_address: { _eq: $address } }
        { to_address: { _eq: $address } }
      ]
    }
  ) {
    from_address
    to_address
    amount_asi
    block_number
  }
}
```

### Aggregate Queries
```graphql
query GetStats {
  blocks_aggregate {
    aggregate {
      count
      max {
        block_number
      }
    }
  }
  
  deployments_aggregate {
    aggregate {
      count
      avg {
        phlo_cost
      }
    }
  }
  
  transfers_aggregate {
    aggregate {
      count
      sum {
        amount_asi
      }
    }
  }
}
```

## Known Limitations

1. **Epoch Data Not Populated**: The `epoch_transitions` table exists but the current indexer never writes rows to it. Epoch rewards and validator reward distribution are not tracked.

2. **`deployments.status` is always `"included"`**: the node's block-stream `DeployInfo` proto has no `status` field, so the gRPC client hardcodes `"included"` and the indexer stores that. Real deploy lifecycle states (`pending` / `finalized` / `failed` / `expired`) would require calling the separate `deployFinalizationStatus` RPC, which the indexer does not currently do. Use `deployments.errored` + `deployments.error_message` to detect failed deploys.

3. **Network metrics need manual refresh**: `get_network_metrics` reads from `network_metrics_buckets`, which is populated by `refresh_network_metrics_buckets()`. That function must be called on a cron schedule (e.g. every 10 min) — otherwise `get_network_metrics` falls back to the slow raw-aggregation path.

4. **Balance from transfers only**: the `Wallet Balance Query` aggregates `transfers` only — genesis allocations ARE included (as `genesis_mint`-status transfers), but validator rewards and gas-fee burns are NOT tracked. For canonical on-chain balances, query `balance_states` instead.

## Accessing the GraphQL API

### Endpoint
```
http://localhost:8080/v1/graphql
```

### Authentication
Include the Hasura admin secret in headers:
```
x-hasura-admin-secret: myadminsecretkey
```

### Example cURL Request
```bash
curl http://localhost:8080/v1/graphql \
  -X POST \
  -H "Content-Type: application/json" \
  -H "x-hasura-admin-secret: myadminsecretkey" \
  -d '{"query":"{ blocks(limit: 5) { block_number block_hash } }"}'
```

### GraphQL Playground
Access the interactive GraphQL playground at:
```
http://localhost:8080/console
```

## WebSocket Subscriptions

Hasura supports real-time subscriptions, but they require WebSocket connections:

```graphql
subscription WatchNewBlocks {
  blocks(
    limit: 1
    order_by: { block_number: desc }
  ) {
    block_number
    block_hash
    timestamp
  }
}
```

Note: WebSocket subscriptions work through the GraphQL playground but require a WebSocket client for programmatic access.