-- An empty database has no block, deployment or transfer timestamps. The slow path
-- then computed range_end from -infinity and failed with "interval out of range".
-- Treat -infinity as no data, so the range ends at now() and every bucket is empty.
BEGIN;

CREATE OR REPLACE FUNCTION public.get_network_metrics(
    p_range_hours integer DEFAULT 24,
    p_divisions integer DEFAULT 7
)
    RETURNS SETOF public.network_metrics_view
    LANGUAGE plpgsql
    STABLE
AS
$$
DECLARE
    v_have_buckets boolean;
BEGIN
    -- Are there any pre-aggregated buckets at all?
    SELECT EXISTS (SELECT 1 FROM public.network_metrics_buckets)
    INTO v_have_buckets;

    IF v_have_buckets THEN
        ----------------------------------------------------------------
        -- FAST PATH: only by network_metrics_buckets
        -- range_end and window are calculated by max(bucket_end), not by blocks
        ----------------------------------------------------------------
        RETURN QUERY
            WITH data_bounds AS (SELECT max(bucket_end) AS max_ts
                                 FROM public.network_metrics_buckets),
                 params AS (SELECT COALESCE(max_ts, now())                                                          AS range_end,
                                   (p_range_hours || ' hours')::interval                                            AS range_window,
                                   ((p_range_hours * 3600)::double precision / p_divisions || ' seconds')::interval AS bucket_step
                            FROM data_bounds),
                 bucket_ranges AS (SELECT gs                                                          AS bucket_index,
                                          (p.range_end - p.range_window) + (gs * p.bucket_step)       AS bucket_start,
                                          (p.range_end - p.range_window) + ((gs + 1) * p.bucket_step) AS bucket_end
                                   FROM params p,
                                        generate_series(0, p_divisions - 1) AS gs),
                 raw AS (SELECT b.bucket_start,
                                b.bucket_end,
                                b.avg_block_time_sec,
                                b.deployments_count,
                                b.transfers_count
                         FROM public.network_metrics_buckets b,
                              params p
                         WHERE b.bucket_start >= p.range_end - p.range_window
                           AND b.bucket_end <= p.range_end),
                 agg AS (SELECT br.bucket_start,
                                br.bucket_end,
                                AVG(r.avg_block_time_sec)        AS avg_block_time_seconds,
                                SUM(r.deployments_count)::bigint AS deployments_count,
                                SUM(r.transfers_count)::bigint   AS transfers_count
                         FROM bucket_ranges br
                                  LEFT JOIN raw r
                                            ON r.bucket_start >= br.bucket_start
                                                AND r.bucket_end <= br.bucket_end
                         GROUP BY br.bucket_start, br.bucket_end)
            SELECT bucket_start,
                   bucket_end,
                   COALESCE(avg_block_time_seconds, 0)    AS avg_block_time_seconds,
                   COALESCE(deployments_count, 0::bigint)
                       / GREATEST(EXTRACT(EPOCH FROM (bucket_end - bucket_start)), 1)
                                                          AS avg_tps,
                   COALESCE(deployments_count, 0::bigint) AS deployments_count,
                   COALESCE(transfers_count, 0::bigint)   AS transfers_count
            FROM agg
            ORDER BY bucket_start;

    ELSE
        ----------------------------------------------------------------
        -- SLOW PATH: the old "ideal" implementation of raw tables
        ----------------------------------------------------------------
        RETURN QUERY
            WITH data_bounds AS (SELECT GREATEST(
                                                COALESCE((SELECT max(to_timestamp(timestamp / 1000.0)) FROM blocks),
                                                         '-infinity'::timestamptz),
                                                COALESCE(
                                                        (SELECT max(to_timestamp(timestamp / 1000.0)) FROM deployments),
                                                        '-infinity'::timestamptz),
                                                COALESCE((SELECT max(to_timestamp(timestamp / 1000.0)) FROM transfers),
                                                         '-infinity'::timestamptz)
                                        ) AS max_ts),
                 params AS (SELECT COALESCE(NULLIF(max_ts, '-infinity'::timestamptz) + interval '1 second', now()) AS range_end,
                                   (p_range_hours || ' hours')::interval                                            AS range_window,
                                   ((p_range_hours * 3600)::double precision / p_divisions || ' seconds')::interval AS bucket_step
                            FROM data_bounds),
                 bucket_ranges AS (SELECT gs                                                          AS bucket_index,
                                          (p.range_end - p.range_window) + (gs * p.bucket_step)       AS bucket_start,
                                          (p.range_end - p.range_window) + ((gs + 1) * p.bucket_step) AS bucket_end
                                   FROM params p,
                                        generate_series(0, p_divisions - 1) AS gs),
                 block_times AS (SELECT b.block_number,
                                        (b.timestamp - LAG(b.timestamp) OVER (ORDER BY b.timestamp)) / 1000.0 AS block_time_sec,
                                        to_timestamp(b.timestamp / 1000.0)                                    AS ts
                                 FROM blocks b),
                 block_agg AS (SELECT br.bucket_start,
                                      AVG(bt.block_time_sec) AS avg_block_time_sec
                               FROM bucket_ranges br
                                        LEFT JOIN block_times bt
                                                  ON bt.ts >= br.bucket_start
                                                      AND bt.ts < br.bucket_end
                               GROUP BY br.bucket_start),
                 deployments_agg AS (SELECT br.bucket_start,
                                            COUNT(d.deploy_id) AS deployments_count
                                     FROM bucket_ranges br
                                              LEFT JOIN deployments d
                                                        ON to_timestamp(d.timestamp / 1000.0) >= br.bucket_start
                                                            AND to_timestamp(d.timestamp / 1000.0) < br.bucket_end
                                     GROUP BY br.bucket_start),
                 transfers_agg AS (SELECT br.bucket_start,
                                          COUNT(t.id) AS transfers_count
                                   FROM bucket_ranges br
                                            LEFT JOIN transfers t
                                                      ON to_timestamp(t.timestamp / 1000.0) >= br.bucket_start
                                                          AND to_timestamp(t.timestamp / 1000.0) < br.bucket_end
                                   GROUP BY br.bucket_start)
            SELECT br.bucket_start,
                   br.bucket_end,
                   COALESCE(ba.avg_block_time_sec, 0) AS avg_block_time_seconds,
                   COALESCE(d.deployments_count, 0)
                       / GREATEST(EXTRACT(EPOCH FROM (br.bucket_end - br.bucket_start)), 1)
                                                      AS avg_tps,
                   COALESCE(d.deployments_count, 0)   AS deployments_count,
                   COALESCE(t.transfers_count, 0)     AS transfers_count
            FROM bucket_ranges br
                     LEFT JOIN block_agg ba ON ba.bucket_start = br.bucket_start
                     LEFT JOIN deployments_agg d ON d.bucket_start = br.bucket_start
                     LEFT JOIN transfers_agg t ON t.bucket_start = br.bucket_start
            ORDER BY br.bucket_start;
    END IF;
END;
$$;

COMMIT;
