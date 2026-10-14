# Production Dashboards — Langfuse Observability

This runbook shows how to build and read the production dashboards on the
self-hosted Langfuse v4 UI so that operational questions can be answered by
looking at a dashboard instead of digging through logs.

Companion file: [`queries.sql`](./queries.sql) — ClickHouse ground-truth
queries. Every dashboard below is a UI recipe; the matching ground-truth query
lets you prove the panel number is right.

## 1. Prerequisites

- The app is running against the production stack (Postgres checkpointer, the
  observability profile is up). The run's trace identity is stamped by the
  invoke site AFTER the graph returns, so nothing here depends on the graph's
  node order.
- Each run's trace carries `schema_version = "2.2"` metadata, `tags` with
  `env:*`, `channel:*` and `status:*`, the token counters (incl. the flat
  `cache_hit_ratio`), `tool_outcomes` (per-call summary) and the error
  taxonomies. That identity is written onto the run's own **app-root span** —
  the only row Langfuse reads a trace's name/tags/metadata from. Confirm once:
  1. Run a single assistant turn with tools.
  2. Open the trace in **Project → Traces**; it is named `assistant:<route>`
     (e.g. `assistant:knowledge`).
  3. In **Trace details → Metadata**, verify `schema_version = "2.2"` and the
     keys above. In the trace **Tags**, verify `status:tool_ok` (or
     `status:tool_error`) plus `env:prod` and `channel:*`.
- A **voice** turn adds two more things to that SAME app-root row: `stt_lang`
  (the language the transcription resolved to; an empty string on a typed turn)
  and six per-turn stage scalars — `stt_ms`, `graph_ttft_ms`, `tts_ttfb_ms`,
  `first_audio_ms`, `tail_ms`, `turn_ms`. They are stamped by
  `stamp_voice_timing` → `timing_metadata` (`backend/app/core/timing.py`) AFTER
  `enrich_trace` has written the identity, and the two calls ADD to the same
  span instead of replacing each other. §6 has the origin of each one, and the
  span waterfall that accompanies them is documented in
  [`../../docs/observability.md`](../../docs/observability.md) — that file, not
  this one, is the authority on the span vocabulary.
- The voice `status:*` tags are two more than the older runbooks list:
  `status:failed` (a crash or timeout, level ERROR) and `status:cancelled` (a
  barge-in, level WARNING). Both are written by `mark_turn_failed` /
  `mark_turn_cancelled` and counted in the Q5 outcome mix; Q13 additionally
  splits them per voice turn. The barge-in RATE inside that query is not a
  widget for the same reason the cache-hit ratio is not (§5.5) — a ratio of two
  counts is not expressible as one metric, and this one is the most useful voice
  product signal there is.
- Ground truth lives in the **`default.events_full`** ClickHouse table, NOT in
  the legacy `traces` / `observations` tables (empty on Langfuse 4.30). Trace
  level fields exist only on the app-root row, so the matching queries filter
  `is_app_root = 1`; run the `queries.sql` §0 probes first if one returns
  nothing.
- A run that never finished (crash, timeout, abandoned socket) keeps the fallback
  trace name `assistant:turn`: it still carries `env:*` / `channel:*` but has no
  `status:*` tag and no route metadata. `assistant:turn` rows in the routing
  distribution are therefore a signal worth reading, not a bug.
- **Cost needs pricing.** The DeepSeek API does not return cost in the
  response, so Langfuse can only infer it from a model definition:
  - **Where:** Project → **Settings → Models** → **+ New model definition**.
  - **Match pattern** is a regex tested against the generation's `model`
    attribute — the value the *app* sends (`settings.deepseek_model_flash`),
    not what the provider bills. `(?i)^(deepseek)` covers every name seen in
    the data (`deepseek-flash`, `deepseek-v4-flash`, `deepseek-v4-pro`). Leave
    the **tokenizer** empty: we ingest real token counts.
  - **Prices are PER TOKEN**, not per 1M — Langfuse stores a price per unit
    (`unit: TOKENS`). Divide the provider's per-1M figure by 10⁶ and add one
    row per usage type that actually arrives in `usage_details` (see Q9). The
    key names must match **exactly**:

    | Usage type | Meaning | DeepSeek flash |
    |---|---|---|
    | `input` | cache **miss** input | $0.30 / 1M → `3e-7` |
    | `input_cache_read` | cache **hit** input | $0.006 / 1M → `6e-9` |
    | `output` | completion tokens | $1.20 / 1M → `1.2e-6` |

  - **Not retroactive.** Inferred cost is computed at ingestion time, so only
    generations logged *after* the definition is saved get cost; existing
    traces stay at $0 forever. Run a fresh turn and re-run Q1b to confirm.
  - **Peak / off-peak is not expressible.** Pricing-tier conditions can match
    usage details, model parameters or metadata — there is no time-of-day
    condition. Enter the **peak** rate (fail-safe: every cost panel then reads
    as an upper bound, and real spend is 0–50% lower), or compute the cost in
    the app and ingest `cost_details`, which takes priority over inference.
  - **Verify** after saving: `GET /api/public/models`, filter for `deepseek`,
    and check `prices.input.price ≈ 3E-07` and that `input_cache_read` is
    present. A value of `0.3` means the per-1M figure was entered as-is.
