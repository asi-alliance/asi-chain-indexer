"""Alembic environment using the existing asyncpg/SQLAlchemy stack."""

import asyncio

from alembic import context
from alembic.util import CommandError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.migration_support import database_url, prepare_connection


def run(connection):
    prepare_connection(connection)
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise CommandError(
        "Offline SQL generation is not supported; use a disposable database."
    )
provided = context.config.attributes.get("connection")
if provided is not None:
    run(provided)
else:
    asyncio.run(online())
