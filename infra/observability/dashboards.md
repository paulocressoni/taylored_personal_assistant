# Production Dashboards — Langfuse Observability

This runbook shows how to build and read the production dashboards on the
self-hosted Langfuse v4 UI so that operational questions can be answered by
looking at a dashboard instead of digging through logs.

Companion file: [`queries.sql`](./queries.sql) — ClickHouse ground-truth
queries. Every dashboard below is a UI recipe; the matching ground-truth query
lets you prove the panel number is right.

## 1. Prerequisites

- The app is running against the production stack (Postgres checkpointer, the
  observability profile is up) and the `telemetry_node` runs last in the graph.
- Each run's trace carries `schema_version = "2.0"` metadata, `tags` with
  `env:*`, `channel:*` and `status:*`, `tool_outcomes`, `permission_denials`,
  `usage` (incl. `cache_hit_ratio`) and the error taxonomies. Confirm once:
  1. Run a single assistant turn with tools.
  2. Open the resulting trace (or use the `trace_url` the API returns).
  3. In **Trace details → Metadata**, verify `schema_version = "2.0"` and the
     keys above. In the trace **Tags**, verify `status:tool_ok` (or
     `status:tool_error`).
- **Cost needs pricing.** The DeepSeek API does not return cost in the
  response, so Langfuse can only infer it from a model definition.
  1. Project → **Settings → Models** → **New model definition**.
  2. Match pattern = the `model` parameter the app sends (see
     `settings.deepseek_model_flash` / `deepseek_model_pro`), tokenizer =
     DeepSeek if listed, otherwise none.
  3. Enter the USD price per 1M tokens for `input`, `output` and (if your
     provider reports it) `cached` usage types.
  4. Save — cost now appears retroactively on existing generations and feeds
     every cost widget below.
- Metric names used below follow Langfuse v4 wording. If a field name differs
  in your build, the "answerable question" column is the source of truth.

## 2. Where things live in the UI

- **Project → Home** — a dashboard. Its default view shows traces, model cost,
  latency percentiles and model usage. You can swap which dashboard shows here.
- **Project → Dashboards** — custom dashboards made of **widgets**
  (one metric query each). Start from the curated **Latency**, **Cost** and
  **Usage** dashboards and edit copies, or build your own from scratch.
- **Project → Traces / Generations / Users / Sessions** — the raw lists behind
  the dashboards. Every list supports filters (tags, metadata, model, trace
  name) and a latency distribution.

Dashboard widget anatomy (used everywhere below):
**Data source** (traces / observations / scores) → **Metric** (count, latency
p50/p95, cost, …) → **Dimension** (group by: day, model, trace name, tag) →
**Filters** → **Chart type** → **Save** → arrange on a dashboard.

## 3. Milestone dashboards → recipe map

| # | Dashboard | Answerable question | UI recipe (quick) | Ground truth |
|---|-----------|---------------------|-------------------|--------------|
| 1 | Cost per day | How much did I spend today / this week, and on which model? | Widget on observations (generation): metric = cost, dimension = day + model | Q1, Q1b |
| 2 | Latency p50 / p95 | Is the assistant feeling slow? | Widget on observations (generation): metric = latency p50/p95 over time | Q2 |
| 3 | Routing distribution | Which intents did users hit this week? | Widget on traces: count, dimension = trace name (time range = week) | Q4 |
| 4 | Tool success / failure | Is any tool failing more than usual? | Widget on traces: count, dimension = day, filter tag `status:tool_error` (plus `status:tool_ok`) | Q5, Q6 |
| 5 | Prompt-cache-hit ratio | How much of my prompt input is served from cache? | Generations list filtered by model → usage per generation; or widget on observations: usage of `cached` vs `input` usage types | Q9 (discover keys), Q10 (ratio) |
| 6 | Permission-denial events | Did the assistant try to do something it wasn't allowed to? | Widget on traces: count, filter tag `status:permission_denied` | Q7 |
| 7 | Error taxonomy | What actually fails — rate limits, tool bugs, unknown tools? | LLM side: Generations list filtered `level = ERROR`, group by error text / bucket; tool side: traces with tag `status:tool_error` → open trace → `tool_error_taxonomy` metadata | Q8, Q8b |

## 4. Building each dashboard

### 4.1 Cost per day
1. Dashboards → New Dashboard → name it `Cost per day`.
2. Add widget: data source **observations**, filter `type = generation`,
   metric **cost (USD)**, dimension **day** → line chart.
3. Add widget: same, but dimension **model** → bar chart, to see which model
   drives spend (DeepSeek flash vs pro, and cached vs fresh tokens).
4. Pin **Cost (USD)** and **Tokens** summary numbers at the top.
5. If every model shows $0, you skipped the model-definition step in §1.

### 4.2 Latency p50 / p95
1. Widget: data source **observations**, filter `type = generation`,
   metric **latency percentile p50** over time; repeat for **p95** on the same
   chart (or use the curated **Latency** dashboard which already has it).
2. Add a second widget on **traces** with **duration** so you see whole-turn
   time, not just the model call. Cross-check with Q3.
