-- Production dashboards — ground-truth queries (ClickHouse)
--
-- These queries answer the dashboard questions directly against Langfuse's
-- own store. They are the "do the widgets match the database?" check: every
-- number a dashboard shows should agree with the matching query below.
--
-- How to run (from the repo root, prod stack up):
--   docker compose -f infra/compose/base.yml -f infra/compose/prod.yml \
--     --env-file infra/compose/.env.prod \
--     exec -T clickhouse clickhouse-client --query "<statement>"
--
-- Notes that apply to every query:
--  * Langfuse tables are ReplacingMergeTree; `FINAL` collapses stale versions
--    of the same row so counts are exact. Small personal volume makes it cheap.
--  * `is_deleted = 0` skips soft-deleted rows (they never appear with FINAL,
--    but the guard is free and keeps the intent explicit).
--  * Default time window is the last 7 days; change `today() - 7` freely.
--  * Single project setup: no project filter. If you later add a second
--    project, add `AND project_id = '<id>'` to every query.
--  * Column names are from the pinned Langfuse v4 build. If a statement
--    fails, run the schema probes in section 0 first and adapt the column
--    name — do not guess.
--
-- Authoritative flat columns used (see section 0 to confirm on your build):
--   traces       : name, timestamp, tags, metadata, user_id, project_id
--   observations : type, name, level, status_message, start_time, end_time,
--                  total_cost (USD), provided_model_name, usage_details,
--                  prompt_name, trace_id
--   total_cost is filled by Langfuse's Model registry; if it is always 0 you
--   have not added DeepSeek model pricing in Project -> Settings -> Models.

-- 0. SCHEMA PROBES (run first if anything below errors) -----------------------
-- 0a. Confirm the tables and exact column names on this build:
--    SHOW TABLES;
--    DESCRIBE TABLE traces;
--    DESCRIBE TABLE observations;
-- 0b. Confirm generation rows carry total_cost / usage_details as expected:
--    SELECT count() FROM observations FINAL
--    WHERE type = 'GENERATION' AND is_deleted = 0;
-- 0c. Confirm the app tags actually arrive:
--    SELECT tags FROM traces FINAL
--    WHERE is_deleted = 0 AND has(tags, 'status:tool_ok') LIMIT 3;

-- 1. COST PER DAY (USD) --------------------------------------------------------
SELECT
    toDate(start_time) AS day,
    round(sum(ifNull(total_cost, 0)), 4) AS cost_usd,
    count()                              AS generations
FROM observations FINAL
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 1b. COST PER DAY BY MODEL — proves Model-registry pricing is applied.
SELECT
    toDate(start_time)                                       AS day,
    ifNull(provided_model_name, name)                        AS model,
    round(sum(ifNull(total_cost, 0)), 4)                     AS cost_usd,
    sum(ifNull(usage_details['input'], 0))
        + sum(ifNull(usage_details['output'], 0))            AS tokens
FROM observations FINAL
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day, model
ORDER BY day, cost_usd DESC;

-- 2. LLM-CALL LATENCY p50 / p95 (ms) -------------------------------------------
SELECT
    toDate(start_time) AS day,
    count()            AS generations,
    round(quantile(0.50)(dateDiff('millisecond', start_time, end_time))) AS p50_ms,
    round(quantile(0.95)(dateDiff('millisecond', start_time, end_time))) AS p95_ms
FROM observations FINAL
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND end_time IS NOT NULL
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 3. WHOLE-TURN LATENCY p50 / p95 (seconds, per trace) -------------------------
-- One "turn" = one trace = many observations; this measures end to end.
WITH run AS (
    SELECT
        trace_id,
        dateDiff('second', min(start_time), max(end_time)) AS latency_s
    FROM observations FINAL
    WHERE is_deleted = 0
      AND toDate(start_time) >= today() - 7
    GROUP BY trace_id
)
SELECT
    round(quantile(0.50)(latency_s)) AS p50_s,
    round(quantile(0.95)(latency_s)) AS p95_s,
    count()                          AS traces
FROM run;

-- 4. ROUTING DISTRIBUTION ACROSS INTENTS ---------------------------------------
-- traces.name == 'assistant:<route>'; strip the prefix to group by route.
SELECT
    substring(name, 11) AS route,        -- drops 'assistant:' (10 chars)
    count()             AS traces
FROM traces FINAL
WHERE is_deleted = 0
  AND toDate(timestamp) >= today() - 7
