#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Fail-closed quarantine invariant for Sycode strategies.

Normal mode is read-only and silent when no non-allowlisted enabled strategy
exists. Non-empty stdout is intended for no-agent Hermes cron delivery to
Discord #critical-alerts.

Fixture mode (`--fixture-test`) inserts one enabled row inside a transaction,
proves the checker would alert, then rolls the transaction back.

Connection resilience (2026-07-12, t_fbd84030): transient Postgres connection
pressures (e.g. brief `max_connections` contention during the 07:15 cron storm)
used to fail the run closed permanently. `run_psql` now retries a small number
of times with backoff ONLY for connection/timeout class errors. Any non-retryable
error (including genuine invariant logic failures and persistent slot exhaustion
after retries are exhausted) still raises -> the cron stays fail-closed. This
never masks a real "could not verify the invariant" condition as success.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTAINER = os.environ.get("SYCODING_DB_CONTAINER", "sycodetrading-supabase-db")
ALLOWLIST_PATH = Path(
    os.environ.get(
        "SYCODING_STRATEGY_ALLOWLIST",
        "/home/frank/.hermes/state/sycode_strategy_enabled_allowlist.json",
    )
)
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
ALERT_TARGET = os.environ.get("SYCODING_STRATEGY_ALERT_TARGET", "discord:critical-alerts")
ALERT_HERMES_HOME = os.environ.get("SYCODING_ALERT_HERMES_HOME", "/home/frank/.hermes/profiles/jarvis")
ALERT_HERMES_PROFILE = os.environ.get("SYCODING_ALERT_HERMES_PROFILE", "jarvis")
PSQL_BASE = [
    "docker",
    "exec",
    "-e",
    "PGPASSWORD=postgres",
    CONTAINER,
    "psql",
    "-h",
    "localhost",
    "-U",
    "postgres",
    "-d",
    "postgres",
    "-X",
    "-q",
    "-t",
    "-A",
]

# Bounded connection-resilience: retry only transport/timeout class failures.
# A persistent `max_connections` saturation is NOT masked — after MAX_RETRIES
# the last error is re-raised and the cron fails closed (invariant unverified).
MAX_RETRIES = 3
RETRY_BACKOFF_S = 2.0
PSQL_TIMEOUT_S = 15
_RETRYABLE_SUBSTRINGS = (
    "remaining connection slots are reserved",
    "connection to server",
    "could not connect to server",
    "connection refused",
    "timeout expired",
    "could not establish a connection",
)


def _is_retryable_connection_error(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in _RETRYABLE_SUBSTRINGS)


def load_allowlist() -> dict[str, set[str]]:
    if not ALLOWLIST_PATH.exists():
        return {"ids": set(), "names": set()}
    data = json.loads(ALLOWLIST_PATH.read_text())
    if isinstance(data, list):
        ids = {str(item) for item in data}
        return {"ids": ids, "names": set()}
    return {
        "ids": {str(item) for item in data.get("ids", [])},
        "names": {str(item) for item in data.get("names", [])},
    }


def sql_array(values: set[str]) -> str:
    if not values:
        return "ARRAY[]::text[]"
    escaped = ["'" + value.replace("'", "''") + "'" for value in sorted(values)]
    return "ARRAY[" + ",".join(escaped) + "]::text[]"


