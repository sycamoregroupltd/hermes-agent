#!/usr/bin/env python3
"""Sycode line-of-sight monitor: lineage join rate, intent gap, SJE-FK watchdog.

Read-only / paper-only watchdog for jarvis-os/t_34675715.
Designed for Hermes no_agent cron: empty stdout when healthy or throttled; non-empty
stdout is the alert payload delivered by the cron layer.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PGHOST = os.environ.get("SYCODE_LOS_PGHOST", "localhost")
PGPORT = os.environ.get("SYCODE_LOS_PGPORT", "5432")
PGUSER = os.environ.get("SYCODE_LOS_PGUSER", "postgres")
PGDATABASE = os.environ.get("SYCODE_LOS_PGDATABASE", "postgres")
PGPASSWORD = os.environ.get("SYCODE_LOS_PGPASSWORD", os.environ.get("PGPASSWORD", "postgres"))
SERVER_CONTAINER = os.environ.get("SYCODE_LOS_SERVER_CONTAINER", "sycodetrading-server")
METRICS_URL = os.environ.get("SYCODE_LOS_METRICS_URL", "http://localhost:3001/metrics")
STATE_PATH = Path(os.environ.get("SYCODE_LOS_STATE_PATH", "/home/frank/.hermes/var/sycode_line_of_sight_monitor_state.json"))
STATUS_PATH = Path(os.environ.get("SYCODE_LOS_STATUS_PATH", "/home/frank/.hermes/var/sycode_line_of_sight_monitor_status.json"))
COOLDOWN_SECONDS = int(os.environ.get("SYCODE_LOS_ALERT_COOLDOWN_SECONDS", "3600"))
LINEAGE_WARN_PCT = float(os.environ.get("SYCODE_LOS_LINEAGE_WARN_PCT", "95"))
LINEAGE_PAGE_PCT = float(os.environ.get("SYCODE_LOS_LINEAGE_PAGE_PCT", "90"))
INTENT_GAP_THRESHOLD = int(os.environ.get("SYCODE_LOS_INTENT_GAP_THRESHOLD", "0"))
SJE_FK_THRESHOLD = int(os.environ.get("SYCODE_LOS_SJE_FK_THRESHOLD", "0"))
LOG_SINCE = os.environ.get("SYCODE_LOS_LOG_SINCE", "24h")
SCRIPT_ID = "sycode-line-of-sight-monitor:t_34675715"

LINEAGE_SQL = r"""
WITH params AS (
  SELECT GREATEST(now() - interval '7 days',
                  timestamptz '2026-07-05 00:00:00+00') AS window_start
), closes AS (
  SELECT mp.id, mp.symbol, mp.close_reason, mp.closed_at
  FROM managed_positions mp, params p
  WHERE mp.status = 'closed'
    AND mp.closed_at >= p.window_start
    AND COALESCE(mp.close_reason, '') <> 'reconciliation'
), linked AS (
  SELECT
    c.id,
    c.symbol,
    c.closed_at,
    EXISTS (SELECT 1 FROM trade_intents ti WHERE ti.position_id = c.id) AS has_intent,
    EXISTS (SELECT 1 FROM trade_close_events tce WHERE tce.position_id = c.id AND tce.correlation_id IS NOT NULL) AS has_close_event,
    EXISTS (SELECT 1 FROM signal_journeys sj WHERE sj.trade_id = c.id::text AND sj.realized_pnl_percent IS NOT NULL) AS has_realized
  FROM closes c
)
SELECT
  count(*)::integer AS closes,
  count(*) FILTER (WHERE has_intent)::integer AS with_intent,
  count(*) FILTER (WHERE has_close_event)::integer AS with_close_event,
  count(*) FILTER (WHERE has_realized)::integer AS with_realized,
  count(*) FILTER (WHERE has_intent AND has_close_event AND has_realized)::integer AS fully_joined,
  round(100.0 * count(*) FILTER (WHERE has_intent AND has_close_event AND has_realized) / NULLIF(count(*), 0), 2)::text AS full_join_rate_pct
FROM linked;
"""

INTENT_GAP_SQL = r"""
SELECT count(*)::integer AS intent_gap_24h
FROM managed_positions mp
WHERE mp.status = 'closed'
  AND mp.closed_at >= now() - interval '24 hours'
  AND COALESCE(mp.close_reason, '') <> 'reconciliation'
  AND NOT EXISTS (SELECT 1 FROM trade_intents ti WHERE ti.position_id = mp.id);
