#!/usr/bin/env python3
"""t_c6f247b4: paper_risk_gate ledger regressions (PR #71).

Covers:
- P1 close-cursor persist only after output succeeds
- empty-close "" vs "none" consistency
- narrow db() missing-table match (does not swallow permission-denied)
- unused HIGH_CONVICTION_FLOOR import removed

Run:
    python3 paper_risk_gate_ledger_test.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from paper_risk_gate import (  # noqa: E402
    EMPTY_CLOSE_SENTINEL,
    db,
    is_missing_table_error,
    main,
    normalize_close_cursor,
    persist_close_cursor,
    should_wake,
)


def _gate(status: str) -> SimpleNamespace:
    verdict = SimpleNamespace(
        quant_report_path=None,
        f052_report_path=None,
        tier1_win_rate_pct=None,
        weighted_mce_pp=None,
        validated_edge_status=None,
    )
    return SimpleNamespace(status=status, reasons=["fixture"], verdict=verdict)


class NormalizeCloseCursorTests(unittest.TestCase):
    def test_empty_and_none_are_the_same_sentinel(self) -> None:
        self.assertEqual(normalize_close_cursor(""), EMPTY_CLOSE_SENTINEL)
        self.assertEqual(normalize_close_cursor("   "), EMPTY_CLOSE_SENTINEL)
        self.assertEqual(normalize_close_cursor("none"), EMPTY_CLOSE_SENTINEL)
        self.assertEqual(normalize_close_cursor("None"), EMPTY_CLOSE_SENTINEL)
        self.assertEqual(normalize_close_cursor(None), EMPTY_CLOSE_SENTINEL)

    def test_real_timestamp_is_preserved(self) -> None:
        self.assertEqual(normalize_close_cursor("2026-09-06 00:00:00+00"), "2026-09-06 00:00:00+00")


class EmptyCloseWakeTests(unittest.TestCase):
    def test_validated_empty_close_does_not_wake_against_none_state(self) -> None:
        gate = _gate("VALIDATED")
        self.assertFalse(should_wake("none", "", gate))
        self.assertFalse(should_wake("", "", gate))
        self.assertFalse(should_wake("none", "none", gate))
        self.assertFalse(should_wake("", "none", gate))

    def test_new_close_wakes_even_when_validated(self) -> None:
        self.assertTrue(should_wake("none", "2026-09-06 12:00:00", _gate("VALIDATED")))

    def test_blocking_gate_still_wakes_with_no_new_close(self) -> None:
        self.assertTrue(should_wake("none", "", _gate("BLOCKED")))
        self.assertTrue(should_wake("none", "none", _gate("UNKNOWN")))


class MissingTableMatchTests(unittest.TestCase):
    def test_postgres_relation_does_not_exist(self) -> None:
        err = 'ERROR:  relation "managed_positions" does not exist\nLINE 1: ...'
        self.assertTrue(is_missing_table_error(err))

    def test_undefined_table_sqlstate(self) -> None:
        self.assertTrue(is_missing_table_error("ERROR: undefined_table"))

    def test_permission_denied_for_relation_is_not_swallowed(self) -> None:
        err = 'ERROR:  permission denied for relation managed_positions'
        self.assertFalse(is_missing_table_error(err))
        err_quoted = 'ERROR:  permission denied for relation "managed_positions"'
        self.assertFalse(is_missing_table_error(err_quoted))

    def test_column_does_not_exist_is_not_swallowed(self) -> None:
        err = 'ERROR:  column "closed_at" does not exist'
        self.assertFalse(is_missing_table_error(err))

    def test_db_raises_on_permission_denied(self) -> None:
        fake = SimpleNamespace(
            returncode=1,
            stderr='ERROR:  permission denied for relation "managed_positions"',
            stdout="",
        )
        with patch("paper_risk_gate.subprocess.run", return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:
                db("SELECT 1")
        self.assertIn("permission denied", str(ctx.exception).lower())

    def test_db_returns_empty_on_missing_table(self) -> None:
        fake = SimpleNamespace(
            returncode=1,
            stderr='ERROR:  relation "managed_positions" does not exist',
            stdout="",
        )
        with patch("paper_risk_gate.subprocess.run", return_value=fake):
            self.assertEqual(db("SELECT 1"), "")


class PersistCursorTests(unittest.TestCase):
    def test_empty_close_writes_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "last.txt")
            persist_close_cursor("", dry_run=False, state_file=path)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), EMPTY_CLOSE_SENTINEL)

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "last.txt")
            persist_close_cursor("2026-09-06", dry_run=True, state_file=path)
            self.assertFalse(os.path.exists(path))


class CursorDeferralTests(unittest.TestCase):
    def test_output_failure_does_not_advance_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "last.txt")
            gate = _gate("VALIDATED")

            def _db(sql: str) -> str:
                if "max(closed_at)" in sql:
                    return "2026-09-06 12:00:00+00"
                return "0"

            with patch("paper_risk_gate.STATE_FILE", state), patch(
                "paper_risk_gate._load_gate", return_value=gate
            ), patch("paper_risk_gate.db", side_effect=_db), patch(
                "paper_risk_gate.json.dumps", side_effect=ValueError("encode failed")
            ):
                with self.assertRaises(ValueError):
                    main()
            self.assertFalse(os.path.exists(state))

    def test_successful_output_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "last.txt")
            gate = _gate("VALIDATED")

            def _db(sql: str) -> str:
                if "max(closed_at)" in sql:
                    return "2026-09-06 12:00:00+00"
                if "json_agg" in sql:
                    return ""
                return "0"

            with patch("paper_risk_gate.STATE_FILE", state), patch(
                "paper_risk_gate._load_gate", return_value=gate
            ), patch("paper_risk_gate.db", side_effect=_db):
                self.assertEqual(main(), 0)
            self.assertEqual(
                Path(state).read_text(encoding="utf-8"),
                "2026-09-06 12:00:00+00",
            )

    def test_later_db_failure_does_not_advance_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "last.txt")
            gate = _gate("VALIDATED")
            calls = {"n": 0}

            def _db(sql: str) -> str:
                calls["n"] += 1
                if "max(closed_at)" in sql:
                    return "2026-09-06 12:00:00+00"
                raise RuntimeError("DB query failed: boom")

            with patch("paper_risk_gate.STATE_FILE", state), patch(
                "paper_risk_gate._load_gate", return_value=gate
            ), patch("paper_risk_gate.db", side_effect=_db):
                with self.assertRaises(RuntimeError):
                    main()
            self.assertFalse(os.path.exists(state))


class UnusedImportTests(unittest.TestCase):
    def test_high_conviction_floor_is_not_reexported(self) -> None:
        text = (HERE / "paper_risk_gate.py").read_text(encoding="utf-8")
        self.assertNotIn("HIGH_CONVICTION_FLOOR", text)
        self.assertNotIn("DEFAULT_HIGH_CONVICTION_MIN", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
