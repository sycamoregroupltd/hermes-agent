#!/usr/bin/env python3
"""
sycode_rmultiple_tracking.py - idempotent R-multiple tracking for G-R1 (I-4).

Uses idempotency key: gqt-integrity-2026-08-06-I4

WHAT THIS DOES
--------------
Read-only measurement of r_multiple_labels health against the I-4 acceptance
criteria (WS01: writer running daily; max lag <48h for settled cohort), plus
quality dimensions (outcome mix, labeler version, contamination, r_achieved
distribution) and the jarvis cron liveness signal. Persisted to a single JSONL
state file keyed by the idempotency key above. Safe to re-run: identical
measurements for the same UTC day are deduped; new measurements append a new
record. No DB mutation (INSERT/UPDATE/DELETE/DDL) -- SELECT only.

The JSONL file is the queryable store. A vault note is rendered from the latest
record (see --render-vault, which prints markdown to stdout so the caller can
persist it via the governed write path).

USAGE
    python3 ~/.hermes/scripts/sycode_rmultiple_tracking.py
    python3 ~/.hermes/scripts/sycode_rmultiple_tracking.py --json
    python3 ~/.hermes/scripts/sycode_rmultiple_tracking.py --render-vault

EXIT CODES
    0  measurement recorded (or deduped no-op)
    1  a tracked criterion is failing
    3  harness error (DB unreachable, query error)
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

KEY = "gqt-integrity-2026-08-06-I4"
TICKET = "t_af3b0e99"
GAP = "G-R1"
CONTAINER = "sycodetrading-supabase-db"
STATE_DIR = "/home/frank/.hermes/state"
STATE = os.path.join(STATE_DIR, "gqt-rmultiple-g-r1.jsonl")
JARVIS_JOBS = "/home/frank/.hermes/profiles/jarvis/cron/jobs.json"
RML_CRON_JOB_ID = "64b9e461b28f"
RML_CRON_JOB_NAME = "sycode-r-multiple-labeler"
MUTATION_LOCK = "/home/frank/.hermes/locks/sycode-data-mutation.lock"
STMT_TIMEOUT = "240s"

# Precise read-only guard: match whole SQL statement keywords with surrounding spaces.
_FORBIDDEN = (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER ",
              " CREATE ", " TRUNCATE ", " VACUUM ", " REINDEX ", " GRANT ",
              " REVOKE ", " SET ", " SET SCHEMA", " COPY ", " ATTACH ",
              " DETACH ", " REFRESH ", " NOTIFY ", " LISTEN ")


def read_mutation_lock():
    try:
        with open(MUTATION_LOCK) as fh:
            raw = fh.read().strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw[:300]}
    except FileNotFoundError:
        return None
    except OSError as e:
        return {"raw": f"lock unreadable: {e}"}


def q(sql, timeout=300):
    """SELECT-only via docker exec psql. Returns (value_str, error). Never raises.
    Takes the LAST output line (single-value queries). Use q_all() for multi-row."""
    up = sql.upper()
    for w in _FORBIDDEN:
        if w in up:
            return None, f"refused: statement is not read-only ({w.strip()})"
    prefix = f"SET statement_timeout='{STMT_TIMEOUT}'; "
    cmd = ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", "postgres",
           "-At", "-c", prefix + sql]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"harness timeout after {timeout}s"
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:400]
    lines = [l for l in r.stdout.strip().splitlines() if l.strip() and l.strip() != "SET"]
    return (lines[-1].strip() if lines else None), None


def q_all(sql, timeout=300):
    """SELECT-only via docker exec psql returning ALL output lines (multi-row).
    Returns (list[str], error). Never raises."""
    up = sql.upper()
    for w in _FORBIDDEN:
        if w in up:
            return None, f"refused: statement is not read-only ({w.strip()})"
    prefix = f"SET statement_timeout='{STMT_TIMEOUT}'; "
    cmd = ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", "postgres",
           "-At", "-c", prefix + sql]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"harness timeout after {timeout}s"
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:400]
    lines = [l.strip() for l in r.stdout.strip().splitlines()
             if l.strip() and l.strip() != "SET"]
    return lines, None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_rows(raw):
    """raw (list[str] or str) -> dict of first-column -> count."""
    out = {}
    if not raw:
        return out
    lines = raw.split("\n") if isinstance(raw, str) else raw
    for line in lines:
        if not line:
            continue
        f = line.split("|")
        if len(f) >= 2:
            out[f[0]] = f[1]
    return out


def cron_liveness():
    """Read jarvis jobs.json for the r-multiple labeler job. Returns dict or error."""
    try:
        with open(JARVIS_JOBS) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"jobs.json unreadable: {e}"}
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return {"error": f"jobs.json has unexpected shape: {type(jobs).__name__}"}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("id") == RML_CRON_JOB_ID or job.get("name") == RML_CRON_JOB_NAME:
            return {
                "id": job.get("id"),
                "name": job.get("name"),
                "enabled": job.get("enabled"),
                "script": job.get("script"),
                "schedule_display": job.get("schedule_display"),
                "last_run_at": job.get("last_run_at"),
                "last_status": job.get("last_status"),
                "last_error": (job.get("last_error") or "")[:300],
                "next_run_at": job.get("next_run_at"),
            }
    return {"error": f"job {RML_CRON_JOB_ID} not found in {JARVIS_JOBS}"}


def measure():
    """Returns a dict of the G-R1 R-multiple tracking measurement."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Core: total, HWM, lag_h, 24h volume, max id
    sql_core = """
    SELECT count(*) AS total,
           max(computed_at) AS max_computed,
           round(extract(epoch FROM (now()-max(computed_at)))/3600.0,2) AS lag_h,
           count(*) FILTER (WHERE computed_at >= now()-interval '24 hours') AS rows_24h,
           max(id) AS max_id
    FROM r_multiple_labels;"""
    raw, err = q(sql_core)
    if err:
        return {"error": err, "idempotency_key": KEY, "measured_at": stamp}
    parts = (raw or "").split("|")
    core = {
        "total": parts[0] if len(parts) > 0 else "?",
        "max_computed": parts[1] if len(parts) > 1 else "?",
        "lag_h": parts[2] if len(parts) > 2 else "?",
        "rows_24h": parts[3] if len(parts) > 3 else "?",
        "max_id": parts[4] if len(parts) > 4 else "?",
    }
    if core["total"] in (None, "", "?"):
        return {"error": "core query returned no row", "idempotency_key": KEY,
                "measured_at": stamp}

    # Rows in the last hour (liveness trend; a healthy 15m writer is always >0)
    raw1h, err1h = q("""
    SELECT count(*) FROM r_multiple_labels WHERE computed_at >= now()-interval '1 hour';""")
    rows_1h = raw1h if not err1h else f"err:{err1h}"

    # Daily cadence (last 10 days)
    raw_daily, err_daily = q_all("""
    SELECT to_char(date_trunc('day', computed_at),'YYYY-MM-DD'), count(*)
    FROM r_multiple_labels
    WHERE computed_at >= now()-interval '10 days'
    GROUP BY 1 ORDER BY 1;""")
    daily = {"error": err_daily} if err_daily else parse_rows(raw_daily)

    # Outcome mix (last 2 days)
    raw_out, err_out = q_all("""
    SELECT outcome, count(*) FROM r_multiple_labels
    WHERE computed_at >= now()-interval '2 days'
    GROUP BY 1 ORDER BY 2 DESC;""")
    outcomes = {"error": err_out} if err_out else parse_rows(raw_out)

    # Labeler version + contamination
    raw_ver, err_ver = q_all("""
    SELECT labeler_version, count(*) FROM r_multiple_labels GROUP BY 1 ORDER BY 2 DESC;""")
    versions = {"error": err_ver} if err_ver else parse_rows(raw_ver)

    raw_contam, err_contam = q("""
    SELECT count(*) FILTER (WHERE contaminated IS TRUE),
           count(*) FILTER (WHERE contaminated IS NOT TRUE),
           count(*) FILTER (WHERE contamination_reason IS NOT NULL)
    FROM r_multiple_labels;""")
    if err_contam:
        contam = {"error": err_contam}
    else:
        cf = (raw_contam or "").split("|")
        contam = {"contaminated": cf[0] if len(cf) > 0 else "?",
                  "clean": cf[1] if len(cf) > 1 else "?",
                  "with_reason": cf[2] if len(cf) > 2 else "?"}

    # r_achieved distribution (min/p50/p95/max)
    raw_r, err_r = q("""
    SELECT round(min(r_achieved)::numeric,3),
           round(percentile_cont(0.5) WITHIN GROUP (ORDER BY r_achieved)::numeric,3),
           round(percentile_cont(0.95) WITHIN GROUP (ORDER BY r_achieved)::numeric,3),
           round(max(r_achieved)::numeric,3)
    FROM r_multiple_labels WHERE r_achieved IS NOT NULL;""")
    if err_r:
        r_stats = {"error": err_r}
    else:
        rf = (raw_r or "").split("|")
        r_stats = {"min": rf[0] if len(rf) > 0 else "?",
                   "p50": rf[1] if len(rf) > 1 else "?",
                   "p95": rf[2] if len(rf) > 2 else "?",
                   "max": rf[3] if len(rf) > 3 else "?"}

    cron = cron_liveness()

    # Acceptance verdicts (idempotent: deterministic on the snapshot)
    lag = num(core.get("lag_h"))
    rows24 = num(core.get("rows_24h"))
    rows1 = num(rows_1h)
    total = num(core.get("total"))

    verdicts = {
        "I-4 A writer daily": {
            "target": ">0 rows in last 24h",
            "measured": f"{core.get('rows_24h','?')} rows/24h",
            "ok": (rows24 is not None and rows24 > 0)},
        "I-4 B max lag <48h": {
            "target": "<48h lag (settled cohort)",
            "measured": f"{core.get('lag_h','?')}h lag (HWM {core.get('max_computed','?')})",
            "ok": (lag is not None and lag < 48.0)},
        "I-4 C writer liveness (watch)": {
            "target": ">0 rows in last hour (15m writer cadence)",
            "measured": f"{rows_1h} rows/1h",
            "ok": (rows1 is not None and rows1 > 0)},
        "I-4 D contamination free": {
            "target": "0 contaminated rows",
            "measured": f"{contam.get('contaminated','?')} contaminated",
            "ok": (contam.get('contaminated') not in (None, "?", "err") and num(contam.get('contaminated')) == 0)},
    }

    return {
        "idempotency_key": KEY,
        "ticket": TICKET,
        "gap": GAP,
        "metric": "r_multiple_labels (computed_at / r_achieved / labeler_version)",
        "measured_at": stamp,
        "core": core,
        "rows_1h": rows_1h,
        "daily_cadence_10d": daily,
        "outcomes_2d": outcomes,
        "labeler_versions": versions,
        "contamination": contam,
        "r_achieved_stats": r_stats,
        "cron": cron,
        "verdicts": verdicts,
        "mutation_lock_active": read_mutation_lock() is not None,
    }