- Metric names below follow Langfuse v4 wording; §2 maps the documentation terms
  onto the labels the UI actually uses, and the "answerable question" column is
  the source of truth if a name has moved again.

## 2. Where things live in the UI

The split that causes the most confusion: **Project → Dashboards** has a
**Widgets** tab (a library of reusable widget definitions) and a **Dashboards**
tab (layouts that *place* widgets). A dashboard has **no metric or dimension
editor of its own** — you always create or edit a **widget** first, then place
it on one or more dashboards. That is why a freshly created dashboard looks
empty with nowhere to type a metric.

- **Project → Home** — itself a dashboard. The dashboard selector in its header
  chooses which one Home renders (**Set as default** makes it the project's
  Home for everyone).
- **Project → Dashboards → Widgets** — the library. **New widget** opens a page
  titled **New Widget** holding the form described in §3.
- **Project → Dashboards → <name>** — the layout. **Add widget** places a
  library widget on it; tiles drag and resize.
- **Project → Traces / Generations / Users / Sessions** — the raw lists behind
  every panel, for drilling into a spike.

### The labels do not match the documentation

| Documentation / schema term | Label in the v4 UI | Notes |
|---|---|---|
| Data source | **View** | Observations or Scores (numeric / boolean / categorical). **`Traces` is not offered for new widgets** — see the box below. |
| Metrics | **Metric** (singular) | **"Metrics"** (plural) appears only when Chart Type is **Pivot Table**, the one type that accepts several measures. |
| Dimensions | **Breakdown** | The JSON field is `dimensions`; the UI never says "Dimensions". A `None` option means "no grouping". |
| Chart kind | **Chart Type** | Grouped into *Time Series* / *Total Value*; **Pivot Table** is the multi-metric, multi-dimension outlier. |
| Conditions | **Filters** | Same `column / type / operator / value` shape as the widget JSON (§3), with a **Presets** popover. |

Two further traps:

- **The Breakdown control is hidden** for chart types that do not support a
  breakdown. Pick *Line Chart* or *Big Number* and there is no breakdown field
  at all — switch to a bar, pie or pivot chart to see it.
- **The environment is a dashboard-level selector, not a widget field**, and a
  widget-level environment filter *overrides* it for that widget only. This
  instance holds `prod`, `dev` **and** `default` rows, so a dashboard with the
  selector left on "All" mixes environments and its numbers will not match the
  ground-truth queries referenced per widget in §5.

## 3. The widget model — exact fields and allowed values

Every widget is one object, and the form fields in §4 are just a UI over it.
Export any tile with **⋯ → Download as JSON** to see your own:

```json
{
  "name": "Cost per day",
  "description": "",
  "view": "observations",
  "dimensions": [{ "field": "providedModelName" }],
  "metrics":    [{ "measure": "totalCost", "agg": "sum" }],
  "filters":    [{ "column": "type", "type": "string", "operator": "=", "value": "GENERATION" }],
  "chartType": "VERTICAL_BAR",
  "chartConfig": { "type": "VERTICAL_BAR", "row_limit": 25 }
}
```

| Field | Allowed values |
|---|---|
| `view` | `observations`, `scores-numeric`, `scores-boolean`, `scores-categorical` |
| `metrics[].measure` | `count`, `latency`, `totalCost`, `totalTokens`, `toolCallInvocations`, `timeToFirstToken`, `usageByType`, `costByType` |
| `metrics[].agg` | `count`, `sum`, `avg`, `p50`, `p95`, `histogram` — the list is filtered by the measure's type |
| `dimensions[].field` | `providedModelName` (Model), `name`, `traceName`, `environment`, `type`, `level`, `tags`, `usageType`, `costType`, `userId`, `sessionId` |
| `chartType` | `NUMBER`, `LINE_TIME_SERIES`, `BAR_TIME_SERIES`, `VERTICAL_BAR`, `HORIZONTAL_BAR`, `HISTOGRAM`, `PIE`, `PIVOT_TABLE` |
| `chartConfig` | `{ type, row_limit (1–1000), bins (1–100), show_value_labels, defaultSort: { column, order } }` |