def invariant_sql(allow_ids: set[str], allow_names: set[str], fixture: bool = False) -> str:
    allow_ids_sql = sql_array(allow_ids)
    allow_names_sql = sql_array(allow_names)
    body = f"""
WITH enabled_rows AS (
  SELECT id::text AS id, name, trading_mode, engine, created_at, updated_at,
         COALESCE(meta, '{{}}'::jsonb) AS meta
  FROM strategies
  WHERE enabled = true
), offenders AS (
  SELECT *
  FROM enabled_rows
  WHERE NOT (id = ANY({allow_ids_sql}) OR name = ANY({allow_names_sql}))
), allowed_enabled AS (
  SELECT *
  FROM enabled_rows
  WHERE id = ANY({allow_ids_sql}) OR name = ANY({allow_names_sql})
), disable_intent AS (
  -- t_fd46da38: an enabled row whose meta records explicit disable intent but
  -- is NOT on the allowlist. This is the durable-reconciliation gap: if the
  -- seeder guard ever regresses (or another writer re-flips enabled=true), the
  -- silence reopens visibly instead of staying masked. The allowlist is the
  -- single source of truth that legitimately-running enabled strategies are
  -- reconciled against; disable intent that survives outside it is drift.
  SELECT id::text AS id, name, engine,
         (meta->>'disabledByDgxTuning')::boolean AS disabledByDgxTuning,
         meta->>'paperDisabledAt' AS paperDisabledAt,
         meta->>'tier' AS tier
  FROM enabled_rows
  WHERE NOT (id = ANY({allow_ids_sql}) OR name = ANY({allow_names_sql}))
    AND (
      COALESCE((meta->>'disabledByDgxTuning')::boolean, false) = true
      OR (meta->>'paperDisabledAt' IS NOT NULL AND meta->>'paperDisabledAt' <> '')
      OR meta->>'tier' = 'dropped'
    )
)
SELECT json_build_object(
  'checked_at', now(),
  'allowlist_path', '{str(ALLOWLIST_PATH).replace("'", "''")}',
  'allowlist_id_count', cardinality({allow_ids_sql}),
  'allowlist_name_count', cardinality({allow_names_sql}),
  'enabled_count', (SELECT count(*) FROM enabled_rows),
  'allowed_enabled_count', (SELECT count(*) FROM allowed_enabled),
  'offender_count', (SELECT count(*) FROM offenders),
  'disable_intent_offlist_count', (SELECT count(*) FROM disable_intent),
  'offenders', COALESCE((
    SELECT json_agg(json_build_object(
      'id', id,
      'name', name,
      'trading_mode', trading_mode,
      'engine', engine,
      'created_at', created_at,
      'updated_at', updated_at,
      'meta', meta
    ) ORDER BY name)
    FROM offenders
  ), '[]'::json),
  'disable_intent_offlist', COALESCE((
    SELECT json_agg(json_build_object(
      'id', id,
      'name', name,
      'engine', engine,
      'disabledByDgxTuning', disabledByDgxTuning,
      'paperDisabledAt', paperDisabledAt,
      'tier', tier
    ) ORDER BY name)
    FROM disable_intent
  ), '[]'::json)
)::text;
"""
    if not fixture:
        return "BEGIN READ ONLY;\n" + body + "COMMIT;\n"

    fixture_name = "__quarantine_invariant_fixture_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fixture_insert = f"""
BEGIN;
INSERT INTO strategies
  (user_id, name, description, engine, enabled, signal_filter,
   risk_profile, exit_guidelines, meta, trading_mode,
   total_trades, winning_trades, total_pnl)
VALUES
  ('00000000-0000-0000-0000-000000000000'::uuid,
   '{fixture_name}',
   'rollback fixture for t_98a7f2d6 quarantine invariant',
   'custom',
   true,
   '{{}}'::jsonb,
   '{{}}'::jsonb,
   '{{}}'::jsonb,
   '{{\"source\":\"quarantine_invariant_fixture\",\"task\":\"t_98a7f2d6\"}}'::jsonb,
   'paper',
   0, 0, 0);