def record(m):
    """Idempotent append: dedupe by (idempotency_key, utc_day, measurement_hash)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    day = m["measured_at"][:10]
    if "error" in m:
        sig = json.dumps({"idempotency_key": KEY, "error": m["error"]}, sort_keys=True)
    else:
        # Hash on decision-relevant STABLE facts: verdicts + lag bucket (1h) +
        # 24h volume bucket (1000) + 1h liveness bucket + contamination count +
        # cron last_status. Row-count drift alone does not change the verdict.
        stable = {
            "idempotency_key": KEY,
            "verdict_ok": {k: v.get("ok") for k, v in (m.get("verdicts") or {}).items()},
            "lag_h_bucket": round(num(m.get("core", {}).get("lag_h")) or 0),
            "rows24_bucket": round((num(m.get("core", {}).get("rows_24h")) or 0) / 1000.0),
            "rows1_bucket": round(num(m.get("rows_1h")) or 0),
            "contaminated": m.get("contamination", {}).get("contaminated"),
            "cron_last_status": m.get("cron", {}).get("last_status"),
        }
        sig = json.dumps(stable, sort_keys=True)
    mhash = hashlib.sha1(sig.encode()).hexdigest()[:12]

    existing = []
    if os.path.exists(STATE):
        with open(STATE) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        existing.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    dup = [r for r in existing
           if r.get("idempotency_key") == KEY
           and r.get("utc_day") == day
           and r.get("measurement_hash") == mhash]
    if dup:
        m["idempotency_key"] = KEY
        m["utc_day"] = day
        m["measurement_hash"] = mhash
        m["idempotent_noop"] = True
        m["appended"] = False
        return m, existing

    m["utc_day"] = day
    m["measurement_hash"] = mhash
    m["idempotent_noop"] = False
    m["appended"] = True
    existing.append(m)
    with open(STATE, "w") as fh:
        for r in existing:
            fh.write(json.dumps(r) + "\n")
    return m, existing


def render_vault(rec, history):
    """Render the vault tracking note markdown from the latest measurement.
    Returns the markdown string (caller persists via governed write path)."""
    latest = history[-1] if history else rec
    day = latest.get("utc_day", latest["measured_at"][:10])

    rows = []
    for k, v in latest.get("verdicts", {}).items():
        if v.get("ok") is True:
            badge = "PASS"
        elif v.get("ok") is False:
            badge = "**FAIL**"
        else:
            badge = "_UNKNOWN_"
        rows.append(f"| {k} | {v['target']} | {v['measured']} | {badge} |")
    verdict_table = "\n".join(rows)

    npass = sum(1 for v in latest.get("verdicts", {}).values() if v.get("ok") is True)
    nfail = sum(1 for v in latest.get("verdicts", {}).values() if v.get("ok") is False)
    nunk = sum(1 for v in latest.get("verdicts", {}).values() if v.get("ok") is None)

    daily = latest.get("daily_cadence_10d", {})
    daily_rows = ""
    if isinstance(daily, dict) and "error" not in daily:
        for d, n in daily.items():
            daily_rows += f"| {d} | {n} |\n"
    if not daily_rows:
        daily_rows = "| _no data_ | - |\n"

    out = latest.get("outcomes_2d", {})
    out_rows = ""
    if isinstance(out, dict) and "error" not in out:
        for o, n in out.items():
            out_rows += f"| {o or '(null)'} | {n} |\n"
    if not out_rows:
        out_rows = "| _no data_ | - |\n"

    ver = latest.get("labeler_versions", {})
    ver_rows = ""
    if isinstance(ver, dict) and "error" not in ver:
        for vname, n in ver.items():
            ver_rows += f"| {vname or '(null)'} | {n} |\n"
    if not ver_rows:
        ver_rows = "| _no data_ | - |\n"

    rstat = latest.get("r_achieved_stats", {})
    contam = latest.get("contamination", {})
    cron = latest.get("cron", {})
    core = latest.get("core", {})

    cron_block = "_no data_"
    if isinstance(cron, dict) and "error" not in cron:
        cron_block = (f"| enabled | {cron.get('enabled')} |\n"
                      f"| schedule | {cron.get('schedule_display')} |\n"
                      f"| script | {cron.get('script')} |\n"
                      f"| last_run_at | {cron.get('last_run_at')} |\n"
                      f"| last_status | {cron.get('last_status')} |\n"
                      f"| last_error (trunc) | {cron.get('last_error') or ''} |\n"
                      f"| next_run_at | {cron.get('next_run_at')} |")
    elif isinstance(cron, dict) and cron.get("error"):
        cron_block = f"| error | {cron['error']} |"

    hist_rows = []
    for h in history[-15:]:
        c = h.get("core", {})
        hist_rows.append(
            f"| {h['measured_at']} | {c.get('total','?')} | {c.get('lag_h','?')}h | "
            f"{c.get('rows_24h','?')} | {h.get('rows_1h','?')} | "
            f"{h.get('contamination',{}).get('contaminated','?')} | "
            f"`{h.get('measurement_hash','?')}` |"
        )
    hist_table = "\n".join(hist_rows) if hist_rows else "| _no prior history_ | - |"

    noop_note = ""
    if latest.get("idempotent_noop"):
        noop_note = (f"\n> _idempotent no-op: this run produced a measurement identical to one "
                     f"already recorded today (hash `{latest.get('measurement_hash','?')}`). "
                     f"No duplicate appended._\n")

    fail_note = ""
    if nfail:
        fail_note = (f"\n> [!caution] **Criterion failing** as of {latest['measured_at']}. "
                     f"See verdict table below.\n")

    return f"""---
