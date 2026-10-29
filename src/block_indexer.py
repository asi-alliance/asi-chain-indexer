"""Enhanced indexer using the node gRPC/HTTP API for comprehensive blockchain synchronization."""

import asyncio
import re
import statistics
from datetime import datetime
from typing import Dict, List, Optional

import aiohttp
import asyncpg
import grpc
import structlog
from sqlalchemy import select, text, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import InterfaceError, OperationalError

from src.addr import convert_to_asi_address, public_key_to_asi_address
from src.alerts import AlertedError, AlertEvent, AlertKind, AlertService, describe_error
from src.config import settings
from src.amounts import amount, token_decimal, integer_json
from src.database import db
from src.models import (
    Block, BlockParent, Deployment, PendingDeploy, Transfer, Validator, ValidatorBond,
    EpochTransition, NetworkStats, BalanceState
)
from src.grpc_node_client import GrpcNodeClient
from src.sync_progress import BlockBatchProgress
from src.transfer_reports import block_events
from src.native_transfers import (
    reconcile_block, reconcile_completed_duplicates, require_indexed_parents,
)

logger = structlog.get_logger(__name__)


def classify_failure(error: Exception) -> Optional[AlertKind]:
    """Map a sync-cycle exception onto the alert kind that names it.

    Returns None when the failure is not a recognisable dependency outage, leaving
    it to the consecutive-failure counter.
    """
    if isinstance(error, (grpc.RpcError, aiohttp.ClientError)):
        return AlertKind.NODE_UNREACHABLE
    # asyncpg.PostgresError already covers PostgresConnectionError and
    # ConnectionDoesNotExistError
    if isinstance(error, (asyncpg.PostgresError, asyncpg.InterfaceError,
                          OperationalError, InterfaceError)):
        return AlertKind.DATABASE_UNREACHABLE
    # Failing to reach Postgres at all (refused, DNS, connect timeout, or "Multiple
    # exceptions" when every address fails) surfaces as a plain OSError that neither
    # asyncpg nor SQLAlchemy wraps. Only those raised inside the driver count: a bare
    # OSError could be anything, and asyncio.TimeoutError is one too.
    if isinstance(error, OSError) and _raised_in_database_driver(error):
        return AlertKind.DATABASE_UNREACHABLE
    return None


def _raised_in_database_driver(error: BaseException) -> bool:
    tb = error.__traceback__
    while tb is not None:
        if tb.tb_frame.f_globals.get("__name__", "").startswith("asyncpg"):
            return True
        tb = tb.tb_next
    return False


