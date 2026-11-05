BEGIN;
DROP VIEW IF EXISTS public.transaction_history_view;
ALTER TABLE balance_states DROP COLUMN total_balance_dust, DROP COLUMN total_balance_asi;
ALTER TABLE blocks ADD COLUMN token_decimals INTEGER NOT NULL DEFAULT 8 CHECK (token_decimals BETWEEN 0 AND 77);
ALTER TABLE transfers ADD COLUMN token_decimals INTEGER NOT NULL DEFAULT 8 CHECK (token_decimals BETWEEN 0 AND 77);
ALTER TABLE transfers ALTER COLUMN amount_dust TYPE NUMERIC(78,0);
ALTER TABLE transfers ADD CONSTRAINT transfers_amount_dust_uint256 CHECK (amount_dust >= 0 AND amount_dust <= 115792089237316195423570985008687907853269984665640564039457584007913129639935);
ALTER TABLE validators ADD COLUMN token_decimals INTEGER NOT NULL DEFAULT 8 CHECK (token_decimals BETWEEN 0 AND 77);
ALTER TABLE validators ALTER COLUMN total_stake TYPE NUMERIC(78,0);
ALTER TABLE validators ADD CONSTRAINT validators_total_stake_uint256 CHECK (total_stake >= 0 AND total_stake <= 115792089237316195423570985008687907853269984665640564039457584007913129639935);
ALTER TABLE validator_bonds ADD COLUMN token_decimals INTEGER NOT NULL DEFAULT 8 CHECK (token_decimals BETWEEN 0 AND 77);
ALTER TABLE validator_bonds ALTER COLUMN stake TYPE NUMERIC(78,0);
ALTER TABLE validator_bonds ADD CONSTRAINT validator_bonds_stake_uint256 CHECK (stake >= 0 AND stake <= 115792089237316195423570985008687907853269984665640564039457584007913129639935);
ALTER TABLE balance_states ADD COLUMN token_decimals INTEGER NOT NULL DEFAULT 8 CHECK (token_decimals BETWEEN 0 AND 77);
ALTER TABLE balance_states ALTER COLUMN unbonded_balance_dust TYPE NUMERIC(78,0);
ALTER TABLE balance_states ADD CONSTRAINT balance_states_unbonded_balance_dust_uint256 CHECK (unbonded_balance_dust >= 0 AND unbonded_balance_dust <= 115792089237316195423570985008687907853269984665640564039457584007913129639935);
ALTER TABLE balance_states ALTER COLUMN bonded_balance_dust TYPE NUMERIC(78,0);
ALTER TABLE balance_states ADD CONSTRAINT balance_states_bonded_balance_dust_uint256 CHECK (bonded_balance_dust >= 0 AND bonded_balance_dust <= 115792089237316195423570985008687907853269984665640564039457584007913129639935);
ALTER TABLE transfers ALTER COLUMN amount_asi TYPE NUMERIC(155,77);
ALTER TABLE balance_states ALTER COLUMN unbonded_balance_asi TYPE NUMERIC(155,77);
ALTER TABLE balance_states ALTER COLUMN bonded_balance_asi TYPE NUMERIC(155,77);
ALTER TABLE balance_states ADD COLUMN total_balance_dust NUMERIC(79,0) GENERATED ALWAYS AS (unbonded_balance_dust + bonded_balance_dust) STORED;
ALTER TABLE balance_states ADD COLUMN total_balance_asi NUMERIC(156,77) GENERATED ALWAYS AS (unbonded_balance_asi + bonded_balance_asi) STORED;
CREATE VIEW public.transaction_history_view AS
SELECT
    t.id AS transfer_id,
    d.deploy_id,
    d.block_hash,
    d.block_number,
    d.timestamp,
    CASE WHEN t.deploy_id IS NOT NULL THEN 'transfer' ELSE 'not_transfer' END AS type,
    d.deployer_address,
    t.from_address,
    t.to_address,
    t.from_public_key,
    t.amount_asi,
    t.status
FROM deployments d
         LEFT JOIN transfers t ON t.deploy_id = d.deploy_id;

COMMENT ON VIEW public.transaction_history_view IS
    'Wallet transaction history: deployments left-joined with their transfers, one row per transfer (or per deployment if it produced none). Filtering, sorting and pagination are done via GraphQL where/order_by/offset/limit, not parameters.';

-- JSONB numeric values otherwise lose precision when read by JavaScript clients.
UPDATE blocks SET bonds_map = (
    SELECT jsonb_agg(bond || jsonb_build_object('stake', bond->>'stake'))
    FROM jsonb_array_elements(bonds_map) AS bond
) WHERE jsonb_typeof(bonds_map) = 'array' AND jsonb_array_length(bonds_map) > 0;
CREATE OR REPLACE FUNCTION notify_new_transfer() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('new_transfer', json_build_object(
        'id', NEW.id,
        'from_address', NEW.from_address,
        'to_address', NEW.to_address,
        'amount_dust', NEW.amount_dust::text,
        'amount_asi', NEW.amount_asi::text,
        'token_decimals', NEW.token_decimals,
        'block_number', NEW.block_number
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
COMMIT;