title: "R-multiple tracking — G-R1 ({KEY})"
type: task-evidence
status: {'active' if nfail else 'complete'}
created: 2026-08-06
updated: {day}
confidence: high
tags:
  - r-multiple
  - G-R1
  - integrity
  - tracking
  - sycode-trading
  - idempotency
sources:
  - "live:sycodetrading-supabase-db@r_multiple_labels (read-only SELECT)"
  - "live:hermes profiles/jarvis/cron/jobs.json@{RML_CRON_JOB_ID}"
  - kanban:{TICKET}
  - kanban:t_7d5ca285
  - kanban:t_07635cac
knowledge_tier: verified-measurement
---

Idempotent R-multiple tracking for **G-R1** (`r_multiple_labels`).
Idempotency key: `{KEY}`. State store: `{STATE}`.

## Latest measurement — {latest['measured_at']} UTC{noop_note}{fail_note}

### Core

| field | value |
|---|---|
| total rows | {core.get('total','?')} |
| max computed_at (HWM) | {core.get('max_computed','?')} |
| lag | {core.get('lag_h','?')}h |
| rows / 24h | {core.get('rows_24h','?')} |
| rows / 1h | {latest.get('rows_1h','?')} |
| max id | {core.get('max_id','?')} |
| mutation_lock_active | {latest.get('mutation_lock_active', False)} |

