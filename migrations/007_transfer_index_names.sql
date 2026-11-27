-- Migration 000 creates two identical btree indexes on each transfer address
-- column: idx_transfers_from and idx_transfers_from_address on from_address,
-- idx_transfers_to and idx_transfers_to_address on to_address. Every transfer
-- insert maintains both copies, and the planner uses only one of them.
-- This migration drops the *_address copies. idx_transfers_from and
-- idx_transfers_to remain, and the ORM declares the same index names.
-- No table, column, data, view or Hasura metadata changes.
-- The names are schema-qualified: a direct psql run follows the caller's
-- search_path and must not drop a same-named index of another schema.
BEGIN;

DROP INDEX IF EXISTS public.idx_transfers_from_address;
DROP INDEX IF EXISTS public.idx_transfers_to_address;

COMMIT;
