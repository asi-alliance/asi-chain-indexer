# PostgreSQL and Hasura upgrade

The Compose baseline is PostgreSQL **18.6 Alpine**, Hasura GraphQL Engine
**v2.51.4**, and an Alpine 3.24 metrics sidecar with the PostgreSQL 18 client.
Hasura v2.51.4 was the latest stable v2 release checked on 2026-11-05. These
versions are pinned explicitly; Hasura DDN/v3 is a separate migration.

PostgreSQL 14 was inherited from the original Compose configuration, rather than
an identified Hasura requirement. PostgreSQL 14 support ends on 2026-11-12.
Hasura supports maintained PostgreSQL versions. See the
[PostgreSQL support calendar](https://www.postgresql.org/support/versioning/),
[Hasura database support](https://hasura.io/docs/2.0/databases/postgres/index/), and
[Hasura v2.51.4 release](https://github.com/hasura/graphql-engine/releases/tag/v2.51.4).

## Storage and existing installations

PostgreSQL 18's official image uses `PGDATA=/var/lib/postgresql/18/docker`.
Compose mounts a **new** `postgres18_data` volume at `/var/lib/postgresql`.
The earlier `postgres_data` volume is retained for rollback. Never attach a
PostgreSQL 14 data directory to PostgreSQL 18 and expect an in-place upgrade.
Do not copy the old directory into the new volume.

For a new installation, Compose initializes migrations 000–005. For an existing
installation, first restore its database into an empty PostgreSQL 18 cluster,
without running the initialization SQL over restored objects. Starting the full
stack before restoring would start the indexer against an empty database.

Do **not** use `deploy.sh`, `deploy.sh --clean`, `make docker-reset`, or
`docker compose down -v` for this upgrade. The deployment helper initializes
fresh Hasura metadata; its clean mode and reset commands delete volumes.
Existing Hasura metadata must be preserved, including site-specific permissions,
relationships, functions, event triggers and sources.

The procedure below is a maintenance-window runbook, not an automatic deployment.
Inspect the actual host before selecting names, paths, networks or credentials.

## 1. Inventory and rehearse

- Record the exact running PostgreSQL, Hasura and indexer images/digests, source
  revision, Compose project/files, container names, networks, volumes and bind
  mounts. Save the old resolved Compose configuration outside the checkout with
  restrictive permissions; it can contain credentials. Retain the old images.
- Check server version, database size, free space, encoding/collations, extensions,
  roles/grants, replication slots, event triggers and any external database
  sources. Budget space and downtime for a full dump, restore and index rebuild.
  The stock schema uses `uuid-ossp`; custom extensions need separate compatibility
  checks. An Alpine/libc change can require collation/index validation.
- Identify the metadata database separately if `HASURA_GRAPHQL_METADATA_DATABASE_URL`
  is configured. The stock Compose uses the same `asichain` database for data and
  Hasura catalog. Back up and migrate every affected database; a data-only dump
  is insufficient for a separate metadata database.
- Record applied indexer migrations, row counts, latest indexed block/cursor,
  exact large amounts, pending native calls and any required history repair.
  An engine upgrade does not replace [schema migrations](migrations/README.md).
  Do not change `NATIVE_TOKEN_DECIMALS` or reindex history as part of this upgrade.
- Restore a preliminary backup into an isolated PostgreSQL 18.6 instance. Start
  Hasura v2.51.4 against that copy, with external event/action/webhook processing
  isolated to avoid duplicate deliveries. Verify the acceptance checks below.

For installations with additional event triggers/actions, review Hasura's release
notes for the whole v2.36.0-to-v2.51.4 range. In v2.51.4 the OpenTelemetry query
attribute changes from `db.query` to `db.query.text`; update affected dashboards.

## 2. Freeze writes and take the final backup

1. Put API consumers in maintenance mode. Stop the indexer, metrics-cron, Hasura
   and every other writer/event processor. Keep PostgreSQL 14 running for the dump.
   Export Hasura metadata through `export_metadata` before stopping Hasura and
   keep the JSON in the protected backup directory, never in Git.
2. Take a complete custom-format logical dump with PostgreSQL 18.6 `pg_dump`,
   including all application and `hdb_catalog` objects. Back up globals separately.
   Keep checksums, the old configuration and protected secret references. A
   metadata JSON export alone does not back up catalog/event state or chain data.

Example for an inspected Docker network (Linux Bash): create a mode-700 directory
outside the checkout, set `BACKUP_DIR` and `DB_NETWORK`, and place `PGHOST`,
`PGPORT`, `PGDATABASE`, `PGUSER` and `PGPASSWORD` in its mode-600 `client.env`.
Use the actual database superuser; never put credentials in shell history.

```bash
docker run --rm --network "$DB_NETWORK" --env-file "$BACKUP_DIR/client.env" \
  -v "$BACKUP_DIR:/backup" postgres:18.6-alpine \
  pg_dump --format=custom --file=/backup/asichain.dump
docker run --rm --network "$DB_NETWORK" --env-file "$BACKUP_DIR/client.env" \
  -v "$BACKUP_DIR:/backup" postgres:18.6-alpine \
  pg_dumpall --globals-only --file=/backup/globals.sql
sha256sum "$BACKUP_DIR/asichain.dump" "$BACKUP_DIR/globals.sql" \
  > "$BACKUP_DIR/SHA256SUMS"
```

The globals dump contains password hashes and must remain protected. Review it
before restoring: the target bootstrap role already exists, and source tablespace
paths may not apply. Preserve all required owners/grants rather than suppressing
restore errors. After a verified dump, stop PostgreSQL 14 and retain its volume
untouched. Remove old containers only after recording their rollback configuration;
do not remove volumes or images.

## 3. Restore into PostgreSQL 18.6

Use the same Compose project name. Create a reviewed upgrade override that removes
the migration initialization mounts while restoring; Compose v2.24.4+ supports:

```yaml
# Store as a temporary override outside Git. The destination volume must be new.
services:
  postgres:
    volumes: !override
      - postgres18_data:/var/lib/postgresql
```

Start **only** `postgres` using `-f docker-compose.yml -f <upgrade-override>`.
The stock credentials/database settings are examples; preserve the installation's
actual settings and use the same override files throughout this procedure.
Verify `SHOW server_version` and `SHOW data_directory`, and verify that no
application tables were created by initialization scripts. Restore reviewed roles
first, then restore the complete database with ownership and grants intact:

```bash
# Default names shown; adapt to the inspected installation.
docker cp "$BACKUP_DIR/asichain.dump" asi-indexer-db:/tmp/asichain.dump
docker exec asi-indexer-db pg_restore -U indexer -d asichain \
  --exit-on-error --verbose /tmp/asichain.dump
docker exec asi-indexer-db vacuumdb -U indexer -d asichain --analyze-in-stages
```

Do not continue after a restore error. Run only missing indexer schema migrations
separately, following their instructions. Starting Compose does not apply missing
migrations to a restored volume.

## 4. Start Hasura and accept the restored stack

Start only Hasura v2.51.4 and allow its automatic catalog migration to finish.
Keep the old Hasura stopped; different versions must not share the same catalog.
Retain `HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES=true`, the existing secrets, role
configuration, source URLs and public API settings.

Before restarting indexing, verify:

- Hasura `/healthz` and `get_inconsistent_metadata` succeed with no inconsistent
  objects. Compare exported metadata with the saved copy, including all sources,
  relationships, permissions, functions and triggers. Do not rerun the fresh
  initialization script or replace metadata blindly.
- Row counts, block hashes/heights, indexer cursor, balances and pending native
  calls match the pre-upgrade snapshot. Large monetary values remain exact strings
  through GraphQL. Verify expected public permissions and rejection of forbidden
  operations, nested queries, aggregate queries and WebSocket subscriptions.
- Run `tests/bigint-storage.sql` against the restored schema (`ON_ERROR_STOP=1`;
  it rolls back its writes). Verify representative explorer/wallet queries with
  their actual roles and authentication.

Start the metrics sidecar and confirm `psql --version` reports 18.x and
`refresh_network_metrics_buckets` succeeds. Then start the indexer using its
previous configuration. Check DB connections, `/health`, `/ready`, cursor
advancement, recent finalized blocks, transfers and the explorer data path before
ending maintenance mode. The opt-in `tests/test_hasura_stack.py` writes synthetic
rows and is for disposable rehearsal/CI databases only, never the live database.

## 5. Rollback

If acceptance fails, keep consumers in maintenance mode and stop all new writers,
Hasura and PostgreSQL 18. Restart the retained PostgreSQL 14 volume with its
recorded old images and Compose configuration, then its original Hasura/indexer.
Verify the saved cursor/data and API behavior before reopening consumers.

Do not start old Hasura on the catalog migrated by new Hasura, and do not attach
the PostgreSQL 18 volume to PostgreSQL 14. Use the untouched pre-upgrade database
and matching Hasura version as the rollback pair. Writes accepted after cutover
need explicit reconciliation; rollback alone does not preserve those writes.

Retain both database volumes and backups until the upgraded stack is accepted and
the agreed rollback period expires. Never automatically delete the old volume.

## Validation

Local isolated checks on 2026-11-05 passed 103 tests without skips on PostgreSQL
18.6 and Hasura v2.51.4, including schema migrations, uint256 storage, ORM round
trips, nested public GraphQL queries and live WebSocket updates. The SQL storage
boundary check, fresh Hasura initialization/permissions checks, and PostgreSQL 18
metrics-client refresh also passed. The database-compatibility workflow repeats
these fresh-stack checks on PRs to dev.

A separate PostgreSQL 14-to-18.6 dump/restore rehearsal preserved counts and row
hashes for all 16 public tables, including a uint256 transfer. Hasura's catalog
migration from v2.36.0 to v2.51.4 preserved exported metadata exactly and reported
no inconsistent objects; nested queries and live updates passed on the restored
database. This synthetic rehearsal does not replace a backup/restore rehearsal
and acceptance against the actual installation, extensions and custom metadata.

See the official [PostgreSQL major-upgrade guide](https://www.postgresql.org/docs/18/upgrading.html),
[Docker image storage changes](https://hub.docker.com/_/postgres), and
[Hasura update guide](https://hasura.io/docs/2.0/deployment/updating-graphql-engine/).
