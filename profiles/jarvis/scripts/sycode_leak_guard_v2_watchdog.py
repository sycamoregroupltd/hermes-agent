#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Weekly script-only leak guard for SycodeTrading canonical_outcomes_v2.

Runs the canonical assert_no_leak_v2.sql read-only against the live Postgres
container. Silent on clean (0 leak keys and 0 nested containers). Emits a
Discord-ready alert message on nonzero findings or command failure.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TASK_ID = "t_786f5882"
SQL_PATH = Path("/home/frank/obsidian/quant-team/rebuild-2026-07-03-artifacts/assert_no_leak_v2.sql")
CONTAINER = "sycodetrading-supabase-db"
PSQL = ["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", "postgres"]


def run_psql(sql: str, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        PSQL + ["-qAt", "-F", "|"],
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def alert(title: str, body: str) -> str:
    return (
        "🚨 SycodeTrading canonical leak guard alert\n"
        f"Task: {TASK_ID}\n"
        "Boundary: PAPER-ONLY / READ-ONLY assertion; no DB writes, no trading actions.\n"
        f"SQL: {SQL_PATH}\n"
        f"{title}\n"
        f"{body}".rstrip()
    )


def parse_counts(raw: str) -> tuple[int, int]:
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("psql returned no count row")
    parts = lines[-1].split("|")
    if len(parts) != 2:
        raise ValueError(f"expected two pipe-delimited counts, got: {lines[-1]!r}")
    return int(parts[0] or 0), int(parts[1] or 0)


def extract_deny_regex(sql_text: str) -> str:
    m = re.search(r"key ~\* '([^']+)'", sql_text)
    if not m:
        raise ValueError("could not find canonical denylist regex in assert_no_leak_v2.sql")
    return m.group(1)


def detail_sql(deny_regex: str) -> str:
    escaped = deny_regex.replace("'", "''")
    return f"""
WITH RECURSIVE walk(correlation_id, key, val) AS (
    SELECT correlation_id, je.key, je.value
    FROM canonical_outcomes_v2, LATERAL jsonb_each(signal_time_features) je
    WHERE signal_time_features IS NOT NULL
  UNION ALL
    SELECT w.correlation_id, coalesce(ne.key,'[arr]'), coalesce(ne.value, ae.value)
    FROM walk w
    LEFT JOIN LATERAL jsonb_each(w.val) ne ON jsonb_typeof(w.val)='object'
    LEFT JOIN LATERAL jsonb_array_elements(w.val) ae ON jsonb_typeof(w.val)='array'
    WHERE jsonb_typeof(w.val) IN ('object','array')
), flagged AS (
    SELECT correlation_id, key, jsonb_typeof(val) AS value_type,
           (key ~* '{escaped}') AS denied_key,
           (jsonb_typeof(val) IN ('object','array')) AS nested_container
    FROM walk
    WHERE key ~* '{escaped}' OR jsonb_typeof(val) IN ('object','array')
)
SELECT key, value_type, count(*) AS rows,
       string_agg(correlation_id::text, ',' ORDER BY correlation_id::text) AS sample_correlation_ids
FROM (
    SELECT * FROM flagged ORDER BY key, correlation_id LIMIT 25
) s
GROUP BY key, value_type
ORDER BY rows DESC, key
LIMIT 20;
"""


def format_details(raw: str) -> str:
    rows = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not rows:
        return "No detail rows returned; inspect canonical assertion manually."
    formatted = ["Offending key/detail sample (key | value_type | rows | sample_correlation_ids):"]
    formatted.extend(f"- {row}" for row in rows[:20])
    return "\n".join(formatted)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-nonzero", action="store_true", help="emit a safe forced alert without touching Docker/Postgres")
    parser.add_argument("--mock-clean", action="store_true", help="exercise clean parse/silent behavior without touching Docker/Postgres")
    args = parser.parse_args()

    if args.mock_clean:
        leak_keys, nested = parse_counts("0|0\n")
        return 0 if (leak_keys, nested) == (0, 0) else 2

    if args.mock_nonzero:
        print(alert("Forced dry-run NONZERO test: leak_keys=2 nested_container_vals=1", "Offending key/detail sample (key | value_type | rows | sample_correlation_ids):\n- current_price | number | 2 | dry-run-correlation-a,dry-run-correlation-b\n- nested_blob | object | 1 | dry-run-correlation-c"))
        return 0

    try:
        sql_text = SQL_PATH.read_text()
    except Exception as exc:
        print(alert("Watchdog setup failure", f"Could not read canonical SQL: {exc}"))
        return 2

    try:
        result = run_psql(sql_text)
    except Exception as exc:
        print(alert("Watchdog execution failure", f"Could not execute psql assertion: {exc}"))
        return 2

    if result.returncode != 0:
        stderr = result.stderr.strip()[-1200:]
        print(alert("Watchdog psql failure", f"psql exit={result.returncode}\nstderr_tail:\n{stderr}"))
        return 2

    try:
        leak_keys, nested = parse_counts(result.stdout)
    except Exception as exc:
        print(alert("Watchdog parse failure", f"stdout={result.stdout.strip()!r}\nerror={exc}"))
        return 2

    if leak_keys == 0 and nested == 0:
        return 0

    details = ""
    try:
        dsql = detail_sql(extract_deny_regex(sql_text))
        dres = run_psql(dsql)
        details = format_details(dres.stdout) if dres.returncode == 0 else f"Detail query failed exit={dres.returncode}: {dres.stderr.strip()[-1200:]}"
    except Exception as exc:
        details = f"Detail query construction/execution failed: {exc}"

    print(alert(f"Canonical assertion NONZERO: leak_keys={leak_keys} nested_container_vals={nested}", details))
    return 0


if __name__ == "__main__":
    sys.exit(main())
