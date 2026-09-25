-- Production dashboards — ground-truth queries (ClickHouse, Langfuse v4)
--
-- These queries answer the dashboard questions directly against Langfuse's
-- own store. They are the "do the widgets match the database?" check: every
-- number a dashboard shows should agree with the matching query below.
--
-- How to run (from the repo root, prod stack up):
--   docker compose -p taylored-assistant-prod \
--     -f infra/compose/docker-compose.base.yml \
--     -f infra/compose/docker-compose.prod.yml \
--     --env-file infra/compose/.env.prod \
--     exec -T clickhouse clickhouse-client --query "<statement>"
--
-- STORAGE LAYOUT (verified on Langfuse 4.30 — do not assume the old tables):
--  * The v4 OTel ingestion writes to `default.events_full` (summary table:
--    `events_core`). The legacy `traces` / `observations` tables still exist
--    but stay EMPTY, so queries against them return nothing.
--  * ONE ROW PER OBSERVATION. A trace's own name/tags/metadata live on the
--    APP-ROOT row, so anything trace-level filters `is_app_root = 1`.
--  * Trace metadata is two parallel arrays:
--    arrayElement(metadata_values, indexOf(metadata_names, '<key>')).
--  * `is_deleted = 0` skips soft-deleted rows.
--  * Default window is the last 7 days; change `today() - 7` freely.
--  * `total_cost` is a Model-registry alias of cost_details['total']; if it is
--    always 0, no DeepSeek pricing is configured (Project -> Settings ->
--    Models).

-- 0. SCHEMA PROBES (run first if anything below errors) ----------------------
-- 0a. Confirm the tables and the v4 column names on this build:
--    SHOW TABLES FROM default;
--    DESCRIBE TABLE default.events_full;

-- 0b. Row counts per observation type (expect GENERATION / CHAIN / SPAN):
SELECT type, count() AS rows
FROM default.events_full
WHERE is_deleted = 0
GROUP BY type
ORDER BY rows DESC;

-- 0c. Do the app tags arrive? (`with_tags` must be > 0 after one app turn.)
SELECT
    count()                                 AS app_roots,
    countIf(notEmpty(tags))                 AS with_tags,
    countIf(has(tags, 'env:prod'))          AS env_prod,
    countIf(has(tags, 'status:tool_ok'))    AS tool_ok,
    countIf(has(tags, 'status:tool_error')) AS tool_error
FROM default.events_full
WHERE is_app_root = 1 AND is_deleted = 0;

-- 0d. Prove which store is authoritative (legacy must be 0 on v4):
SELECT
    (SELECT count() FROM default.traces)       AS legacy_traces,
    (SELECT count() FROM default.observations) AS legacy_observations,
    (SELECT count() FROM default.events_full)  AS v4_events;

-- 1. COST PER DAY (USD) ------------------------------------------------------
SELECT
    toDate(start_time)        AS day,
    round(sum(total_cost), 4) AS cost_usd,
    count()                   AS generations
FROM default.events_full
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 1b. COST PER DAY BY MODEL — proves Model-registry pricing is applied.
SELECT
    toDate(start_time)                 AS day,
    provided_model_name                AS model,
    round(sum(total_cost), 4)          AS cost_usd,
    sum(usage_details['input'])
        + sum(usage_details['output']) AS tokens
FROM default.events_full
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day, model
ORDER BY day, cost_usd DESC;

-- 2. LLM-CALL LATENCY p50 / p95 (ms) -----------------------------------------
SELECT
    toDate(start_time)                                            AS day,
    quantile(0.50)(dateDiff('millisecond', start_time, end_time)) AS p50_ms,
    quantile(0.95)(dateDiff('millisecond', start_time, end_time)) AS p95_ms
FROM default.events_full
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND end_time IS NOT NULL
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 3. WHOLE-TURN LATENCY (ms) — the app-root span wraps the entire run, so its
-- duration IS the turn duration.
SELECT
    toDate(start_time)                                            AS day,
    quantile(0.50)(dateDiff('millisecond', start_time, end_time)) AS turn_p50_ms,
    quantile(0.95)(dateDiff('millisecond', start_time, end_time)) AS turn_p95_ms,
    count()                                                       AS runs
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND end_time IS NOT NULL
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 3b. Slowest individual turns (drill-down for a p95 spike).
SELECT
    trace_id,
    trace_name,
    dateDiff('millisecond', start_time, end_time) AS turn_ms,
    toString(start_time)                          AS started_at
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND end_time IS NOT NULL
ORDER BY turn_ms DESC
LIMIT 20;