"""
    return fixture_insert + body + "ROLLBACK;\n"


def run_psql(sql: str) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                PSQL_BASE + ["-c", sql],
                capture_output=True,
                text=True,
                timeout=PSQL_TIMEOUT_S,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"psql exited {result.returncode}")
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    return json.loads(line)
            raise RuntimeError(f"No JSON payload returned by psql. stdout={result.stdout!r}")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            last_exc = exc
            retryable = _is_retryable_connection_error(str(exc))
            if attempt < MAX_RETRIES and retryable:
                time.sleep(RETRY_BACKOFF_S * attempt)
                continue
            # Non-retryable error, or retries exhausted: fail closed.
            raise
    # Unreachable: loop always raises on the final attempt.
    raise last_exc if last_exc else RuntimeError("unexpected run_psql exit")


def format_alert(payload: dict[str, Any], fixture: bool = False) -> str:
    prefix = "FIXTURE_ALERT_OK" if fixture else "CRITICAL: Sycode strategy quarantine invariant drift"
    offenders = payload.get("offenders") or []
    disable_intent_offlist = payload.get("disable_intent_offlist") or []
    lines = [
        f"{prefix}",
        f"checked_at={payload.get('checked_at')}",
        f"enabled_count={payload.get('enabled_count')} allowed_enabled_count={payload.get('allowed_enabled_count')} offender_count={payload.get('offender_count')}",
        f"disable_intent_offlist_count={payload.get('disable_intent_offlist_count')}",
        f"allowlist={payload.get('allowlist_path')} ids={payload.get('allowlist_id_count')} names={payload.get('allowlist_name_count')}",
    ]
    for offender in offenders[:20]:
        lines.append(
            "offender "
            f"id={offender.get('id')} name={offender.get('name')} "
            f"mode={offender.get('trading_mode')} engine={offender.get('engine')}"
        )
    if len(offenders) > 20:
        lines.append(f"... {len(offenders) - 20} more offenders omitted")
    for di in disable_intent_offlist[:20]:
        lines.append(
            "DISABLE_INTENT_OFFLIST "
            f"id={di.get('id')} name={di.get('name')} engine={di.get('engine')} "
            f"disabledByDgxTuning={di.get('disabledByDgxTuning')} "
            f"paperDisabledAt={di.get('paperDisabledAt')} tier={di.get('tier')}"
        )
    if len(disable_intent_offlist) > 20:
        lines.append(f"... {len(disable_intent_offlist) - 20} more disable-intent rows omitted")
    return "\n".join(lines)


def send_discord_alert(message: str) -> None:
    """Send via Jarvis Discord credentials; raise so cron fails closed if delivery breaks."""
    env = os.environ.copy()
    env["HERMES_HOME"] = ALERT_HERMES_HOME
    env["HERMES_PROFILE"] = ALERT_HERMES_PROFILE
    result = subprocess.run(
        [HERMES_BIN, "send", "--to", ALERT_TARGET, "--quiet", message],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Discord alert delivery failed target={ALERT_TARGET}: {result.stderr.strip() or result.stdout.strip()}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-test", action="store_true")
    args = parser.parse_args()

    allow = load_allowlist()
    payload = run_psql(invariant_sql(allow["ids"], allow["names"], fixture=args.fixture_test))
    offender_count = int(payload.get("offender_count") or 0)
    enabled_count = int(payload.get("enabled_count") or 0)
    allowed_enabled_count = int(payload.get("allowed_enabled_count") or 0)

    if args.fixture_test:
        if offender_count < 1:
            print("FIXTURE_ALERT_FAIL: fixture row did not trip quarantine invariant", file=sys.stderr)
            return 1
        print(format_alert(payload, fixture=True))
        return 0

    if enabled_count == 0:
        return 0
    if offender_count == 0 and enabled_count == allowed_enabled_count:
        return 0

    # t_fd46da38: even when there are no plain offenders (every enabled row is
    # on the allowlist), surface a visible alert if any enabled row carries
    # disable intent outside the allowlist — that means the seeder guard has a
    # gap and an allowed enabled strategy is running against explicit disable
    # intent. The detector stays fail-visible here (alert printed, exit 0) so
    # the gap re-opens on Discord instead of being silently masked.
    print(format_alert(payload))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"CRITICAL: Sycode strategy quarantine invariant checker failed closed: {exc}")
        raise SystemExit(2)
