BEGIN;
-- NULL identifies legacy source-inferred rows until the repair command runs.
ALTER TABLE transfers ADD COLUMN event_index INTEGER CHECK (event_index >= 0);
ALTER TABLE transfers ADD CONSTRAINT uq_transfer_event UNIQUE (deploy_id, event_index);
COMMIT;
