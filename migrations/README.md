# ASI Chain Indexer Database Migrations

## Current schema

Apply these migrations in order to a new database:

1. `000_comprehensive_initial_schema.sql`: original tables, views and functions.
2. `001_bigint_amounts.sql`: exact token amounts and per-record decimal precision.
3. `002_executed_transfers.sql`: stable native transfer event identity.
4. `003_native_transfer_calls.sql`: durable pending calls, completion records, and lifecycle queries.

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/000_comprehensive_initial_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_bigint_amounts.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_executed_transfers.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/003_native_transfer_calls.sql
```

For an existing version 000 database, back up the database, stop indexer writes,
and apply **001, 002, and 003, once each**. The migration takes table locks while widening
columns and rebuilding generated balance totals and `transaction_history_view`.
Reload Hasura metadata after migration and before deploying the matching explorer.
Compose runs these migrations only when initializing an empty PostgreSQL volume;
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

## Repair source-inferred transfer history

Stop indexer writes. For a version 001 database apply 002 and 003. For a version
002 database apply only 003. Then run
`python -m src.reindex_transfers` with the same database/node settings as the
indexer. It replaces inferred rows with successful native movements, rebuilds
genesis allocations/balances, and preserves each block's recorded precision.
It does not reset the node, blocks, deploys or sync cursor. Each block commits
atomically; an unavailable or incompatible report aborts the command, which can safely be rerun.
Restart the indexer and reload Hasura metadata after successful completion.
Do not consider legacy `event_index IS NULL` transfers execution-verified.

Transfer indexing now requires an observer with `/reporting/trace`, retained
replay state and explicit PRECHARGE/USER/REFUND phase markers (tested against
f1r3node-rust 98eb647e6cfd864e4765f22e1fe97d84cac713fe). The native SystemVault
channel is derived from that standard genesis contract; a custom SystemVault
requires a reviewed adapter. Full traces preserve `GInt` and `GBigInt` values;
compact node transfer summaries remain int64 and are not used. Missing/busy or
incompatible traces fail the block for retry, never fall back to source regexes.
Only successful native user movements enter `transfers`; rejected attempts are
not ledger movements, and deploy execution status alone does not imply success.
Repeated identical successful calls retain separate event indexes. A failed
user phase has no committed user movements. Precharge/refund responses can still
complete previously committed user calls. This is not a generic custom-token index.

Genesis allocation events retain every standard generator entry. Balance
snapshots contain one row per address: repeated addresses replace the previous
vault-map entry, rather than summing inaccessible overwritten vaults.

Repeated genesis addresses with different amounts and ambiguous concurrent transfer result channels fail closed and require operator investigation; the repair never guesses which amount moved.

## Pending native transfers

A complete trace can contain a native call without a response. The indexer records
that invocation and advances past its block. The call remains pending until a
committed response arrives in the same or a later deploy. There is no timeout
that silently discards pending calls. Matching uses the recorded return channel
and block ancestry; an unrelated DAG sibling cannot complete the call.
Identical invocations of the same signed deploy on merged branches are matched
once, while each block occurrence remains recorded. Duplicate occurrences link
to the same result with status `coalesced`. A state-dependent deploy can produce
different unresolved calls on separate branches. If their merged reply cannot
be attributed unambiguously, indexing stops for investigation; these traces alone
do not identify which branch's effects survived the merge.
Indexing from a later height can only resolve calls whose initiating block is
included in the indexed history. Missing parents inside that history are retried;
the configured starting boundary and older parents are exempt. For an older
parent not already known at the boundary, the indexer verifies its height with
the node before accepting it as outside the indexed range.

`native_transfer_calls` and `native_transfer_results` retain internal observations.
Their identifiers include the block hash, deploy signature, and event position.
Replay is idempotent, including when the original deploy is repaired after its
completion. Rejected calls, including invalid destinations, do not enter the
unsigned transfer ledger. Only a successful response produces a `transfers` row.
Its deploy, block, and timestamp identify completion, while its `native_result_id`
links back to the initiating call. Amount precision comes from the initiating block.

Reload metadata with `scripts/configure-hasura.py` to expose the read-only
`native_transfer_lifecycle` view. It provides status (`pending`, `completed`,
`rejected`, or `coalesced`), exact requested amount as text, initiation/completion block and deploy
links, timestamps, and the successful transfer ID. Internal return channels are
not exposed through this view. Existing transfer queries and aggregates continue
to contain successful movements only. Explorer UI changes are separate work.
Lifecycle rows describe each observed `(call_id, result_id)` branch outcome, with
a null result for a pending call. Forks can produce multiple outcomes for one
call. `result_success` also exposes the outcome of a coalesced occurrence. Use
the existing transfer ledger for movement totals, not counts of lifecycle rows.
Coalesced rows retain that occurrence's requested amount; the linked transfer
contains the executed amount, which can differ for a state-dependent deploy.

Migration 003 marks populated databases as requiring historical repair. Startup
waits for `python -m src.reindex_transfers` to complete so old pending calls cannot
be silently missed. Repair keeps the sync cursor and stored block precision, and
clears this requirement only after every indexed block succeeds. Stop indexer
writes throughout repair; restart after repair and metadata reload complete.
If repair reports a missing parent inside the indexed range, restore/backfill
that missing block before rerunning; do not clear the repair flag manually.
