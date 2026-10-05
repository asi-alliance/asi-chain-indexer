"""Mattermost alerting for critical indexer failures.

Mirrors asi-chain-faucet's alerts service so both projects post the same shape of
message and are configured with the same environment variables.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

import aiohttp
import structlog
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

logger = structlog.get_logger(__name__)

SERVICE_NAME = "asi-indexer"


class AlertedError(RuntimeError):
    """A fatal failure that has already been alerted on.

    Exit paths that report the indexer stopping skip it, so one failure sends one
    message.
    """


def describe_error(error: BaseException) -> str:
    """Alert-safe text for an exception.

    SQLAlchemy renders the failing SQL statement and its parameters into the
    message; the driver error it wraps (`orig`) names the cause without them.
    """
    if isinstance(error, SQLAlchemyError):
        orig = getattr(error, "orig", None)
        if orig is not None:
            error = orig
    return str(error) or type(error).__name__


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


class ThrottleStore(Protocol):
    """Keeps each kind's last delivery time across restarts (unix seconds)."""

    async def load_alert_last_sent(self) -> Dict[str, float]: ...

    async def save_alert_last_sent(self, kind: str, sent_at: float) -> None: ...


class AlertKind(Enum):
    """The only events that raise an alert.

    Throttling is keyed on this, so a storm of failures of the same kind
    collapses into a single message.
    """

    NODE_UNREACHABLE = "node_unreachable"
    DATABASE_UNREACHABLE = "database_unreachable"
    SYNC_STALLED = "sync_stalled"
    CHAIN_REORG_DETECTED = "chain_reorg_detected"
    INDEXER_STOPPED = "indexer_stopped"
    BLOCKS_STUCK = "blocks_stuck"
    SYNC_FALLING_BEHIND = "sync_falling_behind"

    def title(self) -> str:
        return _TITLES[self]


_TITLES = {
    AlertKind.NODE_UNREACHABLE: "Node unreachable",
    AlertKind.DATABASE_UNREACHABLE: "Database unreachable",
    AlertKind.SYNC_STALLED: "Sync stalled",
    AlertKind.CHAIN_REORG_DETECTED: "Chain reorg detected",
    AlertKind.INDEXER_STOPPED: "Indexer stopped",
    AlertKind.BLOCKS_STUCK: "Block sync stuck",
    AlertKind.SYNC_FALLING_BEHIND: "Sync falling behind",
}


@dataclass
class AlertEvent:
    """A single alert occurrence. Carries no secret-bearing fields by construction."""

    kind: AlertKind
    error: str
    context: List[Tuple[str, str]] = field(default_factory=list)

    def with_context(self, key: str, value: Any) -> "AlertEvent":
        self.context.append((key, str(value)))
        return self


@dataclass
class _ThrottleState:
    last_sent: float
    suppressed: int = 0


@dataclass(frozen=True)
class _DisabledConfig:
    alerts_enabled: bool = False
    mattermost_webhook_url: Optional[SecretStr] = None
    mattermost_channel: Optional[str] = None
    mattermost_username: str = SERVICE_NAME
    alert_throttle_sec: int = 3600
    alert_timeout_sec: int = 5
    alert_max_text_len: int = 300
    alert_store_timeout_sec: float = 1.0
    alert_environment: str = "unknown"