Filter objects (all four forms used by the widgets in §5):

```json
{ "column": "isRootObservation", "type": "boolean",      "operator": "=",      "value": true }
{ "column": "type",              "type": "string",       "operator": "=",      "value": "GENERATION" }
{ "column": "level",             "type": "string",       "operator": "=",      "value": "ERROR" }
{ "column": "tags",              "type": "arrayOptions", "operator": "any of", "value": ["status:tool_error"] }
```

**Metadata is not part of this model.** `metadata_names` / `metadata_values`
(§6) appear in no list above: there is no metadata measure, no metadata
dimension and no metadata filter. A trace-metadata scalar — the voice stage
timings are the live example — therefore CANNOT be charted, broken down or
alerted on natively, and §5.8/§5.9 are careful to claim only what an
observation-level measure can actually express. Read metadata from ClickHouse
instead; that is what the extra queries in
[`queries.sql`](./queries.sql) are for.

Four rules the **Save** button enforces (all are Langfuse v4 changes):
1. **Exactly one metric** on every chart type except `PIVOT_TABLE` — which is
   why p50 and p95 need two widgets instead of one two-line chart.
2. **At most one dimension** on every chart type except `PIVOT_TABLE`.
3. **High-cardinality dimensions** (`userId`, `sessionId`, …) require both a
   `row_limit` and a `defaultSort`, and cannot be used on a time-series chart.
4. `id`, `traceId`, `parentObservationId` and `isRootObservation` are
   **filter-only** — they are never offered as dimensions.

> **The `traces` view is gone for new widgets.** v4 removed it (it forced a
> costly per-trace aggregation); a widget still targeting it falls back to v3
> definitions. The v4 replacement for a *trace-level count* is
> `view: observations` **+ filter `isRootObservation = true`** — the app-owned
> root span, i.e. one row per assistant turn.

## 4. Build a widget — step by step

### 4.1 In the UI

1. **Project → Dashboards** → the **Widgets** tab → **New widget**. The page is
   titled **New Widget**.
2. Leave the name blank for now — Langfuse auto-suggests one from your
   selections and you can overwrite it before saving.
3. **View** → `Observations`. Pick this first: metrics and dimensions are
   per-view, so the *Metric* list is populated only once a view is chosen.
4. **Metric** → the measure (e.g. *Total Cost*) **and** the aggregation
   (e.g. *Sum*). For *Count* the aggregation is fixed.
5. **Breakdown** → the grouping dimension, or leave **None**. If the control is
   missing, the chart type does not support a breakdown — change Chart Type.
6. **Filters** → one row per condition, in the `column / type / operator /
   value` shape from §3.
7. **Chart Type** → *Line Chart* for a metric over time, *Vertical Bar Chart*
   to compare categories, *Big Number* for one value.
8. Watch the live preview, then **Save**. The widget is saved to the
   **library** — not to a dashboard yet.
9. **Project → Dashboards → <the dashboard>** → **Add widget** → pick it from
   the library → arrange by dragging and resizing.
10. On the dashboard, set the **environment selector** (`prod`) and the date
    range. Both live on the dashboard, not in the widget.

### 4.2 The same object via the API

Repeatable and scriptable — useful when rebuilding on the DeskMini. This is how
widget 1a in §5 was created:

```powershell
$c = @{}
Get-Content backend/.env.prod | Where-Object { $_ -match '^\s*LANGFUSE_(PUBLIC_KEY|SECRET_KEY)\s*=' } | ForEach-Object { $k,$v = $_ -split '=',2; $c[$k.Trim()] = $v.Trim() }
$h = @{ Authorization = "Basic $([Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("$($c['LANGFUSE_PUBLIC_KEY']):$($c['LANGFUSE_SECRET_KEY'])")))" }

$body = @{
  name = 'Cost per day'; description = ''; view = 'observations'; dimensions = @()
  metrics = @(@{ measure = 'totalCost'; agg = 'sum' })
  filters = @(@{ column = 'type'; type = 'string'; operator = '='; value = 'GENERATION' })
  chartType = 'LINE_TIME_SERIES'; chartConfig = @{ type = 'LINE_TIME_SERIES' }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Uri 'http://127.0.0.1:3000/api/public/unstable/dashboard-widgets' `
  -Method Post -Headers $h -ContentType 'application/json' -Body $body
