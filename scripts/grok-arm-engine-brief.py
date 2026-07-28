#!/usr/bin/env python3
"""Build a read-only Sycode engine-context brief for Grok-ARM.

The Grok process never receives DB credentials or direct DB access. This helper runs
inside the trusted session wrapper, uses a read-only Postgres transaction through the
local Dockerized Sycode DB, and emits a compact Markdown brief that the wrapper
prepends to Grok's standing prompt.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Iterable

DB_CONTAINER = os.environ.get("GROK_ARM_DB_CONTAINER", "sycodetrading-supabase-db")
DB_NAME = os.environ.get("GROK_ARM_DB_NAME", "postgres")
DB_USER = os.environ.get("GROK_ARM_DB_USER", "postgres")
DB_PASSWORD = os.environ.get("GROK_ARM_DB_PASSWORD", "postgres")
PSQL_TIMEOUT = int(os.environ.get("GROK_ARM_PSQL_TIMEOUT_SECONDS", "45"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("GROK_ARM_STATEMENT_TIMEOUT_MS", "30000"))


@dataclass(frozen=True)
class Query:
    title: str
    sql: str
    empty: str


def _run_query(sql: str) -> list[dict[str, str]]:
    wrapped_sql = f"""
BEGIN READ ONLY;
SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms';
COPY (
{sql.rstrip().rstrip(';')}
) TO STDOUT WITH CSV HEADER DELIMITER E'\\t';
ROLLBACK;
"""
    cmd = [
        "docker",
        "exec",
        "-e",
        f"PGPASSWORD={DB_PASSWORD}",
        DB_CONTAINER,
        "psql",
        "-h",
        "localhost",
        "-U",
        DB_USER,
        "-d",
        DB_NAME,
        "-v",
        "ON_ERROR_STOP=1",
        "-X",
        "-q",
        "-c",
        wrapped_sql,
    ]
    completed = subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=PSQL_TIMEOUT,
    )
    # psql prints BEGIN/SET/ROLLBACK command tags around COPY output. Keep only
    # the tab-separated CSV block beginning at the header row.
    lines = [line for line in completed.stdout.splitlines() if line and line not in {"BEGIN", "SET", "ROLLBACK"}]
    if not lines:
        return []
    reader = csv.DictReader(StringIO("\n".join(lines)), delimiter="\t")
    return [{k: (v or "") for k, v in row.items()} for row in reader]


def _fmt_decimal(value: str, places: int = 2) -> str:
    if value == "":
        return "n/a"
    try:
        q = Decimal("1").scaleb(-places)
        return str(Decimal(value).quantize(q))
    except (InvalidOperation, ValueError):
        return value


def _fmt_pct(value: str, places: int = 2) -> str:
    return f"{_fmt_decimal(value, places)}%" if value else "n/a"


def _render_table(headers: list[str], rows: Iterable[dict[str, str]], max_rows: int = 8) -> list[str]:
    rows = list(rows)[:max_rows]
    if not rows:
        return []
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return lines


def _section(title: str, headers: list[str], rows: list[dict[str, str]], empty: str) -> list[str]:
    lines = [f"### {title}"]
    table = _render_table(headers, rows)
    if table:
        lines.extend(table)
    else:
        lines.append(empty)
    lines.append("")
    return lines


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    queries = {
        "regime": Query(
            "Recent regime mix (signal_journeys, 2h)",
            """
            SELECT
              coalesce(macro_regime, 'NULL') AS macro,
              coalesce(regime_volatility, 'NULL') AS volatility,
              coalesce(regime_trend, 'NULL') AS trend,
              count(*)::text AS signals,
              coalesce(sum(CASE WHEN regime_favorable THEN 1 ELSE 0 END), 0)::text AS favorable,
              max(created_at)::text AS latest
            FROM signal_journeys
            WHERE created_at > now() - interval '2 hours'
            GROUP BY 1, 2, 3
            ORDER BY count(*) DESC
            LIMIT 6
            """,
            "No signal_journeys in the last 2h.",
        ),
        "funding": Query(
            "Funding extremes (latest per symbol, freshest 15m batch)",
            """
            WITH max_ts AS (
              SELECT max(timestamp) AS ts FROM funding_rate_history
            ), latest AS (
              SELECT DISTINCT ON (symbol)
                symbol,
                exchange,
                timestamp,
                (funding_rate * 10000)::numeric(18,4) AS funding_bps,
                (annualized_rate * 100)::numeric(18,2) AS annualized_pct
              FROM funding_rate_history, max_ts
              WHERE timestamp >= max_ts.ts - interval '15 minutes'
              ORDER BY symbol, timestamp DESC
            )
            SELECT symbol, exchange, timestamp::text AS ts, funding_bps::text, annualized_pct::text
            FROM latest
            ORDER BY abs(funding_bps::numeric) DESC
            LIMIT 8
            """,
            "No recent funding_rate_history rows.",
        ),
        "movers": Query(
            "Top 24h movers (1h candles)",
            """
            SELECT
              symbol,
              max(timestamp)::text AS latest_bar,
              (array_agg(close ORDER BY timestamp DESC))[1]::text AS last_close,
              (((array_agg(close ORDER BY timestamp DESC))[1] - (array_agg(close ORDER BY timestamp ASC))[1])
                / NULLIF((array_agg(close ORDER BY timestamp ASC))[1], 0) * 100)::numeric(18,2)::text AS pct_24h,
              count(*)::text AS bars
            FROM candles
            WHERE timeframe = '1h'
              AND timestamp > now() - interval '24 hours'
            GROUP BY symbol
            HAVING count(*) >= 12
            ORDER BY abs((((array_agg(close ORDER BY timestamp DESC))[1] - (array_agg(close ORDER BY timestamp ASC))[1])
                / NULLIF((array_agg(close ORDER BY timestamp ASC))[1], 0) * 100)) DESC NULLS LAST
            LIMIT 8
            """,
            "No 1h candle mover window available.",
        ),
        "levels": Query(
            "Nearest structure/level contexts (NS-P2.2 JSONB, 2h)",
            """
            SELECT
              symbol,
              timeframe,
              direction,
              created_at::text AS ts,
              coalesce(structure_levels->>'nearestLevelDistanceBps', '') AS nearest_bps,
              coalesce(structure_levels->>'nearestSupport', '') AS support,
              coalesce(structure_levels->>'nearestResistance', '') AS resistance,
              coalesce(structure_levels->>'rangePosition', '') AS range_pos,
              coalesce(structure_levels->>'breakoutBias', '') AS breakout_bias
            FROM signal_journeys
            WHERE created_at > now() - interval '2 hours'
              AND structure_levels IS NOT NULL
            ORDER BY nullif(structure_levels->>'nearestLevelDistanceBps', '')::numeric ASC NULLS LAST,
                     created_at DESC
            LIMIT 8
            """,
            "No recent structure_levels JSONB rows. Treat structure/levels as unavailable.",
        ),
        "liquidations": Query(
            "Liquidations (24h)",
            """
            SELECT
              symbol,
              side,
              count(*)::text AS events,
              coalesce(sum(notional_usd), 0)::numeric(18,2)::text AS notional_usd,
              max(timestamp)::text AS latest
            FROM liquidation_events
            WHERE timestamp > now() - interval '24 hours'
            GROUP BY symbol, side
            ORDER BY sum(notional_usd) DESC NULLS LAST, count(*) DESC
            LIMIT 8
            """,
            "No liquidation_events rows in the last 24h; liquidation feed currently unavailable/empty.",
        ),
        "book": Query(
            "Order-book/injector hints (latest signal rows, 2h)",
            """
            SELECT
              symbol,
              timeframe,
              direction,
              created_at::text AS ts,
              coalesce(ob_spread_bps::text, '') AS spread_bps,
              coalesce(ob_imbalance_ratio::text, '') AS imbalance,
              coalesce(market_open_interest::text, '') AS open_interest,
              coalesce(funding_rate_trend, '') AS funding_trend
            FROM signal_journeys
            WHERE created_at > now() - interval '2 hours'
              AND (ob_imbalance_ratio IS NOT NULL OR market_open_interest IS NOT NULL OR funding_rate_trend IS NOT NULL)
            ORDER BY created_at DESC
            LIMIT 8
            """,
            "No recent order-book/OI/funding-trend signal rows.",
        ),
        "positions": Query(
            "Paper/live safety + current book state",
            """
            WITH intent_counts AS (
              SELECT
                count(*) FILTER (WHERE trading_mode <> 'paper') AS non_paper_intents,
                count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS intents_24h,
                count(*) FILTER (WHERE created_at > now() - interval '24 hours' AND coalesce(strategy_name, '') ILIKE '%random%') AS random_intents_24h
              FROM trade_intents
            ), position_counts AS (
              SELECT
                count(*) FILTER (WHERE trading_mode <> 'paper') AS non_paper_positions,
                count(*) FILTER (WHERE closed_at IS NULL AND status <> 'closed') AS open_positions,
                count(*) FILTER (WHERE closed_at IS NULL AND status <> 'closed' AND coalesce(strategy_name, '') ILIKE '%random%') AS open_random_positions
              FROM managed_positions
            )
            SELECT
              intent_counts.non_paper_intents::text,
              position_counts.non_paper_positions::text,
              intent_counts.intents_24h::text,
              intent_counts.random_intents_24h::text,
              position_counts.open_positions::text,
              position_counts.open_random_positions::text
            FROM intent_counts, position_counts
            """,
            "Book-state query returned no rows.",
        ),
    }

    lines = [
        "## MARKET CONTEXT BRIEF — Sycode read-only engine facts",
        f"Generated: {now}",
        "Scope: wrapper-generated only; Grok has zero DB/runtime access. Use as context, not as trade authority.",
        "Amendment 2: if STRATEGY.md has no v0.2 section yet, declare strategy v0.2 with a fresh 30-thesis sample before using this brief.",
        "Safety: PAPER ONLY; no live order, credential, DB, cron, infrastructure, or trading mutation is authorized by this brief.",
        "",
    ]

    try:
        regime_rows = _run_query(queries["regime"].sql)
        lines.extend(_section(queries["regime"].title, ["macro", "volatility", "trend", "signals", "favorable", "latest"], regime_rows, queries["regime"].empty))

        funding_rows = _run_query(queries["funding"].sql)
        for row in funding_rows:
            row["funding_bps"] = _fmt_decimal(row.get("funding_bps", ""), 2)
            row["annualized_pct"] = _fmt_pct(row.get("annualized_pct", ""), 2)
        lines.extend(_section(queries["funding"].title, ["symbol", "exchange", "ts", "funding_bps", "annualized_pct"], funding_rows, queries["funding"].empty))

        mover_rows = _run_query(queries["movers"].sql)
        for row in mover_rows:
            row["pct_24h"] = _fmt_pct(row.get("pct_24h", ""), 2)
        lines.extend(_section(queries["movers"].title, ["symbol", "latest_bar", "last_close", "pct_24h", "bars"], mover_rows, queries["movers"].empty))

        level_rows = _run_query(queries["levels"].sql)
        for row in level_rows:
            row["nearest_bps"] = _fmt_decimal(row.get("nearest_bps", ""), 2)
            row["range_pos"] = _fmt_decimal(row.get("range_pos", ""), 2)
        lines.extend(_section(queries["levels"].title, ["symbol", "timeframe", "direction", "ts", "nearest_bps", "support", "resistance", "range_pos", "breakout_bias"], level_rows, queries["levels"].empty))

        liquidation_rows = _run_query(queries["liquidations"].sql)
        for row in liquidation_rows:
            row["notional_usd"] = _fmt_decimal(row.get("notional_usd", ""), 2)
        lines.extend(_section(queries["liquidations"].title, ["symbol", "side", "events", "notional_usd", "latest"], liquidation_rows, queries["liquidations"].empty))

        book_rows = _run_query(queries["book"].sql)
        for row in book_rows:
            row["spread_bps"] = _fmt_decimal(row.get("spread_bps", ""), 2)
            row["imbalance"] = _fmt_decimal(row.get("imbalance", ""), 3)
        lines.extend(_section(queries["book"].title, ["symbol", "timeframe", "direction", "ts", "spread_bps", "imbalance", "open_interest", "funding_trend"], book_rows, queries["book"].empty))

        position_rows = _run_query(queries["positions"].sql)
        lines.extend(_section(queries["positions"].title, ["non_paper_intents", "non_paper_positions", "intents_24h", "random_intents_24h", "open_positions", "open_random_positions"], position_rows, queries["positions"].empty))
    except Exception as exc:  # Fail visible in the prompt while preserving Grok's operating loop.
        print(
            "## MARKET CONTEXT BRIEF — UNAVAILABLE\n"
            f"Generated: {now}\n"
            f"Reason: {type(exc).__name__}: {exc}\n"
            "Grok must treat engine context as unavailable for this session and keep its paper-only constraints.\n",
            file=sys.stdout,
        )
        return 0

    print("\n".join(lines).rstrip() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
