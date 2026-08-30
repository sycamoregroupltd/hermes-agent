#!/usr/bin/env python3
"""
sycode_canonical_registry_check.py — read-only acceptance check for the canonical dataset registry.

WHY THIS EXISTS
---------------
The reshape panel (DATA & MEASUREMENT FOUNDATION lens, move 6) required the data layer to be
"~10 registered first-class datasets, each with an owner, an SLO, a freshness monitor, and a
named consumer" and explicitly mitigated registry rot by making "the acceptance harness read
its dataset list FROM the registry so drift breaks the run."

This checker reads architecture/canonical-dataset-registry.json (the machine manifest twin of
architecture/CANONICAL-DATASET-REGISTRY.md) and gates on its canonical dataset list. If a
dataset is added/removed/changed there, the gated set changes — the run breaks instead of
rotting silently.

USAGE
    python3 ~/.hermes/scripts/sycode_canonical_registry_check.py            # markdown table
    python3 ~/.hermes/scripts/sycode_canonical_registry_check.py --json     # machine readable
    python3 ~/.hermes/scripts/sycode_canonical_registry_check.py --registry /path/to/canonical-dataset-registry.json

EXIT CODES
    0  all canonical datasets pass their SLO
    1  one or more canonical datasets FAIL their SLO
    3  harness error (cannot read registry or measure) — NOT the same as a pass

EVENT-DRIVEN SUPPRESSION (t_75cc88ff re-baseline, 2026-08-28)
    canonical_outcomes_v2 is a plain VIEW over trade_close_events (relkind='v'),
    not a streaming dataset. Its max(realized_closed_at) legitimately freezes
    during a paper-trading halt, or when the only closes are contaminated
    random-entry control arms (the view filters contaminated IS NOT TRUE) — that
    is NOT a data-freshness incident (t_51cbb2ec false alarm). Two read-only
    measures, matching sycode_surface_freshness_monitor.py:
      (a) probe the SOURCE tape trade_close_events (fresh under control-only)
          instead of the derived view, and
      (b) datasets declaring "suppress_under_flat_book": true are reported PASS/
          FLAT when the book is flat (0 open positions) — the legitimate halt.
    Both are SELECT-only; no DDL/DML. ts-index migration 20260704000001 stays
    A3/Frank-gated.

SAFETY
    Strictly read-only. SELECT only. Same docker-exec psql pattern as sycode_data_acceptance.py.
    Non-canonical datasets are reported but never gate the exit code.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

CONTAINER = "sycodetrading-supabase-db"
STMT_TIMEOUT = "120s"
DEFAULT_REGISTRY = "/home/frank/obsidian/sycode-trading/architecture/canonical-dataset-registry.json"


def load_registry(path):
    with open(path) as fh:
        return json.load(fh)


def q(sql, timeout=180):
    """Run a read-only SELECT via the acceptance docker pattern. Returns (value, error)."""
    if any(w in sql.upper() for w in (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER ",
                                      " CREATE ", " TRUNCATE ", " VACUUM ", " REINDEX ", " GRANT ")):
        return None, "refused: statement is not read-only"
    cmd = ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", "postgres", "-At",
           "-c", f"SET statement_timeout='{STMT_TIMEOUT}'; {sql}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"harness timeout after {timeout}s"
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:300]
    lines = [l for l in r.stdout.strip().splitlines() if l.strip() and l.strip() != "SET"]
    return (lines[-1].strip() if lines else None), None


def age_hours(ts_text):
    if not ts_text or ts_text in ("NEVER", ""):
        return None
    s = ts_text.split("+")[0].split(".")[0].strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, f).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        except ValueError:
            continue
    return None


def is_flat_book():
    """True when there are 0 open positions (managed_positions.closed_at IS
    NULL). Closing-activity / outcome surfaces are DOWNSTREAM OF POSITION
    CLOSING: under a flat book they legitimately stop advancing — that is the
    NS-P1 paper-drought / paper-halt condition, NOT a writer death. Mirrors the
    FLAT_BOOK_SURFACES suppression in sycode_surface_freshness_monitor.py
    (t_16fdf654, 2026-07-11). Fail-OPEN on read error returns False so a genuine
    incident is never masked by doubt (a stuck close-writer with open positions
    still FAILs because is_flat_book() is False)."""
    raw, err = q("SELECT count(*) FROM public.managed_positions WHERE closed_at IS NULL;")
    if err or raw is None:
        return False
    try:
        return int(raw) == 0
    except (ValueError, TypeError):
        return False


def eval_dataset(ds):
    """Return (ok: bool|None, measured: str) for one canonical dataset. ok=None = UNKNOWN."""
    probe = ds.get("probe_sql")
    mode = ds.get("probe_mode", "age_lt")
    slo = ds.get("slo_hours")
    suppress_flat = ds.get("suppress_under_flat_book", False)

    if not probe:
        return None, "no probe_sql (sidecar/mcp surface — verified via its own monitor)"

    raw, err = q(probe)
    if err:
        return None, f"ERROR: {err}"
    if raw is None:
        return None, "no rows returned"

    if mode == "count_ge":
        try:
            n = int(raw)
            return (n >= int(ds.get("probe_target", 1)), f"{n} rows/24h (target >= {ds.get('probe_target')})")
        except ValueError:
            return None, f"unparseable count: {raw}"

    # default: max-timestamp age vs SLO
    a = age_hours(raw)
    if a is None:
        return None, f"unparseable ts: {raw}"
    sem = f" ({ds.get('probe_semantics')})" if ds.get("probe_semantics") else ""
    if a > slo and suppress_flat and is_flat_book():
        # Event-driven surface downstream of position closing: a flat book (0
        # open positions) is the legitimate paper-drought / paper-halt condition.
        # The tape legitimately freezes — this is NOT a data-freshness incident.
        # Mirror of sycode_surface_freshness_monitor FLAT_BOOK_SURFACES (t_16fdf654).
        return True, f"FLAT (0 open positions) — event-driven surface legitimately halted (tape age {a:.2f}h)"
    return (a <= slo, f"age {a:.2f}h{sem} (SLO < {slo}h, max {raw[:19]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    args = ap.parse_args()

    try:
        reg = load_registry(args.registry)
    except Exception as e:
        print(f"HARNESS ERROR: cannot read registry {args.registry}: {e}")
        return 3

    datasets = reg.get("datasets", [])
    canonical = [d for d in datasets if d.get("canonical")]
    if not canonical:
        print("HARNESS ERROR: registry contains zero canonical datasets — nothing to gate on")
        return 3

    rows = []
    harness_err = False
    for ds in datasets:
        if not ds.get("canonical"):
            rows.append(dict(id=ds.get("id"), name=ds.get("name"), canonical=False,
                             target="non-canonical (report only)", measured="-", verdict="INFO", ok=None))
            continue
        ok, measured = eval_dataset(ds)
        if ok is None and "ERROR" in str(measured):
            harness_err = True
        target = f"<{ds.get('slo_hours')}h" if ds.get("probe_mode", "age_lt") != "count_ge" \
            else f">={ds.get('probe_target')}/24h"
        rows.append(dict(id=ds.get("id"), name=ds.get("name"), canonical=True, target=target,
                         measured=measured,
                         verdict=("PASS" if ok else ("FAIL" if ok is False else "UNKNOWN")), ok=ok))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    npass = sum(1 for r in rows if r["ok"] is True)
    nfail = sum(1 for r in rows if r["ok"] is False)
    nunk = sum(1 for r in rows if r["ok"] is None and r.get("canonical"))
    ninfo = sum(1 for r in rows if not r.get("canonical"))

    if args.json:
        print(json.dumps(dict(measured_at=stamp, registry=args.registry, rows=rows), indent=1))
    else:
        print(f"# Sycode canonical dataset registry check — {stamp}")
        print(f"registry: {args.registry}")
        print(f"\n**{npass} PASS · {nfail} FAIL · {nunk} UNKNOWN (canonical) · {ninfo} non-canonical (info)**\n")
        print("| id | dataset | SLO | measured | verdict |")
        print("|---|---|---|---|---|")
        for r in rows:
            badge = {"PASS": "PASS", "FAIL": "**FAIL**", "UNKNOWN": "_UNKNOWN_", "INFO": "info"}[r["verdict"]]
            print(f"| {r['id']} | {r['name']} | {r['target']} | {r['measured']} | {badge} |")
        print("\n> Re-run before quoting. A number in a note is provenance; only a fresh run is status.")

    # FAILs win over harness errors: a genuine canonical-SLO break must surface as
    # CRITICAL (exit 1) even when a sibling dataset probe timed out, so one slow
    # dataset cannot mask a real gate break. (t_7b10dfee)
    if any(r["ok"] is False and r.get("canonical") for r in rows):
        return 1
    if harness_err:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
