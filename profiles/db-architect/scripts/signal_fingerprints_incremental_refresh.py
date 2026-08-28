#!/usr/bin/env python3
"""
Incrementally refresh public.signal_fingerprints from public.signal_journeys.

Paper/data-only, additive insert path. No deletes, no updates, no live trading.
Runs as a Hermes no_agent cron: prints a concise report only when rows are
inserted; stays silent on no-op so recurring cron deliveries are quiet.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

BATCH_SIZE = int(os.environ.get("SIGNAL_FINGERPRINT_BATCH_SIZE", "1000"))
MAX_BATCHES = int(os.environ.get("SIGNAL_FINGERPRINT_MAX_BATCHES", "1"))
BACKLOG_SAMPLE_LIMIT = int(os.environ.get("SIGNAL_FINGERPRINT_BACKLOG_SAMPLE_LIMIT", "10000"))
SQL_TIMEOUT = int(os.environ.get("SIGNAL_FINGERPRINT_SQL_TIMEOUT", "90"))
LOOKBACK_HOURS = int(os.environ.get("SIGNAL_FINGERPRINT_LOOKBACK_HOURS", "24"))
PSQL = [
    "docker",
    "exec",
    "-i",
    "sycodetrading-supabase-db",
    "psql",
    "-h",
    "localhost",
    "-U",
    "postgres",
    "-d",
    "postgres",
    "-v",
    "ON_ERROR_STOP=1",
    "-X",
    "-q",
    "-t",
    "-A",
]


def run_sql(sql: str, timeout: int = SQL_TIMEOUT) -> str:
    result = subprocess.run(
        PSQL,
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def sql_literal_int(value: int) -> int:
    if value < 1:
        raise ValueError("batch/max values must be positive")
    return value


def require_supporting_indexes() -> None:
    """Fail fast if the scheduler DB is missing the indexes this bounded job needs."""
    out = run_sql(
        """
SELECT COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND (
    (tablename = 'signal_fingerprints' AND indexdef ILIKE '%(correlation_id)%')
    OR
    (tablename = 'signal_journeys' AND indexdef ILIKE '%(triggered_at, correlation_id)%')
  );