-- 4. ROUTING DISTRIBUTION ACROSS INTENTS -------------------------------------
-- Trace names are `assistant:<route>` (set by enrich_trace at the end of the
-- run), so the app root is the only row that carries them.
SELECT trace_name, count() AS runs
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND start_time >= now() - INTERVAL 7 DAY
GROUP BY trace_name
ORDER BY runs DESC;

-- 5. RUN OUTCOME MIX PER DAY (the status:* tags) -----------------------------
SELECT
    toDate(start_time)                             AS day,
    count()                                        AS runs,
    countIf(has(tags, 'status:ok'))                AS plain_ok,
    countIf(has(tags, 'status:tool_ok'))           AS tool_ok,
    countIf(has(tags, 'status:tool_error'))        AS tool_error,
    countIf(has(tags, 'status:llm_error'))         AS llm_error,
    countIf(has(tags, 'status:permission_denied')) AS permission_denied
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 5b. Per-call outcomes of the newest failing turns.
-- `tool_outcomes` is the compact per-call summary the app writes, one entry
-- per invocation: `<tool>:<ok|denied|error_type>:<duration_ms>`.
SELECT
    trace_id,
    trace_name,
    tags,
    arrayElement(metadata_values, indexOf(metadata_names, 'tool_outcomes')) AS tool_outcomes
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND has(tags, 'status:tool_error')
ORDER BY start_time DESC
LIMIT 3;

-- 6. TOOL FAILURE BUCKETS ----------------------------------------------------
SELECT
    arrayElement(metadata_values, indexOf(metadata_names, 'tool_error_taxonomy')) AS taxonomy,
    count()                                                                       AS traces
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND has(tags, 'status:tool_error')
  AND start_time >= now() - INTERVAL 7 DAY
GROUP BY taxonomy
ORDER BY traces DESC;

-- 7. PERMISSION-DENIAL EVENTS PER DAY ----------------------------------------
SELECT toDate(start_time) AS day, count() AS denials
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND has(tags, 'status:permission_denied')
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 8. LLM ERROR TAXONOMY ------------------------------------------------------
SELECT
    status_message,
    count()         AS errors,
    max(start_time) AS last_seen
FROM default.events_full
WHERE level = 'ERROR'
  AND is_deleted = 0
  AND start_time >= now() - INTERVAL 7 DAY
GROUP BY status_message
ORDER BY errors DESC;

-- 8b. RAW ERROR COUNT PER DAY (the metric the native alert counts) -----------
SELECT
    toDate(start_time)       AS day,
    countIf(level = 'ERROR') AS errors,
    count()                  AS generations
FROM default.events_full
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 9. PROMPT-CACHE: which usage types actually arrive? ------------------------
-- Run this first; Q10 assumes the app-computed keys, not provider keys.
-- Verified on Langfuse 4.30 / ClickHouse 25.12: the app's DeepSeek calls report
-- `input`, `output`, `total` and `input_cache_read`, so provider-reported cache
-- tokens are available to dashboard 4.5 step 2 as well.
-- NB: `ARRAY JOIN usage_details AS (k, v)` is NOT valid on a Map here — join on
-- mapKeys(...) and look the value back up by key.
SELECT
    k                     AS usage_type,
    count()               AS observations,
    sum(usage_details[k]) AS tokens
FROM default.events_full
ARRAY JOIN mapKeys(usage_details) AS k
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND start_time >= now() - INTERVAL 7 DAY
GROUP BY k
ORDER BY observations DESC;

-- 10. PROMPT-CACHE-HIT RATIO PER DAY -----------------------------------------
-- Uses the app-computed flat metadata (written from the run's own token
-- accounting, so it works even when the provider reports no cache fields).
SELECT
    toDate(start_time) AS day,
    round(
        avg(
            toFloat64OrZero(
                arrayElement(metadata_values, indexOf(metadata_names, 'cache_hit_ratio'))
            )
        ),
        4
    ) AS avg_cache_hit_ratio,
    sum(
        toUInt64OrZero(
            arrayElement(metadata_values, indexOf(metadata_names, 'cache_hit_tokens'))
        )
    ) AS cached_tokens,
    sum(
        toUInt64OrZero(
            arrayElement(metadata_values, indexOf(metadata_names, 'cache_miss_tokens'))
        )
    ) AS fresh_tokens
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;
