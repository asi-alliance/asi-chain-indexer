"""Database connection and session management."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.models import Base

# indexer_state keys holding each alert kind's last delivery time
ALERT_STATE_PREFIX = "alert_last_sent:"


class Database:
    """Database connection manager."""
    
    def __init__(self, database_url: str = None):
        self.database_url = database_url or str(settings.database_url)
        # Convert postgresql:// to postgresql+asyncpg:// for async support
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace("postgresql://", "postgresql+asyncpg://")
        
        self.engine = None
        self.session_factory = None
        self.pool = None
    
    async def connect(self):
        """Initialize database connections."""
        # Create async engine
        self.engine = create_async_engine(
            self.database_url,
            echo=False,
            pool_size=settings.database_pool_size,
            pool_timeout=settings.database_pool_timeout,
            pool_pre_ping=True,
        )
        
        # Create session factory
        self.session_factory = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        # Create asyncpg pool for raw queries
        self.pool = await asyncpg.create_pool(
            self.database_url.replace("postgresql+asyncpg://", "postgresql://"),
            min_size=5,
            max_size=settings.database_pool_size,
            timeout=settings.database_pool_timeout,
        )
    
    async def disconnect(self):
        """Close database connections."""
        if self.pool:
            await self.pool.close()
        if self.engine:
            await self.engine.dispose()
    
    async def create_tables(self):
        """Create all database tables."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    async def drop_tables(self):
        """Drop all database tables (use with caution!)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    
    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
    
    async def execute_raw(self, query: str, *args):
        """Execute a raw SQL query."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)
    
    async def get_last_indexed_block(self) -> int:
        """Get the last block height whose complete sibling set was indexed."""
        query = """
            SELECT value::bigint AS block_number
            FROM indexer_state
            WHERE key = 'last_indexed_block'
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query)
            return row["block_number"] if row else -1

    async def set_last_indexed_block(self, block_number: int) -> None:
        """Record the last height whose complete sibling set was indexed."""
        query = """
            INSERT INTO indexer_state (key, value, updated_at)
            VALUES ('last_indexed_block', $1, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, str(block_number))


class AlertThrottleStore:
    """Keeps each alert kind's last delivery time in indexer_state, so the
    throttle window survives a restart.

    Each call opens its own short-lived connection rather than borrowing from
    db.pool: the fatal exit path runs in a fresh event loop, where db.pool is
    either not created yet or bound to the closed loop, and a pool held up by
    stuck queries would otherwise delay the very alert that reports them.
    """

    def __init__(self, database_url: str = None, timeout: float = None):
        self.dsn = (database_url or str(settings.database_url)).replace(
            "postgresql+asyncpg://", "postgresql://"
        )
        self.timeout = timeout or settings.alert_store_timeout_sec

    async def _run(self, operation):
        """Run `operation(conn)` on a connection of its own, which never outlives
        the call by more than `timeout`."""
        conn = await asyncpg.connect(self.dsn, timeout=self.timeout)
        try:
            result = await operation(conn)
        except BaseException:
            conn.terminate()
            raise
        await conn.close(timeout=self.timeout)
        return result

    async def load_alert_last_sent(self) -> Dict[str, float]:
        """Last delivery time (unix seconds) of each alert kind, keyed by kind."""
        query = """
            SELECT key, value
            FROM indexer_state
            WHERE starts_with(key, $1)
        """
        rows = await self._run(lambda conn: conn.fetch(query, ALERT_STATE_PREFIX))
        return {
            row["key"][len(ALERT_STATE_PREFIX):]: float(row["value"]) for row in rows
        }

    async def save_alert_last_sent(self, kind: str, sent_at: float) -> None:
        """Record when an alert kind was last delivered."""
        query = """
            INSERT INTO indexer_state (key, value, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """
        await self._run(
            lambda conn: conn.execute(query, ALERT_STATE_PREFIX + kind, str(sent_at))
        )


# Global database instance
db = Database()
