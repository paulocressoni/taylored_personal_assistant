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
--  * The voice queries (Q11-Q13) return nothing until a voice turn has been
--    recorded: they key off the `channel:voice` tag or the `stt` / `tts` span
--    names, and a typed turn produces neither.
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
-- `cancelled` and `failed` are the two markers added with the voice work:
-- `mark_turn_cancelled` (barge-in, level WARNING) and `mark_turn_failed` (a
-- crash or a graph timeout, level ERROR). Both helpers rebuild the FULL tag
-- list rather than passing only the marker, so a marked run does NOT also carry
-- the completion tag it would otherwise have had — the buckets stay disjoint.
SELECT
    toDate(start_time)                             AS day,
    count()                                        AS runs,
    countIf(has(tags, 'status:ok'))                AS plain_ok,
    countIf(has(tags, 'status:tool_ok'))           AS tool_ok,
    countIf(has(tags, 'status:tool_error'))        AS tool_error,
    countIf(has(tags, 'status:llm_error'))         AS llm_error,
    countIf(has(tags, 'status:permission_denied')) AS permission_denied,
    countIf(has(tags, 'status:cancelled'))         AS cancelled,
    countIf(has(tags, 'status:failed'))            AS failed
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

-- 11. VOICE STAGE SPAN LATENCY p50 / p95 PER SPAN NAME (ms) -------------------
-- Backs dashboards 8a (STT) and 8b (TTS). The voice session opens one `stt`
-- span per turn and one `tts` span per SENTENCE, both as_type="span", so they
-- are ordinary SPAN rows in this table and their duration IS the provider round
-- trip the stage actually paid (measured from inside the pipeline, no
-- subtraction). This is the honest way to read STT/TTS impact: the six stage
-- scalars on the app-root row (Q12) tell the same story but are metadata, so no
-- widget can read them.
-- NB: `tts` counts SENTENCES, not turns — the reply is split into speakable
-- chunks and each chunk is its own TTS request, so `spans` for `tts` is
-- normally larger than the voice turn count in Q12. For `stt` the two agree,
-- one span per turn.
SELECT
    name                                                          AS span_name,
    count()                                                       AS spans,
    quantile(0.50)(dateDiff('millisecond', start_time, end_time)) AS p50_ms,
    quantile(0.95)(dateDiff('millisecond', start_time, end_time)) AS p95_ms
FROM default.events_full
WHERE type = 'SPAN'
  AND name IN ('stt', 'tts')
  AND is_deleted = 0
  AND end_time IS NOT NULL
  AND toDate(start_time) >= today() - 7
GROUP BY span_name
ORDER BY span_name;

-- 12. VOICE TURN STAGE METRICS FROM TRACE METADATA (ms) -----------------------
-- The per-stage decomposition of a voice turn, read off the app-root row. These
-- are the scalars `stamp_voice_timing` -> `timing_metadata`
-- (backend/app/core/timing.py) writes, one per turn; they live in the metadata
-- arrays, which is exactly why no dashboard widget can show them (metadata is
-- not a measure, a dimension or a filter — dashboards.md §3).
-- `first_audio_ms` is the product target: end-of-speech to first audio byte. It
-- is the SERVER's clock, so it excludes the VAD silence window and the whole
-- endpointing wait; `perceived_ms` is measured client-side by
-- scripts/voice_latency.py and is not and cannot be trace metadata.
-- A stage that never ran is ABSENT, not 0 (a barge-in or a provider error has
-- no `turn_ms`), so every percentile is taken with `quantileIf` over the rows
-- that actually measured that stage. Reading them with `toFloat64OrZero` alone
-- would coerce an absent stage to 0 and pull the p90 DOWN — the one direction a
-- latency alert must never move.
SELECT
    toDate(start_time) AS day,
    count()            AS voice_turns,
    quantileIf(0.50)(toFloat64OrZero(arrayElement(metadata_values, indexOf(metadata_names, 'stt_ms'))),
                     has(metadata_names, 'stt_ms'))           AS stt_p50_ms,
    quantileIf(0.50)(toFloat64OrZero(arrayElement(metadata_values, indexOf(metadata_names, 'graph_ttft_ms'))),
                     has(metadata_names, 'graph_ttft_ms'))    AS ttft_p50_ms,
    quantileIf(0.50)(toFloat64OrZero(arrayElement(metadata_values, indexOf(metadata_names, 'tts_ttfb_ms'))),
                     has(metadata_names, 'tts_ttfb_ms'))      AS tts_ttfb_p50_ms,
    quantileIf(0.50)(toFloat64OrZero(arrayElement(metadata_values, indexOf(metadata_names, 'first_audio_ms'))),
                     has(metadata_names, 'first_audio_ms'))   AS first_audio_p50_ms,
    quantileIf(0.90)(toFloat64OrZero(arrayElement(metadata_values, indexOf(metadata_names, 'first_audio_ms'))),
                     has(metadata_names, 'first_audio_ms'))   AS first_audio_p90_ms,
    quantileIf(0.50)(toFloat64OrZero(arrayElement(metadata_values, indexOf(metadata_names, 'turn_ms'))),
                     has(metadata_names, 'turn_ms'))          AS turn_p50_ms
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND has(tags, 'channel:voice')
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;
-- Each percentile therefore covers a different number of turns: `stt` and
-- `first_audio` run on every turn that got as far as audio, `turn_ms` only on
-- the turns that finished. A percentile with nothing to measure comes back as
-- `nan`, not 0 — a day of nothing but interrupted turns has no `turn_ms` to
-- report. Q13 is what separates them: compare `voice_turns` there against the
-- barge-in and failure counts before reading a stage figure as "typical".
-- `first_audio_p90_ms` is the number the voice alert in alerts.md §6 thresholds
-- (a native alert cannot read a metadata scalar, so that check lives here).
-- There is no p90 on a dashboard either: Langfuse aggregates only
-- count/sum/avg/p50/p95/histogram.

-- 13. VOICE TURN OUTCOMES AND BARGE-IN RATE ----------------------------------
-- Barge-in is the single most useful voice product signal, and until these two
-- tags it was invisible: a turn the user talked over looked identical to one
-- that answered. `status:cancelled` is stamped by `mark_turn_cancelled`
-- (level WARNING, message `barge-in`); `status:failed` by `mark_turn_failed`.
-- A rising `barge_in_rate` alongside a falling `turn_p50_ms` in Q12 is one
-- reading: the answers got slow enough to be cut off, which is why a latency
-- panel read alone can look like an improvement.
SELECT
    toDate(start_time)                                         AS day,
    count()                                                    AS voice_turns,
    countIf(has(tags, 'status:cancelled'))                     AS barge_ins,
    round(countIf(has(tags, 'status:cancelled')) / count(), 4) AS barge_in_rate,
    countIf(has(tags, 'status:failed'))                        AS failed,
    countIf(NOT has(tags, 'status:cancelled') AND NOT has(tags, 'status:failed')) AS answered
FROM default.events_full
WHERE is_app_root = 1
  AND is_deleted = 0
  AND has(tags, 'channel:voice')
  AND toDate(start_time) >= today() - 7
GROUP BY day
ORDER BY day;
