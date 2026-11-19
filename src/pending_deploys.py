"""Pending deploys read from the validators' deploy buffers.

A deploy reaches only the validator that a client sent it to: the node does not
gossip deploys, and a read-only node (observer) rejects them. A network-wide
pending view therefore has to read every validator that accepts deploys. The
indexer reads the validators listed in PENDING_DEPLOYS_NODES; with an empty
list the feature is off and the pending tables are cleared.

The poller runs as its own task, apart from the block sync loop, because a
deploy stays in a buffer only until its validator proposes the next block.
It writes to the database only when the merged snapshot changes, so an idle
network causes no row writes. `fetched_at` is the time the indexer first saw
a deploy. The `pending_deploys_status` row in `indexer_state` is the freshness
signal: it is rewritten with every change and at least every HEARTBEAT_SECONDS.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

import structlog
from prometheus_client import Counter, Gauge
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert

from src.addr import convert_to_asi_address
from src.alerts import AlertEvent, AlertKind, AlertService
from src.amounts import amount
from src.config import Node, settings
from src.database import db
from src.models import PendingDeploy
from src.time_utils import utc_now

logger = structlog.get_logger(__name__)

STATUS_KEY = "pending_deploys_status"
# Kept for explorers that read the pre-cap total from its own key.
TOTAL_KEY = "pending_deploys_total_available"
HEARTBEAT_SECONDS = 10.0
SNAPSHOT_TTL_SECONDS = 30.0
INSERT_BATCH_ROWS = 500

pending_deploys_count = Gauge(
    "indexer_pending_deploys",
    "Deploys in the last merged pending-deploy snapshot of the configured validators",
)
pending_nodes_configured = Gauge(
    "indexer_pending_deploy_nodes_configured",
    "Validators listed in PENDING_DEPLOYS_NODES",
)
pending_nodes_reachable = Gauge(
    "indexer_pending_deploy_nodes_reachable",
    "Validators whose deploy buffer was read in the last pending-deploy refresh",
)


pending_refresh_failures = Counter(
    "indexer_pending_deploy_refresh_failures_total",
    "Refreshes that failed or could not reach any configured validator",
)
pending_refresh_duration = Gauge(
    "indexer_pending_deploy_refresh_duration_seconds",
    "Time spent reading and storing the last pending-deploy refresh",
)


def node_label(node: Node) -> str:
    host, port = node
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class Snapshot:
    """One validator's getPendingDeploys response."""

    deploys: Tuple[dict, ...]
    total_available: int


@dataclass(frozen=True)
class ValidatorSnapshot(Snapshot):
    expires_at: float  # Monotonic deadline measured when the read succeeds.


def merge(snapshots: Sequence[Snapshot]) -> Tuple[Dict[str, dict], int]:
    """Merge validator snapshots by deploy signature.

    Returns the deploys by signature and the total before the node caps. A
    deploy held by several validators counts once. It is marked rejected only
    when every validator that holds it reports it from the rejected-recovery
    buffer: a fresh copy anywhere can still be proposed as is.
    """
    merged: Dict[str, dict] = {}
    beyond_cap = 0
    for snapshot in snapshots:
        beyond_cap += max(0, snapshot.total_available - len(snapshot.deploys))
        for deploy in snapshot.deploys:
            sig = deploy.get("sig")
            if not sig:
                continue
            known = merged.get(sig)
            if known is None or (known.get("isRejected") and not deploy.get("isRejected")):
                merged[sig] = deploy
    return merged, len(merged) + beyond_cap


def _row(deploy: dict, seen_at) -> dict:
    deployer = deploy.get("deployer", "")
    return {
        "sig": deploy["sig"],
        "deployer": deployer,
        "deployer_address": convert_to_asi_address(deployer, deploy),
        "term": deploy.get("term", ""),
        "timestamp": deploy.get("timestamp", 0),
        "phlo_price": amount(deploy.get("phloPrice", 1)),
        "phlo_limit": deploy.get("phloLimit", 1000000),
        "valid_after_block_number": deploy.get("validAfterBlockNumber"),
        "shard_id": deploy.get("shardId"),
        "sig_algorithm": deploy.get("sigAlgorithm", "secp256k1"),
        "language": deploy.get("language"),
        # proto 0 = no expiration
        "expiration_timestamp": deploy.get("expirationTimestamp") or None,
        "is_rejected": bool(deploy.get("isRejected", False)),
        "fetched_at": seen_at,
    }


