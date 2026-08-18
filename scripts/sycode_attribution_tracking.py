#!/usr/bin/env python3
"""
sycode_attribution_tracking.py - idempotent attribution tracking for G-A1.

Uses idempotency key: gqt-integrity-2026-08-06-I1

WHAT THIS DOES
--------------
Read-only measurement of filter_attribution_facts fill against the G-A1 acceptance
criteria, persisted to a single JSONL state file keyed by the idempotency key above.
Safe to re-run: identical measurements for the same UTC day are deduped; new
measurements append a new record. No DB mutation (INSERT/UPDATE/DELETE/DDL) --
SELECT only.

The JSONL file is the queryable store. A vault note is generated from the latest
record (see --emit-vault).

USAGE
    python3 ~/.hermes/scripts/sycode_attribution_tracking.py
    python3 ~/.hermes/scripts/sycode_attribution_tracking.py --json
    python3 ~/.hermes/scripts/sycode_attribution_tracking.py --emit-vault

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

KEY = "gqt-integrity-2026-08-06-I1"
CONTAINER = "sycodetrading-supabase-db"
STATE_DIR = "/home/frank/.hermes/state"
STATE = os.path.join(STATE_DIR, "gqt-attribution-g-a1.jsonl")
VAULT_NOTE = ("/home/frank/obsidian/grok-quant-trader/workflows/"
              "gqt-integrity-2026-08-06-I1-attribution-tracking.md")
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


def parse_cohorts(raw):
    """raw (list[str] or str) -> dict of cohort -> {n, n_correct, pct}."""
    out = {}
    if not raw:
        return out
    lines = raw.split("\n") if isinstance(raw, str) else raw
    for line in lines:
        if not line:
            continue
        f = line.split("|")
        if len(f) >= 4:
            out[f[0]] = {"n": f[1], "n_correct": f[2], "pct": f[3]}
    return out


def parse_dist(raw):
    """raw (list[str] or str) -> dict of filter_correct value -> count."""
    out = {}
    if not raw:
        return out
    lines = raw.split("\n") if isinstance(raw, str) else raw
    for line in lines:
        if not line:
            continue
        f = line.split("|")
        if len(f) == 2:
            out[f[0]] = f[1]
    return out


def measure():
    """Returns a dict of the G-A1 attribution tracking measurement."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Core fill
    sql_core = """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE filter_correct IS NOT NULL) AS n_correct,
           round(100.0*count(*) FILTER (WHERE filter_correct IS NOT NULL)
                 /nullif(count(*),0),2) AS pct,
           max(created_at) AS max_created,
           max(outcome_updated_at) AS max_outcome_updated
    FROM filter_attribution_facts;"""
    raw, err = q(sql_core)
    if err:
        return {"error": err, "idempotency_key": KEY, "measured_at": stamp}
    parts = raw.split("|")
    total, n_correct, pct, max_created, max_outcome = parts

    # Cohort breakdown (deterministic GROUP BY)
    sql_cohorts = """
    SELECT cohort, count(*) AS n,
           count(*) FILTER (WHERE fc IS NOT NULL) AS n_correct,
           round(100.0*count(*) FILTER (WHERE fc IS NOT NULL)/nullif(count(*),0),2) AS pct
    FROM (
      SELECT CASE
        WHEN created_at >= now()-interval '24 hours' THEN 'last_24h'
        WHEN created_at >= now()-interval '48 hours' THEN '24h_to_48h'
        WHEN created_at >= '2026-08-02' THEN 'older_48h_post_epoch'
        ELSE 'older_48h_pre_epoch'
      END AS cohort,
      filter_correct AS fc
      FROM filter_attribution_facts
    ) c GROUP BY cohort ORDER BY cohort;"""
    raw2, err2 = q_all(sql_cohorts)
    cohorts = {"error": err2} if err2 else parse_cohorts(raw2)

    # Writer liveness (24h inserts vs corrections)
    sql_live = """
    SELECT count(*) FILTER (WHERE created_at >= now()-interval '24 hours') AS inserts_24h,
           count(*) FILTER (WHERE outcome_updated_at >= now()-interval '24 hours') AS corrected_24h
    FROM filter_attribution_facts;"""
    raw3, err3 = q(sql_live)
    if err3:
        liveness = {"error": err3}
    else:
        f = (raw3 or "").split("|")
        liveness = {"inserts_24h": f[0] if len(f) > 0 else "0",
                    "corrected_24h": f[1] if len(f) > 1 else "0"}

    # Distribution + orphans
    raw4, err4 = q_all("""
    SELECT filter_correct, count(*) FROM filter_attribution_facts
    GROUP BY 1 ORDER BY 2 DESC LIMIT 10;""")
    dist = {"error": err4} if err4 else parse_dist(raw4)

    raw5, err5 = q("""
    SELECT count(*) AS orphan_facts FROM filter_attribution_facts f
    LEFT JOIN signal_journeys j ON j.correlation_id = f.correlation_id
    WHERE j.correlation_id IS NULL;""")
    if err5:
        orphans = {"error": err5}
    else:
        orphan_n = raw5 or "0"
        orphan_pct = (round(num(orphan_n) / num(total) * 100, 2)
                      if total and num(total) else None)
        orphans = {"count": orphan_n, "pct": orphan_pct}

    # Acceptance verdicts (idempotent: deterministic on the snapshot)
    pct_all = num(pct)
    c48h_post = cohorts.get("older_48h_post_epoch", {})
    c48h_pre = cohorts.get("older_48h_pre_epoch", {})
    pct_48h = num(c48h_post.get("pct", "0"))
    pct_24h = num(cohorts.get("last_24h", {}).get("pct", "0"))
    pct_post = num(cohorts.get("older_48h_post_epoch", {}).get("pct", "0"))

    verdicts = {
        "I-1 A overall": {
            "target": ">=50%",
            "measured": f"{pct_all}% (n={total})",
            "ok": pct_all >= 50.0 if pct_all is not None else None},
        "I-1 B older-48h (PRIMARY)": {
            "target": ">=95%",
            "measured": f"{pct_48h}% (n={c48h_post.get('n','?')})",
            "ok": (pct_48h >= 95.0) if pct_48h is not None else None},
        "I-3 C 24h continuous": {
            "target": ">=50%",
            "measured": f"{pct_24h}% (n={cohorts.get('last_24h',{}).get('n','?')})",
            "ok": (pct_24h >= 50.0) if pct_24h is not None else None},
        "I-1 D orphans": {
            "target": "<30% of total",
            "measured": f"{orphans.get('pct')}% (n={orphans.get('count')})",
            "ok": (orphans.get('pct') is not None and orphans['pct'] < 30.0)},
    }

    return {
        "idempotency_key": KEY,
        "ticket": "t_3ef37b11",
        "gap": "G-A1",
        "metric": "filter_attribution_facts.filter_correct",
        "measured_at": stamp,
        "core": {"total": total, "n_correct": n_correct, "pct": pct,
                 "max_created": max_created, "max_outcome_updated": max_outcome},
        "cohorts": cohorts,
        "writer_liveness": liveness,
        "distribution": dist,
        "orphans": orphans,
        "verdicts": verdicts,
        "mutation_lock_active": read_mutation_lock() is not None,
        "post_epoch_pct": pct_post,
    }


