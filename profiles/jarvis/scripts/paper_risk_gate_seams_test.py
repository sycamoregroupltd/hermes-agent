#!/usr/bin/env python3
"""t_7fdd0ed1: paper_risk_gate FUSION_GATE_* seam wiring.

Proves:
1. apply_fusion_gate_seam_defaults setdefaults the four canonical keys.
2. Existing env values are not overwritten.
3. Seams match run_signal_fusion.py (sibling or live executed copy).
4. load_calibration_gate(env={}) reports missing/unparseable paths.
5. load_calibration_gate after seams resolves report paths (when files exist)
   and does not use the missing-path diagnostic.

Run:
    python3 paper_risk_gate_seams_test.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from paper_risk_gate import (  # noqa: E402
    CANONICAL_FUSION_GATE_SEAMS,
    apply_fusion_gate_seam_defaults,
    _load_gate,
)

SETDEFAULT_RE = re.compile(
    r"os\.environ\.setdefault\(\s*'(FUSION_GATE_[A-Z0-9_]+)'\s*,\s*\n?\s*'([^']+)'\s*\)",
    re.MULTILINE,
)

RUN_SIGNAL_FUSION_CANDIDATES = (
    HERE / "run_signal_fusion.py",
    Path("/home/frank/.hermes/profiles/jarvis/scripts/run_signal_fusion.py"),
)


def _run_signal_fusion_path() -> Path | None:
    for candidate in RUN_SIGNAL_FUSION_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


class SeamDefaultTests(unittest.TestCase):
    def test_fills_four_canonical_keys_on_empty_env(self) -> None:
        env: dict[str, str] = {}
        out = apply_fusion_gate_seam_defaults(env)
        self.assertIs(out, env)
        self.assertEqual(env, CANONICAL_FUSION_GATE_SEAMS)
        self.assertEqual(len(env), 4)

    def test_does_not_override_existing_keys(self) -> None:
        env = {
            "FUSION_GATE_QUANT_REPORT_DIR": "/tmp/override-quant",
            "FUSION_GATE_F052_REPORT_DIR": "/tmp/override-f052",
        }
        apply_fusion_gate_seam_defaults(env)
        self.assertEqual(env["FUSION_GATE_QUANT_REPORT_DIR"], "/tmp/override-quant")
        self.assertEqual(env["FUSION_GATE_F052_REPORT_DIR"], "/tmp/override-f052")
        self.assertEqual(env["FUSION_GATE_QUANT_MAX_AGE_MINUTES"], "720")
        self.assertEqual(env["FUSION_GATE_F052_MAX_AGE_MINUTES"], "720")

    def test_matches_run_signal_fusion_setdefault_literals(self) -> None:
        path = _run_signal_fusion_path()
        if path is None:
            self.skipTest("run_signal_fusion.py not present next to harness or live copy")
        text = path.read_text(encoding="utf-8")
        found = dict(SETDEFAULT_RE.findall(text))
        self.assertEqual(found, CANONICAL_FUSION_GATE_SEAMS)


class GateReadPathTests(unittest.TestCase):
    def test_empty_env_is_missing_unparseable_not_real_verdict(self) -> None:
        d = _load_gate(env={})
        reasons = " ".join(d.reasons).lower()
        self.assertIsNone(d.verdict.quant_report_path)
        self.assertIsNone(d.verdict.f052_report_path)
        self.assertTrue(
            "missing" in reasons or "unparseable" in reasons or "no latest" in reasons,
            msg=f"expected missing-path diagnostic, got: {d.reasons!r}",
        )

    def test_seamed_env_resolves_live_producer_dirs(self) -> None:
        env = apply_fusion_gate_seam_defaults({})
        quant_dir = Path(env["FUSION_GATE_QUANT_REPORT_DIR"])
        f052_dir = Path(env["FUSION_GATE_F052_REPORT_DIR"])
        if not quant_dir.is_dir() and not f052_dir.is_dir():
            self.skipTest("canonical producer dirs absent on this checkout")
        d = _load_gate(env=env)
        bare = _load_gate(env={})
        if quant_dir.is_dir():
            self.assertIsNotNone(d.verdict.quant_report_path)
        else:
            self.assertIsNone(d.verdict.quant_report_path)
        if f052_dir.is_dir():
            self.assertIsNotNone(
                d.verdict.f052_report_path,
                f"f052 dir exists but latest report did not resolve: {f052_dir}",
            )
        self.assertNotEqual(
            (d.verdict.quant_report_path, d.verdict.f052_report_path),
            (bare.verdict.quant_report_path, bare.verdict.f052_report_path),
            "seamed load must change provenance vs empty env",
        )
        self.assertTrue(
            d.verdict.quant_report_path or d.verdict.f052_report_path,
            "at least one present producer dir should resolve a report",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
