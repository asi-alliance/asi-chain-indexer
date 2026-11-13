# ASI Chain Indexer Database Migrations

## Tracked migration commands

Alembic wraps the unchanged SQL files 000–006 as forward-only revisions. Commands
use the explicitly set DATABASE_URL and the installed runtime requirements; no
psql executable or node connection is needed. They do not run on indexer startup.
Back up the actual database, stop indexer writers and rehearse upgrades first.

```bash
python -m src.migrate status
python -m src.migrate upgrade
# Equivalent convenience targets:
make migration-status PYTHON=python
make migrate PYTHON=python
```

On an empty public schema, upgrade creates 000–006 and alembic_version. Subsequent
runs apply only pending revisions. The whole command, including version updates,
is transactional and holds a per-database advisory lock; a concurrent command is
rejected. Lock waits are bounded to five seconds and each SQL statement defaults
to a 300-second timeout. Set MIGRATION_STATEMENT_TIMEOUT_SECONDS (1–86400) deliberately
for larger rehearsed upgrades.

### Adopt a database initialized by existing SQL/Compose

A pre-existing unversioned schema is never automatically stamped or replayed.
Check the actual applied migration level and choose it explicitly:

```bash
# Example: this database already has SQL migrations 000 and 001.
python -m src.migrate adopt --revision 001
python -m src.migrate upgrade
# Equivalent adoption target: make migration-adopt REVISION=001 PYTHON=python
```

Adoption checks columns/types/defaults, constraints, indexes, views, triggers and
functions against committed profiles of the shipped SQL revisions. Internal FK
triggers are checked on both referencing and referenced tables, including their
enabled mode and deferral settings. Serial sequences are checked for type, start,
increment, bounds, cache, cycling, and ownership by the expected table/column with
the same role owner as the table. Missing, detached, reassigned or extra sequences
owned by managed columns are rejected. Sequence allocation state (`last_value`
and `is_called`) is retained as data and is not compared with a fresh database.
A wrong level, missing object or changed owned column is rejected before writing
a version.
Unrelated objects and additional indexes are retained. Review custom schemas and
permissions separately; this structure check does not validate historical data,
row-level permissions, Hasura metadata or whether history repair was performed.
The profiles were generated and tested on PostgreSQL 18.6; verify other server
versions in a disposable rehearsal before adopting them.

Fresh Compose databases still initialize through the seven SQL mounts. After that
initialization, adopt revision 006 once to begin version tracking. Existing Compose
volumes need adoption of their actual level and an explicit upgrade command, for
example docker compose run --rm --no-deps indexer python -m src.migrate status
(with PostgreSQL already running). Do not run native alembic stamp as a shortcut:
it bypasses the adoption checks.

Once tracked, use the CLI for future upgrades rather than applying SQL manually.
The original SQL files remain available for historical/manual procedures below.
Do not edit shipped migrations or their profiles to make a mismatched database pass.
Add a reviewed forward Alembic revision for a new change (alembic revision -m ...),
including SQL views/functions/triggers and data transitions. ORM autogeneration is
not configured as an authority for this hand-written schema. Review the new revision
and test both an empty database and upgrades with real historical fixtures.

Downgrade is intentionally unsupported: widening exact amounts and changing native
history do not have a safe generic reverse transformation. Restore a reviewed backup
with matching application/Hasura versions instead. SQL execution failure leaves
both schema/data and revision at the prior transaction state.

History repair and Hasura metadata remain explicit steps after a schema upgrade.
Adoption does not clear native_transfer_backfill_required; upgrading a populated
legacy database sets the same repair marker as standalone migration 003.

## Current schema

PostgreSQL engine upgrades are separate from these schema migrations. See
[PostgreSQL 14 to 18.6 and Hasura upgrade](../POSTGRES_UPGRADE.md) before changing
an existing installation's images or volumes.

Apply these migrations in order to a new database:

