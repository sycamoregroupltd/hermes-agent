"""Test job-only named-consumer path for kanban-classify-failure-cron (t_ed054723).

Round-7: do NOT treat blanket canary sys.exit(1) as named-consumer proof.
Prove ERROR jarvis/kanban-classify-failure-cron is consumer-visible under
MAX_ALERTS truncation and is keyed separately from closed t_a3055cd5 /
cronhealth_current.

These tests use NO LIVE CONFIG — pure unit tests with temp dirs / FakeHarness.
"""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CANARY_SCRIPT = ROOT / "profiles" / "jarvis" / "scripts" / "dgx_cron_health_canary.py"
ROUTER_SCRIPT = ROOT / "scripts" / "cron_health_kanban_router.py"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class NamedJobCanaryPinTest(unittest.TestCase):
    """ERROR jarvis/kanban-classify-failure-cron must survive MAX_ALERTS=25."""

    def test_canary_exits_zero_when_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cron_dir = root / "profiles" / "test" / "cron"
            cron_dir.mkdir(parents=True)
            (cron_dir / "jobs.json").write_text('{"jobs": []}')
            baseline = root / "config.yaml"
            baseline.write_text("model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4\n")
            result = subprocess.run(
                ["python3", str(CANARY_SCRIPT)],
                capture_output=True,
                text=True,
                env={
                    "HERMES_REAL_HOME": str(root),
                    "CRON_HEALTH_BASELINE_CONFIG": str(baseline),
                    "PATH": "/usr/bin:/bin",
                },
                timeout=5,
            )
        self.assertEqual(result.returncode, 0, "Healthy canary must exit 0")
        self.assertEqual(result.stdout.strip(), "", "Healthy canary must be silent")

    def test_canary_prints_named_job_and_exits_zero(self):
        """Job-only proof: print the ERROR, do not dump via sys.exit(1)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cron_dir = root / "profiles" / "jarvis" / "cron"
            cron_dir.mkdir(parents=True)
            (cron_dir / "jobs.json").write_text(
                """{
  "jobs": [{
    "id": "fe49f09f4e53",
    "name": "kanban-classify-failure-cron",
    "enabled": true,
    "no_agent": true,
    "last_status": "error",
    "last_error": "stage failure diag rc=1 reaper rc=0"
  }]
}"""
            )
            baseline = root / "config.yaml"
            baseline.write_text("model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4\n")
            result = subprocess.run(
                ["python3", str(CANARY_SCRIPT)],
                capture_output=True,
                text=True,
                env={
                    "HERMES_REAL_HOME": str(root),
                    "CRON_HEALTH_BASELINE_CONFIG": str(baseline),
                    "PATH": "/usr/bin:/bin",
                },
                timeout=5,
            )
        self.assertEqual(result.returncode, 0, "Must not treat blanket sys.exit(1) as named-consumer proof")
        self.assertIn("ERROR jarvis/kanban-classify-failure-cron", result.stdout)
        self.assertIn("stage failure", result.stdout)

    def test_named_job_survives_max_alerts_truncation(self):
        canary = _load(CANARY_SCRIPT)
        noise = [f"UNPINNED p/job-{i} [x]: drift" for i in range(25)]
        named = "ERROR jarvis/kanban-classify-failure-cron: stage failure diag rc=1 reaper rc=0"
        shown = canary.select_shown_alerts(noise + [named], max_alerts=25)
        self.assertEqual(len(shown), 25)
        self.assertTrue(any(line.startswith("ERROR jarvis/kanban-classify-failure-cron") for line in shown))
        self.assertEqual(shown.count(named), 1)
        # Last reserved slot is the named job; UNPINNED fill the rest.
        self.assertEqual(shown[-1], named)
        self.assertEqual(sum(1 for line in shown if line.startswith("UNPINNED")), 24)

    def test_without_named_job_truncation_is_unchanged(self):
        canary = _load(CANARY_SCRIPT)
        noise = [f"UNPINNED p/job-{i} [x]: drift" for i in range(30)]
        shown = canary.select_shown_alerts(noise, max_alerts=25)
        self.assertEqual(shown, noise[:25])


class NamedJobRouterKeyTest(unittest.TestCase):
    """Dedicated key must not recurrence-suppress against cronhealth_current."""

    def test_named_job_uses_dedicated_key(self):
        router = _load(ROUTER_SCRIPT)
        mixed = (
            "🔴 CRON HEALTH: 26 issue(s)\n"
            "  • UNPINNED other/job [x]: drift\n"
            "  • ERROR jarvis/kanban-classify-failure-cron: stage failure diag rc=1 reaper rc=0\n"
            "  • … 1 more\n"
        )
        self.assertEqual(router.derive_key(mixed), router.NAMED_JOB_KEY)
        self.assertNotEqual(router.derive_key(mixed), "cronhealth_current")

    def test_fleet_noise_without_named_job_does_not_use_named_key(self):
        router = _load(ROUTER_SCRIPT)
        fleet = "  • ERROR other/job-x: boom\n  • OVERDUE devops/job-y: old\n"
        key = router.derive_key(fleet)
        self.assertIsNotNone(key)
        self.assertNotEqual(key, router.NAMED_JOB_KEY)
        # Splice-not-replace: fleet noise must keep the LIVE constant key.
        # A wholesale PR-61 copy would return cronhealth_<md5> and re-ratchet.
        self.assertEqual(key, "cronhealth_current")

    def test_process_tick_creates_named_job_card_not_suppressed(self):
        import subprocess as sp

        router = _load(ROUTER_SCRIPT)
        tmp = Path(tempfile.mkdtemp())
        router.LEDGER_PATH = tmp / "ledger.json"
        router.AUDIT_PATH = tmp / "audit.jsonl"
        audit = router.Audit(router.AUDIT_PATH)
        created = []
        commented = []
        statuses = {}

        def fake_run(args, timeout=30, attempts=2, base_delay=2.0):
            if "create" in args:
                tid = f"t_named{len(created) + 1:04d}"
                created.append((args, tid))
                statuses.setdefault(tid, "ready")
                return sp.CompletedProcess(args=args, returncode=0, stdout=f'{{"id": "{tid}"}}', stderr="")
            if "comment" in args:
                commented.append(args[-1])
                return sp.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            return sp.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        g = router.__dict__
        real = {
            "_run_hermes": g.get("_run_hermes"),
            "_card_status": g.get("_card_status"),
            "_task_created_at": g.get("_task_created_at"),
            "existing_any_card": g.get("existing_any_card"),
            "_parent_is_usable": g.get("_parent_is_usable"),
        }
        g["_run_hermes"] = fake_run
        g["_card_status"] = lambda tid: statuses.get(tid)
        g["_task_created_at"] = lambda tid: 0
        g["existing_any_card"] = lambda key: (None, None)
        g["_parent_is_usable"] = lambda parent: False
        try:
            mixed = (
                "  • UNPINNED other/job [x]: drift\n"
                "  • ERROR jarvis/kanban-classify-failure-cron: stage failure diag rc=0 reaper rc=1\n"
            )
            # Closed fleet card on cronhealth_current must not swallow this job.
            g["existing_any_card"] = lambda key: (
                ("t_a3055cd5", "done") if key == "cronhealth_current" else (None, None)
            )
            res = router.process_tick(healthy=False, alert_text=mixed, audit=audit)
            self.assertEqual(res["action"], "created")
            self.assertEqual(res["key"], router.NAMED_JOB_KEY)
            self.assertEqual(len(created), 1)
            body = created[0][0][created[0][0].index("--body") + 1]
            self.assertIn("ERROR jarvis/kanban-classify-failure-cron", body)
            self.assertNotIn("UNPINNED", body)
        finally:
            for k, v in real.items():
                g[k] = v

    def test_named_job_alert_text_strips_fleet_noise(self):
        router = _load(ROUTER_SCRIPT)
        mixed = (
            "  • UNPINNED other/job [x]: drift\n"
            "  • ERROR jarvis/kanban-classify-failure-cron: stage failure diag rc=1 reaper rc=0\n"
            "  • ERROR other/job-x: boom\n"
        )
        text = router.named_job_alert_text(mixed)
        self.assertIn("ERROR jarvis/kanban-classify-failure-cron", text)
        self.assertNotIn("UNPINNED", text)
        self.assertNotIn("other/job-x", text)


class WrapperStillRoutesOnEmptyRc(unittest.TestCase):
    """Wrapper already routes non-empty stdout regardless of canary rc."""

    def test_wrapper_routes_when_canary_exits_zero_with_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            canary = Path(tmp) / "fake_canary.py"
            canary.write_text(
                'print("ERROR jarvis/kanban-classify-failure-cron: stage failure")\n'
            )
            seen = Path(tmp) / "seen.txt"
            router = Path(tmp) / "fake_router.sh"
            router.write_text(
                f"#!/usr/bin/env bash\ncat > {str(seen)!s}\n"
            )
            router.chmod(0o755)
            log = Path(tmp) / "router.log"
            wrapper = Path(tmp) / "wrapper.sh"
            wrapper.write_text(
                f"""#!/usr/bin/env bash
set -uo pipefail
CANARY="{canary}"
ROUTER="{router}"
LOG="{log}"
OUT="$( python3 "$CANARY" 2>&1 )"
rc=$?
if [ -z "$OUT" ]; then
    CRON_HEALTH_HEALTHY=1 "$ROUTER" <<< "" >>"$LOG" 2>&1
    exit "$rc"
fi
CRON_HEALTH_HEALTHY=0 "$ROUTER" <<< "$OUT" >>"$LOG" 2>&1
printf '%s\\n' "$OUT"
exit "$rc"
"""
            )
            wrapper.chmod(0o755)
            result = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertIn("ERROR jarvis/kanban-classify-failure-cron", result.stdout)
            self.assertTrue(seen.exists(), result.stderr + result.stdout)
            self.assertIn("ERROR jarvis/kanban-classify-failure-cron", seen.read_text())


if __name__ == "__main__":
    unittest.main()