3. Watch p95, not p50: personal usage is bursty; p95 tells you about the slow
   tail (long ReAct loops, cold cache).

### 4.3 Routing distribution across intents
Each trace is named `assistant:<route>`, so trace name is a clean dimension.
1. Widget: data source **traces**, metric **count**, dimension **trace name**,
   time range = this week → bar chart.
2. Filter `env = prod` and, if you have multiple channels, split by
   `channel:*` tag to see text vs voice vs CLI behaviour.
3. Expected shape for this assistant: the bulk on `knowledge`/`general`, with
   `control` absent until device-control routes exist — a sudden new route is
   itself a signal.

### 4.4 Tool success / failure rate
Each run that called a tool is tagged `status:tool_ok` (all tools returned
normally) or `status:tool_error` (at least one raised). Per-call detail lives
in each trace's `tool_outcomes` metadata (`ok`, `error_type`, `denied`,
`duration_ms`).
1. Widget: data source **traces**, metric **count**, dimension **day**,
   filter tag `status:tool_error` → line chart (failures).
2. Widget: same with tag `status:tool_ok` (successes). Success rate ≈
   ok / (ok + error).
3. To see WHICH tool failed: open a `status:tool_error` trace → Metadata →
   `tool_error_taxonomy` (bucket → count) and `tool_outcomes` (per call).
   Remember: the calculator returns an error *string* on bad input instead of
   raising — those count as `status:tool_ok` by design. The failure taxonomy
   is about raised errors and unknown tools, not user-input mistakes.

### 4.5 Prompt-cache-hit ratio
DeepSeek reports cached vs fresh prompt tokens. Whether they surface as
`cached`/`input` usage types depends on the provider + integration, so verify
first:
1. Project → **Generations** → pick any DeepSeek generation → open it →
   check the **Usage** breakdown for a cached-token line.
2. If present, widget: data source **observations**, filter `type = generation`
   and model = deepseek, metric **usage of cached** and **usage of input**
   over time. Hit ratio = cached ÷ (cached + fresh input).
3. If the provider does not report it there, use the value the app computed
   per run: it is stored as `usage.cache_hit_ratio` on every trace (Metadata)
   and persisted in the Postgres checkpointer — open a trace and read it, or
   average the field across a few traces.
Ground truth: Q9 lists which usage types actually arrive, then Q10 computes
the ratio.

### 4.6 Permission-denial events
Runs where a tool refused authorization are tagged `status:permission_denied`,
and the denial records land in `permission_denials` trace metadata.
1. Widget: data source **traces**, metric **count**, dimension **day**,
   filter tag `status:permission_denied`.
2. On a spike: open a trace → Metadata → `permission_denials` for
   `{tool, reason, user_id}`.
3. Today this stays at zero by design (authorization lands in a later
   milestone) — the dashboard existing now means the moment it becomes real,
   you will already see it without code changes.

### 4.7 Error taxonomy
Two independent views, by design:
- **LLM calls**: the model integration records failures as observations with
  `level = ERROR` and the error text in `status_message`. Generations list →
  filter `level = ERROR`. Langfuse also flags the trace. Ground truth Q8.
- **App-bucketed**: the app also buckets LLM failures (`rate_limit`, `auth`,
  `timeout`, exception class) into `usage.error_types` and tool failures into
  `tool_error_taxonomy`, both stored per trace. Open an errored trace →
  Metadata.
- Quick triage dashboard: one widget counting `level = ERROR` generations per
  day, one counting traces with tag `status:tool_error` per day, one for
  `status:permission_denied` per day.

## 5. Weekly "read the dashboards" routine

Once a week, answer these purely from the dashboards (no log access):

1. **Cost per day** — did spend jump? Which model/day? Did a single turn
   (ReAct loop) blow the budget? (Drill into that day's traces.)
2. **Latency p95** — creeping up? Correlate with cache-hit ratio: if the ratio
   dropped, prompts are cold-caching more (rephrased/rare intents) and calls
   get slower + pricier.
3. **Routing distribution** — any new/unexpected route? Did voice/text usage
   shift?
4. **Tool success/failure** — any tool erroring repeatedly this week? Open one
   trace per bucket to read `tool_error_taxonomy`; fix or file the bug.
5. **Permission denials** — any? (Expected zero until authorization exists.)
6. **Error taxonomy** — are the errors rate limits (provider), timeouts
   (network/setup), or real bugs? Rate-limit clusters suggest adding retry /
   backoff; timeouts suggest the DeskMini or the provider is struggling.

If you cannot answer one of these from the dashboards, add the widget/panel
now — the goal is that a week of real usage makes these one-glance reads.

## 6. Keeping it trustworthy

- Panels and `queries.sql` must agree. Any time you change the shape of trace
  metadata (`schema_version`), re-check the dashboard filters and re-run the
  matching queries; bump `TRACE_SCHEMA_VERSION` when you change the metadata
  shape so old and new traces stay distinguishable.
- Dashboards are configuration stored by Langfuse, not code — back them up by
  backing up the Langfuse Postgres + ClickHouse volumes (see the backup
  runbook; both are already in `backup.conf` as items).
