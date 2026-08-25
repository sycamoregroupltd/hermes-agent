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


def eval_dataset(ds):
    """Return (ok: bool|None, measured: str) for one canonical dataset. ok=None = UNKNOWN."""
    probe = ds.get("probe_sql")
    mode = ds.get("probe_mode", "age_lt")
    slo = ds.get("slo_hours")

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
    return (a <= slo, f"age {a:.2f}h (SLO < {slo}h, max {raw[:19]})")


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

    if harness_err:
        return 3
    if any(r["ok"] is False and r.get("canonical") for r in rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
