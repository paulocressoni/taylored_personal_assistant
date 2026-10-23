# Production Alerts — cost spikes & elevated error rates

Alerts are how the host tells you something is wrong instead of you
finding out by reading a dashboard. The self-hosted Langfuse v4 build ships
native threshold **Alerts** (no alert limit on self-hosted) and the
**Metrics API v2** for programmatic checks. Both are covered here — native
alerts are the primary path; the cron probe at the end is the deterministic
fallback.

Companion files: [`dashboards.md`](./dashboards.md) (baselines come from the
weekly routine) and [`queries.sql`](./queries.sql) (the ground-truth numbers
an alert must agree with). §6 is the odd one out: a threshold that is
**documented but not automated**, because it reads a trace metadata scalar and
neither native alerts nor the probe script can see one.

## 1. How native alerts work

Project → **Alerts** (URL ends in `.../project/~/monitors`) → **New Alert**.
An alert is three parts:

- **Metric** — Data source (`Observations` for us), a metric such as
  `count`, `sum cost`, `p95 cost`, `avg latency`, plus optional **Filters**
  (model name, tags, user id, environment…).
- **Conditions** — Operator (`>`, `≥`, …), **Warning threshold** (optional →
  severity WARNING), **Alert threshold** (required → severity ALERT), and a
  **Window** each evaluation looks back (`1 hour`, `1 day`, `1 week`).
- **Notification** — one or more **Automations** (Slack message, GitHub
  Action, or a plain **Webhook**) that fire when severity changes.

Two settings matter for a home box that goes quiet overnight:

- **No-data handling**: choose **"Treat missing data as 0"** so no usage is
  not an error — you never get paged at 3 am because nobody talked to the
  assistant.
- **Renotify**: `Off` (default) notifies once per severity change, which is
  the right amount for personal use. Leave it off.

## 2. Alert A — cost spike

Cost alerts only work once DeepSeek model pricing is set (dashboards runbook
§1), otherwise `totalCost` is always 0 and the alert never fires. The
environment filter is the Langfuse **environment** attribute, which the app
sets from `ENV` — that is `prod` on the deployment host, so filtering on
`production` matches nothing.

Two flavours, pick what matches the spike you care about:

| | Catch the runaway day | Catch the runaway single call |
|---|---|---|
| **Name** | `Cost spike — daily budget` | `Cost spike — expensive generation` |
| **Data source** | Observations | Observations |
| **Metric** | `sum cost` (USD) | `p95 cost` (USD) |
| **Filters** | environment = prod | environment = prod, model contains `deepseek` |
| **Window** | 1 day | 1 day |
| **Warning** | your `WARN_DAY` | your `WARN_P95` |
| **Alert** | your `ALERT_DAY` | your `ALERT_P95` |
| **Catches** | a ReAct loop / many calls burn the day's budget | one call (huge cached prompt + long output) is abnormally expensive |

Don't invent thresholds — derive them from the dashboard baseline:

1. After ~1 week of real use, read **Cost per day** from the dashboard and
   note the median day (call it `M`) and the 95th-percentile single
   generation cost (call it `P95`).
2. Set `WARN_DAY = 2 × M`, `ALERT_DAY = 5 × M`; set `WARN_P95 = 2 × P95`,
   `ALERT_P95 = 5 × P95`.
3. Until you have that baseline, start conservative (low) and expect a few
   test fires — a false alert you see is a cheap way to learn the pipeline
   is working.

## 3. Alert B — elevated error rate

Data source **Observations**, metric `count`, filtered to failures. Whether
the level filter is exposed depends on the exact build, so verify with one
breach test before trusting it.

| | Value |
|---|---|
| **Name** | `Elevated error rate` |
| **Data source** | Observations |
| **Metric** | `count` |
| **Filter** | `level = ERROR` (verify availability; if missing use §5 probe) |
| **Window** | 1 hour |
| **Warning** | `> 3` (personal usage: more than a few failures an hour is abnormal) |
| **Alert** | `> 10` |
| **No-data** | Treat missing data as 0 |

