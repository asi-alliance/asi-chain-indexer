# ASI Chain Indexer Database Migrations

## Current schema

Apply these migrations in order to a new database:

1. `000_comprehensive_initial_schema.sql`: original tables, views and functions.
2. `001_bigint_amounts.sql`: exact token amounts and per-record decimal precision.

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/000_comprehensive_initial_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_bigint_amounts.sql
```

For an existing version 000 database, back up the database, stop indexer writes,
and apply **only 001, once**. The migration takes table locks while widening
columns and rebuilding generated balance totals and `transaction_history_view`.
Reload Hasura metadata after migration and before deploying the matching explorer.
Compose runs both migrations only when initializing an empty PostgreSQL volume;
restarting Compose does not migrate an existing volume.

The migration preserves existing raw units and human amounts, labels historical
rows with `token_decimals=8`, and stringifies historical bond amounts in JSONB.
New ORM rows explicitly record `NATIVE_TOKEN_DECIMALS` (default 8; configure 18 for
an 18-decimal chain). SQL inserts that omit precision retain the legacy default 8.
No balance multiplication or automatic chain denomination change occurs.

Raw monetary columns use `NUMERIC(78,0)` with a uint256 range check. Display columns
support up to 77 fractional digits; the extra width avoids rounding at any supported
precision. Keep `HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES=true`. Protocol counters,
block heights, timestamps and the node's int64 phlo fields retain their bounded
integer types. The node's int64 bond/transfer RPC limits are unchanged.

Historical migrations in `backup_old_migrations/` are reference material, not part
of this sequence.

## Schema Features

### Enhanced Tables
- **Extended validator names**: VARCHAR(160) to accommodate full public keys
- **JSONB fields**: bonds_map, justifications for rich data storage
- **Balance tracking**: Separate bonded/unbonded balance states
- **Pending deploys**: Ephemeral snapshot of the node's deploy buffers (`pending_deploys`), fully refreshed every sync cycle — no block_hash/FK by design, `sig` matches `deployments.deploy_id` once included
- **Network statistics**: Real-time consensus and validator metrics
- **Computed columns**: Auto-calculated total balances

### Performance Optimizations
- Comprehensive indexing strategy
- Partial indexes for pattern matching
- Optimized foreign key relationships
- Real-time notification triggers

### Compatibility
- Fully compatible with Hasura GraphQL auto-generation
- Supports all current indexer functionality
- Includes all enhancements from previous migrations
- Ready for production deployment
