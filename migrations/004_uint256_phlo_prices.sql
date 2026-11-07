-- Forward-only monetary widening; counters, gas units and history keep their units.
BEGIN;

ALTER TABLE deployments ALTER COLUMN phlo_price TYPE NUMERIC(78,0);
ALTER TABLE pending_deploys ALTER COLUMN phlo_price TYPE NUMERIC(78,0);

ALTER TABLE deployments ADD CONSTRAINT deployments_phlo_price_uint256
    CHECK (phlo_price >= 0 AND phlo_price <= 115792089237316195423570985008687907853269984665640564039457584007913129639935);
ALTER TABLE pending_deploys ADD CONSTRAINT pending_deploys_phlo_price_uint256
    CHECK (phlo_price >= 0 AND phlo_price <= 115792089237316195423570985008687907853269984665640564039457584007913129639935);

COMMIT;