### Daily cadence (last 10 days)

| day | rows |
|---|---|
{daily_rows}
### Outcome mix (last 2 days)

| outcome | rows |
|---|---|
{out_rows}
### Labeler version (all-time)

| labeler_version | rows |
|---|---|
{ver_rows}
### Contamination

| contaminated | clean | with_reason |
|---|---|---|
| {contam.get('contaminated','?')} | {contam.get('clean','?')} | {contam.get('with_reason','?')} |

### r_achieved distribution (all-time, non-null)

| min | p50 | p95 | max |
|---|---|---|---|
| {rstat.get('min','?')} | {rstat.get('p50','?')} | {rstat.get('p95','?')} | {rstat.get('max','?')} |

### Producer cron (jarvis {RML_CRON_JOB_ID})

{cron_block}

### Acceptance verdicts — I-4 A–D

> {npass} PASS · **{nfail} FAIL** · {nunk} UNKNOWN (UNKNOWN is never green)

| criterion | target | measured | verdict |
|---|---|---|---|
{verdict_table}

## Measurement history (last 15 records)

| measured_at | total | lag_h | rows24h | rows1h | contaminated | hash |
|---|---|---|---|---|---|---|
{hist_table}

## Acceptance criteria (from WS01 / I-4 G-R1)

- [ ] I-4 A: writer running daily (rows in last 24h > 0)
- [ ] I-4 B: max lag <48h for settled cohort (PRIMARY acceptance)
- [ ] I-4 C: writer liveness watch — rows in last hour > 0 (healthy 15m cadence)
- [ ] I-4 D: contamination free (0 contaminated rows)