"""


@dataclass
class Alert:
    key: str
    severity: str
    title: str
    detail: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_psql(sql: str, timeout: int = 120) -> list[dict[str, str]]:
    # The canonical 7d lineage query can exceed 30s during DB slot pressure; keep
    # it bounded but do not turn transient slowness into a permanent blind spot.
    wrapped = "BEGIN READ ONLY; SET LOCAL statement_timeout = '90s';\n" + sql + "\nROLLBACK;"
    cmd = [
        "psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", PGDATABASE,
        "-X", "-q", "-v", "ON_ERROR_STOP=1", "--csv", "--pset", "footer=off", "-c", wrapped,
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = PGPASSWORD
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:800])
    lines = [line for line in proc.stdout.splitlines() if line and line not in ("BEGIN", "ROLLBACK")]
    return list(csv.DictReader(io.StringIO("\n".join(lines))))


def one_row(sql: str) -> dict[str, str]:
    rows = run_psql(sql)
    return rows[0] if rows else {}


def parse_float(v: Any) -> float | None:
    if v in (None, "", "NULL"):
        return None
    return float(v)


def parse_int(v: Any) -> int:
    if v in (None, "", "NULL"):
        return 0
    return int(float(v))


def fetch_metrics() -> dict[str, Any]:
    try:
        proc = subprocess.run(["curl", "-fsS", METRICS_URL], capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[:300]}
        failed = None
        healthy = None
        for line in proc.stdout.splitlines():
            if line.startswith('sycodetrading_bullmq_queue_failed{queue="database-writes"}'):
                failed = float(line.rsplit(" ", 1)[-1])
            elif line.startswith('sycodetrading_bullmq_queue_healthy{queue="database-writes"}'):
                healthy = float(line.rsplit(" ", 1)[-1])
        return {"ok": True, "database_writes_failed": failed, "database_writes_healthy": healthy}
    except Exception as exc:  # noqa: BLE001 - monitor must report, not crash silently
        return {"ok": False, "error": str(exc)[:300]}


def count_sje_fk_logs() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["docker", "logs", "--since", LOG_SINCE, SERVER_CONTAINER],
            capture_output=True, text=True, timeout=60,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0:
            return {"ok": False, "count": 0, "error": text.strip()[:500], "since": LOG_SINCE}
        # Primary exact constraint; fallback keeps us sensitive if the logger redacts the constraint name.
        exact = len(re.findall(r"signal_journey_events_journey_id_fkey", text))
        fallback = len(re.findall(r"signal_journey_events.*foreign key|foreign key.*signal_journey_events", text, re.IGNORECASE))
        count = max(exact, fallback)
        return {"ok": True, "count": count, "per_hour": round(count / 24.0, 3), "since": LOG_SINCE}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "count": 0, "error": str(exc)[:500], "since": LOG_SINCE}


def evaluate(snapshot: dict[str, Any]) -> list[Alert]:
    alerts: list[Alert] = []
    lineage = snapshot.get("lineage", {})
    pct = lineage.get("full_join_rate_pct")
    if pct is not None:
        if pct < LINEAGE_PAGE_PCT:
            alerts.append(Alert("lineage_page", "PAGE", "7d lineage join rate below page threshold", f"fully_joined={pct:.2f}% < {LINEAGE_PAGE_PCT:.0f}% (closes={lineage.get('closes')}, fully_joined={lineage.get('fully_joined')})"))
        elif pct < LINEAGE_WARN_PCT:
            alerts.append(Alert("lineage_warn", "WARN", "7d lineage join rate below warning threshold", f"fully_joined={pct:.2f}% < {LINEAGE_WARN_PCT:.0f}% (closes={lineage.get('closes')}, fully_joined={lineage.get('fully_joined')})"))
    elif lineage.get("closes", 0) > 0:
        alerts.append(Alert("lineage_unknown", "WARN", "7d lineage join rate could not be computed", json.dumps(lineage, sort_keys=True)))

    gap = int(snapshot.get("intent_gap_24h", 0))
    if gap > INTENT_GAP_THRESHOLD:
        alerts.append(Alert("intent_gap", "PAGE", "Closed positions lack trade_intents link", f"intent_gap_24h={gap} > {INTENT_GAP_THRESHOLD}"))

    sje = snapshot.get("sje_fk", {})
    if not sje.get("ok"):
        alerts.append(Alert("sje_fk_monitor_degraded", "WARN", "SJE-FK watchdog could not inspect server logs", sje.get("error", "unknown error")))
    elif int(sje.get("count", 0)) > SJE_FK_THRESHOLD:
        alerts.append(Alert("sje_fk", "PAGE", "signal_journey_events FK failures detected", f"signal_journey_events_journey_id_fkey count={sje.get('count')} over {sje.get('since')} (~{sje.get('per_hour')}/h) > {SJE_FK_THRESHOLD}"))

    metrics = snapshot.get("metrics", {})
    if metrics.get("ok") and metrics.get("database_writes_healthy") == 0:
        alerts.append(Alert("database_writes_unhealthy", "PAGE", "BullMQ database-writes queue unhealthy", json.dumps(metrics, sort_keys=True)))
    return alerts


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def filter_cooldown(alerts: list[Alert], dry_run: bool) -> tuple[list[Alert], dict[str, Any]]:
    state = load_json(STATE_PATH)
    now = time.time()
    emitted: list[Alert] = []
    for alert in alerts:
        last = float(state.get(alert.key, {}).get("last_sent_epoch", 0) or 0)
        if dry_run or not last or now - last >= COOLDOWN_SECONDS:
            emitted.append(alert)
            state.setdefault(alert.key, {})["last_sent_epoch"] = now
            state[alert.key]["last_sent_utc"] = utc_now()
            state[alert.key]["last_severity"] = alert.severity
    return emitted, state


def collect_snapshot() -> dict[str, Any]:
    lineage_row = one_row(LINEAGE_SQL)
    intent_row = one_row(INTENT_GAP_SQL)
    lineage = {
        "closes": parse_int(lineage_row.get("closes")),
        "with_intent": parse_int(lineage_row.get("with_intent")),
        "with_close_event": parse_int(lineage_row.get("with_close_event")),
        "with_realized": parse_int(lineage_row.get("with_realized")),
        "fully_joined": parse_int(lineage_row.get("fully_joined")),
        "full_join_rate_pct": parse_float(lineage_row.get("full_join_rate_pct")),
    }
    return {
        "script_id": SCRIPT_ID,
        "timestamp_utc": utc_now(),
        "safety": "READ_ONLY_PAPER_ONLY_NO_DB_MUTATION_NO_RESTART_NO_DEPLOY_NO_TRADING",
        "lineage": lineage,
        "intent_gap_24h": parse_int(intent_row.get("intent_gap_24h")),
        "sje_fk": count_sje_fk_logs(),
        "metrics": fetch_metrics(),
        "thresholds": {
            "lineage_warn_pct": LINEAGE_WARN_PCT,
            "lineage_page_pct": LINEAGE_PAGE_PCT,
            "intent_gap_threshold": INTENT_GAP_THRESHOLD,
            "sje_fk_threshold": SJE_FK_THRESHOLD,
            "cooldown_seconds": COOLDOWN_SECONDS,
        },
    }


def format_alert(snapshot: dict[str, Any], alerts: list[Alert]) -> str:
    lines = [
        "🚨 SYCODE LINE-OF-SIGHT MONITOR ALERT 🚨",
        f"timestamp_utc: {snapshot['timestamp_utc']}",
        "scope: lineage_join_rate_7d + closed-position intent gap + signal_journey_events FK watchdog",
        "safety: read-only / paper-only; no DB mutation, no restart, no deploy, no trading action",
        "",
        "Alerts:",
    ]
    for alert in alerts:
        lines.append(f"- [{alert.severity}] {alert.title}: {alert.detail}")
    lines += [
        "",
        "Snapshot:",
        f"- lineage_7d: {snapshot['lineage']}",
        f"- intent_gap_24h: {snapshot['intent_gap_24h']}",
        f"- sje_fk: {snapshot['sje_fk']}",
        f"- bullmq_database_writes_metrics: {snapshot['metrics']}",
        "",
        "Runbook owner: Sycode Trading PM / trading-devops. Source card: jarvis-os/t_34675715.",
    ]
    return "\n".join(lines)


def self_test() -> int:
    sample = {
        "lineage": {"closes": 10, "fully_joined": 8, "full_join_rate_pct": 80.0},
        "intent_gap_24h": 1,
        "sje_fk": {"ok": True, "count": 2, "since": "24h", "per_hour": 0.083},
        "metrics": {"ok": True, "database_writes_failed": 0.0, "database_writes_healthy": 1.0},
    }
    alerts = evaluate(sample)
    keys = {a.key for a in alerts}
    assert {"lineage_page", "intent_gap", "sje_fk"}.issubset(keys), keys
    sample["lineage"]["full_join_rate_pct"] = 93.0
    assert any(a.key == "lineage_warn" for a in evaluate(sample)), "lineage warn missing"
    sample["lineage"]["full_join_rate_pct"] = 100.0
    sample["intent_gap_24h"] = 0
    sample["sje_fk"]["count"] = 0
    assert evaluate(sample) == [], "healthy sample should not alert"
    print("SELF_TEST_PASS sycode_line_of_sight_monitor")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print snapshot + alert decision; do not update cooldown state")
    parser.add_argument("--verbose", action="store_true", help="print healthy/throttled status too")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        snapshot = collect_snapshot()
        alerts = evaluate(snapshot)
        snapshot["alerts"] = [asdict(a) for a in alerts]
        snapshot["status"] = "ALERT" if alerts else "OK"
        if not args.dry_run:
            atomic_write(STATUS_PATH, snapshot)
        emitted, state = filter_cooldown(alerts, dry_run=args.dry_run)
        if not args.dry_run:
            atomic_write(STATE_PATH, state)
        if emitted:
            print(format_alert(snapshot, emitted))
        elif args.dry_run or args.verbose:
            print(json.dumps({"snapshot": snapshot, "alerts_after_cooldown": [asdict(a) for a in emitted]}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - no_agent must deliver monitor blindness visibly
        payload = {
            "script_id": SCRIPT_ID,
            "timestamp_utc": utc_now(),
            "status": "MONITOR_ERROR",
            "error": str(exc)[:1000],
            "safety": "read-only monitor failed before any mutation",
        }
        try:
            atomic_write(STATUS_PATH, payload)
        except Exception:
            pass
        print("🚨 SYCODE LINE-OF-SIGHT MONITOR ERROR 🚨")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