```

`GET` the same path to list what exists. These endpoints are flagged
**unstable** upstream, so if a call stops working, trust the UI form.

### 4.3 Moving widgets between instances

**⋯ → Download as JSON** on a widget exports
`{"$langfuseWidget": true, "version": 1, ...}`; a dashboard exports the matching
`{"$langfuseDashboard": true, "version": 1, ...}` envelope with its widgets
inlined. **Drag the file onto a dashboard to import it** — Langfuse validates it
and recreates the widgets. That is the supported way to reproduce a dashboard on
a second instance without re-clicking every field.

## 5. Widget-by-widget specification

Build each widget in the **Widgets** library (§4), then place it on a dashboard.
Provenance (§6) says where the number physically comes from; "ground truth" is
the query in [`queries.sql`](./queries.sql) the panel must agree with.

| # | Widget | View | Metric | Breakdown | Filters | Chart | Ground truth |
|---|---|---|---|---|---|---|---|
| 1a | Cost per day | observations | `totalCost` / `sum` | — | `type = GENERATION` | Line | Q1 |
| 1b | Cost by model | observations | `totalCost` / `sum` | `providedModelName` | `type = GENERATION` | Vertical bar | Q1b |
| 2a | Latency p50 | observations | `latency` / `p50` | — | `type = GENERATION` | Line | Q2 |
| 2b | Latency p95 | observations | `latency` / `p95` | — | `type = GENERATION` | Line | Q2 |
| 3 | Routing distribution | observations | `count` / `count` | `traceName` | `isRootObservation = true` | Horizontal bar | Q4 |
| 4a | Tool errors per day | observations | `count` / `count` | — | `isRootObservation = true` + tag `status:tool_error` | Line | Q5, Q6 |
| 4b | Runs with tools OK | observations | `count` / `count` | — | `isRootObservation = true` + tag `status:tool_ok` | Line | Q5 |
| 5 | Cache tokens by usage type | observations | `usageByType` / `sum` | `usageType` | `type = GENERATION` | Vertical bar | Q9, Q10 |
| 6 | Permission denials | observations | `count` / `count` | — | `isRootObservation = true` + tag `status:permission_denied` | Line | Q7 |
| 7a | LLM errors per day | observations | `count` / `count` | — | `level = ERROR` | Line | Q8b |
| 7b | Tool errors by day | observations | `count` / `count` | — | `isRootObservation = true` + tag `status:tool_error` | Line | Q5 |
| 8a | STT span latency | observations | `latency` / `p50` (p95 twin: same widget, `agg` swapped) | — | `type = SPAN` + `name = stt` | Line | Q11 |
| 8b | TTS span latency | observations | `latency` / `p50` (p95 twin: same widget, `agg` swapped) | — | `type = SPAN` + `name = tts` | Line | Q11 |
| 9 | Voice turn latency | observations | `latency` / `p50` | — | `isRootObservation = true` + tag `channel:voice` | Line | Q3, Q12 |

Useful starting points: the Langfuse-curated **Cost**, **Latency** and **Usage**
dashboards are managed, and editing one duplicates it into your project —
often faster than building from scratch.

> **Two identifiers to confirm on the first build.** The dimension and filter
> names here are taken from the v4 widget schema and have been validated through
> the widgets API, but two have not yet been eyeballed in this instance's form:
> the trace-level name dimension (expected `traceName`) and the tags filter
> operator label (expected `any of`). If either differs, correct this table —
> everything else is copied straight from the JSON the form saves.
>
> **Three more the voice widgets in §5.8/§5.9 depend on.** The `name` dimension
> is documented in the schema (§3) but its *filter* has never been eyeballed on
> this instance — widgets 8a/8b need `name = stt|tts` as a string filter, so
> check it first; if the label or operator differs, correct §5.8/§5.9 here.
> The same applies to the tag operator on widget 9 (`any of`
> `["channel:voice"]`, single-element, exactly like 4a/4b/6) and to
> `isRootObservation = true` on a `latency` measure rather than a `count`,
> which no widget above exercises. All three are one-line corrections to this
> file, never a reason to doubt the numbers in `queries.sql`.

### 5.1 Dashboard "Cost per day" (widgets 1a, 1b)

Answers: how much did I spend today / this week, and on which model?

| Field | 1a Cost per day | 1b Cost by model |
|---|---|---|
| View | Observations | Observations |
| Metric | `totalCost` / `sum` | `totalCost` / `sum` |
| Breakdown | None — a line chart is already over time | `providedModelName` (label *Model*) |
| Filters | `type` `string` `=` `GENERATION` | same |
| Chart Type | Line Chart | Vertical Bar Chart (`row_limit` ~25) |

**Provenance:** DeepSeek returns no cost, so `totalCost` is **inferred** — the
model definition's per-token prices (§1) multiplied by the generation's
`usage_details`. A flat $0 panel therefore means "no definition matched", not
"no usage".

**Gotchas:** cost is computed at ingestion, so newly added pricing never
reprices old traces — expect a step change on the day you configured it. And
set the dashboard's environment selector to `prod`, or the pre-fix `dev` and
`default` rows appear in the same line.

### 5.2 Dashboard "Latency" (widgets 2a, 2b)

Two widgets, not one: a non-pivot chart accepts **exactly one metric**, so p50
and p95 cannot share a chart unless you use a Pivot Table.

| Field | Value (both widgets) |
|---|---|
| View | Observations |
| Metric | `latency` / `p50` (2a) and `latency` / `p95` (2b) |
| Breakdown | None |
| Filters | `type` `string` `=` `GENERATION` |
| Chart Type | Line Chart |

**Provenance:** `latency` is the duration of the *generation* — one model call,
taken from its span's start/end timestamps. It is not the whole assistant turn.

**Gotchas:** `latency` here is one *generation*, so this panel cannot answer "how
long was the turn"; that question needs the app-root span instead — a `latency`
measure with `isRootObservation = true` (§3) is the whole run by construction, at
every invoke site. Widget 9 in §5.9 is the voice instance of that recipe and works
for any other channel unchanged. (This replaces the older note here that said a
whole-turn duration had no direct metric: the removed `traces` view was the
reason then, not the measurements.) Cross-check generations against Q2 and turns
against Q3. Read p95, not p50 — personal usage is bursty and the tail is where
ReAct loops and cold caches show up.

### 5.3 Dashboard "Routing distribution" (widget 3)

Answers: which intents did users hit this week?

| Field | Value |
|---|---|
| View | Observations |
| Metric | `count` / `count` |
| Breakdown | `traceName` |
| Filters | `isRootObservation` `boolean` `=` `true` |
| Chart Type | Horizontal Bar Chart, `row_limit` 20, `defaultSort` on the count (DESC) |

**Provenance:** the app stamps the trace name at invoke time as
`langfuse_trace_name` (`assistant:turn`), and `enrich_trace` refines it to
`assistant:<route>` once the run finishes — so `assistant:turn` rows are runs
that *never completed* (crash, timeout, abandoned socket). The
`isRootObservation = true` filter is what makes this a **per-run** count: it
selects the app-owned root span, our `turn_span()`.

**Gotchas:** without the root filter this counts every span in every run and the
total is meaningless. A new, unexpected `assistant:<route>` value is itself a
signal. Ground truth Q4.

### 5.4 Dashboard "Tool success / failure" (widgets 4a, 4b)

| Field | 4a Tool errors per day | 4b Runs with tools OK |
|---|---|---|
| View | Observations | Observations |
| Metric | `count` / `count` | `count` / `count` |
| Breakdown | None | None |
| Filters | `isRootObservation` `boolean` `=` `true` **+** `tags` `arrayOptions` `any of` `["status:tool_error"]` | same, with `["status:tool_ok"]` |
| Chart Type | Line Chart | Line Chart |

Success rate ≈ 4b ÷ (4a + 4b).

**Provenance:** both tags are written by `trace_attributes()` from the run's
`tool_outcomes`: `status:tool_ok` when at least one tool ran and none raised,
`status:tool_error` when at least one raised, and `status:permission_denied`
when a denial occurred (it wins over the other two). Per-call detail is in the
`tool_outcomes` metadata key, a comma-separated
`<tool>:<ok|denied|error_type>:<duration_ms>` list — e.g.
`calculate:ok:3.3,calculate:ok:4.4`.

**Gotchas:** the calculator returns an error *string* on bad input instead of
raising, so user-input mistakes count as `status:tool_ok` by design — this panel
is about raised errors and unknown tools. Ground truth Q5, Q6.

### 5.5 Dashboard "Prompt cache" (widget 5)

The single widget that shows cached vs fresh prompt tokens, without hunting for
a "cached" metric that does not exist:

| Field | Value |
|---|---|
| View | Observations |
| Metric | `usageByType` / `sum` |
| Breakdown | `usageType` |
| Filters | `type` `string` `=` `GENERATION` |
| Chart Type | Vertical Bar Chart |

**Provenance:** `usageType` / `usageByType` pivot the generation's
`usage_details` map. On this stack the keys are exactly `input`,
`input_cache_read` and `output` (Q9), and they are **mutually exclusive
buckets** — `input` is the cache **miss** count, `input_cache_read` the **hit**
count, and `input + input_cache_read + output = total`. The bar chart therefore
reads directly as fresh vs cached.

**The ratio is not a widget.** A ratio of two buckets is not expressible as one
metric, so read the app-computed `cache_hit_ratio` from a trace's metadata (it
sits next to `cache_hit_tokens` / `cache_miss_tokens`), or compute Q10.

**Gotcha:** cached tokens are cheap but not free — they only contribute to cost
once `input_cache_read` has a price in the model definition (§1).

### 5.6 Dashboard "Permission denials" (widget 6)

| Field | Value |
|---|---|
| View | Observations |
| Metric | `count` / `count` |
| Breakdown | None |
| Filters | `isRootObservation` `boolean` `=` `true` **+** `tags` `arrayOptions` `any of` `["status:permission_denied"]` |
| Chart Type | Line Chart |

**Provenance:** the app tags a run `status:permission_denied` when a tool raises
a denial — a `PermissionError`, a name in the tool layer's deny list, or any
exception carrying a truthy `permission` attribute.

**Where the detail lives:** the per-denial records (`{tool, reason, user_id}`)
are persisted in the run's session state in the `app-db` checkpointer. **No
trace metadata key carries them**, so when the tag fires, read them from
Postgres — this widget is the trigger, not the report.

Expected to stay at zero until authorization ships (a later milestone); the
widget exists so the first real denial needs no code change to become visible.
Ground truth Q7.

### 5.7 Dashboard "Error taxonomy" (widgets 7a, 7b)

| Field | 7a LLM errors per day | 7b Tool errors by day |
|---|---|---|
| View | Observations | Observations |
| Metric | `count` / `count` | `count` / `count` |
| Breakdown | None | None |
| Filters | `level` `string` `=` `ERROR` | `isRootObservation = true` + tag `status:tool_error` |
| Chart Type | Line Chart | Line Chart |

**Provenance — two independent paths by design:**

- `level` is set by the tracing layer: a failed model call records an
  observation with `level = ERROR` and the error text in `status_message`.
- The app *also* buckets failures into flat metadata strings —
  `llm_error_taxonomy` (`rate_limit`, `auth`, `timeout`, or the exception
  class) and `tool_error_taxonomy` — both `name:count`.

**Drill-down:** an ERROR generation shows the provider message; a
`status:tool_error` trace → Metadata shows both taxonomies and the per-call
`tool_outcomes`. Ground truth Q8, Q8b.

### 5.8 Dashboard "Voice stage latency" (widgets 8a, 8b)

Answers: is the transcription provider or the speech provider the slow one, and
is either one getting worse?

| Field | 8a STT span latency | 8b TTS span latency |
|---|---|---|
| View | Observations | Observations |
| Metric | `latency` / `p50` — and a second widget at `/ p95` | same |
| Breakdown | None | None |
| Filters | `type` `string` `=` `SPAN` **+** `name` `string` `=` `stt` | `type` `string` `=` `SPAN` **+** `name` `string` `=` `tts` |
| Chart Type | Line Chart | Line Chart |

**Why the spans, and not the six metadata scalars.** The voice session opens one
`stt` child span per turn and one `tts` child span per SENTENCE, both
`as_type="span"` (so observation `type = SPAN`) *inside* the app-root span — via
`child_observation()` in `backend/app/core/observability.py`. Because they are
real observations and real children of the turn, their `latency` IS the provider
round trip itself: it is measured from inside the pipeline, between handing the
stage its input and getting its result back, with no subtraction and no
inference. The same story is also in the six metadata scalars, but a metadata
value is not a measure, not a dimension and not a filter (§3), so no widget can
ever read it — Q12 is where that story is told in full. The span vocabulary
(`stt`/`tts`, their `ttfb_ms`/`bytes`/`index` metadata, and how the waterfall
reads) is owned by
[`../../docs/observability.md`](../../docs/observability.md) → "The voice trace
waterfall"; it is not restated here.

**Read 8b as per SENTENCE, not per turn.** The reply is split into speakable
chunks and every chunk is its own TTS request, so a turn that speaks three
chunks contributes three `tts` spans, and a short one contributes one. The
panel's p50/p95 is therefore *sentence synthesis time*, and the turn's total
speech cost is roughly that number × the span count in the window (Q11 returns
both). It is deliberately NOT the same question as the `tts_ttfb_ms` scalar in
Q12, which is stamped once per turn on the app-root row — comparing the two
directly is a category error, not a discrepancy.

**8a, by contrast, is per turn** — one `stt` span per turn — so its p50 IS
comparable with `stt_ms` in Q12. If the two disagree by more than a few
milliseconds, the span was opened at a different moment than the mark; that gap
is a signal about where the pipeline waited, not noise (see the "different
origins" note in `docs/observability.md`).

**Gotchas:** both panels stay empty until a voice turn lands — run the §0 probes
in [`queries.sql`](./queries.sql) before believing a blank chart. p50 and p95
need two widgets because a non-pivot chart accepts exactly one metric (§3), the
same reason 2a/2b are two widgets. And there is no p90 (§3): the p90 columns in
[`../../docs/voice.md`](../../docs/voice.md) come from the latency tool's own
client-side maths, so p90 has to be computed in SQL (Q11, Q12) rather than
placed on a dashboard. Ground truth Q11.

### 5.9 Dashboard "Voice turn latency" (widget 9)

Answers: how long is a spoken turn, end to end, on the server's own clock?

| Field | Value |
|---|---|
| View | Observations |
| Metric | `latency` / `p50` |
| Breakdown | None |
| Filters | `isRootObservation` `boolean` `=` `true` **+** `tags` `arrayOptions` `any of` `["channel:voice"]` |
| Chart Type | Line Chart |

**What the number is.** `isRootObservation = true` selects the app-owned root
span — one row per turn (§3). For a voice turn the voice session opens that span
in `_run_turn` around the WHOLE turn: before the `stt` span and after the last
audio byte is sent. Its duration is therefore approximately `turn_ms` from
`timing_metadata` (§6), and it still carries the `stt`/`tts` children inside it.
"Approximately" is deliberate: `turn_ms` is derived from `monotonic_now()` marks
while the span is timed by the SDK, so the two agree to within a few
milliseconds rather than exactly — a wide gap is a signal about where the
pipeline waited, the same read as the `ttfb_ms` note in
`docs/observability.md`.

**What it is not.** The span starts when the endpointer CLOSED the utterance, so
it EXCLUDES the trailing-silence window and the entire endpointing wait. An
answer that looks fast here can still have felt slow: `VOICE_VAD_MIN_SILENCE_MS`
moves the felt latency without moving this panel, which is exactly what the
300 ms experiment in [`../../docs/voice.md`](../../docs/voice.md) demonstrates.
It is not `perceived_ms` either — no trace can carry that number, for the reason
in §6.

**Gotchas:** a turn cut short by a barge-in still produces this row, and its
duration is short *by construction* — so a burst of interruptions reads as a
latency improvement. The `tags` filter (`any of`, §3) selects rows that HAVE a
tag and has no negated form, so there is no way to exclude the cancelled turns
from this panel; read it next to the barge-in rate that Q13 computes (the rate
is a ratio of two counts, so it is not a widget — §5.5). If cancellations are
frequent enough to move this line, that is itself the finding.

Ground truth Q3 (all channels) and Q12 (the per-stage split of the same rows),
with the `stt`/`tts` children after Q11.

## 6. Where each number comes from

Every widget reads one denormalised ClickHouse table, `default.events_full`
(a superset of the legacy `traces` / `observations` tables, which stay **empty**
on 4.30). This maps each panel element to the column and the code that writes
it:

| Panel element | Langfuse field | `events_full` column | Written by |
|---|---|---|---|
| Cost | `totalCost` | `total_cost` | **Inferred by Langfuse**: model-definition price × usage — no cost is ever ingested by the app |
| Token usage per type | `usageByType` | `usage_details[k]` | DeepSeek response → LangChain → Langfuse callback handler |
| Latency | `latency` | `end_time − start_time` | Span timestamps from the tracer |
| Run / trace count | `count` with `isRootObservation = true` | `is_app_root = 1` | The app-owned root span opened by `turn_span()` |
| Trace name | `traceName` | `trace_name` | `langfuse_trace_name` at invoke time, refined to `assistant:<route>` by `enrich_trace()` |
| Tags | `tags` | `tags` | `langfuse_tags`, built by `trace_attributes()` |
| Environment | `environment` | `environment` | Client construction: `environment=settings.env` |
| Model | `providedModelName` | `provided_model_name` | The `model=` the app sends for that tier |
| Level | `level` | `level` | Tracer; `ERROR` on a failed model call |
| Session / user | `sessionId` / `userId` | `session_id` / `user_id` | `langfuse_session_id` / `langfuse_user_id` |
| Trace metadata | trace metadata | `metadata_names` / `metadata_values` | `trace_attributes()`, see below |
| Voice language | trace metadata — **not chartable** | `metadata_values` at key `stt_lang` | `trace_attributes()` from the run's `stt_lang`; an EMPTY STRING on a typed turn |
| Voice stage timings | trace metadata — **not chartable** | `metadata_values` at the keys `stt_ms`, `graph_ttft_ms`, `tts_ttfb_ms`, `first_audio_ms`, `tail_ms`, `turn_ms` | `stamp_voice_timing()` → `timing_metadata()` (`backend/app/core/timing.py`): one scalar per voice turn, on the same app-root row |

The chain for the trace-metadata rows above:

```mermaid
flowchart LR
  A[DeepSeek response] --> B[RunTelemetry callback]
  B --> C[telemetry node]
  C --> D[IPAState]
  D --> E["trace_attributes()"]
  E --> F["turn_span + enrich_trace"]
  F --> G[Langfuse app-root span]
  G --> H["ClickHouse events_full (is_app_root = 1)"]