## Idempotent store

The canonical queryable store is the JSONL at `{STATE}`. Each line is a measurement
record; records are deduped by `(idempotency_key, utc_day, measurement_hash)` so re-runs
within the same measurement window are no-ops, not duplicates.

**Vault query:** filter notes tagged `G-R1` and `tracking`, or open this note directly.
**Script query:** `python3 ~/.hermes/scripts/sycode_rmultiple_tracking.py --json`
returns the latest measurement + full history.

## Safety

Read-only. SELECT against `r_multiple_labels` only; reads jarvis `jobs.json` for the
cron liveness signal. No DB writes, no live trading, no secrets. Paper-only research
per the Grok quant seat policy.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--render-vault", action="store_true")
    args = ap.parse_args()

    rec = measure()
    if "error" in rec:
        if not args.json and not args.render_vault:
            print(f"# R-multiple tracking ({KEY}) - ERROR", file=sys.stderr)
            print(rec["error"], file=sys.stderr)
        if args.json:
            print(json.dumps({"idempotency_key": KEY, "latest": rec}, indent=1))
        return 3

    rec, history = record(rec)
    appended = rec.get("appended", False)
    noop = rec.get("idempotent_noop", False)

    if args.json:
        out = {"idempotency_key": KEY, "recorded": appended, "idempotent_noop": noop,
               "latest": rec, "history_len": len(history),
               "history_last": history[-1] if history else None}
        print(json.dumps(out, indent=1))

    if args.render_vault:
        print(render_vault(rec, history))

    if not args.json and not args.render_vault:
        c = rec.get("core", {})
        mode = "NO-OP (idempotent)" if noop else "RECORDED" if appended else "NO-OP"
        print(f"# R-multiple tracking ({KEY}) [{mode}] - {rec['measured_at']}")
        print(f"\n- total rows: {c.get('total','?')}")
        print(f"- lag: {c.get('lag_h','?')}h (HWM {c.get('max_computed','?')})")
        print(f"- rows/24h: {c.get('rows_24h','?')} | rows/1h: {rec.get('rows_1h','?')}")
        for k, v in rec.get("verdicts", {}).items():
            if v.get("ok") is True:
                badge = "PASS"
            elif v.get("ok") is False:
                badge = "FAIL"
            else:
                badge = "UNKNOWN"
            print(f"- {k}: {badge} - {v['measured']} (target {v['target']})")
        cron = rec.get("cron", {})
        if isinstance(cron, dict) and cron.get("last_status"):
            print(f"- cron {RML_CRON_JOB_ID}: last_status={cron.get('last_status')} "
                  f"last_run={cron.get('last_run_at')}")

    # Exit code: 1 if any tracked criterion fails, 0 otherwise
    nfail = sum(1 for v in rec.get("verdicts", {}).values() if v.get("ok") is False)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
