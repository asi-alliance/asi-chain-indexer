"""Transactional SQL revisions and explicit adoption of existing public schemas."""

import json
import os
import re
from pathlib import Path

from alembic import op
from alembic.config import Config
from alembic.util import CommandError
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
LOCK_KEY = 18387725196134226
REVISIONS = ("000", "001", "002", "003", "004")

# SQL definitions, not data, are inspected. Extension-owned objects are excluded.
CATALOG_SQL = """
WITH relations AS (
    SELECT c.oid, c.relname, c.relkind, c.relowner FROM pg_class c
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
    -- Internal trigger names contain database-local OIDs; use their FK and role.
    SELECT 'fk-trigger:' || r.relname || ':' || c.conname || ':' ||
           n.nspname || '.' || target.relname || ':' || p.proname,
           json_build_array(pn.nspname, t.tgtype, t.tgenabled,
                            t.tgdeferrable, t.tginitdeferred)::text
    FROM relations r JOIN pg_constraint c ON c.conrelid = r.oid AND c.contype = 'f'
    JOIN pg_trigger t ON t.tgconstraint = c.oid AND t.tgisinternal
    JOIN pg_class target ON target.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = target.relnamespace
    JOIN pg_proc p ON p.oid = t.tgfoid
    JOIN pg_namespace pn ON pn.oid = p.pronamespace
    UNION ALL
    -- Sequence allocation state is data; only structure and ownership are compared.
    SELECT 'sequence:' || r.relname || ':' || a.attname || ':' ||
           n.nspname || '.' || s.relname,
           json_build_array(format_type(q.seqtypid, NULL), q.seqstart,
                            q.seqincrement, q.seqmin, q.seqmax, q.seqcache,
                            q.seqcycle, d.deptype, s.relowner = r.relowner)::text
    FROM relations r JOIN pg_attribute a ON a.attrelid = r.oid
    JOIN pg_depend d ON d.refclassid = 'pg_class'::regclass AND d.refobjid = r.oid
                    AND d.refobjsubid = a.attnum AND d.classid = 'pg_class'::regclass
                    AND d.deptype IN ('a', 'i')
    JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
    JOIN pg_namespace n ON n.oid = s.relnamespace
    JOIN pg_sequence q ON q.seqrelid = s.oid
    WHERE a.attnum > 0 AND NOT a.attisdropped
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
            "Select an explicit existing SQL revision: " + ", ".join(REVISIONS) + "."
        )
    expected = json.loads(
        (ROOT / "migration_env" / "profiles" / (revision + ".json")).read_text()
    )
    actual = catalog(connection)
    # Extra columns/sequences on managed tables and later owned tables can indicate
    # a partially applied revision. Unrelated objects/indexes are still allowed.
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
        if key.startswith(("column:", "sequence:"))
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


def _statement_spans(sql):
    """Locate top-level SQL statements without splitting quoted function bodies."""
    spans = []
    start = None
    i = 0
    while i < len(sql):
        if sql[i].isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith("/*", i):
            depth = 1
            i += 2
            while depth and i < len(sql):
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise CommandError("SQL migration has an unterminated comment.")
        else:
            if start is None:
                start = i
            if sql[i] in ("'", '"'):
                quote = sql[i]
                escapes = (
                    quote == "'"
                    and i > 0
                    and sql[i - 1] in "eE"
                    and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
                )
                i += 1
                while i < len(sql):
                    if escapes and sql[i] == "\\":
                        i += 2
                    elif sql[i] == quote:
                        i += 1
                        if i < len(sql) and sql[i] == quote:
                            i += 1
                        else:
                            break
                    else:
                        i += 1
                else:
                    raise CommandError("SQL migration has an unterminated quote.")
            elif sql[i] == "$" and (
                tag := re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", sql[i:])
            ):
                delimiter = tag.group()
                end = sql.find(delimiter, i + len(delimiter))
                if end < 0:
                    raise CommandError(
                        "SQL migration has an unterminated dollar quote."
                    )
                i = end + len(delimiter)
            elif sql[i] == ";":
                spans.append((start, i + 1))
                start = None
                i += 1
            else:
                i += 1
    if start is not None:
        spans.append((start, len(sql)))
    return spans


def transaction_body(sql):
    """Strip one standalone wrapper and reject other transaction-control statements."""
    spans = _statement_spans(sql)
    if not spans:
        return sql

    def command(span):
        match = re.match(r"[A-Za-z]+", sql[span[0] : span[1]])
        return match.group().upper() if match else ""

    first, last = spans[0], spans[-1]
    wrapped = command(first) == "BEGIN" or command(last) == "COMMIT"
    if wrapped:
        if (
            len(spans) < 2
            or not re.fullmatch(r"BEGIN\s*;", sql[first[0] : first[1]], re.IGNORECASE)
            or not re.fullmatch(r"COMMIT\s*;", sql[last[0] : last[1]], re.IGNORECASE)
        ):
            raise CommandError("SQL migration has an unexpected transaction boundary.")
        spans = spans[1:-1]
    for span in spans:
        if command(span) in {
            "BEGIN",
            "COMMIT",
            "END",
            "ROLLBACK",
            "ABORT",
            "START",
            "SAVEPOINT",
            "RELEASE",
            "PREPARE",
        }:
            raise CommandError("SQL migration has an unexpected transaction boundary.")
    return sql[first[1] : last[0]] if wrapped else sql


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
    sql = transaction_body(sql)
    connection.connection.dbapi_connection.run_async(lambda driver: driver.execute(sql))