```

`trace_attributes()` (`backend/app/core/observability.py`) is the single place
that turns run state into trace identity. It produces the flat keys
`schema_version`, `route`, `lang`, `stt_lang`, `channel`, `tool_iterations`,
`llm_calls`, `input_tokens`, `output_tokens`, `cache_hit_tokens`,
`cache_miss_tokens`, `cache_hit_ratio`, `tools_called`, `tool_outcomes`,
`tool_error_taxonomy`, `llm_error_taxonomy`, plus the `env:*` / `channel:*` /
`status:*` tags.

The six voice stage timings come from the OTHER writer, `stamp_voice_timing`
(`timing_metadata` in `backend/app/core/timing.py`). It is called in
`_run_turn` after `enrich_trace`, and because `propagate_attributes` writes each
metadata key as its own span attribute the two calls ADD to the same app-root
row rather than replacing each other — so a voice turn carries the identity
above AND the timings. Each value is a decimal string in milliseconds, one
scalar per turn, and a stage that never ran is ABSENT rather than `0`: a
barge-in or a provider error leaves no `turn_ms`, which is why Q12 takes each
percentile with `quantileIf` over the rows that actually measured that stage —
a missing key must never be read as "instant", and must never be allowed to
pull a latency percentile down.

### `perceived_ms` is not server-side — it will never be on a dashboard

The measured tables in [`../../docs/voice.md`](../../docs/voice.md) quote
`perceived_ms` next to `first_audio_ms`. Only one of the two can exist in a
trace:

- `first_audio_ms` is the server's own number and IS trace metadata — the
  app-root scalar in the table above, readable only through Q12.
- `perceived_ms` is measured CLIENT-side by `scripts/voice_latency.py`, from the
  last frame the caller sent to the first audio byte it received. The server
  cannot observe the instant the caller stopped talking, so it cannot stamp it:
  `perceived_ms` is not a metadata key, not a span and not in `events_full`.
  **No dashboard or alert can show it, and any panel that claims to is wrong.**

Use `first_audio_ms` as the server-visible budget. The known gap that remains
even then: the VAD silence window (and the endpointing wait after it) is
invisible in traces, so the server's `first_audio_ms` is optimistic against what
the caller felt by roughly that window — quote `perceived_ms` from the latency
tool when the question is "what did it feel like".

Three consequences worth internalising:

- **Trace metadata lives on the app-root row only**, so trace-level panels must
  filter `isRootObservation = true` while observation-level ones (cost, latency,
  tokens) filter `type = GENERATION`.
- **Metadata is readable, not chartable.** Nothing in §3 can name a metadata
  key, so the voice stage timings have no widget by construction. They stay
  perfectly usable as numbers — that is what Q12 and §7 of
  [`queries.sql`](./queries.sql) are for — they just never become a panel or an
  alert condition.
- **Anything you want to slice a panel by must be added in code** — a new
  metadata key means a change in `trace_attributes()` and a
  `TRACE_SCHEMA_VERSION` bump, never just a dashboard edit. That bump is what
  keeps old and new traces distinguishable; the voice stage spans deliberately
  needed none (they add spans, not metadata — see `docs/observability.md`).

## 7. Weekly "read the dashboards" routine

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
7. **Voice latency and barge-in** (widgets 8a/8b/9, Q11–Q13) — if you used the
   voice channel, did `first_audio_ms` move, and did STT or TTS move with it? A
   rising barge-in rate with falling turn latency means the answers got slow
   enough that you started cutting them off; that pair is one reading, not two.

If you cannot answer one of these from the dashboards, add the widget/panel
now — the goal is that a week of real usage makes these one-glance reads.

## 8. Keeping it trustworthy

- Panels and `queries.sql` must agree. Any time you change the shape of trace
  metadata (`schema_version`), re-check the dashboard filters, re-run the
  matching queries AND re-run the `queries.sql` §0 probes; bump
  `TRACE_SCHEMA_VERSION` when you change the metadata shape so old and new
  traces stay distinguishable. The layout to remember: trace fields live on
  the app-root row of `default.events_full` (`is_app_root = 1`), and the
  legacy `traces` / `observations` tables stay empty on Langfuse 4.30.
- Dashboards are configuration stored by Langfuse, not code — back them up by
  backing up the Langfuse Postgres + ClickHouse volumes (see the backup
  runbook; both are already in `backup.conf` as items).