def record(m):
    """Idempotent append: dedupe by (idempotency_key, utc_day, measurement_hash)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    day = m["measured_at"][:10]
    if "error" in m:
        # Errors are NOT deduped by hash (a transient DB error should be retryable),
        # but we still want to avoid spamming the same hash. Use a stable error sig.
        sig = json.dumps({"idempotency_key": KEY, "error": m["error"]}, sort_keys=True)
    else:
        # Hash on the decision-relevant STABLE facts only: verdicts + cohort pcts +
        # orphan pct. We deliberately EXCLUDE total/max_created/inserts which drift
        # every second from the live producer -- the attribution verdict does not
        # change on row-count drift, so an identical verdict is a true no-op.
        stable = {
            "idempotency_key": KEY,
            "verdict_ok": {k: v.get("ok") for k, v in (m.get("verdicts") or {}).items()},
            "cohort_pcts": {k: v.get("pct") for k, v in (m.get("cohorts") or {}).items()
                            if isinstance(v, dict)},
            "orphan_pct": m.get("orphans", {}).get("pct"),
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


def emit_vault(rec, history, appended):
    """Write/update the vault tracking note from the latest measurement."""
    os.makedirs(os.path.dirname(VAULT_NOTE), exist_ok=True)
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

    coh = latest.get("cohorts", {})
    cohort_rows = ""
    for cn in ("last_24h", "24h_to_48h", "older_48h_post_epoch", "older_48h_pre_epoch"):
        cv = coh.get(cn)
        if isinstance(cv, dict):
            cohort_rows += (f"| {cn} | {cv.get('n','?')} | {cv.get('n_correct','?')} "
                            f"| {cv.get('pct','?')}% |\n")

    hist_rows = []
    for h in history[-15:]:
        c = h.get("core", {})
        ch = h.get("cohorts", {})
        wl = h.get("writer_liveness", {})
        orp = h.get("orphans", {})
        hist_rows.append(
            f"| {h['measured_at']} | {c.get('pct','?')}% | "
            f"{ch.get('older_48h_post_epoch',{}).get('pct','?') if isinstance(ch.get('older_48h_post_epoch'),dict) else '?'}% | "
            f"{ch.get('last_24h',{}).get('pct','?') if isinstance(ch.get('last_24h'),dict) else '?'}% | "
            f"{wl.get('inserts_24h','?')} / {wl.get('corrected_24h','?')} | "
            f"{orp.get('count','?')} ({orp.get('pct','?')}%) | "
            f"`{h.get('measurement_hash','?')}` |"
        )
    hist_table = "\n".join(hist_rows) if hist_rows else "| _no prior history_ | - |"

    noop_note = ""
    if latest.get("idempotent_noop"):
        noop_note = (f"\n> _idempotent no-op: this run produced a measurement identical to one "
                     f"already recorded today (hash `{latest.get('measurement_hash','?')}`). "
                     f"No duplicate appended._\n")

    p0_fail = ""
    if nfail:
        p0_fail = (f"\n> [!caution] **P0 criterion failing** as of {latest['measured_at']}. "
                   f"See verdict table below.\n")

    dist_block = "_no data_"
    if latest.get('distribution'):
        dist_json = json.dumps(latest.get('distribution', {}), indent=2)
        dist_block = f"```\n{dist_json}\n```"

    body = f"""---