What this counts: every failed LLM generation in the window (rate limits,
timeouts, auth) — the same population as ground-truth Q8b. Tool-side failures
and permission denials surface as trace tags (`status:tool_error`,
`status:permission_denied`), which are not an Observations metric, so those
belong to the dashboards (4.4/4.6) rather than to native alerts.

## 4. Notifications via Automations

1. Project → **Settings → Automations** (or the Automations panel next to
   Alerts) → **New Automation**.
2. Pick **Webhook** on the host (no account needed): URL = any endpoint
   that can ding you. The zero-setup option is a private
   [ntfy](https://ntfy.sh) topic: `https://ntfy.sh/<your-secret-topic>` —
   subscribe on your phone and alerts become push notifications. (Slack /
   GitHub Actions work the same way if you already run them.)
3. Attach the automation to each alert in **Notification channel** when
   creating/editing it.
4. **Test the whole path** (cheap breach test): temporarily set Alert A's
   alert threshold to `0` and wait for the next evaluation (≤ window length).
   You should get the push/webhook within minutes. Restore the real
   threshold. Repeat once for Alert B.

## 5. Deterministic fallback — Metrics API v2 + cron probe

If you ever want an alert that native alerts can't express (e.g. an error
*ratio*, or a check that doesn't depend on the UI), use the Metrics API v2 —
also available on self-hosted v4. It answers the same questions as
`queries.sql` over HTTP with HTTP-Basic auth
(`LANGFUSE_PUBLIC_KEY : LANGFUSE_SECRET_KEY`).

Sanity check from any shell (from the repo root):

```bash
# ERROR generations in the last hour
curl -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" -G \
  --data-urlencode 'query={"view":"observations",\
    "metrics":[{"measure":"count","aggregation":"count"}],\
    "filters":[{"column":"type","operator":"=","value":"GENERATION","type":"string"},\
               {"column":"level","operator":"=","value":"ERROR","type":"string"}],\
    "fromTimestamp":"'"$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)"'",\
    "toTimestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}' \
  "$LANGFUSE_BASE_URL/api/public/v2/metrics"

# Sum cost over the last 24 hours
curl -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" -G \
  --data-urlencode 'query={"view":"observations",\
    "metrics":[{"measure":"totalCost","aggregation":"sum"}],\
    "filters":[{"column":"type","operator":"=","value":"GENERATION","type":"string"}],\
    "fromTimestamp":"'"$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)"'",\
    "toTimestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}' \
  "$LANGFUSE_BASE_URL/api/public/v2/metrics"
```

(`date -u -d` is GNU date — fine on Linux.)

For unattended checks, run the probe script as a cron job:
`backend/scripts/langfuse_alert.py` (delivered with this runbook). It queries
the same two metrics, compares against the thresholds in §2 and §3, and exits `2`
(warning) / `3` (alert) so cron mail or a wrapper can act, optionally pushing
to a webhook. Note what it does NOT do: it has no ClickHouse path, so it cannot
evaluate the voice threshold in §6.

```cron
# every hour at minute 5 (root crontab on the deployment host)
5 * * * * /usr/local/bin/python3 /srv/taylored-assistant/backend/scripts/langfuse_alert.py \
    --conf /srv/taylored-assistant/backend/scripts/.langfuse-alert.conf >> /var/log/taylored-alert.log 2>&1
```

## 6. Alert C — voice turn latency (documented, NOT automated yet)

Voice is the one channel where a slow answer is felt immediately, so it deserves a
threshold. It cannot be a native alert, and the reason is the same limitation §3
already documents for tags:

- A native alert reads the **Metrics API / observation metrics** only. Its
  Filters offer model name, tags, user id, environment, level and type (§1) —
  there is no way to name a trace **metadata** key, so the six voice stage
  scalars (`stt_ms`, `graph_ttft_ms`, `tts_ttfb_ms`, `first_audio_ms`, `tail_ms`,
  `turn_ms`) are invisible to it. Dashboards have exactly the same hole; see
  `dashboards.md` §3.
- What a native alert CAN see is a span metric, so an alert on the **`tts`
  span** (metric `p95 latency`, filter `type = SPAN` + `name = tts`) is
  expressible — but it aggregates per SENTENCE, not per turn (dashboards.md
  §5.8), which is the wrong unit for "did the assistant get slow".

So this check belongs to the **§5 deterministic path**: a ClickHouse query,
evaluated by you or by a wrapper, over the number the dashboard cannot show.

### The query

Use the voice stage-metric query from
[`queries.sql`](./queries.sql) — **Q12** — and watch `first_audio_p90_ms`:

```bash
docker compose -p taylored-assistant-prod \
  -f infra/compose/docker-compose.base.yml \
  -f infra/compose/docker-compose.prod.yml \
  --env-file infra/compose/.env.prod \
  exec -T clickhouse clickhouse-client --query "<Q12 from queries.sql>"
```

Q12 groups by day; drop the `toDate(start_time) AS day` column and the `GROUP BY`
to get one figure per window, which is what a cron wrapper wants. It filters
`is_app_root = 1` + tag `channel:voice`, reads the scalars with
`arrayElement(metadata_values, indexOf(metadata_names, '<key>'))`, and takes each
percentile with `quantileIf` over the turns that actually measured that stage —
so a barge-in cannot drag `first_audio_p90_ms` down and hide a regression.

### The threshold

Derived from the CURRENT measured table in
[`../../docs/voice.md`](../../docs/voice.md) — "After the eager first chunk
(`VOICE_VAD_MIN_SILENCE_MS=300`)":

| Stage | p50 | p90 |
|---|---|---|
| `stt_ms` | 282 ms | 291 ms |
| `graph_ttft_ms` | 2452 ms | 2861 ms |
| `tts_ttfb_ms` | 623 ms | 676 ms |
| **`first_audio_ms`** | **3419 ms** | **3685 ms** |
| `perceived_ms` (from the caller's last frame) | 3499 ms | 3766 ms |

`first_audio_ms` is the server-visible budget, and its **p90 is 3685 ms** — that
is the only number here a trace can actually carry (`perceived_ms` is measured
client-side by the latency tool and is not trace metadata; `dashboards.md` §6):

- **WARNING: `first_audio_p90_ms > 3700`** — the lowest p90 measured across the
  three tables in `docs/voice.md` (3685 ms), rounded up.
- **ALERT: `first_audio_p90_ms > 7400`** — twice it: the assistant is taking
  roughly double the worst turn ever measured on this fixture.

Two honest caveats about that baseline: it rests on **5 runs** (the latency tool
is `--runs 5 --warmup 1`), which is far too few to call a p90 stable, and it was
taken at `VOICE_VAD_MIN_SILENCE_MS=300` rather than the shipped default of 500.
The silence window does not affect `first_audio_ms` at all — that is the whole
point of the experiment in `docs/voice.md` — so the second caveat only matters
if you compare against `perceived_ms`. **Re-derive both numbers after a week of
real voice use**, exactly as §2 does for cost, and treat the figures above as a
starting point rather than a specification.

### Not wired up yet

**No alert fires today.** There is no cron entry, no script and no webhook for
this threshold — it is a number and a query you run by hand. The existing probe,
`backend/scripts/langfuse_alert.py`, cannot take this job over as written: it
speaks the **Metrics API v2** (§5), which is the same interface that cannot read
metadata. Giving it a ClickHouse path is a code change and a separate follow-up —
until that lands, do not assume anything is watching this.

## 7. Keeping alerts trustworthy

- An alert is only as good as its baseline. Re-derive thresholds from the
  dashboards monthly, or after a model/prompt change that shifts cost or
  error rates.
- Re-run the breach test (§4 step 4) after any Langfuse upgrade or compose
  change — it proves the whole path end to end.
- Alerts are config stored by Langfuse (like dashboards), so they ride along
  with the Langfuse Postgres/ClickHouse volumes already in `backup.conf`.
