#!/usr/bin/env python3
"""Optional cron alert probe for the self-hosted Langfuse stack.

Native Langfuse Alerts (see infra/observability/alerts.md) are the primary
alerting path. This probe is the deterministic fallback: every run it asks the
Metrics API v2 two questions and compares the answers against thresholds:

* sum of generation cost over the last 24 hours  -> cost-spike alert
* count of failed (level = ERROR) generations in the last hour -> error alert

Exit codes make it cron-friendly: 0 = all quiet, 2 = at least one WARNING,
3 = at least one ALERT, 1 = probe itself failed (auth, network, or an
unexpected response shape). When NOTIFY_WEBHOOK_URL is set and the run ends in
warning or alert, a short JSON message is POSTed there (a private ntfy topic
works with zero setup).

It uses only the Python standard library and talks to the same endpoint the
dashboards read from, so it is safe to run as root from cron on the DeskMini.

Configuration comes from a KEY=VALUE conf file (path via --conf) layered over
environment variables, which are layered over the defaults below. Example
conf file:

    LANGFUSE_BASE_URL=http://127.0.0.1:3000
    LANGFUSE_PUBLIC_KEY=pk-...
    LANGFUSE_SECRET_KEY=sk-...
    COST_WARNING_USD=0.10
    COST_ALERT_USD=0.50
    ERROR_WARNING=3
    ERROR_ALERT=10
    NOTIFY_WEBHOOK_URL=https://ntfy.sh/your-secret-topic

The Metrics API v2 response format varies slightly by build. If a run reports
"unexpected response shape", re-run with --show-response and compare against
the curl examples in infra/observability/alerts.md, then adjust the field-name
tokens at the top of `_sum_rows`.

Requires Python 3.10+ and the prod stack being up (the API it queries).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("langfuse-alert")

# Field-name tokens used to recognise the metric values inside each response
# row. Order matters (first hit wins) and matching is case-insensitive.
COST_TOKENS = ("sum_totalcost", "sum_total_cost", "totalcost", "total_cost", "cost")
COUNT_TOKENS = ("count_count", "count", "n")

DEFAULTS: dict[str, str] = {
    "LANGFUSE_BASE_URL": "http://127.0.0.1:3000",
    "LANGFUSE_PUBLIC_KEY": "",
    "LANGFUSE_SECRET_KEY": "",
    "COST_WINDOW_HOURS": "24",
    "COST_WARNING_USD": "0.10",
    "COST_ALERT_USD": "0.50",
    "ERROR_WINDOW_MINUTES": "60",
    "ERROR_WARNING": "3",
    "ERROR_ALERT": "10",
    "NOTIFY_WEBHOOK_URL": "",
}


def load_config(conf_path: Path) -> dict[str, str]:
    """Merge environment variables and a KEY=VALUE conf file over defaults.

    Args:
        conf_path: Path to the optional conf file.

    Returns:
        Merged config; conf file wins over environment, which wins over defaults.
    """
    cfg: dict[str, str] = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in os.environ:
            cfg[key] = os.environ[key]
    if conf_path.exists():
        for raw in conf_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip()
    return cfg


def metrics_query(
    cfg: dict[str, str],
    measure: str,
    aggregation: str,
    minutes_back: int,
    extra_filters: list[dict[str, str]],
) -> dict:
    """Build the v2 Metrics API query body for one aggregated measure.

    Args:
        cfg: Resolved config (for the base URL).
        measure: Observation measure, e.g. ``totalCost`` or ``count``.
        aggregation: Aggregation over the measure, e.g. ``sum``.
        minutes_back: How far back the window reaches (from now).
        extra_filters: Extra ``{column, operator, value, type}`` filters.

    Returns:
        The parsed JSON response of the metrics endpoint.
    """
    now = datetime.now(UTC)
    query = {
        "view": "observations",
        "metrics": [{"measure": measure, "aggregation": aggregation}],
        "filters": [
            {
                "column": "type",
                "operator": "=",
                "value": "GENERATION",
                "type": "string",
            },
            *extra_filters,
        ],
        "fromTimestamp": (now - timedelta(minutes=minutes_back)).isoformat(),
        "toTimestamp": now.isoformat(),
    }
    url = (
        f"{cfg['LANGFUSE_BASE_URL'].rstrip('/')}/api/public/v2/metrics?"
        + urllib.parse.urlencode({"query": json.dumps(query)})
    )
    request = urllib.request.Request(url)
    basic = (f"{cfg['LANGFUSE_PUBLIC_KEY']}:{cfg['LANGFUSE_SECRET_KEY']}").encode()
    import base64

    request.add_header("Authorization", "Basic " + base64.b64encode(basic).decode())
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _sum_rows(payload: dict, tokens: tuple[str, ...]) -> tuple[float, str | None]:
    """Sum the first token-matching numeric field across the response rows.

    Args:
        payload: Parsed v2 metrics response.
        tokens: Candidate field-name substrings, in priority order.

    Returns:
        (total, matched_field_name). ``matched_field_name`` is None and total 0
        when no row carried a recognised field.
    """
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return 0.0, None
    lowered = [key.lower() for key in rows[0]]
    for token in tokens:
        for key, lower in zip(rows[0], lowered):
            if token in lower:
                total = 0.0
                for row in rows:
                    value = row.get(key, 0) or 0
                    total += float(value)
                return total, key
    return 0.0, None


def notify(cfg: dict[str, str], body: dict) -> None:
    """POST a JSON alert body to the configured webhook (best-effort).

    Args:
        cfg: Resolved config.
        body: JSON-serialisable payload describing the breached thresholds.
    """
    url = cfg.get("NOTIFY_WEBHOOK_URL", "")
    if not url:
        return
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            logger.info("notification delivered to %s", url)
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:  # pragma: no cover - network boundary
        logger.warning("notification failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    """Run both probes and map the outcome to a cron-friendly exit code.

    Args:
        argv: CLI arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code 0 (quiet), 2 (warning), 3 (alert), or 1 (probe failure).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--conf",
        type=Path,
        default=Path.home() / ".langfuse-alert.conf",
        help="path to a KEY=VALUE conf file (optional)",
    )
    parser.add_argument(
        "--show-response",
        action="store_true",
        help="print the raw API responses and exit (debugging)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    cfg = load_config(args.conf)
    if not cfg["LANGFUSE_PUBLIC_KEY"] or not cfg["LANGFUSE_SECRET_KEY"]:
        logger.error("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are required")
        return 1

    severity = 0
    findings: list[dict[str, object]] = []
    try:
        cost_payload = metrics_query(
            cfg, "totalCost", "sum", int(cfg["COST_WINDOW_HOURS"]) * 60, []
        )
        error_payload = metrics_query(
            cfg,
            "count",
            "count",
            int(cfg["ERROR_WINDOW_MINUTES"]),
            [
                {
                    "column": "level",
                    "operator": "=",
                    "value": "ERROR",
                    "type": "string",
                }
            ],
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.error("metrics request failed: %s", exc)
        return 1

    if args.show_response:
        print(json.dumps({"cost": cost_payload, "errors": error_payload}, indent=2))
        return 0

    cost_usd, cost_field = _sum_rows(cost_payload, COST_TOKENS)
    error_count, count_field = _sum_rows(error_payload, COUNT_TOKENS)
    if cost_field is None or count_field is None:
        logger.error(
            "unexpected response shape (cost field %s, error field %s) - "
            "re-run with --show-response and check the curl examples",
            cost_field,
            count_field,
        )
        return 1

    cost_warn = float(cfg["COST_WARNING_USD"])
    cost_alert = float(cfg["COST_ALERT_USD"])
    err_warn = int(cfg["ERROR_WARNING"])
    err_alert = int(cfg["ERROR_ALERT"])

    checks = [
        ("cost_usd", cost_usd, cost_warn, cost_alert),
        ("error_count", float(error_count), float(err_warn), float(err_alert)),
    ]
    for name, value, warn, alert in checks:
        if value >= alert:
            severity = max(severity, 3)
            findings.append({"check": name, "value": value, "level": "ALERT"})
            logger.warning("%s ALERT: %.4f (threshold %.4f)", name, value, alert)
        elif value >= warn:
            severity = max(severity, 2)
            findings.append({"check": name, "value": value, "level": "WARNING"})
            logger.warning("%s WARNING: %.4f (threshold %.4f)", name, value, warn)
        else:
            logger.info("%s ok: %.4f", name, value)

    if findings and cfg.get("NOTIFY_WEBHOOK_URL"):
        notify(
            cfg,
            {
                "title": "taylored-assistant alert probe",
                "findings": findings,
                "checked_at": datetime.now(UTC).isoformat(),
            },
        )
    return severity


if __name__ == "__main__":
    sys.exit(main())
