#!/usr/bin/env python3
"""Seed a disposable indexer database with reproducible benchmark rows.

The script writes blocks with DAG siblings, deployments and transfers directly
through asyncpg. It does not talk to a node and never runs migrations. Use it
only on a disposable database, then run ANALYZE and the public API benchmark.

Examples:
    python scripts/seed_benchmark_data.py --heights 20000 --siblings 2
    python scripts/seed_benchmark_data.py --heights 100000 --siblings 2 \
        --deployments-per-block 1 --transfers-per-block 1

The default run creates about 40,000 blocks, 40,000 deployments and 40,000
transfers. Every row is tagged with a random run id so it can be removed later.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.block_indexer import BlockIndexer  # noqa: E402

WEI = 10**18


def block_hash(run_id: str, height: int, sibling: int) -> str:
    # A 64-character key that starts with a removable seed marker. It is not a
    # consensus hash; the script is only for disposable benchmark databases.
    return f"seed{run_id}{height:016d}{sibling:02d}".ljust(64, "0")


def address(prefix: str, value: str) -> str:
    # Public read models accept up to 150 characters; this is not a valid ASI
    # address, only a stable high-cardinality search value for benchmarks.
    return (prefix + value)[:150]


def term_for(run_id: str, height: int, variant: int) -> str:
    token = f"seedtoken{run_id}{height}"
    if variant % 10 == 0:
        return (
            "new rl(`rho:registry:lookup`), poSCh in { rl!(`rho:system:pos`, *poSCh) | "
            f'@PoS!("delegate", *deployerId, "04", 1, *retCh) }} // {token}'
        )
    if variant % 10 == 1:
        return (
            "new deployerId(`rho:system:deployerId`), rl(`rho:registry:lookup`), "
            "vaultCh in { rl!(`rho:vault:system`, *vaultCh) | "
            '@SystemVault!("findOrCreate", "seed", *vaultCh) | '
            f'@vault!("transfer", "seed", 1, *retCh) }} // {token}'
        )
    if variant % 10 == 2:
        return (
            "new rl(`rho:registry:lookup`), ch in { rl!(`rho:system:test`, *ch) } "
            f"// {token}"
        )
    return f'new x in {{ x!("{token}") }} // generic contract call'


async def seed(args) -> tuple[str, int, int, int, int]:
    run_id = args.run_id or os.urandom(8).hex()
    conn = await asyncpg.connect(args.dsn)
    try:
        base_height = (
            int(
                await conn.fetchval("SELECT COALESCE(max(block_number), 0) FROM blocks")
            )
            + 1
        )
        base_timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        block_rows = []
        deployment_rows = []
        transfer_rows = []

        for height_offset in range(args.heights):
            height = base_height + height_offset
            timestamp = base_timestamp + height_offset * 1000
            for sibling in range(args.siblings):
                block_rows.append(
                    (
                        block_hash(run_id, height, sibling),
                        height,
                        timestamp + sibling,
                        address("04", f"{run_id}{height:016d}{sibling:02d}"),
                        "finalized" if (height_offset + sibling) % 5 else "unfinalized",
                        0,
                        18,
                        1000 + (height_offset % 5000),
                    )
                )
            for variant in range(args.deployments_per_block):
                deploy_id = f"seed_{run_id}_{height}_{variant}"
                bhash = block_hash(run_id, height, variant % args.siblings)
                term_variant = (height_offset + variant) % 10
                term = term_for(run_id, height, term_variant)
                deployment_type, deployment_action = (
                    BlockIndexer.classify_deployment_action(term)
                )
                deployment_rows.append(
                    (
                        deploy_id,
                        bhash,
                        height,
                        address("04", f"{run_id}{height:016d}{variant:02d}"),
                        address("1111", f"{run_id}{height:016d}{variant:02d}"),
                        term,
                        timestamp + variant,
                        deploy_id,
                        1,
                        1_000_000,
                        1,
                        0,
                        "included",
                        deployment_type,
                        deployment_action,
                    )
                )
                if variant < args.transfers_per_block:
                    transfer_rows.append(
                        (
                            deploy_id,
                            bhash,
                            height,
                            address("1111", f"{run_id}{height:016d}{variant:02d}"),
                            address(
                                "1111", f"{run_id}{height:016d}{(variant + 1):02d}"
                            ),
                            WEI + variant,
                            Decimal(1),
                            "success",
                            timestamp + variant,
                            0,
                            18,
                        )
                    )

        # Triggers update blocks.deployment_count and publish NOTIFY. They are
        # useful in production but dominate a synthetic load; session_replication_role
        # is transactional and cannot leak into a later connection.
        triggers_disabled = False
        try:
            await conn.execute("SET session_replication_role = replica")
            triggers_disabled = True
        except asyncpg.PostgresError:
            print("warning: cannot disable triggers; seeding with normal triggers")

        async with conn.transaction():
            await conn.copy_records_to_table(
                "blocks",
                records=block_rows,
                columns=(
                    "block_hash",
                    "block_number",
                    "timestamp",
                    "proposer",
                    "finalization_status",
                    "deployment_count",
                    "token_decimals",
                    "block_size",
                ),
            )
            await conn.copy_records_to_table(
                "deployments",
                records=deployment_rows,
                columns=(
                    "deploy_id",
                    "block_hash",
                    "block_number",
                    "deployer",
                    "deployer_address",
                    "term",
                    "timestamp",
                    "sig",
                    "phlo_price",
                    "phlo_limit",
                    "phlo_cost",
                    "valid_after_block_number",
                    "status",
                    "deployment_type",
                    "deployment_action",
                ),
            )
            if transfer_rows:
                await conn.copy_records_to_table(
                    "transfers",
                    records=transfer_rows,
                    columns=(
                        "deploy_id",
                        "block_hash",
                        "block_number",
                        "from_address",
                        "to_address",
                        "amount_dust",
                        "amount_asi",
                        "status",
                        "timestamp",
                        "event_index",
                        "token_decimals",
                    ),
                )
            if triggers_disabled:
                await conn.execute("SET session_replication_role = origin")
            await conn.execute("""
                UPDATE blocks AS b
                SET deployment_count = d.c
                FROM (
                    SELECT block_hash, count(*)::integer AS c
                    FROM deployments
                    GROUP BY block_hash
                ) AS d
                WHERE b.block_hash = d.block_hash
                """)
            await conn.execute("""
                INSERT INTO block_heights(height, block_count, latest_block_hash,
                                          latest_timestamp, finalized_count)
                SELECT block_number,
                       count(*)::integer,
                       (array_agg(block_hash ORDER BY timestamp DESC, block_hash DESC))[1],
                       max(timestamp),
                       count(*) FILTER (WHERE finalization_status = 'finalized')::integer
                FROM blocks
                GROUP BY block_number
                ON CONFLICT (height) DO UPDATE
                    SET block_count = EXCLUDED.block_count,
                        latest_block_hash = EXCLUDED.latest_block_hash,
                        latest_timestamp = EXCLUDED.latest_timestamp,
                        finalized_count = EXCLUDED.finalized_count
                """)

        await conn.execute("ANALYZE blocks")
        await conn.execute("ANALYZE deployments")
        await conn.execute("ANALYZE transfers")
        await conn.execute("ANALYZE block_heights")
        return (
            run_id,
            len(block_rows),
            len(deployment_rows),
            len(transfer_rows),
            base_height,
        )
    finally:
        await conn.close()


async def clean(dsn: str, run_id: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        result = await conn.execute(
            "DELETE FROM blocks WHERE block_hash LIKE $1",
            f"seed{run_id}%",
        )
        print(f"cleaned blocks: {result}")
    finally:
        await conn.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL")
        or "postgresql://indexer:indexer_pass@localhost:5432/asichain",
    )
    parser.add_argument("--heights", type=int, default=20_000)
    parser.add_argument("--siblings", type=int, default=2)
    parser.add_argument("--deployments-per-block", type=int, default=1)
    parser.add_argument("--transfers-per-block", type=int, default=1)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    if min(args.heights, args.siblings, args.deployments_per_block) < 1:
        parser.error("heights, siblings and deployments-per-block must be positive")
    if args.transfers_per_block < 0:
        parser.error("transfers-per-block must be zero or positive")
    if args.clean:
        if not args.run_id:
            parser.error("--clean requires --run-id")
        await clean(args.dsn, args.run_id)
        return

    run_id, blocks, deployments, transfers, base_height = await seed(args)
    print(f"run id: {run_id}")
    print(f"base height: {base_height}")
    print(f"seeded blocks={blocks} deployments={deployments} transfers={transfers}")
    print(f"remove with: --clean --run-id {run_id}")


if __name__ == "__main__":
    asyncio.run(main())
