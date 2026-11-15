"""Configuration management for the indexer."""

from typing import Literal, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


Node = tuple[str, int]


def parse_nodes(value: str) -> list[Node]:
    """Parse a comma-separated list of host:port gRPC addresses.

    IPv6 hosts use brackets ([::1]:40402). Duplicates are dropped, order kept.
    Raises ValueError for an entry without a host or with an invalid port.
    """
    nodes: list[Node] = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if "://" in entry or any(c.isspace() for c in entry):
            raise ValueError(f"PENDING_DEPLOYS_NODES entry {entry!r} must be host:port")
        if entry.startswith("["):
            host, sep, port = entry[1:].partition("]:")
        else:
            host, sep, port = entry.rpartition(":")
            if ":" in host:
                raise ValueError(f"PENDING_DEPLOYS_NODES entry {entry!r}: put an IPv6 host in brackets")
        if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError(f"PENDING_DEPLOYS_NODES entry {entry!r} must be host:port with a port 1-65535")
        node = (host, int(port))
        if node not in nodes:
            nodes.append(node)
    return nodes


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    native_token_decimals: int = Field(default=18, ge=0, le=77)

    node_protocol_profile: Literal["classic", "uint256"] = "uint256"

    # Node Configuration
    node_timeout: int = Field(
        default=30,
        description="HTTP/gRPC request timeout in seconds"
    )

    http_port: int = Field(
        default=40453,
        description="HTTP port for status queries"
    )

    grpc_port: int = Field(
        default=40452,
        description="GRPC port for status queries"
    )

    grpc_max_receive_message_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1,
        le=2**31 - 1,
        description="Maximum size of a gRPC response message in bytes"
    )

    node_host: str = Field(
        default="localhost",
        description="host port for status queries"
    )

    fault_tolerance_threshold: float = Field(
        default=0.67,
        description="BFT safety threshold for consensus status, mirrors the node's own "
                     "casper.fault-tolerance-threshold (defaults.conf); not exposed via any API, "
                     "so it must be kept in sync with the shard's actual config by hand"
    )

    # Database Configuration
    database_url: str = Field(
        default="postgresql://indexer:indexer_pass@localhost:5432/asichain",
        description="PostgreSQL connection URL"
    )
    database_pool_size: int = Field(
        default=20,
        description="Database connection pool size"
    )
    database_pool_timeout: int = Field(
        default=10,
        description="Database pool timeout in seconds"
    )

    # Sync Configuration
    sync_interval: int = Field(
        default=5,
        description="Seconds between sync cycles"
    )
    delay_before_node: float = Field(
        default=0.02,
        description="small delay to avoid overwhelming the node"
    )
    batch_size: int = Field(
        default=100,
        description="Number of blocks to process per batch"
    )
    start_from_block: int = Field(
        default=0,
        description="Block number to start syncing from"
    )

    # Monitoring
    monitoring_port: int = Field(
        default=9090,
        description="Port for metrics and health endpoints"
    )
    health_check_interval: int = Field(
        default=60,
        description="Health check interval in seconds"
    )

    # Alerts (Mattermost incoming webhook)
    alerts_enabled: bool = Field(
        default=False,
        description="Enable Mattermost alerts (requires MATTERMOST_WEBHOOK_URL)"
    )
    mattermost_webhook_url: Optional[SecretStr] = Field(
        default=None,
        description="Mattermost incoming webhook URL (secret, never logged)"
    )
    mattermost_channel: Optional[str] = Field(
        default=None,
        description="Override the webhook's default channel"
    )
    mattermost_username: str = Field(
        default="asi-indexer",
        description="Username alerts are posted under"
    )
    alert_throttle_sec: int = Field(
        default=3600,
        ge=0,
        description="Per-alert-kind throttle window in seconds; repeats inside it are "
                    "collapsed into a suppressed count"
    )
    alert_timeout_sec: int = Field(
        default=5,
        ge=1,
        description="Timeout in seconds for a single webhook delivery attempt"
    )
    alert_max_text_len: int = Field(
        default=300,
        ge=20,
        description="Longest error or context value posted to the channel; longer "
                    "text is cut off"
    )
    alert_store_timeout_sec: float = Field(
        default=1.0,
        gt=0,
        description="Timeout in seconds for each read or write of the persisted "
                    "throttle state, so a dead database never holds up an alert"
    )
    alert_environment: str = Field(
        default="unknown",
        description="Environment label shown in the alert title, e.g. internal-dev or devnet"
    )
    sync_stall_threshold: int = Field(
        default=3,
        ge=1,
        description="Consecutive failed sync cycles before the loop is considered stalled"
    )
    node_unreachable_cycles: int = Field(
        default=3,
        ge=1,
        description="Consecutive sync cycles the node may return no data (no last "
                    "finalized block, or no blocks for a non-empty range) before alerting"
    )
    lag_alert_blocks: int = Field(
        default=500,
        ge=0,
        description="Lag depth (in blocks) that, held for lag_alert_cycles without net "
                    "progress, is treated as falling behind rather than a backlog being "
                    "worked through"
    )
    lag_alert_cycles: int = Field(
        default=60,
        ge=2,
        description="Cycles of sustained lag deeper than lag_alert_blocks before alerting"
    )
    lag_recovery_ratio: float = Field(
        default=0.9,
        gt=0,
        le=1,
        description="Lag counts as recovering when the recent half of the window is at "
                    "most this fraction of the older half; a smaller value demands "
                    "faster catch-up before the alert is held back"
    )
    cursor_stuck_cycles: int = Field(
        default=3,
        ge=1,
        description="Cycles the sync cursor may sit at the same height, retrying a "
                    "failing block, before that stops looking transient"
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level"
    )
    log_format: str = Field(
        default="json",
        description="Log format (json or text)"
    )

    # Feature Flags
    enable_asi_transfer_extraction: bool = Field(
        default=True,
        description="Enable ASI transfer extraction from deployments"
    )
    # Pending deploys. A deploy stays only in the buffer of the validator that
    # received it (no gossip; an observer rejects deploys), so the pending view
    # reads every listed validator. An empty list turns the feature off.
    pending_deploys_nodes: str = Field(
        default="",
        description="Comma-separated host:port gRPC addresses of the validators that accept deploys; "
                    "empty disables pending deploys"
    )
    pending_deploys_interval: float = Field(
        default=1.0,
        ge=0.2,
        le=60,
        description="Seconds between pending-deploy reads of the listed validators"
    )
    pending_deploys_timeout: float = Field(
        default=2.0,
        gt=0,
        le=30,
        description="gRPC timeout in seconds for one validator's pending-deploy read"
    )
    enable_metrics: bool = Field(
        default=True,
        description="Enable Prometheus metrics"
    )
    enable_health_check: bool = Field(
        default=True,
        description="Enable health check endpoint"
    )

    # Read cache for /status counts and transfer lists; dropped on every committed block
    read_cache_enabled: bool = Field(
        default=True,
        description="Serve repeated /status counts and transfer list reads from process memory"
    )
    read_cache_max_entries: int = Field(
        default=256,
        ge=1,
        description="Maximum cached read results; the least recently used entry is dropped first"
    )

    # Hasura Configuration (optional, not used by indexer but may be in env)
    hasura_admin_secret: Optional[SecretStr] = Field(
        default=None,
        description="Hasura admin secret (not used by indexer)"
    )

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        try:
            make_url(value)
        except ArgumentError:
            raise ValueError("DATABASE_URL is not a valid database URL") from None
        return value

    @field_validator("pending_deploys_nodes")
    @classmethod
    def _check_pending_deploys_nodes(cls, value: str) -> str:
        parse_nodes(value)
        return value

    @property
    def pending_deploy_nodes(self) -> list[tuple[str, int]]:
        """Parsed PENDING_DEPLOYS_NODES; empty when pending deploys are off."""
        return parse_nodes(self.pending_deploys_nodes)

    @model_validator(mode="after")
    def _require_webhook_when_alerting(self) -> "Settings":
        webhook = self.mattermost_webhook_url
        if self.alerts_enabled and not (webhook and webhook.get_secret_value().strip()):
            raise ValueError("ALERTS_ENABLED is true but MATTERMOST_WEBHOOK_URL is empty")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="allow",  # allow extra fields in .env
        hide_input_in_errors=True,
    )


# Global settings instance
settings = Settings()
