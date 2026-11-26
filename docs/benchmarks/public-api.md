# Public API benchmark: aggregates, grouped heights and search

Measured on 2026-11-27 against the local Compose stack:

- PostgreSQL 18.6, Hasura 2.51.4, Docker Desktop on Windows.
- Public role, no admin secret.
- 1,000,000 blocks, 500,000 deployments, 500,000 transfers and 500,000
  grouped height rows. Every synthetic row carries a removable `seed` marker.
- `scripts/seed_benchmark_data.py` and `scripts/benchmark_public_api.py`;
  `ANALYZE` ran after seeding.
- 20 recorded requests per query, after three warm-up requests.

## Latency

Concurrency 20, broad search pattern `%seed%` (matches the whole seed set):

| Query | p50 | p95 | Max |
|---|---:|---:|---:|
| Exact height `blocks_aggregate` | 49.8 ms | 51.3 ms | 52.2 ms |
| Unfiltered `blocks_aggregate` count | 91.9 ms | 106.1 ms | 110.2 ms |
| `block_heights_view` page (20 heights) | 9.7 ms | 11.5 ms | 11.7 ms |
| Unfiltered `deployments_aggregate` count | 48.3 ms | 58.3 ms | 59.6 ms |
| Unfiltered `transfers_aggregate` count | 49.2 ms | 66.4 ms | 66.7 ms |
| `blocks_aggregate` max height | 5.7 ms | 7.0 ms | 7.1 ms |
| `deployments_aggregate` `sum(phlo_cost)` | 258.7 ms | 295.3 ms | 301.0 ms |
| `transfers_aggregate` `sum(amount_asi)` | 143.0 ms | 159.4 ms | 163.7 ms |
| `deployments_aggregate` by `deployment_action` | 15.1 ms | 17.7 ms | 17.7 ms |
| Substring search, blocks | 10.1 ms | 12.0 ms | 13.4 ms |
| Substring search, deployments | 7.5 ms | 8.8 ms | 9.0 ms |
| Substring search, transfers | 8.3 ms | 10.2 ms | 10.2 ms |

The broad search benefits from the rows matching almost immediately. A long,
selective substring pattern exercises the trigram index fully:

| Query (selective `%...%`, concurrency 5) | p50 | p95 | Max |
|---|---:|---:|---:|
| Search blocks | 970 ms | 1009 ms | 1028 ms |
| Search deployments | 4 ms | 500 ms | 500 ms |
| Search transfers (`_or` over 3 fields) | 1037 ms | 1089 ms | 1092 ms |

`EXPLAIN ANALYZE` for the selective block search used
`idx_blocks_hash_trgm`, but the index returned 578 candidate rows and the
bitmap scan read about 5,000 buffers (140 ms in PostgreSQL). The transfer
`_or` plan used a `BitmapOr` over the three trigram indexes (135 ms). Prefix
lookups use the pattern-ops indexes and exact lookups use the primary/unique
indexes; those are the supported fast shapes for complete identifiers.

## Plans

| Query | Plan | Execution |
|---|---|---:|
| `SELECT count(*) FROM blocks WHERE block_number=500000` | Index Only Scan `idx_blocks_number` | 0.095 ms |
| `block_heights_view ORDER BY height DESC LIMIT 20` | Index Scan Backward `block_heights_pkey` | 0.070 ms |
| `block_hash LIKE 'seed...%' LIMIT 20` | Seq Scan stops at the first 20 matches; pattern index available with a selective prefix | 0.16 ms |
| `from_address LIKE '1111...%' LIMIT 20` | Seq Scan stops early; `idx_transfers_from_prefix` available | 0.12 ms |
| `block_hash ILIKE '%...%'` | Bitmap Index Scan `idx_blocks_hash_trgm` + heap recheck | 140-158 ms |
| `native_transfer_lifecycle_aggregate` | Public role rejected | — |

## Indexer HTTP endpoints

The same database and a local `MonitoringServer` were used with the read path
only; no node was connected. Each endpoint received 25 requests.

| Endpoint | p50 | p95 | Max |
|---|---:|---:|---:|
| `/health` | 0.48 ms | 1.22 ms | 1.92 ms |
| `/status` | 1.07 ms | 4.07 ms | 123.0 ms |
| `/metrics` | 1.98 ms | 2.78 ms | 2.97 ms |
| `/api/blocks?limit=20` | 30.7 ms | 53.1 ms | 55.9 ms |

`/status` has a read-through count cache; the 123 ms maximum is the first
uncached count on a 1M-row database. Repeated calls are milliseconds. The
`/api/blocks` endpoint returns 20 rows from `blocks` with keyset-like offset
pagination; it is not the grouped height view used by the public GraphQL API.

## Interpretation

- Exact height aggregates and grouped heights are safe for the public role at
  this size: they use primary/secondary indexes and stay below 35 ms p95.
- Unfiltered `count(*)` aggregates scan the table. At 1M rows they are around
  50-110 ms p95; `sum()` over monetary columns is heavier: `phlo_cost` reached
  295 ms p95 and `amount_asi` 159 ms p95. At 10M rows these will approach
  seconds. Keep the Hasura query timeout and public rate limits in place, and
  prefer filtered counts. A deployment/transfer aggregate filtered by
  `deployment_action`, deployer or address uses an index.
- `block_heights_view` is backed by the private `block_heights` table maintained
  by the `block_height_upsert` trigger. It replaced a normal grouped view whose
  20-row page took 80 ms at only 100k blocks; the maintained table page takes
  0.07 ms.
- Substring search over complete long identifiers is best-effort. The supported
  fast path is exact, then prefix; use substring only for partial terms. The
  explorer should use `_eq`/`_like 'value%'` when the input is a complete hash,
  deploy ID or address.
- The indexer `/status` and `/metrics` probes were measured against the same
  database through the local `MonitoringServer`: p95 4.1 ms and 2.8 ms. They
  remained fast at 1M blocks. A connected node was not available, so node-read
  and full-ingest latency were not measured here.
