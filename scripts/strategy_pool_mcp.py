#!/usr/bin/env python3
"""
FastMCP Server for Sycode Trading System - Strategy Pool

Exposes:
  Resources:
    - strategy://list
    - strategy://{name}
    - strategy://match/{symbol}/{direction}/{timeframe}

  Tools:
    - discover_patterns(direction, regime, timeframe)
    - add_strategy(name, entry_rules, exit_rules)
    - get_calibration(indicator, regime)

Connects to Postgres via: docker exec -i sycode-postgres psql ...

Run with:
    python3 /home/frank/.hermes/scripts/strategy_pool_mcp.py
"""

import asyncio
import json
import subprocess
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

mcp = FastMCP("strategy-pool")

# ---------------------------------------------------------------------------
# Helper: run psql inside the sycode-postgres container
# ---------------------------------------------------------------------------

def run_psql(query: str) -> List[Dict[str, Any]]:
    """Execute SQL via docker exec and return list of dict rows."""
    cmd = [
        "docker", "exec", "-i", "sycode-postgres",
        "psql", "-U", "postgres", "-d", "sycode", "-t", "-A", "-F", "\t",
        "-c", query
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"psql error: {result.stderr.strip()}")

    rows = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        # naive tab-split; assumes no tabs in data for this demo
        parts = line.split("\t")
        rows.append(parts)
    return rows


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------

@mcp.resource("strategy://list")
async def list_strategies() -> List[Dict[str, Any]]:
    """List all strategies in strategy_pool."""
    rows = run_psql("SELECT id, name, entry_rules, exit_rules, performance FROM strategy_pool ORDER BY id;")
    return [
        {"id": r[0], "name": r[1], "entry_rules": r[2], "exit_rules": r[3], "performance": r[4]}
        for r in rows
    ]


@mcp.resource("strategy://{name}")
async def get_strategy(name: str) -> Optional[Dict[str, Any]]:
    """Get details for a single strategy by name."""
    rows = run_psql(
        f"SELECT id, name, entry_rules, exit_rules, performance FROM strategy_pool WHERE name = '{name}' LIMIT 1;"
    )
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "name": r[1], "entry_rules": r[2], "exit_rules": r[3], "performance": r[4]}


@mcp.resource("strategy://match/{symbol}/{direction}/{timeframe}")
async def match_strategy(symbol: str, direction: str, timeframe: str) -> Optional[Dict[str, Any]]:
    """Find best strategy for a given signal (simple heuristic)."""
    # In a real system this would join signal_fingerprints + strategy_pool
    rows = run_psql(
        f"SELECT id, name, entry_rules, exit_rules, performance FROM strategy_pool "
        f"ORDER BY performance DESC LIMIT 1;"
    )
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "name": r[1], "entry_rules": r[2], "exit_rules": r[3], "performance": r[4]}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def discover_patterns(direction: str, regime: str, timeframe: str) -> List[Dict[str, Any]]:
    """Query signal_fingerprints for matching patterns."""
    query = (
        f"SELECT pattern_id, symbol, direction, regime, timeframe, strength "
        f"FROM signal_fingerprints "
        f"WHERE direction = '{direction}' AND regime = '{regime}' AND timeframe = '{timeframe}' "
        f"ORDER BY strength DESC LIMIT 20;"
    )
    rows = run_psql(query)
    return [
        {"pattern_id": r[0], "symbol": r[1], "direction": r[2], "regime": r[3], "timeframe": r[4], "strength": r[5]}
        for r in rows
    ]


@mcp.tool()
async def add_strategy(name: str, entry_rules: str, exit_rules: str) -> Dict[str, Any]:
    """Insert a new strategy into strategy_pool."""
    query = (
        f"INSERT INTO strategy_pool (name, entry_rules, exit_rules, performance) "
        f"VALUES ('{name}', '{entry_rules}', '{exit_rules}', 0.0) RETURNING id;"
    )
    rows = run_psql(query)
    return {"id": rows[0][0], "name": name, "status": "inserted"}


@mcp.tool()
async def get_calibration(indicator: str, regime: str) -> List[Dict[str, Any]]:
    """Query sweet_spot_calibration table."""
    query = (
        f"SELECT indicator, regime, optimal_value, confidence "
        f"FROM sweet_spot_calibration "
        f"WHERE indicator = '{indicator}' AND regime = '{regime}' "
        f"ORDER BY confidence DESC;"
    )
    rows = run_psql(query)
    return [
        {"indicator": r[0], "regime": r[1], "optimal_value": r[2], "confidence": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()  # stdio transport (default for MCP servers)