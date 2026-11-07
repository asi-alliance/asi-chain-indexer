"""Transactional SQL revisions and explicit adoption of existing public schemas."""

import json
import os
from pathlib import Path

from alembic import op
from alembic.config import Config
from alembic.util import CommandError
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
LOCK_KEY = 18387725196134226
REVISIONS = ("000", "001", "002", "003")

# SQL definitions, not data, are inspected. Extension-owned objects are excluded.
CATALOG_SQL = """
WITH relations AS (
    SELECT c.oid, c.relname, c.relkind FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v')
      AND c.relname != 'alembic_version'
), objects AS (
    SELECT 'relation:' || relname AS key, relkind::text AS definition FROM relations
    UNION ALL
    SELECT 'column:' || r.relname || ':' || a.attname,
           json_build_array(format_type(a.atttypid, a.atttypmod), a.attnotnull,
                            a.attgenerated, pg_get_expr(d.adbin, d.adrelid))::text
    FROM relations r JOIN pg_attribute a ON a.attrelid = r.oid
    LEFT JOIN pg_attrdef d ON d.adrelid = r.oid AND d.adnum = a.attnum
    WHERE a.attnum > 0 AND NOT a.attisdropped
    UNION ALL
    SELECT 'constraint:' || r.relname || ':' || c.conname, pg_get_constraintdef(c.oid)
    FROM relations r JOIN pg_constraint c ON c.conrelid = r.oid
    UNION ALL
    SELECT 'index:' || i.relname,
           json_build_array(pg_get_indexdef(i.oid), x.indisvalid, x.indisready)::text
    FROM relations r JOIN pg_index x ON x.indrelid = r.oid
    JOIN pg_class i ON i.oid = x.indexrelid
    UNION ALL
    SELECT 'view:' || relname, pg_get_viewdef(oid) FROM relations WHERE relkind = 'v'
    UNION ALL
    SELECT 'trigger:' || r.relname || ':' || t.tgname,
           json_build_array(pg_get_triggerdef(t.oid), t.tgenabled)::text
    FROM relations r JOIN pg_trigger t ON t.tgrelid = r.oid WHERE NOT t.tgisinternal
    UNION ALL
    SELECT 'function:' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
           pg_get_functiondef(p.oid)
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.prokind = 'f'
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                      AND d.objid = p.oid AND d.deptype = 'e')
)
SELECT key, definition FROM objects ORDER BY key
"""


def database_url():
    load_dotenv()
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise CommandError("Set DATABASE_URL explicitly for migration commands.")
    try:
        url = make_url(value)
    except Exception:
        raise CommandError("DATABASE_URL is invalid.") from None
    if url.drivername not in ("postgresql", "postgresql+asyncpg"):
        raise CommandError("Migrations require PostgreSQL with asyncpg.")
    return url.set(drivername="postgresql+asyncpg")


def alembic_config(connection=None):
    config = Config(str(ROOT / "alembic.ini"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def prepare_connection(connection):
    connection.exec_driver_sql("SET LOCAL search_path TO public, pg_catalog")
    connection.exec_driver_sql("SET LOCAL lock_timeout TO '5s'")
    try:
        seconds = int(os.environ.get("MIGRATION_STATEMENT_TIMEOUT_SECONDS", "300"))
    except ValueError:
        raise CommandError(
            "MIGRATION_STATEMENT_TIMEOUT_SECONDS must be an integer."
        ) from None
    if not 1 <= seconds <= 86400:
        raise CommandError(
            "MIGRATION_STATEMENT_TIMEOUT_SECONDS must be between 1 and 86400."
        )
    connection.execute(
        text("SELECT set_config('statement_timeout', :value, true)"),
        {"value": str(seconds * 1000)},
    )
    if not connection.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_KEY}
    ):
        raise CommandError(
            "Another migration command holds the database migration lock."
        )


def catalog(connection):
    return dict(connection.execute(text(CATALOG_SQL)).all())


def validate_adoption(connection, revision):
    if revision not in REVISIONS:
        raise CommandError(
            "Select an explicit existing SQL revision: 000, 001, 002 or 003."
        )
    expected = json.loads(
        (ROOT / "migration_env" / "profiles" / (revision + ".json")).read_text()
    )
    actual = catalog(connection)
    # Additional unrelated objects/indexes are allowed. Added columns or later owned
    # tables are not: they can indicate a partially applied or misidentified revision.
    profiles = [
        json.loads((ROOT / "migration_env" / "profiles" / (r + ".json")).read_text())
        for r in REVISIONS
    ]
    managed_relations = {
        key for profile in profiles for key in profile if key.startswith("relation:")
    }
    mismatches = {key for key, value in expected.items() if actual.get(key) != value}
    mismatches.update(
        key for key in managed_relations if key in actual and key not in expected
    )
    owned_names = {key.split(":", 1)[1] for key in managed_relations}
    mismatches.update(
        key
        for key in actual
        if key.startswith("column:")
        and key.split(":")[1] in owned_names
        and key not in expected
    )
    if mismatches:
        # Names only: do not log data, connection strings or complete definitions.
        raise CommandError(
            "Schema does not match revision "
            + revision
            + ": "
            + ", ".join(sorted(mismatches)[:8])
        )


def apply_sql(filename):
    connection = op.get_bind()
    if filename.startswith("000_") and connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','v','m','S','f','p') "
            "AND c.relname != 'alembic_version')"
        )
    ):
        raise CommandError(
            "Database is not empty and has no revision. Use explicit adopt first."
        )
    sql = (ROOT / "migrations" / filename).read_text()
    # Existing standalone files keep their BEGIN/COMMIT. Alembic owns the outer
    # transaction here, including every revision marker and the advisory lock.
    if sql.lstrip().startswith("BEGIN;"):
        sql = sql.lstrip()[len("BEGIN;") :].rstrip()
        if not sql.endswith("COMMIT;"):
            raise CommandError("SQL migration has an unexpected transaction boundary.")
        sql = sql[: -len("COMMIT;")]
    connection.connection.dbapi_connection.run_async(lambda driver: driver.execute(sql))
