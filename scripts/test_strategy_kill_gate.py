#!/usr/bin/env python3
"""Regression tests for strategy_kill_gate.py contamination hardening (t_4e09e1f4).

Run:
  python3 scripts/test_strategy_kill_gate.py
  python3 -m pytest scripts/test_strategy_kill_gate.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path("/home/frank/.hermes/scripts/strategy_kill_gate.py")
spec = importlib.util.spec_from_file_location("strategy_kill_gate", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules["strategy_kill_gate"] = mod
spec.loader.exec_module(mod)

# The exact three flags produced by the pre-fix SQL on 2026-08-03 (live DB).
ORIGINAL_THREE_FLAGS = "\n".join([
    "Data-Opt Smart Momentum|-1.506149479127599546|-86.071880843794658204|-87.578030322922257750|10",
    "Data-Opt Scalping Ninja|-0.48541958732616388|-16.9247673559381608740|-17.4101869432643247540|10",
    "Volatility Regime Rotation|-3.727423321949570545|-1.908841289746435005|-5.636264611696005550|16",
])


def flag_semantics(rows, exclude_contaminated):
    """Pure-Python mirror of the kill-gate weekly/ranked/flag SQL semantics.

    rows: list of dicts with strategy_name, closed_at (ISO), pnl (net bar),
          contaminated (bool).
    Returns: list of (name, combined_pnl, latest_trades) for flagged strategies
             (2 consecutive negative weeks, combined < -5), sorted by combined.
    """
    from collections import defaultdict
    weekly = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "weeks": set()})
    for r in rows:
        if exclude_contaminated and r["contaminated"]:
            continue
        week = datetime.fromisoformat(r["closed_at"]).isocalendar()
        wk = f"{week.year}-W{week.week:02d}"
        key = (r["strategy_name"], wk)
        agg = weekly[r["strategy_name"]]
        agg["pnl"] += r["pnl"]
        agg["trades"] += 1
        agg["weeks"].add(wk)
    flagged = []
    for name, agg in weekly.items():
        weeks = sorted(agg["weeks"])
        if len(weeks) < 2:
            continue
        # Per-week buckets (faithful to the SQL per-week aggregation).
        per_week = defaultdict(float)
        for r in rows:
            if exclude_contaminated and r["contaminated"]:
                continue
            if r["strategy_name"] != name:
                continue
            week = datetime.fromisoformat(r["closed_at"]).isocalendar()
            per_week[f"{week.year}-W{week.week:02d}"] += r["pnl"]
        wk_sorted = sorted(per_week)
        if len(wk_sorted) < 2:
            continue
        latest = per_week[wk_sorted[-1]]
        prev = per_week[wk_sorted[-2]]
        combined = latest + prev
        if latest < 0 and prev < 0 and combined < -5:
            flagged.append((name, combined))
    return sorted(flagged, key=lambda x: x[1])


class KillGateSQLStructureTests(unittest.TestCase):
    def test_sql_joins_trade_close_events(self):
        self.assertIn("LEFT JOIN trade_close_events tce ON tce.position_id = mp.id", mod.KILL_GATE_SQL)

    def test_sql_excludes_contaminated_rows(self):
        self.assertIn("tce.contaminated IS NOT TRUE", mod.KILL_GATE_SQL)

    def test_sql_uses_net_of_cost_bar(self):
        self.assertIn("COALESCE(mp.net_realized_pnl_usd, mp.realized_pnl)", mod.KILL_GATE_SQL)

    def test_sql_no_longer_sums_raw_realized_pnl_directly(self):
        # The weekly sum must go through the net bar, never raw realized_pnl alone.
        self.assertNotIn("sum(realized_pnl)", mod.KILL_GATE_SQL)
        self.assertNotIn("sum(mp.realized_pnl)", mod.KILL_GATE_SQL)

    def test_sql_aliases_managed_positions(self):
        self.assertIn("FROM managed_positions mp", mod.KILL_GATE_SQL)


class KillGateFixtureDryRunTests(unittest.TestCase):
    def run_fixture(self, fixture_lines, extra_env=None):
        env = {
            **os.environ,
            "KILL_GATE_FIXTURE_LINES": fixture_lines,
            "KILL_GATE_DRY_RUN": "1",
            "KILL_GATE_BOARD": "fixture-board",
            "KILL_GATE_ASSIGNEE": "trading-risk-reviewer",
            "HERMES_BIN": "/bin/echo",
        }
        if extra_env:
            env.update(extra_env)
        cp = subprocess.run(
            [sys.executable, str(SCRIPT)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=env,
        )
        return cp

    def test_original_three_flags_reproduce(self):
        cp = self.run_fixture(ORIGINAL_THREE_FLAGS)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("KILL-GATE: 3 strategy(s) losing 2+ weeks straight", cp.stdout)
        self.assertIn("cards_attempted\": 3", cp.stdout)
        for name in ["Data-Opt Smart Momentum", "Data-Opt Scalping Ninja", "Volatility Regime Rotation"]:
            self.assertIn(name, cp.stdout)

    def test_dry_run_emits_idempotency_key(self):
        cp = self.run_fixture("Volatility Regime Rotation|-3.727423321949570545|-1.908841289746435005|-5.636264611696005550|16")
        self.assertIn("kill-gate-volatility-regime-rotation-", cp.stdout)

    def test_silent_exit_when_no_flags(self):
        # Whitespace-only fixture: truthy env but empty after strip -> silent exit.
        cp = self.run_fixture("\n")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("KILL-GATE:", cp.stdout)

    def test_parse_flagged_lines_handles_psql_format(self):
        rows = mod.parse_flagged_lines("A|-1.0|-2.0|-3.0|5\n\nB|-1.0|-2.0|-3.0|6\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "A")
        self.assertEqual(rows[1][4], "6")


class KillGateSyntheticPhantomFillTests(unittest.TestCase):
    """Synthetic phantom-fill fixture: one contaminated row must not flag a
    strategy; legit clean losers must still flag (t_ef97910b scenario)."""

    def make_rows(self):
        # Strategy A (Smart Momentum analogue): small clean losses + one
        # contaminated phantom fill of -86 in the previous week.
        rows = []
        for i, pnl in enumerate([-0.2, -0.3, -0.1, -0.15]):
            rows.append({"strategy_name": "Data-Opt Smart Momentum",
                         "closed_at": "2026-07-28T10:00:00Z", "pnl": pnl, "contaminated": False})
        rows.append({"strategy_name": "Data-Opt Smart Momentum",
                     "closed_at": "2026-07-20T09:00:59Z", "pnl": -86.33, "contaminated": True})
        rows.append({"strategy_name": "Data-Opt Smart Momentum",
                     "closed_at": "2026-07-20T11:00:00Z", "pnl": -0.2, "contaminated": False})
        # Strategy B (legit loser): clean losses across two weeks.
        for pnl in [-1.5, -2.0]:
            rows.append({"strategy_name": "Volatility Regime Rotation",
                         "closed_at": "2026-07-28T10:00:00Z", "pnl": pnl, "contaminated": False})
        for pnl in [-0.5, -1.0, -1.2]:
            rows.append({"strategy_name": "Volatility Regime Rotation",
                         "closed_at": "2026-07-20T10:00:00Z", "pnl": pnl, "contaminated": False})
        return rows

    def test_contaminated_row_exclusion_flips_verdict(self):
        rows = self.make_rows()
        with_contamination = flag_semantics(rows, exclude_contaminated=False)
        without_contamination = flag_semantics(rows, exclude_contaminated=True)
        names_with = {n for n, _ in with_contamination}
        names_without = {n for n, _ in without_contamination}
        # Pre-fix: the phantom fill drags Smart Momentum below -5.
        self.assertIn("Data-Opt Smart Momentum", names_with)
        # Post-fix (contaminated excluded): Smart Momentum no longer flags.
        self.assertNotIn("Data-Opt Smart Momentum", names_without)
        # Legit clean loser still flags either way.
        self.assertIn("Volatility Regime Rotation", names_with)
        self.assertIn("Volatility Regime Rotation", names_without)


class KillGateIdempotencyTests(unittest.TestCase):
    def test_week_key_format(self):
        wk = mod.week_key(datetime(2026, 8, 3, tzinfo=timezone.utc))
        self.assertEqual(wk, "2026-W32")

    def test_slugify_preserves_card_keys(self):
        self.assertEqual(mod.slugify("Data-Opt Smart Momentum"), "data-opt-smart-momentum")
        self.assertEqual(mod.slugify("Volatility Regime Rotation"), "volatility-regime-rotation")
        self.assertEqual(mod.slugify("Data-Opt Scalping Ninja"), "data-opt-scalping-ninja")

    def test_idempotency_key_format_preserved(self):
        key = f"kill-gate-{mod.slugify('Data-Opt Smart Momentum')}-{mod.week_key()}"
        self.assertRegex(key, r"^kill-gate-data-opt-smart-momentum-\d{4}-W\d{2}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
