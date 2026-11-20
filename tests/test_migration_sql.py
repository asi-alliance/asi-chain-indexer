"""Standalone transaction boundaries must not commit the Alembic transaction."""

import pytest
from alembic.util import CommandError

from src.migration_support import transaction_body


@pytest.mark.parametrize(
    "prefix", ["-- widening\n", "/* outer /* nested */ comment */\n", ""]
)
def test_commented_transaction_wrapper_is_removed(prefix):
    sql = (
        prefix
        + "BEGIN;\nALTER TABLE example ADD COLUMN value int;\nCOMMIT; -- trailing\n"
    )
    assert transaction_body(sql).strip() == "ALTER TABLE example ADD COLUMN value int;"


def test_function_bodies_strings_and_quoted_identifiers_keep_their_semicolons():
    body = """
    CREATE FUNCTION sample() RETURNS void AS $body$
    BEGIN PERFORM 'COMMIT;'; END;
    $body$ LANGUAGE plpgsql;
    SELECT 'BEGIN; ROLLBACK;', 'it''s unchanged', E'it\\'s unchanged', "BEGIN;";
    """
    assert transaction_body("BEGIN;" + body + "COMMIT;") == body
    assert transaction_body(body) == body


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN; SELECT 1;",
        "SELECT 1; COMMIT;",
        "BEGIN; COMMIT; COMMIT;",
        "SELECT 1; ROLLBACK;",
        "START TRANSACTION; SELECT 1;",
        "END;",
        "/* unterminated",
        "SELECT 'unterminated",
        "DO $body$ unterminated",
    ],
)
def test_unexpected_boundaries_and_unterminated_sql_are_rejected(sql):
    with pytest.raises(CommandError):
        transaction_body(sql)
