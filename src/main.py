"""Main entry point for the indexer."""

import asyncio
import signal
import sys
from typing import Optional

import click
import structlog
from dotenv import load_dotenv

from src.alerts import AlertedError, AlertEvent, AlertKind, AlertService, describe_error
from src.config import settings
from src.monitoring import MonitoringServer
from src.block_indexer import BlockIndexer
from src.database import AlertThrottleStore, db

# Load environment variables
load_dotenv()

# Add basic logging setup first
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer() if settings.log_format == "json" else structlog.dev.ConsoleRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


class IndexerService:
    """Main service orchestrator."""

    def __init__(self):
        self.indexer: Optional[BlockIndexer] = None
        self.monitoring: Optional[MonitoringServer] = None
        self.alerts: Optional[AlertService] = None
        self.shutdown_event = asyncio.Event()
        self._indexer_died = False

    async def start(self):
        """Start all services."""
        # Mask password in database URL for logging
        db_url_masked = settings.database_url
        if "@" in db_url_masked and ":" in db_url_masked.split("@")[0]:
            # Extract and mask password
            parts = db_url_masked.split("@")
            creds = parts[0].split("//")[1]
            if ":" in creds:
                user, _ = creds.split(":", 1)
                db_url_masked = db_url_masked.replace(creds, f"{user}:***")

        logger.info(
            "🚀 Starting ASI-Chain Enhanced Indexer",
            node_host=settings.node_host,
            grpc_port=settings.grpc_port,
            http_port=settings.http_port,
            database_url=db_url_masked,
            sync_interval=settings.sync_interval,
            batch_size=settings.batch_size
        )

        logger.info(
            "🌐 Service endpoints will be available at:",
            metrics="http://localhost:9090",
            graphql="http://localhost:8080",
            console="http://localhost:8080/console"
        )

        # the throttle state is kept in the database, so a restart loop does not
        # re-alert on every start
        self.alerts = AlertService(settings, store=AlertThrottleStore())

        self.indexer = BlockIndexer(alerts=self.alerts)

        # Create monitoring server
        if settings.enable_health_check or settings.enable_metrics:
            self.monitoring = MonitoringServer(self.indexer)
            await self.monitoring.start()

        # Start indexer
        indexer_task = asyncio.create_task(self.indexer.start())
        indexer_task.add_done_callback(self._on_indexer_task_done)

        # Wait for shutdown signal
        await self.shutdown_event.wait()

        await self._alert_if_indexer_died(indexer_task)

        # Stop services
        await self.stop()

        # Cancel indexer task
        indexer_task.cancel()
        try:
            await indexer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # already reported by _alert_if_indexer_died

        if self._indexer_died:
            # non-zero exit, so the container's restart policy treats it as a crash;
            # AlertedError keeps main() from sending INDEXER_STOPPED a second time
            error = self._task_error(indexer_task)
            raise AlertedError(
                describe_error(error) if error else "sync loop exited"
            ) from error

    def _on_indexer_task_done(self, task: asyncio.Task):
        """Notice a sync loop that ended on its own.

        Nothing else observes this task, so without it a crashed loop leaves the
        process up and the health endpoint still reporting healthy.
        """
        if self.shutdown_event.is_set():
            return

        self._indexer_died = True
        self.shutdown_event.set()

    async def _alert_if_indexer_died(self, task: asyncio.Task):
        """Report an unrequested exit, then let the process go so the container's
        restart policy can bring it back."""
        if not self._indexer_died:
            return

        error = self._task_error(task)
        logger.error("Indexer stopped unexpectedly", error=str(error) if error else None)
        if isinstance(error, AlertedError):
            return  # the cause has been alerted on already

        await self.alerts.notify_and_wait(
            AlertEvent(
                AlertKind.INDEXER_STOPPED,
                describe_error(error) if error else "sync loop exited"
            )
        )

    @staticmethod
    def _task_error(task: asyncio.Task) -> Optional[BaseException]:
        if not task.done() or task.cancelled():
            return None
        return task.exception()

    async def stop(self):
        """Stop all services."""
        logger.info("Shutting down services")

        if self.indexer:
            await self.indexer.stop()

    def handle_signal(self, sig, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {sig}")
        self.shutdown_event.set()


@click.command()
@click.option(
    "--reset",
    is_flag=True,
    help="Reset database before starting (WARNING: deletes all data)"
)
@click.option(
    "--start-from",
    type=int,
    help="Start indexing from specific block number"
)
def main(reset: bool, start_from: Optional[int]):
    """ASI-Chain Indexer - Blockchain data synchronization service."""
    if reset:
        click.confirm(
            "⚠️  This will DELETE all indexed data. Are you sure?",
            abort=True
        )
        asyncio.run(reset_database())
        click.echo("✅ Database reset complete")

    if start_from is not None:
        # Update start block in environment
        settings.start_from_block = start_from
        logger.info(f"Starting from block {start_from}")

    # Run the service
    service = IndexerService()

    # Setup signal handlers
    signal.signal(signal.SIGINT, service.handle_signal)
    signal.signal(signal.SIGTERM, service.handle_signal)

    try:
        asyncio.run(service.start())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except AlertedError as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        asyncio.run(
            AlertService(settings, store=AlertThrottleStore()).notify_and_wait(
                AlertEvent(AlertKind.INDEXER_STOPPED, describe_error(e))
            )
        )
        sys.exit(1)


async def reset_database():
    """Reset the database (drop and recreate tables)."""
    from src.database import db

    logger.warning("Resetting database")
    await db.connect()
    await db.drop_tables()
    await db.create_tables()
    await db.disconnect()


if __name__ == "__main__":
    import sys

    print("Starting ASI-Chain Indexer...", flush=True)
    sys.stdout.flush()
    sys.stderr.write("STDERR: Starting indexer\n")
    sys.stderr.flush()
    main()