async def _set_state(session, key: str, value: str) -> None:
    await session.execute(
        text("""
             INSERT INTO indexer_state (key, value, updated_at)
             VALUES (:key, :value, NOW())
             ON CONFLICT (key) DO UPDATE SET value = :value, updated_at = NOW()
             """),
        {"key": key, "value": value},
    )


async def clear_pending_snapshot() -> None:
    """Remove the pending view when the feature is off.

    Without the status row, explorers treat pending deploys as not configured
    instead of showing a snapshot that nothing refreshes.
    """
    async with db.session() as session:
        await session.execute(delete(PendingDeploy))
        await session.execute(
            text("DELETE FROM indexer_state WHERE key IN (:status, :total)"),
            {"status": STATUS_KEY, "total": TOTAL_KEY},
        )
        await session.commit()
    pending_deploys_count.set(0)
    pending_nodes_configured.set(0)
    pending_nodes_reachable.set(0)


class PendingDeploysPoller:
    """Reads the configured validators' deploy buffers on a fixed interval."""

    def __init__(
        self,
        nodes: Sequence[Node],
        interval: float,
        timeout: float,
        client_factory: Optional[Callable[[Node], object]] = None,
        clock: Callable[[], float] = time.monotonic,
        alerts: Optional[AlertService] = None,
    ):
        if not nodes:
            raise ValueError("PendingDeploysPoller needs at least one node")
        if client_factory is None:
            from src.grpc_node_client import GrpcNodeClient

            def client_factory(node: Node):
                return GrpcNodeClient(node_host=node[0], grpc_port=node[1])

        self.nodes = list(nodes)
        self.interval = interval
        self.timeout = timeout
        self._clock = clock
        self._alerts = alerts
        self._clients = {node: client_factory(node) for node in self.nodes}
        self._last: Dict[Node, ValidatorSnapshot] = {}
        self._reachable: Dict[Node, Optional[bool]] = {node: None for node in self.nodes}
        # What the database holds after the last successful write: sig -> is_rejected.
        self._written: Optional[Dict[str, bool]] = None
        self._written_status: Optional[str] = None
        self._written_total: Optional[int] = None
        self._status_written_at: Optional[float] = None
        self._last_error: Optional[str] = None
        pending_nodes_configured.set(len(self.nodes))

    async def run(self) -> None:
        """Refresh until cancelled. Errors are logged when they change, not every tick."""
        logger.info(
            "Pending deploys enabled",
            nodes=[node_label(node) for node in self.nodes],
            interval_seconds=self.interval,
        )
        consecutive_failures = 0
        while True:
            started = self._clock()
            try:
                reachable = await self.refresh()
                failure = None if reachable else "No configured validator returned its deploy buffer"
                if reachable and self._last_error is not None:
                    logger.info("Pending deploys refresh recovered")
                    self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                pending_refresh_failures.inc()
                failure = f"Pending deploy refresh raised {type(e).__name__}"
                message = f"{type(e).__name__}: {e}"
                if message != self._last_error:
                    logger.error("Pending deploys refresh failed", error=message, exc_info=True)
                    self._last_error = message
            consecutive_failures = consecutive_failures + 1 if failure else 0
            if consecutive_failures == settings.sync_stall_threshold and self._alerts is not None:
                self._alerts.notify(
                    AlertEvent(AlertKind.PENDING_DEPLOYS_STALLED, failure)
                    .with_context("consecutive_failures", consecutive_failures)
                )
            elapsed = self._clock() - started
            pending_refresh_duration.set(elapsed)
            await asyncio.sleep(max(0.0, self.interval - elapsed))

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception as e:  # closing must not hide the shutdown reason
                logger.warning("Failed to close pending deploys client", error=str(e))

    async def refresh(self) -> int:
        """Store the current view and return the number of validators that answered."""
        results = await asyncio.gather(*(self._read(node) for node in self.nodes))
        now = self._clock()
        reachable = 0
        for node, snapshot in zip(self.nodes, results):
            self._mark(node, snapshot is not None)
            if snapshot is not None:
                reachable += 1
                self._last[node] = snapshot
            elif node in self._last and self._last[node].expires_at <= now:
                del self._last[node]
        merged, total = merge([self._last[node] for node in self.nodes if node in self._last])
        pending_nodes_reachable.set(reachable)
        await self._store(merged, total, reachable)
        if reachable == 0:
            pending_refresh_failures.inc()
        pending_deploys_count.set(len(merged))
        return reachable

    async def _read(self, node: Node) -> Optional[ValidatorSnapshot]:
        try:
            data = await self._clients[node].get_pending_deploys(timeout=self.timeout)
        except Exception as e:
            logger.debug("Pending deploys read failed", node=node_label(node), error=str(e))
            return None
        if data is None:
            return None
        deploys = tuple(data.get("deploys", []))
        return ValidatorSnapshot(
            deploys, int(data.get("totalAvailable", len(deploys))),
            expires_at=self._clock() + SNAPSHOT_TTL_SECONDS,
        )

    def _mark(self, node: Node, ok: bool) -> None:
        previous = self._reachable[node]
        self._reachable[node] = ok
        if ok and previous is False:
            logger.info("Pending deploys node reachable again", node=node_label(node))
        elif not ok and previous is not False:
            logger.warning(
                "Pending deploys node unreachable",
                node=node_label(node),
                has_snapshot=node in self._last,
            )

    async def _store(self, merged: Dict[str, dict], total: int, reachable: int) -> None:
        current = {sig: bool(d.get("isRejected", False)) for sig, d in merged.items()}
        status = json.dumps(
            {
                "nodes": len(self.nodes),
                "reachable": reachable,
                "count": len(current),
                "total_available": total,
                "interval_ms": round(self.interval * 1000),
            },
            sort_keys=True,
        )
        rows_changed = current != self._written
        heartbeat_due = (
            self._status_written_at is None
            or self._clock() - self._status_written_at >= HEARTBEAT_SECONDS
        )
        if not rows_changed and status == self._written_status and not heartbeat_due:
            return

        async with db.session() as session:
            if rows_changed:
                await self._write_rows(session, merged, current)
            if total != self._written_total:
                await _set_state(session, TOTAL_KEY, str(total))
            await _set_state(session, STATUS_KEY, status)
            await session.commit()

        if rows_changed:
            logger.info(
                "Pending deploys changed",
                count=len(current),
                total_available=total,
                rejected=sum(current.values()),
                reachable_nodes=reachable,
            )
        self._written = current
        self._written_total = total
        self._written_status = status
        self._status_written_at = self._clock()

    async def _write_rows(self, session, merged: Dict[str, dict], current: Dict[str, bool]) -> None:
        # Delete by the current set rather than the last write, so the first
        # write after a restart also removes rows left by the previous process.
        await session.execute(delete(PendingDeploy).where(PendingDeploy.sig.not_in(list(current))))
        written = self._written or {}
        changed = [sig for sig, rejected in current.items() if written.get(sig) != rejected]
        if not changed:
            return
        seen_at = utc_now()
        # Each node returns up to 1000 deploys; batches keep a statement well
        # below the PostgreSQL bind-parameter limit.
        for start in range(0, len(changed), INSERT_BATCH_ROWS):
            batch = changed[start:start + INSERT_BATCH_ROWS]
            statement = insert(PendingDeploy).values([_row(merged[sig], seen_at) for sig in batch])
            # A signature identifies the signed deploy data, so only the buffer
            # flag can change. fetched_at keeps the time the deploy was first seen.
            statement = statement.on_conflict_do_update(
                index_elements=[PendingDeploy.sig],
                set_={"is_rejected": statement.excluded.is_rejected},
            )
            await session.execute(statement)