1. `000_comprehensive_initial_schema.sql`: original tables, views and functions.
2. `001_bigint_amounts.sql`: exact token amounts and per-record decimal precision.
3. `002_executed_transfers.sql`: stable native transfer event identity.
4. `003_native_transfer_calls.sql`: durable pending calls, completion records, and lifecycle queries.
5. `004_uint256_phlo_prices.sql`: widen included/pending fee prices with uint256 checks.
6. `005_network_metrics_empty_range.sql`: return empty network metric buckets for a database without data.
7. `006_native_transfer_lifecycle_plans.sql`: index lifecycle lookups by initiating and completing deploy.

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/000_comprehensive_initial_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_bigint_amounts.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_executed_transfers.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/003_native_transfer_calls.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/004_uint256_phlo_prices.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_network_metrics_empty_range.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/006_native_transfer_lifecycle_plans.sql
```

For an existing version 000 database, back up the database, stop indexer writes,
and apply **001, 002, 003, 004, 005 and 006, once each**. The migration takes table locks while widening
columns and rebuilding generated balance totals and `transaction_history_view`.
Reload Hasura metadata after migration and before deploying the matching explorer.
Compose runs these migrations only when initializing an empty PostgreSQL volume;
restarting Compose does not migrate an existing volume.

The migration preserves existing raw units and human amounts, labels historical
rows with `token_decimals=8`, and stringifies historical bond amounts in JSONB.
New ORM rows explicitly record `NATIVE_TOKEN_DECIMALS` (default 18 for ASI in
Python, Compose and the deploy wrapper; configure 8 for a legacy eight-decimal
chain). SQL inserts that omit precision retain the legacy default 8.
No balance multiplication or automatic chain denomination change occurs.

Raw monetary columns use `NUMERIC(78,0)` with a uint256 range check. Display columns
support up to 77 fractional digits; the extra width avoids rounding at any supported
precision. Keep `HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES=true`. Protocol counters,
block heights, timestamps, phlo limits and gas counts retain their bounded
integer types. Monetary wire types depend on the explicitly selected
[protocol profile](../protos/README.md).

For an installation already at version 003, stop writers and apply only 004.
It preserves prices without rescaling; existing invalid negative prices cause the
transaction to fail rather than being clamped. Back up the database and metadata,
reload Hasura metadata after the column type change, and update typed price filters
from `bigint` to `numeric`. With `HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES=true`,
price query results remain exact strings. No table/view recreation, history repair
or chain reset is required by 004.

### Tracked price migration 004

Alembic revision 004 applies SQL file 004. A tracked 003 database obtains the
checked NUMERIC price columns through `upgrade`. An unversioned database already
initialized through SQL 004 uses `adopt --revision 004`.
Both paths preserve existing prices and recorded native8/native18 precision.
Reload Hasura metadata and adapt typed price filters after the column type change.

The runner removes the standalone BEGIN/COMMIT wrapper, including leading/trailing
comments, and rejects unexpected transaction-control statements. Function bodies,
quoted identifiers and literals retain their contents. SQL, the revision stamp and
the advisory lock stay in the outer transaction; failure after SQL execution and
before stamping rolls the price columns and version back to 003.

### Network metrics migration 005

On a
database without blocks, deployments and transfers, `get_network_metrics` computed
its range from `-infinity` and failed with `interval out of range`. Migration 005
replaces the function with the same signature and result type. When no data
exists, the range ends at `now()` and the function returns `p_divisions` empty
buckets. Results for a database with data do not change.

A tracked 004 database obtains the function through `upgrade`. For an untracked
SQL installation at version 004, apply only 005. An unversioned database already
initialized through SQL 005 uses `adopt --revision 005`. The migration changes no
table, column or data, and Hasura metadata does not need a reload.

### Lifecycle query plans migration 006

The Alembic head is 006, matching the SQL initialized by current Compose.
Hasura exposes `native_transfer_lifecycle` to the public role, and the explorer
reads it by initiating and by completing deploy. Before 006, each such lookup read
every native transfer result: the `UNION` that links results and aliases to
calls sorted all rows, and `completion_deploy_id` had no index. On 450,000
results, a lookup took about one second. Migration 006 adds an index on
`native_transfer_results(deploy_id)` and replaces the `UNION` with `UNION ALL`.
After 006, a lookup by `initiation_deploy_id` or by `completion_deploy_id` takes
less than one millisecond.

The indexer writes an alias only for a call other than the call of the result,
so the two branches have no common rows. Migration 006 deletes alias rows that
repeat the result's own call, if a database has them. The view keeps its columns
and its rows. Hasura metadata does not need a reload.

Filter the view by one deploy column per query. A filter that combines
`initiation_deploy_id` and `completion_deploy_id` with `_or` still reads the
whole view.

A tracked 005 database obtains the change through `upgrade`. For an untracked
SQL installation at version 005, apply only 006. An unversioned database already
initialized through SQL 006 uses `adopt --revision 006`. The index build locks
writes to `native_transfer_results`. Stop the indexer during the upgrade.

The separate [adoption review findings](https://github.com/asi-alliance/asi-chain-indexer/pull/37#issuecomment-6736839084)
on internal FK-trigger enforcement and owned-sequence structure remain open.
The current catalog profiles do not validate those properties; the current-dev
integration and 004 support do not resolve those additional adoption checks.

Legacy migrations were consolidated into `000_comprehensive_initial_schema.sql`
and removed from the active tree. Earlier versions remain available in Git history.

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
compact summaries are not used as ledger evidence. Missing/busy or
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

`scripts/full-init-hasura.sh` exposes the read-only `native_transfer_lifecycle`
view to the public role; run it on fresh metadata. The script stops at the first
object that is already tracked, so it does not update existing metadata. For an
existing installation, add the view, its public select permission (no
aggregations) and the relationships listed in the script through the Hasura
metadata API or console, then check `get_inconsistent_metadata`. It provides status (`pending`, `completed`,
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