""",
        timeout=15,
    )
    matches = re.findall(r"\b\d+\b", out)
    if not matches or int(matches[-1]) < 2:
        raise RuntimeError(
            "missing support index(es): expected public.signal_fingerprints(correlation_id) "
            "and public.signal_journeys(triggered_at, correlation_id)"
        )


def insert_batch(limit: int) -> int:
    limit = sql_literal_int(limit)
    lookback_hours = sql_literal_int(LOOKBACK_HOURS)
    # Projection matches the 38 live signal_fingerprints columns:
    # fingerprint_id default-equivalent, 36 data columns, created_at.
    sql = f"""
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('signal_fingerprints_incremental_refresh'));
WITH source_rows AS (
  SELECT sj.*
  FROM public.signal_journeys sj
  WHERE sj.triggered_at >= now() - make_interval(hours => {lookback_hours})
  AND NOT EXISTS (
    SELECT 1
    FROM public.signal_fingerprints sf
    WHERE sf.correlation_id = sj.correlation_id
  )
  ORDER BY sj.triggered_at, sj.correlation_id
  LIMIT {limit}
), projected AS (
  SELECT
    gen_random_uuid() AS fingerprint_id,
    sj.correlation_id,
    sj.symbol,
    sj.direction,
    sj.timeframe,
    sj.triggered_at,
    sj.entry_price,
    sj.exit_price,
    sj.pnl_percent,
    sj.pnl_usd,
    sj.final_status,
    sj.exit_type,
    sj.mfe_percent,
    sj.mae_percent,
    sj.mfe_mae_ratio,
    sj.excursion_efficiency,
    sj.trajectory_label,
    sj.trigger_patterns,
    sj.indicators,
    sj.regime_volatility::text AS regime_volatility,
    sj.regime_trend::text AS regime_trend,
    sj.regime_favorable,
    sj.regime_macro,
    EXTRACT(HOUR FROM sj.triggered_at)::int AS hour_utc,
    EXTRACT(DOW FROM sj.triggered_at)::int AS day_of_week,
    CASE
      WHEN EXTRACT(HOUR FROM sj.triggered_at)::int BETWEEN 0 AND 7 THEN 'ASIA'
      WHEN EXTRACT(HOUR FROM sj.triggered_at)::int = 8 THEN 'ASIA_EUROPE_OVERLAP'
      WHEN EXTRACT(HOUR FROM sj.triggered_at)::int BETWEEN 9 AND 12 THEN 'EUROPE'
      WHEN EXTRACT(HOUR FROM sj.triggered_at)::int BETWEEN 13 AND 16 THEN 'EUROPE_US_OVERLAP'
      WHEN EXTRACT(HOUR FROM sj.triggered_at)::int BETWEEN 17 AND 23 THEN 'US'
      ELSE NULL
    END AS trading_session,
    sj.market_fear_greed,
    sj.market_funding_rate,
    sj.composite_confidence_score,
    sj.conviction_score,
    md5(
      coalesce(sj.symbol, '') || '|' ||
      coalesce(sj.direction, '') || '|' ||
      coalesce(sj.timeframe, '') || '|' ||
      coalesce(sj.indicators::text, '') || '|' ||
      coalesce(sj.regime_volatility::text, '') || '|' ||
      coalesce(sj.regime_trend::text, '')
    ) AS signal_fingerprint,
    now() AS created_at,
    COALESCE(
      CASE WHEN (sj.indicators->>'confluenceScore') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->>'confluenceScore')::numeric END,
      CASE WHEN (sj.indicators->>'confluence_score') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->>'confluence_score')::numeric END,
      CASE WHEN (sj.indicators->'confluencePacks'->(sj.indicators->>'confluencePackId')->>'score') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->'confluencePacks'->(sj.indicators->>'confluencePackId')->>'score')::numeric END,
      CASE WHEN (sj.confluence_log->'scores'->>'total') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.confluence_log->'scores'->>'total')::numeric END
    ) AS confluence_score,
    COALESCE(
      sj.indicators->>'confluenceDirection',
      sj.indicators->>'confluence_direction',
      sj.indicators->'confluencePacks'->(sj.indicators->>'confluencePackId')->>'direction'
    ) AS confluence_direction,
    COALESCE(
      CASE WHEN (sj.indicators->>'momentumScore') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->>'momentumScore')::numeric END,
      CASE WHEN (sj.indicators->>'momentum_score') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->>'momentum_score')::numeric END,
      CASE WHEN (sj.indicators->'momentum'->>'score') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->'momentum'->>'score')::numeric END,
      CASE WHEN (sj.indicators->'macd'->>'histogram') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN LEAST(100, GREATEST(-100, (sj.indicators->'macd'->>'histogram')::numeric * 10000)) END
    ) AS momentum_score,
    COALESCE(
      CASE WHEN (sj.indicators->>'meanReversionScore') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->>'meanReversionScore')::numeric END,
      CASE WHEN (sj.indicators->>'mean_reversion_score') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->>'mean_reversion_score')::numeric END,
      CASE WHEN (sj.indicators->'meanReversion'->>'score') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->'meanReversion'->>'score')::numeric END,
      CASE WHEN COALESCE(sj.indicators->>'rsi14', sj.indicators->>'rsi', sj.indicators->>'RSI') ~ '^-?[0-9]+(\\.[0-9]+)?$'
        THEN LEAST(100, GREATEST(-100, ((50 - COALESCE(sj.indicators->>'rsi14', sj.indicators->>'rsi', sj.indicators->>'RSI')::numeric) / 50) * 100))
      END
    ) AS mean_reversion_score,
    COALESCE(
      CASE WHEN (sj.indicators->>'volatilityScore') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->>'volatilityScore')::numeric END,
      CASE WHEN (sj.indicators->>'volatility_score') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->>'volatility_score')::numeric END,
      CASE WHEN (sj.indicators->'volatility'->>'score') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN (sj.indicators->'volatility'->>'score')::numeric END,
      CASE WHEN (sj.indicators->>'atrPercent') ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN LEAST(100, GREATEST(0, (sj.indicators->>'atrPercent')::numeric * 10)) END
    ) AS volatility_score,
    COALESCE(
      sj.indicators->>'volatilityRegime',
      sj.indicators->>'volatility_regime',
      sj.indicators->>'volatilityLevel'
    ) AS volatility_regime
  FROM source_rows sj
), inserted AS (
  INSERT INTO public.signal_fingerprints (
    fingerprint_id,
    correlation_id,
    symbol,
    direction,
    timeframe,
    triggered_at,
    entry_price,
    exit_price,
    pnl_percent,
    pnl_usd,
    final_status,
    exit_type,
    mfe_percent,
    mae_percent,
    mfe_mae_ratio,
    excursion_efficiency,
    trajectory_label,
    trigger_patterns,
    indicators,
    regime_volatility,
    regime_trend,
    regime_favorable,
    regime_macro,
    hour_utc,
    day_of_week,
    trading_session,
    market_fear_greed,
    market_funding_rate,
    composite_confidence_score,
    conviction_score,
    signal_fingerprint,
    created_at,
    confluence_score,
    confluence_direction,
    momentum_score,
    mean_reversion_score,
    volatility_score,
    volatility_regime
  )
  SELECT
    fingerprint_id,
    correlation_id,
    symbol,
    direction,
    timeframe,
    triggered_at,
    entry_price,
    exit_price,
    pnl_percent,
    pnl_usd,
    final_status,
    exit_type,
    mfe_percent,
    mae_percent,
    mfe_mae_ratio,
    excursion_efficiency,
    trajectory_label,
    trigger_patterns,
    indicators,
    regime_volatility,
    regime_trend,
    regime_favorable,
    regime_macro,
    hour_utc,
    day_of_week,
    trading_session,
    market_fear_greed,
    market_funding_rate,
    composite_confidence_score,
    conviction_score,
    signal_fingerprint,
    created_at,
    confluence_score,
    confluence_direction,
    momentum_score,
    mean_reversion_score,
    volatility_score,
    volatility_regime
  FROM projected
  RETURNING 1
)
SELECT COUNT(*) FROM inserted;
COMMIT;
"""
    out = run_sql(sql)
    matches = re.findall(r"\b\d+\b", out)
    if not matches:
        raise RuntimeError(f"could not parse insert count from psql output: {out!r}")
    return int(matches[-1])


def backlog_sample(limit: int) -> int:
    """Count missing rows up to a cap; avoids full-table COUNT in 120s cron envelope."""
    limit = sql_literal_int(limit)
    lookback_hours = sql_literal_int(LOOKBACK_HOURS)
    out = run_sql(
        f"""