class AlertService:
    """Posts short, throttled failure notices to a Mattermost incoming webhook."""

    def __init__(self, config, store: Optional[ThrottleStore] = None):
        webhook = config.mattermost_webhook_url
        self.webhook_url: Optional[str] = (webhook.get_secret_value() if webhook else None) or None
        self.enabled: bool = bool(config.alerts_enabled and self.webhook_url)
        self.channel: Optional[str] = config.mattermost_channel or None
        self.username: Optional[str] = config.mattermost_username or None
        self.environment: str = config.alert_environment
        self.throttle_window: float = float(config.alert_throttle_sec)
        self.request_timeout: float = float(config.alert_timeout_sec)
        self.max_text_len: int = config.alert_max_text_len
        self.store_timeout: float = float(config.alert_store_timeout_sec)

        self._throttle: Dict[AlertKind, _ThrottleState] = {}
        # per kind, held across delivery: a repeat arriving mid-delivery must see
        # its outcome before deciding whether it is throttled
        self._locks: Dict[AlertKind, asyncio.Lock] = {}
        self._store = store
        self._store_loaded = store is None
        # asyncio keeps only weak references to tasks, so an in-flight delivery can be
        # garbage collected mid-request unless we hold onto it
        self._pending: Set[asyncio.Task] = set()

        if self.enabled:
            logger.info(
                "Alerting enabled",
                environment=self.environment,
                throttle_sec=int(self.throttle_window),
            )
        else:
            logger.info("Alerting is disabled")

    @classmethod
    def disabled(cls) -> "AlertService":
        """A no-op service, for callers with no alerting configured."""
        return cls(_DisabledConfig())

    def notify(self, event: AlertEvent) -> None:
        """Fire-and-forget: never blocks the caller, never raises.

        Delivery failures are logged and nothing else.
        """
        if not self.enabled:
            return

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Alert dropped, no running event loop", kind=event.kind.value)
            return

        task = asyncio.create_task(self._process(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def notify_and_wait(self, event: AlertEvent) -> None:
        """Awaitable variant for exit paths, where process teardown would otherwise
        race an in-flight delivery. Bounded by the delivery timeout; never raises.
        """
        if not self.enabled:
            return

        # a margin over the request's own timeout, so that timeout fires first and
        # the outer one only catches a hang around it
        timeout = self.request_timeout + 1.0
        if self._store is not None:
            timeout += 2 * self.store_timeout
        try:
            await asyncio.wait_for(self._process(event), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Alert delivery timed out", kind=event.kind.value)
        except Exception as e:
            logger.warning(
                "Alert delivery failed", kind=event.kind.value, error_type=type(e).__name__
            )

    async def _process(self, event: AlertEvent) -> None:
        """Deliver unless throttled. The throttle window only starts on a delivery
        that succeeded, so a failed one leaves the next occurrence free to retry.
        """
        kind = event.kind
        lock = self._locks.setdefault(kind, asyncio.Lock())
        async with lock:
            await self._load_throttle()

            state = self._throttle.get(kind)
            if state is not None and time.time() - state.last_sent < self.throttle_window:
                state.suppressed += 1
                return

            suppressed = state.suppressed if state is not None else 0
            if not await self._deliver(self._format_message(event, suppressed)):
                return

            sent_at = time.time()
            self._throttle[kind] = _ThrottleState(last_sent=sent_at)
            await self._save_throttle(kind, sent_at)

    async def _load_throttle(self) -> None:
        """Seed the throttle from the store once, so a restart loop does not
        re-alert on every start. Retried on the next alert if it fails."""
        if self._store_loaded:
            return
        try:
            stored = await asyncio.wait_for(
                self._store.load_alert_last_sent(), timeout=self.store_timeout
            )
        except Exception as e:
            logger.debug("Alert throttle state unavailable", error=str(e))
            return

        self._store_loaded = True
        for kind in AlertKind:
            sent_at = stored.get(kind.value)
            state = self._throttle.get(kind)
            if sent_at is not None and (state is None or state.last_sent < sent_at):
                self._throttle[kind] = _ThrottleState(
                    last_sent=sent_at, suppressed=state.suppressed if state else 0
                )

    async def _save_throttle(self, kind: AlertKind, sent_at: float) -> None:
        if self._store is None:
            return
        try:
            await asyncio.wait_for(
                self._store.save_alert_last_sent(kind.value, sent_at),
                timeout=self.store_timeout,
            )
        except Exception as e:
            logger.debug("Could not persist alert throttle state", error=str(e))

    def _format_message(self, event: AlertEvent, suppressed: int) -> str:
        lines = [
            f":rotating_light: **[{self.environment}] {SERVICE_NAME} — {event.kind.title()}**",
            f"- error: {_truncate(event.error, self.max_text_len)}",
        ]
        lines.extend(
            f"- {key}: {_truncate(value, self.max_text_len)}"
            for key, value in event.context
        )
        if suppressed > 0:
            lines.append(
                f"- suppressed: {suppressed} repeat(s) in the previous "
                f"{int(self.throttle_window)}s window"
            )
        return "\n".join(lines) + "\n"

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.request_timeout)
        )

    async def _deliver(self, text: str) -> bool:
        """Post one message. True only when the webhook accepted it."""
        payload: Dict[str, str] = {"text": text}
        if self.username:
            payload["username"] = self.username
        if self.channel:
            payload["channel"] = self.channel

        try:
            async with self._session() as session:
                async with session.post(self.webhook_url, json=payload) as response:
                    if response.status >= 400:
                        logger.warning(
                            "Alert delivery rejected", status=response.status
                        )
                        return False
                    return True
        except asyncio.TimeoutError:
            logger.warning("Alert delivery timed out")
        except Exception as e:
            logger.warning("Alert delivery failed", error_type=type(e).__name__)
        return False
