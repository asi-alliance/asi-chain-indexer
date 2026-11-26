#!/usr/bin/env python3
"""Backfill deployments.deployment_action for rows indexed before migration 008.

The indexer writes the action for every new deployment. Existing rows keep NULL
so the migration itself does not rewrite a large deployments table. Run this
script with indexer writes stopped, or accept that new rows may be updated twice.

Usage:
    python scripts/backfill_deployment_actions.py --dsn "$DATABASE_URL"
    python scripts/backfill_deployment_actions.py --dsn ... --dry-run
"""

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.block_indexer import BlockIndexer  # noqa: E402
from src.time_utils import utc_now  # noqa: E402


async def backfill(conn, batch_size: int, dry_run: bool) -> tuple[int, int]:
    updated = 0
    scanned = 0
    cursor = ""
    while True:
        rows = await conn.fetch(
            """
            SELECT deploy_id, term
            FROM deployments
            WHERE deployment_action IS NULL AND deploy_id > $2
            ORDER BY deploy_id
            LIMIT $1
            """,
            batch_size,
            cursor,
        )
        if not rows:
            break
        scanned += len(rows)
        cursor = rows[-1]["deploy_id"]
        changes = []
        for row in rows:
            deployment_type, action = BlockIndexer.classify_deployment_action(
                row["term"] or ""
            )
            changes.append((deployment_type, action, row["deploy_id"]))
        if changes and not dry_run:
            await conn.executemany(
                """
                UPDATE deployments
                SET deployment_type=$1, deployment_action=$2
                WHERE deploy_id=$3
                """,
                changes,
            )
        updated += len(changes)
        if dry_run:
            # Avoid scanning the same NULL batch forever during a dry run.
            break
    return scanned, updated


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL")
        or "postgresql://indexer:indexer_pass@localhost:5432/asichain",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    conn = await asyncpg.connect(args.dsn)
    try:
        scanned, updated = await backfill(conn, args.batch_size, args.dry_run)
        print(
            f"{'would update' if args.dry_run else 'updated'} {updated} of "
            f"{scanned} scanned deployment(s) at {utc_now().isoformat()}"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
