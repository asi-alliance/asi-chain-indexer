BEGIN;

CREATE TABLE native_transfer_calls (
    id VARCHAR(64) PRIMARY KEY,
    block_hash VARCHAR(64) NOT NULL REFERENCES blocks(block_hash),
    deploy_id VARCHAR(160) NOT NULL REFERENCES deployments(deploy_id),
    event_index INTEGER NOT NULL CHECK (event_index >= 0),
    return_channel TEXT NOT NULL,
    from_address TEXT,
    to_address TEXT,
    requested_amount TEXT NOT NULL,
    token_decimals INTEGER NOT NULL CHECK (token_decimals BETWEEN 0 AND 77),
    timestamp BIGINT NOT NULL,
    CONSTRAINT uq_native_call UNIQUE (block_hash, deploy_id, event_index)
);
CREATE INDEX ix_native_transfer_calls_return_channel ON native_transfer_calls(return_channel);
CREATE INDEX ix_native_transfer_call_identity ON native_transfer_calls(deploy_id, event_index);

CREATE TABLE native_transfer_results (
    id VARCHAR(64) PRIMARY KEY,
    call_id VARCHAR(64) NOT NULL REFERENCES native_transfer_calls(id),
    block_hash VARCHAR(64) NOT NULL REFERENCES blocks(block_hash),
    deploy_id VARCHAR(160) NOT NULL REFERENCES deployments(deploy_id),
    response_index INTEGER NOT NULL CHECK (response_index >= 0),
    phase VARCHAR(32) NOT NULL,
    success BOOLEAN NOT NULL,
    timestamp BIGINT NOT NULL,
    CONSTRAINT uq_native_result UNIQUE (block_hash, deploy_id, response_index)
);
CREATE INDEX ix_native_transfer_results_call_id ON native_transfer_results(call_id);

CREATE TABLE native_transfer_result_aliases (
    result_id VARCHAR(64) NOT NULL REFERENCES native_transfer_results(id),
    call_id VARCHAR(64) NOT NULL REFERENCES native_transfer_calls(id),
    PRIMARY KEY (result_id, call_id)
);
CREATE INDEX ix_native_transfer_result_aliases_call_id ON native_transfer_result_aliases(call_id);

ALTER TABLE transfers ADD COLUMN native_result_id VARCHAR(64)
    REFERENCES native_transfer_results(id) UNIQUE;

CREATE VIEW native_transfer_lifecycle AS
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
    UNION
    SELECT result_id, call_id FROM native_transfer_result_aliases
) links ON links.call_id = c.id
LEFT JOIN native_transfer_results r ON r.id = links.result_id
LEFT JOIN blocks rb ON rb.block_hash = r.block_hash
LEFT JOIN transfers t ON t.native_result_id = r.id;

INSERT INTO indexer_state(key, value, updated_at)
VALUES ('native_transfer_backfill_required',
        CASE WHEN EXISTS(SELECT 1 FROM blocks WHERE block_number > 0) THEN '1' ELSE '0' END,
        NOW());

COMMIT;
