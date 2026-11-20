"""Explicit schema migration commands; never invoked by normal indexer startup."""

import asyncio

import click
from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.migration_support import (
    REVISIONS,
    alembic_config,
    database_url,
    prepare_connection,
    validate_adoption,
)


def execute(connection, action, revision=None):
    prepare_connection(connection)
    config = alembic_config(connection)
    current = MigrationContext.configure(connection).get_current_revision()
    if action == "status":
        click.echo(current or "unversioned")
    elif action == "upgrade":
        command.upgrade(config, "head")
        click.echo(
            "Schema is at head ("
            + ScriptDirectory.from_config(config).get_current_head()
            + "). History repair and Hasura metadata are separate steps."
        )
    else:
        if current is not None:
            raise CommandError(
                "Database already has an Alembic revision; use upgrade or status."
            )
        validate_adoption(connection, revision)
        command.stamp(config, revision)
        click.echo(
            "Adopted existing SQL revision " + revision + "; no SQL migration replayed."
        )


async def run(action, revision=None):
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(execute, action, revision)
    finally:
        await engine.dispose()


@click.group()
def main():
    """Use DATABASE_URL for a backed-up database with indexer writes stopped."""


def invoke(action, revision=None):
    try:
        asyncio.run(run(action, revision))
    except CommandError as error:
        raise click.ClickException(str(error)) from None
    except Exception:
        # Driver exceptions can include connection details or SQL parameter values.
        raise click.ClickException(
            "Migration command failed; transaction rolled back. "
            "Check database access/schema with the operator."
        ) from None


@main.command()
def status():
    """Show the recorded revision (or unversioned)."""
    invoke("status")


@main.command()
def upgrade():
    """Apply pending forward revisions atomically; never adopt implicitly."""
    invoke("upgrade")


@main.command()
@click.option("--revision", required=True, type=click.Choice(REVISIONS))
def adopt(revision):
    """Validate and record an already applied SQL revision without replaying it."""
    invoke("adopt", revision)


if __name__ == "__main__":
    main()