SELECT COUNT(*)
FROM (
  SELECT 1
  FROM public.signal_journeys sj
  WHERE sj.triggered_at >= now() - make_interval(hours => {lookback_hours})
  AND NOT EXISTS (
    SELECT 1 FROM public.signal_fingerprints sf WHERE sf.correlation_id = sj.correlation_id
  )
  ORDER BY sj.triggered_at, sj.correlation_id
  LIMIT {limit}
) missing;
""",
    )
    matches = re.findall(r"\b\d+\b", out)
    if not matches:
        raise RuntimeError(f"could not parse backlog sample from psql output: {out!r}")
    return int(matches[-1])


def freshness_line() -> str:
    """Return the freshness report line, or a degraded marker on read failure.

    The full-table COUNT(*) can exceed the DB statement_timeout under load
    (fleet lesson t_080c0eef: never seq-scan huge tables in a monitor path).
    This is a diagnostic report line only — a slow read must NOT flip the
    data-refresh run to failure (t_a3055cd5). The insert path above is the
    actual work and already completed.
    """
    try:
        return run_sql(
            """
SELECT CONCAT(
  COUNT(*), '|',
  COALESCE(MAX(created_at)::text, ''), '|',
  COALESCE(MAX(triggered_at)::text, ''), '|',
  COUNT(*) FILTER (WHERE confluence_score IS NOT NULL), '|',
  COUNT(*) FILTER (WHERE signal_fingerprint IS NOT NULL)
)
FROM public.signal_fingerprints;
""",
        ).splitlines()[-1]
    except Exception as exc:
        return f"DEGRADED: freshness read failed ({str(exc)[:80]})"


def main() -> int:
    started = datetime.now(timezone.utc).isoformat()
    elapsed_started = time.monotonic()
    total = 0
    batches = 0
    require_supporting_indexes()
    for _ in range(MAX_BATCHES):
        inserted = insert_batch(BATCH_SIZE)
        if inserted == 0:
            break
        total += inserted
        batches += 1
        if inserted < BATCH_SIZE:
            break
    if total > 0:
        after_sample = backlog_sample(BACKLOG_SAMPLE_LIMIT)
        before_observed = total + after_sample
        elapsed = time.monotonic() - elapsed_started
        print("signal_fingerprints incremental refresh")
        print(f"started_utc={started}")
        print(f"batch_size={BATCH_SIZE} batches={batches} inserted={total} elapsed_seconds={elapsed:.3f}")
        print(f"lookback_hours={LOOKBACK_HOURS}")
        print(
            f"backlog_before_observed_at_least={before_observed} "
            f"backlog_after_sample={after_sample} backlog_sample_limit={BACKLOG_SAMPLE_LIMIT}"
        )
        print(f"freshness=count|max_created_at|max_triggered_at|confluence_rows|fingerprint_rows={freshness_line()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR signal_fingerprints incremental refresh failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