GROUP BY route
ORDER BY traces DESC;

-- 5. TOOL SUCCESS / FAILURE — per-trace tags -----------------------------------
-- Tag is per trace (any failed call marks the run), not per call. Per-call
-- truth lives in the trace metadata `tool_outcomes`; run 5b to read it.
SELECT
    toDate(timestamp) AS day,
    countIf(has(tags, 'status:tool_ok'))    AS ok_traces,
    countIf(has(tags, 'status:tool_error')) AS error_traces,
    countIf(has(tags, 'status:tool_ok') OR has(tags, 'status:tool_error'))
                                            AS tool_traces
FROM traces FINAL
WHERE is_deleted = 0
  AND toDate(timestamp) >= today() - 7
GROUP BY day
ORDER BY day;

-- 5b. Read the per-call outcomes of one failing trace --------------------------
SELECT trace_id, metadata
FROM traces FINAL
WHERE is_deleted = 0
  AND has(tags, 'status:tool_error')
  AND toDate(timestamp) >= today() - 1
ORDER BY timestamp DESC
LIMIT 3;

-- 6. TOOL FAILURE BUCKETS (tool_error_taxonomy on the trace) -------------------
-- NOTE: nested metadata JSON is not a stable flat column in every build; if
-- `metadata` is a Map or String on yours, this needs `JSONExtract`/map access.
SELECT
    metadata['tool_error_taxonomy'] AS bucket_json,
    count()                         AS traces
FROM traces FINAL
WHERE is_deleted = 0
  AND has(tags, 'status:tool_error')
  AND toDate(timestamp) >= today() - 7
GROUP BY bucket_json;

-- 7. PERMISSION-DENIAL EVENTS PER DAY ------------------------------------------
SELECT
    toDate(timestamp) AS day,
    countIf(has(tags, 'status:permission_denied')) AS denied_traces
FROM traces FINAL
WHERE is_deleted = 0
  AND toDate(timestamp) >= today() - 7
GROUP BY day
ORDER BY day;

-- 8. LLM ERROR TAXONOMY — error generations with their message -----------------
-- The `level = 'ERROR'` observations are Langfuse's own record of failed LLM
-- calls; the app additionally buckets them in the trace metadata under
-- `usage.error_types`. status_message can be long — truncate in the tool.
SELECT
    toDate(start_time)               AS day,
    ifNull(status_message, '(none)') AS message,
    count()                          AS n
FROM observations FINAL
WHERE type = 'GENERATION'
  AND level = 'ERROR'
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day, message
ORDER BY day, n DESC
LIMIT 100;

-- 8b. RAW ERROR COUNT PER DAY (the alert's metric) -----------------------------
SELECT
    toDate(start_time) AS day,
    countIf(level = 'ERROR') AS error_generations,
    count()                  AS generations
FROM observations FINAL
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;

-- 9. PROMPT-CACHE: WHICH USAGE TYPES ACTUALLY ARRIVE? --------------------------
-- DeepSeek reports cached vs fresh prompt tokens; whether Langfuse stores them
-- depends on the provider + integration on this build. Run this FIRST; adapt
-- Q10 to the keys it returns (common: input / output / input_cache_read /
-- input_cache_creation / cached). If only input+output appear, the cache
-- signal is not reaching the store and the per-trace usage.cache_hit_ratio
-- metadata (set by the app) is the source of truth instead.
SELECT
    arrayJoin(usage_details.keys()) AS usage_key,
    count()                         AS n
FROM observations FINAL
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND length(usage_details) > 0
  AND toDate(start_time) >= today() - 7
GROUP BY usage_key
ORDER BY n DESC;

-- 10. PROMPT-CACHE-HIT RATIO PER DAY (adapt keys from Q9) ----------------------
SELECT
    toDate(start_time) AS day,
    sum(ifNull(usage_details['input_cache_read'], 0))              AS cache_read,
    sum(ifNull(usage_details['input_cache_creation'], 0))          AS cache_creation,
    sum(ifNull(usage_details['input'], 0))                         AS input_tokens,
    if(
        cache_read + cache_creation + input_tokens > 0,
        round((cache_read + cache_creation)
              / (cache_read + cache_creation + input_tokens), 4),
        NULL
    ) AS cache_hit_ratio
FROM observations FINAL
WHERE type = 'GENERATION'
  AND is_deleted = 0
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;
