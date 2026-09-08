# ASI-Chain GraphQL API Guide

**Version**: 2.3.0 (pending_deploys) | **Updated**: September 2026

This guide provides comprehensive documentation for accessing ASI-Chain blockchain data through the Hasura GraphQL endpoint with automatic relationship configuration.

## Table of Contents
- [Overview](#overview)
- [Access Details](#access-details)
- [Available Tables & Schema](#available-tables--schema)
- [Relationships](#relationships)
- [Query Examples](#query-examples)
- [Advanced Features](#advanced-features)
- [Real-time Subscriptions](#real-time-subscriptions)
- [Performance Tips](#performance-tips)

## Overview

The ASI-Chain indexer provides a powerful GraphQL API powered by Hasura, offering:
- **Zero-Touch Setup**: Automatic relationship configuration during deployment
- **Single Query Access**: Fetch related data across multiple tables in one request
- **Real-time Subscriptions**: Live updates as new blocks are indexed
- **Flexible Filtering**: Complex where clauses, sorting, and pagination
- **Aggregate Functions**: Count, sum, avg, max, min operations
- **JSONB Support**: Query into complex JSON fields like bonds_map and justifications
- **Enhanced Transfer Detection**: Supports both variable-based and match-based Rholang patterns
- **Data Quality**: Proper NULL handling for deployment error messages
- **Validator Bond Detection**: Full support for new CLI output format
- **Pending Deploys**: Live snapshot of the node's deploy buffers (deploys not yet in a block), refreshed every sync cycle

## Access Details

### Endpoints
- **GraphQL Endpoint**: `http://localhost:8080/v1/graphql`
- **GraphQL Console**: `http://localhost:8080/console`
- **Admin Secret**: `myadminsecretkey`

### Authentication
Include the admin secret in your requests:
```bash
curl -X POST http://localhost:8080/v1/graphql \
  -H "Content-Type: application/json" \
  -H "x-hasura-admin-secret: myadminsecretkey" \
  -d '{"query": "{ blocks(limit: 1) { block_number } }"}'
```

## Available Tables & Schema

### 1. **blocks**
Core blockchain blocks. **Primary key: `block_hash`** (DAG-aware — `block_number` is NOT unique, forks can reuse a height). Parents live in the `block_parents` junction table (no `parent_hash` column here).

| Field | Type | Description |
|-------|------|-------------|
| block_hash | String (PK) | Block hash (varchar(64)) |
| block_number | BigInt | Block height (NOT unique — DAG may have multiple blocks at the same height) |
| timestamp | BigInt | Block creation time (epoch ms) |
| proposer | String | Validator public key who proposed the block |
| state_hash | String | State hash |
| state_root_hash | String | Post-execution state root hash |
| pre_state_hash | String | Pre-execution state hash |
| seq_num | Int | Sequence number |
| sig | String | Block signature (varchar(200)) |
| sig_algorithm | String | Signature algorithm |
| shard_id | String | Shard identifier |
| extra_bytes | String | Extra block data |
| version | Int | Block version |
| deployment_count | Int | Number of deployments in block |
| finalization_status | String | `'finalized'` or `'unfinalized'` (real status from node, NOT NULL) |
| bonds_map | JSONB | Array of validator bonds |
| justifications | JSONB | Array of validator justifications |
| fault_tolerance | Numeric | Network fault tolerance (0-1) |
| created_at | Timestamp | When indexed |

> **DAG parents**: use the `parent_links` array relationship to fetch a block's parents (no `parent_hash` column on `blocks` directly).

### 2. **deployments**
Smart contract deployments and transactions.

| Field | Type | Description |
|-------|------|-------------|
| deploy_id | String (PK) | Unique deployment signature (varchar(200)) |
| block_hash | String | Block hash containing the deployment (FK → blocks) |
| block_number | BigInt | Block height (denormalised, no FK) |
| deployer | String | Deployer public key (varchar(200)) |
| deployer_address | String | ASI address derived from deployer public key (NOT NULL) |
| term | Text | Rholang source code |
| timestamp | BigInt | Deployment creation time (epoch ms) |
| sig | String | Deploy signature (varchar(200)) |
| sig_algorithm | String | Signature algorithm (default `secp256k1`) |
| phlo_price | BigInt | Phlo price (default 1) |
| phlo_limit | BigInt | Phlo limit (default 1000000) |
| phlo_cost | BigInt | Phlo gas cost (default 0) |
| valid_after_block_number | BigInt | Valid after block |
| errored | Boolean | Whether deployment failed at execution |
| error_message | String | Rholang error text (NULL when no error) |
| deployment_type | String | Classification (smart_contract, validator_operation, asi_transfer, ...) |
| seq_num | Int | Sequence number |
| shard_id | String | Shard ID |
| status | String | Deploy lifecycle status — currently always `"included"` (no status field in node's block-stream) |
| created_at | Timestamp | When indexed |

### 3. **pending_deploys**
Ephemeral snapshot of the node's deploy buffers — deploys signed and accepted but **not yet included in any block**. Fully refreshed (DELETE + INSERT) every sync cycle; no `block_hash`/FK by design. `sig` equals `deployments.deploy_id` once the deploy is included.

| Field | Type | Description |
|-------|------|-------------|
| sig | String (PK) | Hex deploy signature (varchar(160)) — matches `deployments.deploy_id` when included |
| deployer | String | Deployer public key (hex, varchar(200)) |
| deployer_address | String | ASI address derived from deployer public key (NOT NULL) |
| term | Text | Rholang source code |
| timestamp | BigInt | Deploy creation time (epoch ms) |
| phlo_price | BigInt | Phlo price (default 1) |
| phlo_limit | BigInt | Phlo limit (default 1000000) |
| valid_after_block_number | BigInt | Deploy not valid before this height |
| shard_id | String | Shard ID |
| sig_algorithm | String | Signature algorithm (default `secp256k1`) |
| language | String | Source language (`rholang` / `metta`) |
| expiration_timestamp | BigInt | Expiry (NULL = no expiration) |
| is_rejected | Boolean | `false` = fresh in deploy_storage; `true` = recovering after a merge conflict (rejected-recovery buffer) |
| fetched_at | Timestamp | When this snapshot row was written |

Hasura permissions: public SELECT, `limit: 5000`, `allow_aggregations: true`.

> The node caps the response at 1000 entries — the pre-cap total is in `indexer_state` key `pending_deploys_total_available`. Compare with `pending_deploys_aggregate { aggregate { count } }` to detect truncation.

### 4. **transfers**
ASI token transfers extracted from deployments.

| Field | Type | Description                                             |
|-------|------|---------------------------------------------------------|
| id | BigInt | Auto-increment transfer ID (bigserial PK) |
| deploy_id | String | Associated deployment (FK → deployments) |
| block_hash | String | Block hash containing the transfer (FK → blocks) |
| block_number | BigInt | Block height (denormalised, no FK) |
| from_address | String | Sender ASI address (varchar(150)) |
| from_public_key | String | Sender public key (nullable — NULL when sender is a pure ASI address) |
| to_address | String | Recipient ASI address (varchar(150)) |
| amount_dust | BigInt | Amount in dust (smallest unit) |
| amount_asi | Numeric | ASI amount (8 decimals — numeric(20,8), NOT bigint) |
| status | String | Transfer status (`success` / `failed` / `genesis_mint` / `genesis_bond`) |
| timestamp | BigInt | Transfer timestamp (epoch ms) |
| created_at | Timestamp | When indexed |

### 5. **validator_bonds**
Historical validator stake records per block (including genesis bonds).

| Field | Type | Description |
|-------|------|-------------|
| id | BigInt | Unique record ID (bigserial PK) |
| block_hash | String | Block hash (FK → blocks) |
| block_number | BigInt | Block height (denormalised, no FK) |
| validator_public_key | String | Full validator public key (up to 200 chars) |
| stake | BigInt | Staked amount in dust |
| | | UNIQUE (block_hash, validator_public_key) |

### 6. **network_stats**
Network health metrics over time.

| Field | Type | Description |
|-------|------|-------------|
| id | BigInt | Unique record ID (bigserial PK) |
| block_number | BigInt | Block height |
| total_validators | Int | Total validator count |
| active_validators | Int | Active validator count |
| validators_in_quarantine | Int | Quarantined validators (default 0) |
| consensus_participation | Numeric | Participation rate (%) — numeric(5,2) |
| consensus_status | String | Network health status (NOT NULL) |
| timestamp | Timestamp | When captured (SQL timestamp, NOT bigint) |

### 7. **balance_states**
Address balance tracking with bonded/unbonded separation.

| Field | Type | Description |
|-------|------|-------------|
| id | BigInt | Unique record ID (bigserial PK) |
| address | String | ASI address or validator public key (up to 150 chars) |
| block_hash | String | Block hash (FK → blocks) |
| block_number | BigInt | Block height (denormalised, no FK) |
| unbonded_balance_dust | BigInt | Unbonded balance in dust |
| unbonded_balance_asi | Numeric | Unbonded balance in ASI (numeric(20,8)) |
| bonded_balance_dust | BigInt | Bonded/staked balance in dust |
| bonded_balance_asi | Numeric | Bonded balance in ASI (numeric(20,8)) |
| total_balance_dust | BigInt | Computed total in dust (GENERATED column) |
| total_balance_asi | Numeric | Computed total in ASI (GENERATED column) |
| updated_at | Timestamp | Last update time |
| | | UNIQUE (address, block_hash) |

### 8. **epoch_transitions**
Epoch boundary tracking (⚠️ not populated by current indexer).

| Field | Type | Description |
|-------|------|-------------|
| id | BigInt | Unique record ID (bigserial PK) |
| epoch_number | BigInt | Epoch number (UNIQUE, NOT integer — bigint) |
| start_block | BigInt | First block of epoch |
| end_block | BigInt | Last block of epoch |
| active_validators | Int | Number of active validators |
| quarantine_length | Int | Quarantine period length (NOT NULL) |
| timestamp | Timestamp | When recorded (SQL `timestamp`, NOT `created_at`) |

### 9. **block_validators**
Block-validator relationships for justifications. **Composite PK** — no `id` / `block_number` / `role` columns.

| Field | Type | Description |
|-------|------|-------------|
| block_hash | String | Block hash (composite PK, FK → blocks) |
| validator_public_key | String | Validator who signed/justified (composite PK) |

### 10. **validators**
Validator registry.

| Field | Type | Description |
|-------|------|-------------|
| public_key | String | Validator public key (PK, varchar(200)) |
| name | String | Validator name (nullable, up to 160 chars) |
| total_stake | BigInt | Total staked amount in dust (default 0) |
| status | String | `active` / `bonded` / `quarantine` / `inactive` (default `bonded`) |
| first_seen_block | BigInt | First appearance |
| last_seen_block | BigInt | Last activity |
| created_at | Timestamp | When first indexed |
| updated_at | Timestamp | Last update |

### 11. **indexer_state**
Indexer metadata and sync status. Populated on migration with `last_indexed_block='0'`, `indexer_version='1.0.0'`, `schema_version='000'`.

| Field | Type | Description |
|-------|------|-------------|
| key | String | State key (PK, varchar(50)) |
| value | Text | State value (NOT NULL) |
| updated_at | Timestamp | Last update |

### 12. **block_parents**
DAG junction table tracking all parents of a block (a block may have multiple parents).

| Field | Type | Description |
|-------|------|-------------|
| block_hash | String | Child block hash (composite PK, FK → blocks ON DELETE CASCADE) |
| parent_hash | String | Parent block hash (composite PK, no FK — parent may not be indexed yet) |
| parent_index | Int | Order of this parent among the block's parents (default 0) |
| created_at | Timestamp | When the link was indexed |

### 13. **transaction_history_view**
Combined wallet transaction history (deployments ⨝ transfers via LEFT JOIN). One row per transfer, or one row per deployment that produced no transfer.

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| transfer_id | Int | ✓ | `transfers.id` (NULL when `type = 'not_transfer'`) |
| deploy_id | String | ✗ | Deployment signature (PK) |
| block_hash | String | ✗ | Containing block hash |
| block_number | BigInt | ✗ | Containing block number |
| timestamp | BigInt | ✗ | Deployment timestamp (epoch ms) |
| type | String | ✗ | `'transfer'` or `'not_transfer'` |
| deployer_address | String | ✗ | ASI address derived from deployer public key |
| from_address | String | ✓ | Sender ASI address (NULL when `type = 'not_transfer'`) |
| to_address | String | ✓ | Recipient ASI address (NULL when `type = 'not_transfer'`) |
| from_public_key | String | ✓ | Sender public key (NULL when `type = 'not_transfer'`) |
| amount_asi | numeric(20,8) | ✓ | Transfer amount in ASI (NULL when `type = 'not_transfer'`) |
| status | String | ✓ | Transfer status (`'success'` / `'failed'` / ...) — NULL when `type = 'not_transfer'` |

Hasura permissions: public SELECT, `limit: 5000`, `allow_aggregations: true`.

### 14. **block_ancestors_view / block_descendants_view**
Read-only views that type the returns of `get_block_ancestors` / `get_block_descendants` SQL functions.

| Field | Type | Description |
|-------|------|-------------|
| ancestor_hash / descendant_hash | String | Reachable block hash |
| ancestor_number / descendant_number | BigInt | Reachable block number |
| depth | Int | Distance from the queried block |

### 15. **network_stats_view**
Analytics view over recent blocks (last 100 non-genesis blocks): `total_blocks`, `avg_block_time_seconds`, `earliest_block_time`, `latest_block_time`. Hasura-tracked.

### 16. **network_metrics_view** + **network_metrics_buckets** (table)
`network_metrics_view` is a schema-holder view (0 rows) for Hasura that types the return of `get_network_metrics`.
`network_metrics_buckets` is the underlying pre-aggregated table (columns: `bucket_start` timestamptz PK, `bucket_end` timestamptz, `avg_block_time_sec` numeric, `deployments_count` bigint, `transfers_count` bigint), populated by `refresh_network_metrics_buckets()`.

## SQL Functions (callable via GraphQL)

- **`get_block_ancestors(p_block_hash)`** → `SETOF block_ancestors_view` — recursive DAG traversal of `block_parents`, `UNION`-deduplicated.
- **`get_block_descendants(p_block_hash)`** → `SETOF block_descendants_view` — symmetric to `get_block_ancestors`.
- **`get_network_metrics(p_range_hours=24, p_divisions=7)`** → `SETOF network_metrics_view` — hybrid: reads `network_metrics_buckets` when available (fast), falls back to raw aggregation (slow) otherwise.
- **`refresh_network_metrics_buckets(p_lookback_hours=720, p_bucket_seconds=600)`** → `void` — cron-friendly incremental upsert of buckets (default: 30 days lookback, 10-min buckets).

## Relationships

The following relationships are configured for nested queries:

### One-to-Many (Array Relationships)
- `blocks` → `deployments`: All deployments in a block (by `block_hash`)
- `blocks` → `transfers`: All transfers in a block (by `block_hash`)
- `blocks` → `validator_bonds`: Validator stakes at block (by `block_hash`)
- `blocks` → `block_validators`: Validators who justified block (by `block_hash`)
- `blocks` → `balance_states`: Balance snapshots at block (by `block_hash`)
- `blocks` → `parent_links` (`block_parents`): This block's own parent links
- `blocks` → `child_links` (`block_parents`, manual): Links where this block is the parent (i.e. its children)
- `deployments` → `transfers`: ASI transfers from deployment (by `deploy_id`)

### Many-to-One (Object Relationships)
- `deployments` → `block`: Parent block details
- `transfers` → `deployment`: Source deployment
- `transfers` → `block`: Block containing transfer
- `validator_bonds` → `block`: Block reference
- `balance_states` → `block`: Block reference
- `pending_deploys` → `deployer_validator` (manual, → validators): Validator record for the deployer public key (nullable — deployer may not be a validator)
- `pending_deploys` → `included_deployment` (manual, → deployments): The confirmed deployment once the pending deploy is included in a block (null while still pending; joins on `sig = deploy_id`)

### DAG Relationships (block_parents)
- `blocks` → `parent_links` (array): This block's own parent links in `block_parents`
- `blocks` → `child_links` (array, manual): Links where this block is the parent (i.e. its children)
- `block_parents` → `child_block` (object): The child block of a parent link
- `block_parents` → `parent_block` (object, manual): The parent block of a parent link

## Query Examples

### Basic Queries

#### Get Latest Blocks
```graphql
query GetLatestBlocks {
  blocks(limit: 10, order_by: {block_number: desc}) {
    block_number
    block_hash
    timestamp
    proposer
    deployment_count
  }
}
```

#### Search Deployments by Deployer
```graphql
query SearchDeployments($deployer: String!) {
  deployments(where: {deployer: {_eq: $deployer}}) {
    deploy_id
    deployment_type
    timestamp
    errored
    error_message
  }
}
```

#### Get Pending Deploys (node buffers)
```graphql
query GetPendingDeploys {
  pending_deploys(order_by: {timestamp: desc}, limit: 50) {
    sig
    deployer
    deployer_address
    timestamp
    phlo_limit
    is_rejected
    included_deployment {
      deploy_id
      block_number
    }
  }
  pending_deploys_aggregate {
    aggregate {
      count
    }
  }
  indexer_state(where: {key: {_eq: "pending_deploys_total_available"}}) {
    value
  }
}
```

> `included_deployment` is null while the deploy is still pending — it resolves to the `deployments` row (joined on `sig = deploy_id`) once the deploy is included in a block. Use `pending_deploys_aggregate` for count badges and the `indexer_state` value to detect truncation (node caps the response at 1000 entries).

### Nested Queries

#### Blocks with All Related Data
```graphql
query BlocksWithDetails {
  blocks(limit: 5, order_by: {block_number: desc}) {
    block_number
    block_hash
    state_root_hash
    fault_tolerance
    finalization_status

    # DAG parents
    parent_links {
      parent_hash
      parent_index
    }

    # Nested deployments
    deployments {
      deploy_id
      deployment_type
      errored
      error_message

      # Nested transfers
      transfers {
        from_address
        to_address
        amount_asi
      }
    }

    # Validator bonds
    validator_bonds {
      validator_public_key
      stake
    }
  }
}
```

#### ASI Transfer Analysis (Enhanced in v2.1)
```graphql
query TransferAnalysis {
  transfers(order_by: {amount_asi: desc}, limit: 10) {
    from_address  # Now supports 53-56 char addresses
    to_address    # Both ASI addresses and validator keys
    amount_asi
    
    # Parent deployment details
    deployment {
      deploy_id
      deployer
      
      # Parent block details
      block {
        block_number
        timestamp
      }
    }
  }
}
```

#### All Transfers Including Genesis
```graphql
query AllTransfers {
  transfers(order_by: {block_number: asc}) {
    block_number
    from_address
    to_address
    amount_asi
    status
  }
  transfers_aggregate {
    aggregate {
      count
      sum { amount_asi }
    }
  }
}
```

### Aggregate Queries

#### Network Statistics
```graphql
query NetworkStats {
  blocks_aggregate {
    aggregate {
      count
      avg {
        deployment_count
      }
    }
  }
  
  deployments_aggregate(where: {errored: {_eq: true}}) {
    aggregate {
      count
    }
  }
  
  transfers_aggregate {
    aggregate {
      count
      sum {
        amount_asi
      }
      avg {
        amount_asi
      }
      max {
        amount_asi
      }
    }
  }
}
```

#### Validator Performance
```graphql
query ValidatorStats {
  validator_bonds_aggregate(
    distinct_on: validator_public_key
  ) {
    aggregate {
      count
    }
    nodes {
      validator_public_key
      stake
    }
  }
}
```

### JSONB Queries

#### Query Bonds Map
```graphql
query BlockBonds {
  blocks(where: {block_number: {_eq: 100}}) {
    block_number
    bonds_map
    justifications
  }
}
```

#### Filter by JSONB Content
```graphql
query HighStakeValidators {
  blocks(
    where: {
      bonds_map: {
        _contains: [{stake: 50000000000000}]
      }
    },
    limit: 5
  ) {
    block_number
    bonds_map
  }
}
```

### Wallet Balance Query

#### Check Any Wallet Balance
```graphql
query GetWalletBalance($address: String!) {
  # Incoming transfers
  incoming: transfers_aggregate(
    where: {to_address: {_eq: $address}}
  ) {
    aggregate {
      sum { amount_asi }
      count
    }
  }
  
  # Outgoing transfers
  outgoing: transfers_aggregate(
    where: {from_address: {_eq: $address}}
  ) {
    aggregate {
      sum { amount_asi }
      count
    }
  }
  
  # Transaction history
  transactions: transfers(
    where: {
      _or: [
        {from_address: {_eq: $address}},
        {to_address: {_eq: $address}}
      ]
    },
    order_by: {block_number: desc}
  ) {
    from_address
    to_address
    amount_asi
    block_number
    status
    deployment {
      timestamp
      block {
        timestamp
      }
    }
  }
}
```

**Example Usage:**
```bash
curl -X POST http://localhost:8080/v1/graphql \
  -H "Content-Type: application/json" \
  -H "x-hasura-admin-secret: myadminsecretkey" \
  -d '{
    "query": "query { incoming: transfers_aggregate(where: {to_address: {_eq: \"111129p33f7vaRrpLqK8Nr35Y2aacAjrR5pd6PCzqcdrMuPHzymczH\"}}) { aggregate { sum { amount_asi } count } } outgoing: transfers_aggregate(where: {from_address: {_eq: \"111129p33f7vaRrpLqK8Nr35Y2aacAjrR5pd6PCzqcdrMuPHzymczH\"}}) { aggregate { sum { amount_asi } count } } }"
  }'
```

**Balance Calculation:**
- Balance = Total Received - Total Sent
- The query returns aggregated sums that you can subtract client-side
- Transaction history helps verify the calculation

**Note:** This shows the balance based on indexed transfers only. Consider:
- Genesis allocations aren't included
- Validator rewards aren't tracked as transfers
- Gas fees aren't deducted
- Only successful transfers are counted

### Complex Analysis Query

```graphql
query BlockchainAnalysis($start_block: bigint!, $end_block: bigint!) {
  # Block range analysis
  block_range: blocks(
    where: {
      _and: [
        {block_number: {_gte: $start_block}},
        {block_number: {_lte: $end_block}}
      ]
    }
  ) {
    block_number
    timestamp
    deployment_count
    fault_tolerance
  }

  # Deployment types in range
  deployment_types: deployments_aggregate(
    where: {
      block_number: {_gte: $start_block, _lte: $end_block}
    }
  ) {
    aggregate {
      count
    }
    group_by {
      deployment_type
    }
  }

  # Transfer volume in range
  transfer_volume: transfers_aggregate(
    where: {
      block_number: {_gte: $start_block, _lte: $end_block}
    }
  ) {
    aggregate {
      sum {
        amount_asi
      }
      count
    }
  }
}
```

## Advanced Features

### Pagination
```graphql
query PaginatedBlocks($offset: Int!, $limit: Int!) {
  blocks(
    offset: $offset,
    limit: $limit,
    order_by: {block_number: desc}
  ) {
    block_number
    block_hash
  }
}
```

### Complex Filtering
```graphql
query ComplexFilter {
  deployments(
    where: {
      _and: [
        {deployment_type: {_eq: "smart_contract"}},
        {errored: {_eq: false}},
        {timestamp: {_gte: 1754373000000}}
      ]
    }
  ) {
    deploy_id
    term
  }
}
```

### Distinct Values
```graphql
query UniqueDeployers {
  deployments(
    distinct_on: deployer,
    order_by: {deployer: asc}
  ) {
    deployer
  }
}
```

## Real-time Subscriptions

Hasura supports GraphQL subscriptions for real-time updates:

```graphql
subscription NewBlocks {
  blocks(
    order_by: {block_number: desc},
    limit: 1
  ) {
    block_number
    block_hash
    timestamp
    deployment_count
  }
}
```

```graphql
subscription TransferStream {
  transfers(
    order_by: {created_at: desc},
    limit: 10,
    where: {amount_asi: {_gt: 100}}
  ) {
    from_address
    to_address
    amount_asi
    created_at
  }
}
```

## Performance Tips

1. **Use Specific Fields**: Only request fields you need
   ```graphql
   # Good
   blocks { block_number, block_hash }
   
   # Avoid
   blocks { ... all fields ... }
   ```

2. **Limit Nested Queries**: Deep nesting can be expensive
   ```graphql
   # Set reasonable limits on nested arrays
   blocks(limit: 10) {
     deployments(limit: 5) {
       transfers(limit: 2)
     }
   }
   ```

3. **Use Indexes**: Queries on indexed columns are faster
   - block_number, block_hash, deploy_id are indexed
   - timestamp fields are indexed
   - Foreign key columns are indexed

4. **Aggregate Wisely**: Large aggregations can be slow
   ```graphql
   # Add filters to aggregates
   deployments_aggregate(
     where: {block_number: {_gte: 1000}}
   )
   ```

5. **Pagination**: Use offset/limit for large datasets
   ```graphql
   blocks(offset: 0, limit: 100)
   ```

## GraphQL vs REST API

While the indexer also provides REST endpoints, GraphQL offers advantages:

| Feature | GraphQL | REST API |
|---------|---------|----------|
| Single Request | ✅ Fetch related data in one query | ❌ Multiple endpoints |
| Over/Under-fetching | ✅ Request exact fields | ❌ Fixed responses |
| Real-time | ✅ Subscriptions | ❌ Polling required |
| Type Safety | ✅ Strong typing | ⚠️ Limited |
| Filtering | ✅ Complex where clauses | ⚠️ Basic |
| Relationships | ✅ Nested queries | ❌ Manual joins |

## Practical Examples

### Wallet Balance Checker (JavaScript)
```javascript
async function getWalletBalance(address) {
  const query = `
    query GetBalance($address: String!) {
      incoming: transfers_aggregate(where: {to_address: {_eq: $address}}) {
        aggregate { sum { amount_asi } count }
      }
      outgoing: transfers_aggregate(where: {from_address: {_eq: $address}}) {
        aggregate { sum { amount_asi } count }
      }
      transactions: transfers(
        where: {_or: [{from_address: {_eq: $address}}, {to_address: {_eq: $address}}]},
        order_by: {block_number: desc},
        limit: 10
      ) {
        from_address
        to_address
        amount_asi
        block_number
      }
    }
  `;

  const response = await fetch('http://localhost:8080/v1/graphql', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-hasura-admin-secret': 'myadminsecretkey'
    },
    body: JSON.stringify({ query, variables: { address } })
  });

  const { data } = await response.json();
  
  const received = parseFloat(data.incoming.aggregate.sum?.amount_asi || 0);
  const sent = parseFloat(data.outgoing.aggregate.sum?.amount_asi || 0);
  
  return {
    address,
    balance: received - sent,
    totalReceived: received,
    totalSent: sent,
    transactionCount: data.incoming.aggregate.count + data.outgoing.aggregate.count,
    recentTransactions: data.transactions
  };
}

// Usage
const balance = await getWalletBalance('111129p33f7vaRrpLqK8Nr35Y2aacAjrR5pd6PCzqcdrMuPHzymczH');
console.log(`Balance: ${balance.balance} ASI`);
```

### Top Wallets by Activity
```graphql
query TopWallets {
  # Most active senders
  top_senders: transfers_aggregate(
    group_by: from_address,
    order_by: {count: desc},
    limit: 10
  ) {
    aggregate {
      count
      sum { amount_asi }
    }
    group_by {
      from_address
    }
  }
  
  # Most active receivers
  top_receivers: transfers_aggregate(
    group_by: to_address,
    order_by: {count: desc},
    limit: 10
  ) {
    aggregate {
      count
      sum { amount_asi }
    }
    group_by {
      to_address
    }
  }
}
```

### Wallet Transaction History with Pagination (Recommended — via transaction_history_view)
```graphql
query WalletHistory($address: String!, $offset: Int!, $limit: Int!) {
  transaction_history_view(
    where: {
      _or: [
        { deployer_address: { _eq: $address } }
        { from_address: { _eq: $address } }
        { to_address: { _eq: $address } }
      ]
    }
    order_by: { timestamp: desc }
    offset: $offset
    limit: $limit
  ) {
    transfer_id
    deploy_id
    block_hash
    block_number
    timestamp
    type
    deployer_address
    from_address
    to_address
    from_public_key
    amount_asi
    status
  }

  transaction_history_view_aggregate(
    where: {
      _or: [
        { deployer_address: { _eq: $address } }
        { from_address: { _eq: $address } }
        { to_address: { _eq: $address } }
      ]
    }
  ) {
    aggregate { count }
  }
}
```

#### `status` field — possible values

> **Important**: the `status` column in this view is `transfers.status`, **not** `deployments.status`. The view exposes only transfer-level status, because the `status` / `errored` / `error_message` fields of a deployment are **not** projected into `transaction_history_view` — query the `deployments` table directly if you need deployment status.

Because the view is `deployments LEFT JOIN transfers`, `status` is **NULL when `type = 'not_transfer'`** — meaning that deployment produced no transfer row (so there is no transfer status to show). It does NOT mean the deployment has no status: every deployment always has its own `status` (see `deployments` table below).

Production `status` values written by the indexer (taken from `transfers.status`):

| `status` value | When | Source in indexer |
|---|---|---|
| `"success"` | ASI transfer whose deployment did NOT error | `rust_indexer.py:742, 868` |
| `"failed"` | ASI transfer whose deployment errored (`deploy_data.errored=True`) | `rust_indexer.py:742, 868` |
| `"genesis_mint"` | Genesis wallet allocation (block 0) | `rust_indexer.py:1076` |
| `"genesis_bond"` | Genesis validator bond / staking (block 0) | `rust_indexer.py:1114` |
| `NULL` | `type = 'not_transfer'` — deployment produced no transfer (LEFT JOIN found no `transfers` row) | LEFT JOIN |

> The DDL default `DEFAULT 'success'` is never used in production — the indexer always sets `status` explicitly for transfer rows.
> The test data seeder (`scripts/seed_test_data.py`) also writes `"expired"` and `None` on purpose so frontends can gracefully handle unknown / future statuses.

Per `type`:
- `type = 'transfer'` → `status ∈ {"success", "failed", "genesis_mint", "genesis_bond"}`
- `type = 'not_transfer'` → `status IS NULL` (no transfer exists, not a status bug)

#### `deployments` table — deployment-level status (NOT in the view)

If you need the deployment's own lifecycle status / error info, query `deployments` directly — it is **always populated**, never NULL for a deployment row.

| Field | GraphQL type | Nullable | Source in node (proto) | Description |
|---|---|---|---|---|
| `status` | `String` | ❌ (default `"included"`) | `DeployInfo.status` — currently always `"included"` for in-block deploys (the gRPC client at `grpc_node_client.py:146` sets `"status": "included"`; node proto comment lists `pending/included/error`) | Deploy lifecycle status |
| `errored` | `Boolean` | ❌ (default `false`) | `DeployInfo.errored` (`protos/DeployServiceCommon.proto:144`) | Whether the deploy errored at execution |
| `error_message` | `String` | ✅ | `DeployInfo.systemDeployError` (`protos/DeployServiceCommon.proto:145`) — indexer reads it via `deploy_data.get("systemDeployError")` (`rust_indexer.py:349`) and normalises `""` → `NULL` | Error text, NULL when no error |
| `deployment_type` | `String` | ✅ | Classified by the indexer from the Rholang term (`rust_indexer.py`: `asi_transfer` / `validator_operation` / `smart_contract` / ...) | Classification of the deploy |

Indexer mapping (`rust_indexer.py:374-379`):
```python
errored = deploy_data.get("errored", False) or bool(error_message)
error_message = deploy_data.get("systemDeployError")  # "" → NULL
status = deploy_data.get("status", "included")         # currently always "included"
```

So to render full deploy + transfer status in the UI, the wallet should either:
- query `transaction_history_view` for the transfer-level row, AND
- separately query `deployments` by `deploy_id` for `status` / `errored` / `error_message`, OR
- use a GraphQL nested query: `transaction_history_view { ... deployment { status errored error_message } }` (the `deployment` object relationship exists on `transfers`, but **not** on the view — so use the second option, a direct `deployments` query).

#### Filtering — `where` variants

**One address (sender OR recipient OR deployer):**
```graphql
where: {
  _or: [
    { deployer_address: { _eq: $address } }
    { from_address: { _eq: $address } }
    { to_address: { _eq: $address } }
  ]
}
```

**Outgoing only (sent):**
```graphql
where: { from_address: { _eq: $address } }
```

**Incoming only (received):**
```graphql
where: { to_address: { _eq: $address } }
```

**Deployments only (no transfer produced):**
```graphql
where: {
  deployer_address: { _eq: $address }
  type: { _eq: "not_transfer" }
}
```

**Successful transfers only:**
```graphql
where: {
  type: { _eq: "transfer" }
  status: { _eq: "success" }
}
```

**Failed transfers only:**
```graphql
where: { status: { _eq: "failed" } }
```

**Genesis operations only:**
```graphql
where: {
  _or: [
    { status: { _eq: "genesis_mint" } }
    { status: { _eq: "genesis_bond" } }
  ]
}
```

**By block range:**
```graphql
where: { block_number: { _gte: $from, _lte: $to } }
```

**By time range (epoch ms):**
```graphql
where: { timestamp: { _gte: $fromTs, _lte: $toTs } }
```

**Transfers with amount ≥ threshold:**
```graphql
where: {
  type: { _eq: "transfer" }
  amount_asi: { _gte: $minAmount }
}
```

#### Sorting — `order_by` variants
- `{ timestamp: desc }` — chronological recent-first (recommended)
- `{ block_number: desc }` — by block
- `{ amount_asi: desc }` — largest amounts first
- `{ timestamp: asc }` — chronological oldest-first

#### Aggregations
`allow_aggregations: true` is set for the public role, so `transaction_history_view_aggregate` is available:
```graphql
transaction_history_view_aggregate(where: { ... }) {
  aggregate {
    count
    max { amount_asi timestamp }
    min { amount_asi timestamp }
    sum { amount_asi }
    avg { amount_asi }
  }
}
```
Use `count` for pagination totals (as in the query above); use `sum { amount_asi }` for total volume filtered by address / status / time.

#### TypeScript types (reference)

```typescript
type TransactionType = 'transfer' | 'not_transfer';
type TransferStatus = 'success' | 'failed' | 'genesis_mint' | 'genesis_bond' | null;

interface TransactionHistoryItem {
  transfer_id: number | null;           // transfers.id — NULL when type='not_transfer'
  deploy_id: string;                    // NOT NULL
  block_hash: string;                   // NOT NULL
  block_number: number;                 // bigint → number, NOT NULL
  timestamp: number;                    // bigint → number (epoch ms), NOT NULL
  type: TransactionType;                 // NOT NULL
  deployer_address: string;             // NOT NULL
  from_address: string | null;          // NULL when type='not_transfer'
  to_address: string | null;            // NULL when type='not_transfer'
  from_public_key: string | null;       // NULL when type='not_transfer'
  amount_asi: number | string | null;   // numeric(20,8) — use string for BigInt safety; NULL when type='not_transfer'
  status: TransferStatus;               // transfers.status — NULL only when type='not_transfer'
}

// Deployment-level fields are NOT exposed by the view. Query `deployments` if you need them:
interface DeploymentStatus {
  deploy_id: string;
  status: string;                       // NOT NULL, default "included" (currently always "included")
  errored: boolean;                     // NOT NULL, default false
  error_message: string | null;         // NULL when no error
  deployment_type: string | null;      // classified by indexer from Rholang term
}
```

#### React status labels (reference)

```typescript
const STATUS_LABELS: Record<NonNullable<TransferStatus>, string> = {
  success: 'Completed',
  failed: 'Failed',
  genesis_mint: 'Genesis Mint',
  genesis_bond: 'Genesis Bond',
};

// True when the row is a deployment that produced no transfer (no transfer-level status).
// Note: the deployment itself still has its own `status` / `errored` / `error_message` —
// fetch them from `deployments` separately if the UI needs deploy lifecycle.
const isPending = (tx: TransactionHistoryItem) =>
  tx.type === 'not_transfer' || tx.status === null;
```

Frontends should always handle unknown `status` values gracefully (e.g. render the raw string) so that future indexer additions don't break the UI.

### Wallet Transaction History (Legacy — via transfers)
```graphql
query WalletHistory($address: String!, $offset: Int!, $limit: Int!) {
  transfers(
    where: {
      _or: [
        {from_address: {_eq: $address}},
        {to_address: {_eq: $address}}
      ]
    },
    order_by: {block_number: desc},
    offset: $offset,
    limit: $limit
  ) {
    from_address
    to_address
    amount_asi
    block_number
    status
    deployment {
      timestamp
      deployer
      deployment_type
    }
  }
  
  # Get total count for pagination
  transfers_aggregate(
    where: {
      _or: [
        {from_address: {_eq: $address}},
        {to_address: {_eq: $address}}
      ]
    }
  ) {
    aggregate { count }
  }
}
```

## Client Libraries

### JavaScript/TypeScript
```javascript
import { GraphQLClient } from 'graphql-request';

const client = new GraphQLClient('http://localhost:8080/v1/graphql', {
  headers: {
    'x-hasura-admin-secret': 'myadminsecretkey'
  }
});

const query = `
  query GetBlocks($limit: Int!) {
    blocks(limit: $limit, order_by: {block_number: desc}) {
      block_number
      block_hash
    }
  }
`;

const data = await client.request(query, { limit: 10 });
```

### Python
```python
import requests

url = 'http://localhost:8080/v1/graphql'
headers = {
    'Content-Type': 'application/json',
    'x-hasura-admin-secret': 'myadminsecretkey'
}

query = '''
query GetBlocks($limit: Int!) {
  blocks(limit: $limit, order_by: {block_number: desc}) {
    block_number
    block_hash
  }
}
'''

response = requests.post(url, json={
    'query': query,
    'variables': {'limit': 10}
}, headers=headers)

data = response.json()
```

## Troubleshooting

### Common Issues

1. **Authentication Error**
   ```json
   {"error": "x-hasura-admin-secret required"}
   ```
   Solution: Include the admin secret header

2. **Field Not Found**
   ```json
   {"error": "field 'xyz' not found in type: 'blocks'"}
   ```
   Solution: Check field names in schema

3. **Relationship Not Found**
   ```json
   {"error": "field 'deployments' not found"}
   ```
   Solution: Ensure relationships are configured

4. **Timeout on Large Queries**
   - Add limits to nested queries
   - Use pagination
   - Add where clauses to reduce dataset

## Next Steps

1. **Explore the Console**: Visit http://localhost:8080/console to:
   - Browse the schema
   - Build queries visually
   - Test subscriptions
   - View relationships

2. **Set Up Permissions**: Configure role-based access control

3. **Add Custom Business Logic**: Create Actions and Remote Schemas

4. **Monitor Performance**: Use Hasura's built-in analytics

5. **Deploy to Production**: Consider Hasura Cloud for managed hosting