class BlockIndexer:
    """Enhanced indexer using the node gRPC/HTTP API for full blockchain data extraction."""

    # Indexed blocks between main-chain (reorg) verifications
    MAIN_CHAIN_CHECK_INTERVAL = 500

    @staticmethod
    def classify_deployment(term: str) -> str:
        """Classify deployment type based on Rholang term content."""
        if 'ASIVault' in term and 'transfer' in term:
            return 'asi_transfer'
        elif 'validator' in term or 'bond' in term:
            return 'validator_operation'
        elif 'finalizer' in term:
            return 'finalizer_contract'
        elif 'registry' in term and 'lookup' in term:
            return 'registry_lookup'
        elif 'auction' in term:
            return 'auction_contract'
        else:
            return 'smart_contract'

    def __init__(self, alerts: Optional[AlertService] = None):
        self.client = None
        self.running = False
        self.last_epoch_check_block = 0
        self.last_consensus_check_block = 0
        # None until the first verification, so a restart verifies straight away
        self._last_verified_block: Optional[int] = None
        self._genesis_data_cache = None  # Cache genesis data to avoid multiple extractions
        self.alerts = alerts if alerts is not None else AlertService.disabled()
        self._consecutive_failures = 0
        self._cycle_error: Optional[Exception] = None
        self._cycle_kind: Optional[AlertKind] = None
        self._node_unavailable_cycles = 0
        self._lag_history: List[int] = []
        self._stuck_height: Optional[int] = None
        self._stuck_cycles = 0

    async def start(self):
        """Start the enhanced indexer."""
        self.running = True
        print("🚀 Starting enhanced blockchain indexer", flush=True)
        logger.info("🚀 Starting enhanced blockchain indexer")

        # Initialize database
        logger.info("📊 Connecting to database...")
        await db.connect()
        await db.create_tables()
        if settings.enable_asi_transfer_extraction:
            async with db.session() as session:
                needs_repair = await session.scalar(text(
                    "SELECT value FROM indexer_state WHERE key = 'native_transfer_backfill_required'"
                ))
                if needs_repair == "1":
                    raise ValueError("Run python -m src.reindex_transfers before starting transfer indexing")
        logger.info("✅ Database connected and tables ready")

        # Initialize gRPC node client
        logger.info("🔧 Initializing gRPC node client...")
        try:
            self.client = GrpcNodeClient()
        except Exception as e:
            logger.error(str(e), exc_info=True)

        # Check node health
        logger.info("🔍 Checking ASI-Chain node health...")
        if not await self.client.health_check():
            logger.error("❌ Node is not healthy - cannot connect to ASI-Chain node")
            # awaited, not fire-and-forget: the process exits right after this raises
            await self.alerts.notify_and_wait(
                AlertEvent(AlertKind.NODE_UNREACHABLE, "health check failed at startup")
                .with_context("node", f"{settings.node_host}:{settings.grpc_port}")
            )
            raise AlertedError("Cannot connect to node")

        await self.client.require_native_precision(settings.native_token_decimals)
        logger.info("✅ ASI-Chain node connection established")

        # Get current indexer state
        last_indexed = await db.get_last_indexed_block()
        logger.info("📈 Current indexer state", last_indexed_block=last_indexed)

        # Run sync loop
        logger.info("🔄 Starting continuous sync loop...")
        sync_cycles = 0
        while self.running:
            self._cycle_error = None
            self._cycle_kind = None
            try:
                await self._sync_blocks()
                await self._sync_pending_deploys()
                await self._update_validator_states()
                await self._check_epoch_transitions()
                await self._update_network_stats()
                await self._verify_main_chain()

                # Log status every 10 sync cycles
                sync_cycles += 1
                if sync_cycles % 10 == 0:
                    current_block = await db.get_last_indexed_block()
                    logger.info("📊 Indexer status update",
                                current_block=current_block,
                                sync_cycles_completed=sync_cycles,
                                status="running_normally")

            except Exception as e:
                logger.error("❌ Sync cycle failed", error=str(e), exc_info=True)
                self._record_cycle_failure(e)

            self._close_cycle()

            await asyncio.sleep(settings.sync_interval)

    def _record_cycle_failure(self, error: Exception):
        """Note a failed step of the current sync cycle.

        Called from each step's own `except` rather than from the sync loop: every
        step swallows its exceptions, so nothing reaches the loop's handler.
        """
        kind = classify_failure(error)
        if kind is None and self._cycle_kind is not None:
            return
        self._cycle_error = error
        self._cycle_kind = kind

    def _close_cycle(self):
        """Count the cycle against the stall threshold and alert once it is reached.

        A single failed cycle, even a dependency error, is often a blip that the
        next cycle recovers from, so every kind waits for the same run of
        consecutive failures. The alert is named after the outage behind the
        latest cycle when there is one, and is a plain stall otherwise.

        Runs outside the loop's `try`, so it must never raise: an escaped exception
        here would kill the sync task.
        """
        try:
            if self._cycle_error is None:
                self._consecutive_failures = 0
                return

            self._consecutive_failures += 1
            if self._consecutive_failures < settings.sync_stall_threshold:
                return

            kind = self._cycle_kind or AlertKind.SYNC_STALLED
            event = AlertEvent(kind, describe_error(self._cycle_error))
            if kind is AlertKind.NODE_UNREACHABLE:
                event.with_context("node", f"{settings.node_host}:{settings.grpc_port}")
            self.alerts.notify(
                event.with_context("consecutive_failures", self._consecutive_failures)
            )
        except Exception as e:
            logger.error("Failed to close sync cycle", error=str(e), exc_info=True)

    def _track_node_availability(self, available: bool, error: str = ""):
        """Alert when the node keeps coming back empty at runtime.

        The node client swallows RPC errors and returns None / [] instead, so an
        outage after startup never reaches `classify_failure`. Counting empty
        results over consecutive cycles is what surfaces it.
        """
        if available:
            self._node_unavailable_cycles = 0
            return

        self._node_unavailable_cycles += 1
        if self._node_unavailable_cycles < settings.node_unreachable_cycles:
            return

        self.alerts.notify(
            AlertEvent(AlertKind.NODE_UNREACHABLE, error)
            .with_context("node", f"{settings.node_host}:{settings.grpc_port}")
            .with_context("cycles", self._node_unavailable_cycles)
        )

    def _track_cursor(self, completed_height: int, end: int, failures: int,
                      error: Optional[str]):
        """Alert when the sync cursor stops advancing.

        A failed block is not lost — the cursor halts below it and the batch is
        retried next cycle. So a single failure is normal and self-healing; the
        thing worth waking someone for is the same block failing over and over,
        which pins the cursor and stops indexing entirely.
        """
        if not failures and completed_height >= end:
            self._stuck_height = None
            self._stuck_cycles = 0
            return

        if completed_height == self._stuck_height:
            self._stuck_cycles += 1
        else:
            self._stuck_height = completed_height
            self._stuck_cycles = 1

        if self._stuck_cycles < settings.cursor_stuck_cycles:
            return

        self.alerts.notify(
            AlertEvent(AlertKind.BLOCKS_STUCK, error or "block processing failed")
            .with_context("stuck_after_block", completed_height)
            .with_context("cycles", self._stuck_cycles)
        )

    def _track_lag(self, lag: int):
        """Alert when lag is deep and has stopped shrinking.

        Depth alone is not enough: a genesis backfill sits legitimately far behind
        while still catching up every cycle.

        Recovery is judged on the medians of the window's two halves rather than
        its endpoints, so neither a single spike nor a one-block drift decides it.
        """
        self._lag_history.append(lag)
        if len(self._lag_history) > settings.lag_alert_cycles:
            self._lag_history.pop(0)

        if len(self._lag_history) < settings.lag_alert_cycles:
            return
        if min(self._lag_history) <= settings.lag_alert_blocks:
            return

        half = len(self._lag_history) // 2
        older = statistics.median(self._lag_history[:half])
        recent = statistics.median(self._lag_history[half:])
        if recent <= older * settings.lag_recovery_ratio:
            return

        self.alerts.notify(
            AlertEvent(AlertKind.SYNC_FALLING_BEHIND, "lag is not recovering")
            .with_context("lag_blocks", lag)
            .with_context("cycles", settings.lag_alert_cycles)
        )

    async def stop(self):
        """Stop the indexer."""
        logger.info("Stopping enhanced indexer")
        self.running = False
        await db.disconnect()

    async def _sync_blocks(self):
        """Sync blocks using the node's getBlocksByHeights RPC."""
        try:
            # Get last indexed block
            last_indexed = await db.get_last_indexed_block()

            # Get latest finalized block
            last_finalized_data = await self.client.get_last_finalized_block()
            if not last_finalized_data:
                logger.warning("Could not get last finalized block")
                self._track_node_availability(False, "could not get last finalized block")
                return

            latest_block_number = last_finalized_data.get("blockNumber")
            if latest_block_number is None:
                logger.warning("No block number in finalized block data")
                self._track_node_availability(False, "could not get latest block number")
                return

            self._track_lag(max(0, latest_block_number - last_indexed))

            if last_indexed >= latest_block_number:
                logger.debug("Already up to date", last_indexed=last_indexed, latest=latest_block_number)
                self._track_node_availability(True)
                return

            # Check whether the database is fresh. A fresh database starts at
            # the configured height; otherwise continue from the last height
            # whose complete sibling set was indexed.
            async with db.session() as session:
                block_count = await session.scalar(
                    text("SELECT COUNT(*) FROM blocks")
                )

            if block_count == 0:
                start = settings.start_from_block
            else:
                start = max(settings.start_from_block, last_indexed + 1)
            batch_size = settings.batch_size
            end = min(start + batch_size - 1, latest_block_number)

            logger.info(
                "🔄 Syncing blocks",
                start_block=start,
                end_block=end,
                blocks_behind=latest_block_number - last_indexed,
                latest_block=latest_block_number
            )

            block_summaries = await self.client.get_blocks_by_height(start, end)

            if not block_summaries:
                logger.warning("No blocks returned for range", start=start, end=end)
                # an empty range (start_from_block above the finalized tip) is not an outage
                self._track_node_availability(
                    end < start, f"no blocks returned for heights {start}-{end}"
                )
                return

            self._track_node_availability(True)

            logger.info(f"Retrieved {len(block_summaries)} blocks, fetching details...")

            # Only advance through heights for which every returned sibling
            # succeeded. A failed sibling is retried on the next sync cycle.
            progress = BlockBatchProgress(start, end)
            processed_count = 0
            skipped = []
            last_skip_error = None
            for block_summary in sorted(block_summaries, key=lambda block: (
                block.get("blockNumber") if block.get("blockNumber") is not None else -1,
                block.get("blockHash") or ""
            )):
                block_number = block_summary.get("blockNumber")
                if block_number is not None and block_number > progress.last_completed_height + 1:
                    break
                progress.mark_seen(block_number)
                try:
                    # Get full block details
                    block_hash = block_summary.get("blockHash")
                    if not block_hash:
                        logger.warning("Block summary missing hash", block=block_summary)
                        progress.mark_failed(block_number)
                        skipped.append(block_number)
                        last_skip_error = "block summary missing hash"
                        continue

                    full_block = await self.client.get_block_details(block_hash)
                    if not full_block:
                        logger.warning(f"Could not get details for block {block_hash}")
                        progress.mark_failed(block_number)
                        skipped.append(block_number)
                        last_skip_error = "could not get block details"
                        continue

                    await self._process_block(full_block)
                    processed_count += 1

                    # Small delay to avoid overwhelming the node
                    await asyncio.sleep(settings.delay_before_node)

                except Exception as e:
                    progress.mark_failed(block_number)
                    logger.error(f"Failed to process block", error=str(e), block=block_summary)
                    skipped.append(block_number)
                    last_skip_error = describe_error(e)

            completed_height = progress.last_completed_height
            if completed_height >= start:
                await db.set_last_indexed_block(completed_height)

            self._track_cursor(completed_height, end, len(skipped), last_skip_error)

            if completed_height < end:
                logger.warning(
                    "Batch incomplete; failed or missing height will be retried",
                    first_uncompleted_height=completed_height + 1,
                    failed_heights=sorted(progress.failed_heights),
                    has_unmapped_failure=progress.has_unmapped_failure,
                )

            if processed_count > 0:
                logger.info("✅ Sync cycle complete",
                            last_indexed_block=completed_height,
                            blocks_processed=processed_count,
                            remaining_blocks=latest_block_number - completed_height)

        except Exception as e:
            logger.error(f"Sync blocks error: {e}", exc_info=True)
            self._record_cycle_failure(e)

    async def _sync_pending_deploys(self):
        """Refresh the pending_deploys snapshot from the node's deploy buffers.

        The node's getPendingDeploys returns the current buffer contents
        (deploy_storage + rejected-recovery buffer), so the table is fully
        refreshed (DELETE + INSERT) each cycle to stay consistent. Deploys
        that get included in a block disappear here and show up in the
        deployments table with the same sig/deploy_id.
        """
        if not settings.enable_pending_deploys_sync:
            return

        try:
            data = await self.client.get_pending_deploys()
            if data is None:
                # Node unreachable or RPC unavailable (e.g. old node version);
                # keep the last snapshot rather than wiping it.
                return

            deploys = data.get("deploys", [])
            total_available = data.get("totalAvailable", len(deploys))

            async with db.session() as session:
                await session.execute(text("DELETE FROM pending_deploys"))

                for d in deploys:
                    sig = d.get("sig", "")
                    if not sig:
                        logger.warning("Skipping pending deploy without sig", deployer=d.get("deployer", "")[:20])
                        continue

                    deployer = d.get("deployer", "")
                    session.add(PendingDeploy(
                        sig=sig,
                        deployer=deployer,
                        deployer_address=convert_to_asi_address(deployer, d),
                        term=d.get("term", ""),
                        timestamp=d.get("timestamp", 0),
                        phlo_price=d.get("phloPrice", 1),
                        phlo_limit=d.get("phloLimit", 1000000),
                        valid_after_block_number=d.get("validAfterBlockNumber"),
                        shard_id=d.get("shardId"),
                        sig_algorithm=d.get("sigAlgorithm", "secp256k1"),
                        language=d.get("language"),
                        # proto 0 = no expiration
                        expiration_timestamp=d.get("expirationTimestamp") or None,
                        is_rejected=d.get("isRejected", False),
                    ))

                # Persist the pre-cap total so the explorer can detect
                # truncation (node caps the response at 1000 entries).
                await session.execute(
                    text("""
                         INSERT INTO indexer_state (key, value, updated_at)
                         VALUES ('pending_deploys_total_available', :value, NOW())
                         ON CONFLICT (key) DO UPDATE SET value = :value, updated_at = NOW()
                         """),
                    {"value": str(total_available)},
                )
                await session.commit()

            if len(deploys) < total_available:
                logger.warning(
                    "Pending deploys truncated by node cap",
                    returned=len(deploys),
                    total_available=total_available,
                )

            logger.info(
                "Refreshed pending deploys",
                count=len(deploys),
                total_available=total_available,
                rejected=sum(1 for d in deploys if d.get("isRejected")),
            )

        except Exception as e:
            logger.error(f"Failed to sync pending deploys: {e}", exc_info=True)
            self._record_cycle_failure(e)

    async def _process_block(self, block_data: Dict):
        """Process a single block with full details."""
        block_info = block_data.get("blockInfo", {})
        deployments = block_data.get("deploys", [])

        block_hash = block_info.get("blockHash")
        block_number = block_info.get("blockNumber")

        if not block_hash or block_number is None:
            raise ValueError("Block missing required blockHash or blockNumber")

        # Check if already processed
        async with db.session() as session:
            exists = await session.scalar(
                select(Block).where(Block.block_hash == block_hash).limit(1)
            )
            if exists:
                logger.debug("Block already indexed", block_number=block_number)
                return

        traces = None
        if settings.enable_asi_transfer_extraction and deployments and block_number != 0:
            report = await self.client.get_block_report(block_hash)
            traces = block_events(report, block_hash, [d["sig"] for d in deployments])

        # Process block in transaction
        async with db.session() as session:
            # Extract block data
            parent_hashes = block_info.get("parentsHashList") or []
            state_root_hash = block_info.get("postStateHash", "")
            bonds_map = block_info.get("bonds", [])
            justifications = block_info.get("justifications", [])

            block_insert = insert(Block).values(
                block_number=block_number,
                block_hash=block_hash,
                timestamp=block_info.get("timestamp", 0),
                proposer=block_info.get("sender", ""),
                state_hash=state_root_hash,
                state_root_hash=state_root_hash,
                finalization_status="finalized" if block_info.get("isFinalized") else "unfinalized",
                bonds_map=integer_json(bonds_map),
                seq_num=block_info.get("seqNum"),
                sig=block_info.get("sig"),
                sig_algorithm=block_info.get("sigAlgorithm"),
                shard_id=block_info.get("shardId"),
                extra_bytes=block_info.get("extraBytes"),
                version=block_info.get("version"),
                fault_tolerance=block_info.get("faultTolerance", 0.0),
                pre_state_hash=block_info.get("preStateHash"),
                justifications=justifications
            ).on_conflict_do_nothing(index_elements=["block_hash"])
            await session.execute(block_insert)

            if parent_hashes:
                parent_rows = [
                    {"block_hash": block_hash, "parent_hash": p, "parent_index": idx}
                    for idx, p in enumerate(parent_hashes)
                ]
                parents_insert = insert(BlockParent).values(parent_rows).on_conflict_do_nothing(
                    index_elements=["block_hash", "parent_hash"]
                )
                await session.execute(parents_insert)

            # Process validators from bonds
            await self._process_validators(session, block_info)

            # Process deployments with enhanced data
            for deploy_data in deployments:
                await self._process_deployment_enhanced(
                    session, block_info, deploy_data, store_transfers=False
                )

            if traces is not None:
                movements = await reconcile_block(session, block_info, deployments, traces, self.client)
                for deploy_data in deployments:
                    deploy_data["executedTransfers"] = movements[deploy_data["sig"]]
                    await self._store_transfers(session, block_info, deploy_data)
            elif settings.enable_asi_transfer_extraction and block_number != 0:
                await require_indexed_parents(session, block_info, self.client)

            if settings.enable_asi_transfer_extraction and block_number != 0:
                await reconcile_completed_duplicates(session, block_info)

            # Process genesis transfers if this is block 0
            if block_number == 0:
                # Ensure bonds are included in the block_info for genesis processing
                genesis_block_info = dict(block_info)
                genesis_block_info['bonds'] = bonds_map
                genesis_block_info['deploys'] = deployments
                await self._process_genesis_transfers(session, genesis_block_info)
                await self._process_genesis_balance_states(session, genesis_block_info)

            await session.commit()

            # Process block validators from justifications
            if justifications:
                async with db.session() as validator_session:
                    for justification in justifications:
                        validator_key = justification.get("validator", "")
                        if validator_key:
                            await validator_session.execute(
                                text("""
                                     INSERT INTO block_validators (block_hash, validator_public_key)
                                     VALUES (:block_hash, :validator_key)
                                     ON CONFLICT DO NOTHING
                                     """),
                                {"block_hash": block_hash, "validator_key": validator_key}
                            )
                    await validator_session.commit()

        # Special logging for genesis block
        if block_number == 0:
            logger.info(
                "🎯 Genesis block indexed with enhanced features",
                block_number=block_number,
                deployments=len(deployments),
                genesis_transfers="Created",
                genesis_balances="Initialized",
                timestamp=datetime.fromtimestamp(block_info.get("timestamp", 0) / 1000)
            )
        else:
            logger.info(
                "📦 Block indexed",
                block_number=block_number,
                deployments=len(deployments),
                timestamp=datetime.fromtimestamp(block_info.get("timestamp", 0) / 1000)
            )

    async def _process_deployment_enhanced(self, session, block_data: Dict, deploy_data: Dict,
                                         *, store_transfers=True):
        """Process a deployment; deploy_data already has everything from the block response."""
        deploy_id = deploy_data.get("sig")
        if not deploy_id:
            return

        # Classify deployment and normalize error flags
        term = deploy_data.get("term", "")
        deployment_type = self.classify_deployment(term)

        error_message = deploy_data.get("systemDeployError")
        # Only set error_message if it's not an empty string
        if error_message == "":
            error_message = None
        errored = deploy_data.get("errored", False) or bool(error_message)

        # -------------------------
        # 1) DEPLOYMENTS: UPSERT
        # -------------------------
        dep_insert = insert(Deployment).values(
            deploy_id=deploy_id,
            block_hash=block_data.get("blockHash"),
            block_number=block_data.get("blockNumber"),
            deployer=deploy_data.get("deployer", deploy_data.get("sender", "")),
            deployer_address=convert_to_asi_address(
                deploy_data.get("deployer", deploy_data.get("sender", "")), deploy_data
            ),
            term=term,
            timestamp=deploy_data.get("timestamp", block_data.get("timestamp")),
            sig=deploy_data.get("sig"),
            sig_algorithm=deploy_data.get("sigAlgorithm", "secp256k1"),
            phlo_price=deploy_data.get("phloPrice", 1),
            phlo_limit=deploy_data.get("phloLimit", 1000000),
            phlo_cost=deploy_data.get("cost", 0),
            valid_after_block_number=deploy_data.get("validAfterBlockNumber"),
            errored=errored,
            error_message=error_message,
            deployment_type=deployment_type,
            seq_num=deploy_data.get("seqNum"),
            shard_id=deploy_data.get("shardId"),
            status=deploy_data.get("status", "included"),
        )
        dep_upsert = dep_insert.on_conflict_do_update(
            index_elements=["deploy_id"],
            set_={
                "block_hash": dep_insert.excluded.block_hash,
                "block_number": dep_insert.excluded.block_number,
                "status": dep_insert.excluded.status,
                "phlo_cost": dep_insert.excluded.phlo_cost,
                "errored": dep_insert.excluded.errored,
                "error_message": dep_insert.excluded.error_message,
                "deployment_type": dep_insert.excluded.deployment_type,
                "timestamp": dep_insert.excluded.timestamp,
                "sig_algorithm": dep_insert.excluded.sig_algorithm,
                "seq_num": dep_insert.excluded.seq_num,
                "shard_id": dep_insert.excluded.shard_id,
                "deployer": dep_insert.excluded.deployer,
                "deployer_address": dep_insert.excluded.deployer_address,
                "term": dep_insert.excluded.term,
                "phlo_price": dep_insert.excluded.phlo_price,
                "phlo_limit": dep_insert.excluded.phlo_limit,
                "valid_after_block_number": dep_insert.excluded.valid_after_block_number,
            },
        )

        try:
            await session.execute(dep_upsert)
            await session.flush()  # surface FK/NOT NULL issues early
        except Exception as e:
            logger.warning("deployment upsert failed", deploy_id=deploy_id, err=str(e))
            raise

        if not store_transfers or not settings.enable_asi_transfer_extraction:
            return

        if block_data["blockNumber"] == 0:
            return  # genesis issuance is handled separately
        await self._store_transfers(session, block_data, deploy_data)

    async def _store_transfers(self, session, block_data, deploy_data):
        # Replace this deploy's old inferred rows atomically. This also makes
        # historical repair, restart and a deploy in multiple DAG blocks idempotent.
        transfers = self._extract_transfers(deploy_data, block_data["blockNumber"],
                                           block_data.get("token_decimals", settings.native_token_decimals))
        await session.execute(delete(Transfer).where(Transfer.deploy_id == deploy_data["sig"]))
        for transfer in transfers:
            transfer.block_hash = block_data["blockHash"]
            session.add(transfer)

    async def _update_validator_states(self):
        """Update validator states from the node's bonds (gRPC) and active validators (HTTP)."""
        try:
            # Get current bonds
            bonds_data = await self.client.get_bonds()
            if not bonds_data:
                return

            bonds = bonds_data.get("bonds", [])

            # Get active validators
            active_validators = await self.client.get_active_validators()
            if active_validators is None:
                active_validators = []

            async with db.session() as session:
                # Update validator records
                for bond in bonds:
                    validator_key = bond["validator"]
                    stake = bond["stake"]
                    is_active = validator_key in active_validators

                    # Get current block number for tracking
                    current_block = await db.get_last_indexed_block()

                    stmt = insert(Validator).values(
                        public_key=validator_key,
                        name=validator_key,
                        total_stake=stake,
                        token_decimals=settings.native_token_decimals,
                        first_seen_block=current_block,
                        last_seen_block=current_block,
                        status="active" if is_active else "bonded",
                        updated_at=datetime.utcnow()
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["public_key"],
                        set_={
                            "total_stake": stake,
                            "token_decimals": stmt.excluded.token_decimals,
                            "last_seen_block": current_block,
                            "status": "active" if is_active else "bonded",
                            "updated_at": datetime.utcnow()
                        }
                    )
                    await session.execute(stmt)

                await session.commit()

            logger.info(
                "Updated validator states",
                total_bonded=len(bonds),
                active=len(active_validators)
            )

        except Exception as e:
            logger.error(f"Failed to update validator states: {e}")
            self._record_cycle_failure(e)

    async def _check_epoch_transitions(self):
        """Check and record epoch transitions."""
        try:
            # Get current block
            current_block = await db.get_last_indexed_block()

            # Only check every 100 blocks to avoid too many node requests
            if current_block - self.last_epoch_check_block < 100:
                return

            self.last_epoch_check_block = current_block

            # Get epoch info
            epoch_info = await self.client.get_epoch_info()
            if not epoch_info:
                return

            current_epoch = epoch_info.get("current_epoch")
            epoch_length = epoch_info.get("epoch_length", 10000)
            quarantine_length = epoch_info.get("quarantine_length", 50000)
            blocks_until_next = epoch_info.get("blocks_until_next_epoch")

            if current_epoch is None:
                return

            # Check if we need to record this epoch
            async with db.session() as session:
                # Check if epoch already recorded
                exists = await session.scalar(
                    select(EpochTransition).where(
                        EpochTransition.epoch_number == current_epoch
                    ).limit(1)
                )

                if not exists and blocks_until_next is not None:
                    # Calculate epoch boundaries
                    epoch_start = current_block - (epoch_length - blocks_until_next)
                    epoch_end = epoch_start + epoch_length - 1

                    # Get active validators for this epoch
                    active_validators = await self.client.get_active_validators()
                    active_count = len(active_validators) if active_validators else 0

                    epoch_transition = EpochTransition(
                        epoch_number=current_epoch,
                        start_block=epoch_start,
                        end_block=epoch_end,
                        active_validators=active_count,
                        quarantine_length=quarantine_length,
                        timestamp=datetime.utcnow()
                    )
                    session.add(epoch_transition)
                    await session.commit()

                    logger.info(
                        "Recorded epoch transition",
                        epoch=current_epoch,
                        start=epoch_start,
                        end=epoch_end,
                        validators=active_count
                    )

        except Exception as e:
            logger.error(f"Failed to check epoch transitions: {e}")
            self._record_cycle_failure(e)

    async def _update_network_stats(self):
        """Update network statistics from the node's consensus data (finalized block, bonds, active validators)."""
        try:
            # Only update every 50 blocks
            current_block = await db.get_last_indexed_block()
            if current_block - self.last_consensus_check_block < 50:
                return

            self.last_consensus_check_block = current_block

            # Get network consensus data
            consensus = await self.client.get_network_consensus()
            if not consensus:
                return

            async with db.session() as session:
                network_stat = NetworkStats(
                    block_number=consensus.get("current_block", current_block),
                    total_validators=consensus.get("total_bonded_validators", 0),
                    active_validators=consensus.get("active_validators", 0),
                    validators_in_quarantine=consensus.get("validators_in_quarantine", 0),
                    consensus_participation=consensus.get("participation_rate", 0.0),
                    consensus_status=consensus.get("status", "unknown"),
                    timestamp=datetime.utcnow()
                )
                session.add(network_stat)
                await session.commit()

                logger.info(
                    "Updated network stats",
                    block=current_block,
                    participation=consensus.get("participation_rate", 0.0),
                    status=consensus.get("status", "unknown")
                )

        except Exception as e:
            logger.error(f"Failed to update network stats: {e}")
            self._record_cycle_failure(e)

    async def _verify_main_chain(self):
        """Periodically verify main chain integrity.

        The node's main chain runs past what is indexed (unfinalized tip, sync lag),
        so only heights at or below the cursor are compared. A height can hold
        several DAG siblings: a reorg is a stored height whose canonical hash is not
        among the hashes stored there. A height with no rows is not indexed yet.
        """
        try:
            # A distance, not a multiple: the cursor advances in BATCH_SIZE steps
            # and can jump over any given multiple
            current_block = await db.get_last_indexed_block()
            if (self._last_verified_block is not None
                    and current_block - self._last_verified_block
                    < self.MAIN_CHAIN_CHECK_INTERVAL):
                return

            main_chain = await self.client.show_main_chain(depth=20)
            if not main_chain:
                return

            canonical = []
            for block_info in main_chain:
                block_num = block_info.get("blockNumber")
                block_hash = block_info.get("blockHash")
                if block_num is not None and block_hash and block_num <= current_block:
                    canonical.append((block_num, block_hash))
            if not canonical:
                logger.debug("Main chain verification skipped, nothing indexed in range")
                return

            async with db.session() as session:
                rows = await session.execute(
                    select(Block.block_number, Block.block_hash).where(
                        Block.block_number.in_({block_num for block_num, _ in canonical})
                    )
                )
                stored: Dict[int, set] = {}
                for block_num, block_hash in rows:
                    stored.setdefault(block_num, set()).add(block_hash)

            mismatches = sorted(
                (block_num, block_hash) for block_num, block_hash in canonical
                if block_num in stored and block_hash not in stored[block_num]
            )

            if mismatches:
                logger.warning(
                    "Main chain mismatch detected",
                    heights=[block_num for block_num, _ in mismatches],
                    expected_hashes=[block_hash for _, block_hash in mismatches],
                )
                shown = [block_hash for _, block_hash in mismatches[:3]]
                if len(mismatches) > len(shown):
                    shown.append(f"+{len(mismatches) - len(shown)} more")
                self.alerts.notify(
                    AlertEvent(
                        AlertKind.CHAIN_REORG_DETECTED,
                        "stored blocks do not include the canonical hash at their height"
                    )
                    .with_context("heights", ", ".join(str(n) for n, _ in mismatches))
                    .with_context("expected_hashes", ", ".join(shown))
                )

            self._last_verified_block = current_block
            logger.info("Main chain verification complete", blocks_checked=len(canonical))

        except Exception as e:
            logger.error(f"Failed to verify main chain: {e}")
            self._record_cycle_failure(e)

    async def _process_validators(self, session, block_data: Dict):
        """Process validator bonds for a block."""
        bonds = block_data.get("bonds", [])

        for bond_data in bonds:
            validator_key = bond_data["validator"]
            stake = bond_data["stake"]

            # First ensure validator exists in validators table
            result = await session.execute(
                text("""
                     INSERT INTO validators (public_key, name, total_stake, token_decimals, status, first_seen_block, last_seen_block)
                     VALUES (:public_key, :name, :stake, :token_decimals, 'active', :block_number,
                             :block_number)
                     ON CONFLICT (public_key) DO UPDATE SET total_stake = CASE
                         WHEN validators.token_decimals = EXCLUDED.token_decimals
                         THEN GREATEST(validators.total_stake, EXCLUDED.total_stake)
                         ELSE EXCLUDED.total_stake END,
                                                            token_decimals = EXCLUDED.token_decimals,
                                                            last_seen_block = :block_number,
                                                            status          = 'active'
                     """),
                {
                    "public_key": validator_key,
                    "name": validator_key[:20] + "...",  # Short name for display
                    "stake": amount(stake),
                    "token_decimals": settings.native_token_decimals,
                    "block_number": block_data["blockNumber"]
                }
            )

            # Add validator bond record
            bond = ValidatorBond(
                block_hash=block_data["blockHash"],
                block_number=block_data["blockNumber"],
                validator_public_key=validator_key,
                stake=stake
            )
            session.add(bond)

    def _extract_transfers(self, deploy_data: Dict, block_number: int, decimals=None) -> List[Transfer]:
        """Only movements proved by the native replay trace are transfers."""
        if "executedTransfers" not in deploy_data:
            raise ValueError("Missing executed transfer report")
        decimals = settings.native_token_decimals if decimals is None else decimals
        return [Transfer(
            deploy_id=deploy_data["sig"], block_number=block_number,
            event_index=movement["event_index"],
            native_result_id=movement.get("native_result_id"),
            from_address=movement["from_address"], to_address=movement["to_address"],
            from_public_key=(deploy_data.get("deployer") if
                convert_to_asi_address(deploy_data.get("deployer", ""), deploy_data) == movement["from_address"] else None),
            amount_dust=movement["amount"],
            amount_asi=token_decimal(movement["amount"], movement.get("token_decimals", decimals)),
            token_decimals=movement.get("token_decimals", decimals),
            status="success", timestamp=movement.get("timestamp", deploy_data["timestamp"]),
        ) for movement in deploy_data["executedTransfers"]]

    async def _extract_genesis_data(self, block_info: Dict) -> Dict:
        """Read the immutable genesis generator and that block's bonds only."""
        if self._genesis_data_cache is not None:
            return self._genesis_data_cache
        decimals = block_info.get("token_decimals", settings.native_token_decimals)
        allocations = []
        for deploy in block_info.get("deploys", []):
            term = deploy.get("term", "")
            # Limit parsing to the standard VaultsGenerator initialization list.
            # Do not scan arbitrary contracts, comments or current wallet balances.
            if deploy.get("errored") or 'initVault!(*vault, addr, initialBalance)' not in term:
                continue
            term = re.sub(r'/\*.*?\*/|//[^\n]*', '', term, flags=re.S)
            for match in re.finditer(r'match\s*\[(.*?)\]\s*\{\s*vaults\s*=>', term, re.S):
                entries = re.findall(r'\(\s*"([1-9A-HJ-NP-Za-km-z]{50,60})"\s*,\s*(\d+)n?\s*\)', match[1])
                if not entries:
                    raise ValueError("Unsupported genesis vault allocation list")
                for address, raw in entries:
                    raw = amount(int(raw))
                    allocations.append((address, raw, token_decimal(raw, decimals)))
        seen = {}
        for address, raw, _ in allocations:
            if address in seen and seen[address] != raw:
                raise ValueError("Ambiguous genesis allocations for repeated address")
            seen[address] = raw
        bonds = []
        for bond in block_info.get("bonds", []):
            raw = amount(bond["stake"])
            if raw:
                bonds.append((bond["validator"], raw, token_decimal(raw, decimals)))
        self._genesis_data_cache = {"allocations": allocations, "bonds": bonds}
        return self._genesis_data_cache

    async def _process_genesis_transfers(self, session, block_info: Dict):
        """Process genesis transfers for initial ASI allocations and validator bonds."""
        logger.info("💰 Processing genesis transfers and validator bonds")

        decimals = block_info.get("token_decimals", settings.native_token_decimals)
        # Extract genesis data dynamically
        genesis_data = await self._extract_genesis_data(block_info)

        if not genesis_data:
            logger.warning("⚠️ Could not extract genesis data dynamically, skipping genesis transfers")
            return

        # Genesis ASI wallet allocations (minting from genesis)
        genesis_allocations = genesis_data['allocations']

        # Create genesis allocation deployments and transfers
        for i, (address, amount_dust, amount_asi) in enumerate(genesis_allocations, 1):
            deploy_id = f"genesis_allocation_{i}"

            # Create genesis allocation deployment
            deployment = Deployment(
                deploy_id=deploy_id,
                block_number=0,
                block_hash=block_info.get("blockHash"),
                deployer="0000000000000000000000000000000000000000000000000000000000000000",
                deployer_address="genesis",
                term=f"Genesis ASI allocation to {address}: {amount_asi:f} ASI",
                timestamp=block_info.get("timestamp"),
                sig=deploy_id,
                deployment_type="genesis_mint",
                errored=False,
                status="included"
            )
            session.add(deployment)

            # Create genesis allocation transfer
            transfer = Transfer(
                token_decimals=decimals, event_index=0,
                timestamp=block_info.get("timestamp", 0),
                deploy_id=deploy_id,
                block_hash=block_info.get("blockHash"),
                block_number=0,
                # Genesis mint
                from_address="genesis",
                from_public_key=None,
                to_address=address,
                amount_dust=amount_dust,
                amount_asi=amount_asi,
                status="genesis_mint"
            )
            session.add(transfer)

        # Genesis validator bonds (staking transactions)
        validator_bonds = genesis_data['bonds']

        # Create genesis bond deployments and transfers
        for i, (validator_pubkey, amount_dust, amount_asi) in enumerate(validator_bonds, 1):
            deploy_id = f"genesis_bond_{i}"

            # Create genesis bond deployment
            deployment = Deployment(
                deploy_id=deploy_id,
                block_number=0,
                block_hash=block_info.get("blockHash"),
                deployer=validator_pubkey,
                deployer_address=public_key_to_asi_address(validator_pubkey),
                term=f"Genesis validator bond: {amount_asi:f} ASI staked",
                timestamp=block_info.get("timestamp"),
                sig=deploy_id,
                deployment_type="genesis_bond",
                errored=False,
                status="included"
            )
            session.add(deployment)

            # Create genesis bond transfer (validator -> PoS contract)
            transfer = Transfer(
                token_decimals=decimals, event_index=0,
                timestamp=block_info.get("timestamp", 0),
                deploy_id=deploy_id,
                block_hash=block_info.get("blockHash"),
                block_number=0,
                from_address=public_key_to_asi_address(validator_pubkey),
                from_public_key=validator_pubkey,
                to_address="1111gW5kkGxHg7xDg6dRkZx2f7qxTizJzaCH9VEM1oJKWRvSX9Sk5",  # PoS contract address
                amount_dust=amount_dust,
                amount_asi=amount_asi,
                status="genesis_bond"
            )
            session.add(transfer)

        logger.info("✅ Genesis transfers created",
                    wallet_allocations=len(genesis_allocations),
                    validator_bonds=len(validator_bonds),
                    total_transfers=len(genesis_allocations) + len(validator_bonds))

    async def _process_genesis_balance_states(self, session, block_info: Dict):
        """Process genesis balance states for initial allocations and validator bonds."""
        logger.info("⚖️ Processing genesis balance states")

        decimals = block_info.get("token_decimals", settings.native_token_decimals)
        # Extract genesis data dynamically
        genesis_data = await self._extract_genesis_data(block_info)

        if not genesis_data:
            logger.warning("⚠️ Could not extract genesis data dynamically, skipping balance states")
            return

        # Genesis ASI wallet allocations (all unbonded initially)
        genesis_allocations = genesis_data['allocations']

        # Add genesis wallet balance states
        # Repeated genesis addresses replace the vault-map entry; never add
        # duplicate balance rows or sum inaccessible overwritten vaults.
        balances = {address: (raw, human) for address, raw, human in genesis_allocations}
        for address, (amount_dust, amount_asi) in balances.items():
            balance_state = BalanceState(
                token_decimals=decimals,
                address=address,
                block_hash=block_info.get("blockHash"),
                block_number=0,
                unbonded_balance_dust=amount_dust,
                unbonded_balance_asi=amount_asi,
                bonded_balance_dust=0,
                bonded_balance_asi=0
            )
            session.add(balance_state)

        # Validator balance states (they staked at genesis, so all bonded)
        validator_bonds = genesis_data['bonds']

        # Add validator balance states
        for validator_pubkey, amount_dust, amount_asi in validator_bonds:
            balance_state = BalanceState(
                token_decimals=decimals,
                address=validator_pubkey,
                block_hash=block_info.get("blockHash"),
                block_number=0,
                unbonded_balance_dust=0,
                unbonded_balance_asi=0,
                bonded_balance_dust=amount_dust,
                bonded_balance_asi=amount_asi
            )
            session.add(balance_state)

        # PoS contract balance state (holds all bonded ASI)
        total_bonded_dust = sum(bond[1] for bond in validator_bonds)
        total_bonded_asi = token_decimal(total_bonded_dust, decimals)

        pos_balance_state = BalanceState(
            token_decimals=decimals,
            address="1111gW5kkGxHg7xDg6dRkZx2f7qxTizJzaCH9VEM1oJKWRvSX9Sk5",
            block_hash=block_info.get("blockHash"),
            block_number=0,
            unbonded_balance_dust=0,
            unbonded_balance_asi=0,
            bonded_balance_dust=total_bonded_dust,
            bonded_balance_asi=total_bonded_asi
        )
        session.add(pos_balance_state)

        logger.info("✅ Genesis balance states created",
                    wallet_balances=len(genesis_allocations),
                    validator_balances=len(validator_bonds),
                    total_states=len(genesis_allocations) + len(validator_bonds) + 1)
