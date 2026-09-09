# Production Alerts — cost spikes & elevated error rates

Alerts are how the DeskMini tells you something is wrong instead of you
finding out by reading a dashboard. The self-hosted Langfuse v4 build ships
native threshold **Alerts** (no alert limit on self-hosted) and the
**Metrics API v2** for programmatic checks. Both are covered here — native
alerts are the primary path; the cron probe at the end is the deterministic
fallback.

Companion files: [`dashboards.md`](./dashboards.md) (baselines come from the
weekly routine) and [`queries.sql`](./queries.sql) (the ground-truth numbers
an alert must agree with).

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
§1), otherwise `totalCost` is always 0 and the alert never fires.

Two flavours, pick what matches the spike you care about:

| | Catch the runaway day | Catch the runaway single call |
|---|---|---|
| **Name** | `Cost spike — daily budget` | `Cost spike — expensive generation` |
| **Data source** | Observations | Observations |
| **Metric** | `sum cost` (USD) | `p95 cost` (USD) |
| **Filters** | environment = production | environment = production, model contains `deepseek` |
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
2. Pick **Webhook** for the DeskMini (no account needed): URL = any endpoint
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

(`date -u -d` is GNU date — fine on the DeskMini's Linux.)

For unattended checks, run the probe script as a cron job:
`backend/scripts/langfuse_alert.py` (delivered with this runbook). It queries
the same two metrics, compares against the thresholds below, and exits `2`
(warning) / `3` (alert) so cron mail or a wrapper can act, optionally pushing
to a webhook.

```cron
# every hour at minute 5 (root crontab on the DeskMini)
5 * * * * /usr/local/bin/python3 /srv/taylored-assistant/backend/scripts/langfuse_alert.py \
    --conf /srv/taylored-assistant/backend/scripts/.langfuse-alert.conf >> /var/log/taylored-alert.log 2>&1
```

## 6. Keeping alerts trustworthy

- An alert is only as good as its baseline. Re-derive thresholds from the
  dashboards monthly, or after a model/prompt change that shifts cost or
  error rates.
- Re-run the breach test (§4 step 4) after any Langfuse upgrade or compose
  change — it proves the whole path end to end.
- Alerts are config stored by Langfuse (like dashboards), so they ride along
  with the Langfuse Postgres/ClickHouse volumes already in `backup.conf`.