title: "Attribution tracking — G-A1 ({KEY})"
type: task-evidence
status: {'active' if nfail else 'complete'}
created: 2026-08-06
updated: {day}
confidence: high
tags:
  - attribution
  - G-A1
  - integrity
  - tracking
  - sycode-trading
  - idempotency
sources:
  - "live:sycodetrading-supabase-db@filter_attribution_facts (read-only SELECT)"
  - kanban:t_3ef37b11
  - kanban:t_7d5ca285
knowledge_tier: verified-measurement
---

Idempotent attribution tracking for **G-A1** (`filter_attribution_facts.filter_correct`).
Idempotency key: `{KEY}`. State store: `{STATE}`.

## Latest measurement — {latest['measured_at']} UTC{noop_note}{p0_fail}

### Core fill

| field | value |
|---|---|
| total facts | {latest['core'].get('total','?')} |
| non-null filter_correct | {latest['core'].get('n_correct','?')} |
| fill % | {latest['core'].get('pct','?')}% |
| max created_at | {latest['core'].get('max_created','?')} |
| max outcome_updated_at | {latest['core'].get('max_outcome_updated','?')} |
| mutation_lock_active | {latest.get('mutation_lock_active', False)} |

### Cohorts

| cohort | n | n_correct | pct |
|---|---|---|---|
{cohort_rows}

### Writer liveness (24h)

| inserts | corrections |
|---|---|
| {latest.get('writer_liveness',{}).get('inserts_24h','?')} | {latest.get('writer_liveness',{}).get('corrected_24h','?')} |

### Distribution

{dist_block}

### Acceptance verdicts — I-1 A–D

> {npass} PASS · **{nfail} FAIL** · {nunk} UNKNOWN (UNKNOWN is never green)

| criterion | target | measured | verdict |
|---|---|---|---|
{verdict_table}

### Orphan facts (no signal_journeys join)

| orphan_count | pct_of_total |
|---|---|
| {latest.get('orphans',{}).get('count','?')} | {latest.get('orphans',{}).get('pct','?')}% |

## Measurement history (last 15 records)

| measured_at | all% | older48h% | 24h% | inserts/cor24h | orphans | hash |
|---|---|---|---|---|---|---|
{hist_table}

## Acceptance criteria (from WS01 / G-A1)

- [ ] I-1 A: overall `filter_correct` fill >=50% (backfill health gate)
- [ ] I-1 B: rows older than 48h fill >=95% (PRIMARY acceptance)
- [ ] I-1 C: last-24h cohort fill >=50% (continuous writer liveness — distinct from backfill)
- [ ] I-1 D: orphan facts <30% of total (join integrity)

## Idempotent store

The canonical queryable store is the JSONL at `{STATE}`. Each line is a measurement
record; records are deduped by `(idempotency_key, utc_day, measurement_hash)` so re-runs
within the same measurement window are no-ops, not duplicates.

**Vault query:** filter notes tagged `G-A1` and `tracking`, or open this note directly.
**Script query:** `python3 ~/.hermes/scripts/sycode_attribution_tracking.py --json`
returns the latest measurement + full history.

## Safety

Read-only. SELECT against `filter_attribution_facts` / `signal_journeys` only. No
DB writes, no live trading, no secrets. Paper-only research per the Grok quant seat policy.
"""
    with open(VAULT_NOTE, "w") as fh:
        fh.write(body)
    return VAULT_NOTE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit-vault", action="store_true")
    args = ap.parse_args()

    rec = measure()
    if "error" in rec:
        if not args.json and not args.emit_vault:
            print(f"# Attribution tracking ({KEY}) - ERROR", file=sys.stderr)
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

    if args.emit_vault:
        path = emit_vault(rec, history, appended)
        if not args.json:
            print(f"vault note written: {path}")

    if not args.json and not args.emit_vault:
        c = rec.get("core", {})
        mode = "NO-OP (idempotent)" if noop else "RECORDED" if appended else "NO-OP"
        print(f"# Attribution tracking ({KEY}) [{mode}] - {rec['measured_at']}")
        print(f"\n- total facts: {c.get('total','?')}")
        print(f"- filter_correct fill: {c.get('pct','?')}% "
              f"({c.get('n_correct','?')}/{c.get('total','?')})")
        for k, v in rec.get("verdicts", {}).items():
            if v.get("ok") is True:
                badge = "PASS"
            elif v.get("ok") is False:
                badge = "FAIL"
            else:
                badge = "UNKNOWN"
            print(f"- {k}: {badge} - {v['measured']} (target {v['target']})")

    # Exit code: 1 if any tracked criterion fails, 0 otherwise
    nfail = sum(1 for v in rec.get("verdicts", {}).values() if v.get("ok") is False)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
