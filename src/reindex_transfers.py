"""Repair indexed transfer history without resetting the node or chain data.

Stop indexer writes, apply migrations through 003, then run python -m src.reindex_transfers.
Each block is atomic and a failed/missing report aborts; rerunning is safe.
"""

import asyncio
from sqlalchemy import delete, select, text

from src.block_indexer import BlockIndexer
from src.database import db
from src.grpc_node_client import GrpcNodeClient
from src.models import BalanceState, Block, Deployment, Transfer
from src.transfer_reports import block_events
from src.native_transfers import (
    reconcile_block, reconcile_completed_duplicates, require_indexed_parents,
)


async def repair():
    await db.connect()
    indexer = BlockIndexer()
    client = GrpcNodeClient()
    indexer.client = client
    try:
        async with db.session() as session:
            blocks = (
                await session.scalars(
                    select(Block)
                    .order_by(Block.block_number, Block.block_hash)
                )
            ).all()
        for block in blocks:
            details = await client.get_block_details(block.block_hash)
            if not details:
                raise ValueError(f"Missing block {block.block_hash}")
            info = details["blockInfo"]
            info["token_decimals"] = block.token_decimals
            deploys = details.get("deploys", [])
            if block.block_number == 0:
                info["deploys"] = deploys
                async with db.session() as session:
                    # Synthetic genesis rows are reconstructed from immutable
                    # allocations; real deployment records remain untouched.
                    synthetic = select(Deployment.deploy_id).where(
                        Deployment.block_number == 0,
                        Deployment.deployment_type.in_(
                            ["genesis_mint", "genesis_bond"]
                        ),
                    )
                    await session.execute(
                        delete(Transfer).where(Transfer.block_number == 0)
                    )
                    await session.execute(
                        delete(Deployment).where(Deployment.deploy_id.in_(synthetic))
                    )
                    await session.execute(
                        delete(BalanceState).where(BalanceState.block_number == 0)
                    )
                    await indexer._process_genesis_transfers(session, info)
                    await indexer._process_genesis_balance_states(session, info)
            elif deploys:
                report = await client.get_block_report(block.block_hash)
                traces = block_events(
                    report, block.block_hash, [d["sig"] for d in deploys]
                )
                async with db.session() as session:
                    movements = await reconcile_block(session, info, deploys, traces, client)
                    for deploy in deploys:
                        # deployments has one canonical occurrence per deploy.
                        owner = await session.scalar(
                            select(Deployment.block_hash).where(
                                Deployment.deploy_id == deploy["sig"]
                            )
                        )
                        if owner != block.block_hash:
                            continue
                        deploy["executedTransfers"] = movements[deploy["sig"]]
                        await indexer._store_transfers(session, info, deploy)
                    await reconcile_completed_duplicates(session, info)
            else:
                async with db.session() as session:
                    await require_indexed_parents(session, info, client)
                    await reconcile_completed_duplicates(session, info)
            print(f"Repaired block {block.block_number} {block.block_hash}", flush=True)
        async with db.session() as session:
            await session.execute(text("""
                UPDATE indexer_state SET value = '0', updated_at = NOW()
                WHERE key = 'native_transfer_backfill_required'
            """))
    finally:
        await client.close()
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(repair())
