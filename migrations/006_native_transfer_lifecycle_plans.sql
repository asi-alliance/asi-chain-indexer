-- native_transfer_lifecycle is public through Hasura. Two of its plans read every
-- native transfer result:
-- * the UNION that links results and aliases to calls sorted all rows to remove
--   duplicates, and a deploy filter could not reach the tables below it;
-- * completion_deploy_id had no index.
-- UNION ALL lets PostgreSQL push the call_id join into both branches. The indexer
-- writes an alias only for a call other than the call of the result, so the two
-- branches have no common rows. The DELETE removes such rows if a database has
-- them; they repeat a link that native_transfer_results already records.
-- Movements, statuses and the view columns do not change.
BEGIN;

DELETE FROM native_transfer_result_aliases a
USING native_transfer_results r
WHERE r.id = a.result_id AND r.call_id = a.call_id;

CREATE INDEX ix_native_transfer_results_deploy_id ON native_transfer_results(deploy_id);

CREATE OR REPLACE VIEW native_transfer_lifecycle AS
SELECT c.id AS call_id, c.event_index AS call_index,
       c.block_hash AS initiation_block_hash, b.block_number AS initiation_block_number,
       c.deploy_id AS initiation_deploy_id, c.timestamp AS initiation_timestamp,
       c.from_address, c.to_address, c.requested_amount, c.token_decimals,
       CASE WHEN r.id IS NULL THEN 'pending'
            WHEN r.call_id != c.id THEN 'coalesced'
            WHEN r.success THEN 'completed' ELSE 'rejected' END AS status,
       r.id AS result_id, r.success AS result_success, r.block_hash AS completion_block_hash,
       rb.block_number AS completion_block_number,
       r.deploy_id AS completion_deploy_id, r.timestamp AS completion_timestamp,
       t.id AS transfer_id
FROM native_transfer_calls c
JOIN blocks b ON b.block_hash = c.block_hash
LEFT JOIN (
    SELECT id AS result_id, call_id FROM native_transfer_results
    UNION ALL
    SELECT result_id, call_id FROM native_transfer_result_aliases
) links ON links.call_id = c.id
LEFT JOIN native_transfer_results r ON r.id = links.result_id
LEFT JOIN blocks rb ON rb.block_hash = r.block_hash
LEFT JOIN transfers t ON t.native_result_id = r.id;

COMMIT;
