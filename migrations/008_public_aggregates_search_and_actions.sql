-- Public height aggregates, grouped heights and text-search indexes.
--
-- Height counts use blocks.block_number, which is already indexed. The grouped
-- view exposes one row per DAG height for stable keyset pagination. Trigram
-- indexes support the explorer's leading-wildcard _ilike searches. Deployment
-- actions add exact system-contract operations without changing deployment_type.
BEGIN;

ALTER TABLE public.deployments
    ADD COLUMN IF NOT EXISTS deployment_action VARCHAR(50);

-- 000 created this non-unique copy of the primary-key index; the PK already
-- serves every block_hash lookup and unique constraint.
DROP INDEX IF EXISTS public.idx_blocks_hash;

CREATE INDEX IF NOT EXISTS idx_deployments_action
    ON public.deployments (deployment_action)
    WHERE deployment_action IS NOT NULL;

-- Prefix lookups use pattern-ops btrees; substring lookups use the trigram GIN
-- indexes below. Exact lookups use the primary/unique indexes.
CREATE INDEX IF NOT EXISTS idx_blocks_proposer_prefix
    ON public.blocks (proposer varchar_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_deployments_deployer_address_prefix
    ON public.deployments (deployer_address varchar_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_transfers_deploy_id_prefix
    ON public.transfers (deploy_id varchar_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_transfers_from_prefix
    ON public.transfers (from_address varchar_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_transfers_to_prefix
    ON public.transfers (to_address varchar_pattern_ops);

-- Stable (timestamp, id) cursors used by /search.
CREATE INDEX IF NOT EXISTS idx_blocks_timestamp_hash
    ON public.blocks (timestamp DESC, block_hash);
CREATE INDEX IF NOT EXISTS idx_deployments_timestamp_id
    ON public.deployments (timestamp DESC, deploy_id);
CREATE INDEX IF NOT EXISTS idx_transfers_timestamp_id
    ON public.transfers (timestamp DESC, id);

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_blocks_hash_trgm
    ON public.blocks USING gin (block_hash gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_blocks_proposer_trgm
    ON public.blocks USING gin (proposer gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_deployments_deploy_id_trgm
    ON public.deployments USING gin (deploy_id gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_deployments_deployer_trgm
    ON public.deployments USING gin (deployer gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_deployments_deployer_address_trgm
    ON public.deployments USING gin (deployer_address gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_transfers_deploy_id_trgm
    ON public.transfers USING gin (deploy_id gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_transfers_from_trgm
    ON public.transfers USING gin (from_address gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_transfers_to_trgm
    ON public.transfers USING gin (to_address gin_trgm_ops);

-- Maintain one aggregate row per height so a Recent Blocks page does not scan
-- the whole blocks table. The table stays private; public reads use the view.
CREATE TABLE IF NOT EXISTS public.block_heights
(
    height            BIGINT  PRIMARY KEY,
    block_count       INTEGER NOT NULL,
    latest_block_hash VARCHAR(64) NOT NULL,
    latest_timestamp  BIGINT NOT NULL,
    finalized_count   INTEGER NOT NULL
);

CREATE OR REPLACE FUNCTION public.refresh_block_height(p_height BIGINT)
    RETURNS void
    LANGUAGE plpgsql
AS
$$
BEGIN
    -- Serialize aggregate updates for one height. Blocks at one height can be
    -- written by different transactions; without the lock the delete/insert
    -- pair could lose a sibling or conflict after both transactions commit.
    PERFORM pg_advisory_xact_lock(p_height);
    DELETE FROM public.block_heights WHERE height = p_height;
    INSERT INTO public.block_heights(height, block_count, latest_block_hash,
                                     latest_timestamp, finalized_count)
    SELECT block_number,
           count(*)::integer,
           (array_agg(block_hash ORDER BY timestamp DESC, block_hash DESC))[1],
           max(timestamp),
           count(*) FILTER (WHERE finalization_status = 'finalized')::integer
    FROM public.blocks
    WHERE block_number = p_height
    GROUP BY block_number;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_block_height_from_blocks()
    RETURNS trigger
    LANGUAGE plpgsql
AS
$$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM public.refresh_block_height(OLD.block_number);
    ELSIF TG_OP = 'INSERT' THEN
        PERFORM public.refresh_block_height(NEW.block_number);
    ELSE
        PERFORM public.refresh_block_height(OLD.block_number);
        IF NEW.block_number IS DISTINCT FROM OLD.block_number THEN
            PERFORM public.refresh_block_height(NEW.block_number);
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS block_height_upsert ON public.blocks;
CREATE TRIGGER block_height_upsert
    AFTER INSERT OR DELETE OR UPDATE OF block_number, timestamp, block_hash, finalization_status
    ON public.blocks
    FOR EACH ROW
EXECUTE FUNCTION public.update_block_height_from_blocks();

-- Backfill the aggregate table for an existing database before the trigger is used.
INSERT INTO public.block_heights(height, block_count, latest_block_hash,
                                 latest_timestamp, finalized_count)
SELECT block_number,
       count(*)::integer,
       (array_agg(block_hash ORDER BY timestamp DESC, block_hash DESC))[1],
       max(timestamp),
       count(*) FILTER (WHERE finalization_status = 'finalized')::integer
FROM public.blocks
GROUP BY block_number
ON CONFLICT (height) DO UPDATE
    SET block_count = EXCLUDED.block_count,
        latest_block_hash = EXCLUDED.latest_block_hash,
        latest_timestamp = EXCLUDED.latest_timestamp,
        finalized_count = EXCLUDED.finalized_count;

CREATE OR REPLACE VIEW public.block_heights_view AS
SELECT height, block_count, latest_block_hash, latest_timestamp, finalized_count
FROM public.block_heights;

COMMENT ON VIEW public.block_heights_view IS
    'One row per DAG height: sibling count, latest block hash/time and finalized sibling count.';
COMMENT ON TABLE public.block_heights IS
    'Private aggregate table maintained by the blocks trigger; public reads use block_heights_view.';
COMMENT ON COLUMN public.deployments.deployment_action IS
    'Exact system-contract action such as pos_delegate, vault_transfer or registry_lookup; NULL for an unrecognised contract call.';

COMMIT;
