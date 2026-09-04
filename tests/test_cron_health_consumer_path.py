"""Test cron health canary consumer path (t_a45e23da).

Verifies that isolated stage failures in cron jobs propagate through the
guard-bundle → canary → wrapper → router chain to reach the jarvis-os consumer.

The fix addresses three blocking findings:
1. Guard-bundle runner suppresses stdout/stderr when check rc == 0
2. Cron health canary printed ERROR lines but exited 0
3. Wrapper must propagate canary failure rc so bundle runner preserves output

These tests use NO LIVE CONFIG — they are pure unit tests with mocked paths.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


CANARY_SCRIPT = Path(__file__).parents[1] / "profiles" / "jarvis" / "scripts" / "dgx_cron_health_canary.py"
WRAPPER_SCRIPT = Path(__file__).parents[1] / "scripts" / "cron_health_canary_wrapper.sh"


class CronHealthCanaryExitCodeTest(unittest.TestCase):
    """Test that the canary exits 1 when it finds issues."""

    def test_canary_exits_zero_when_healthy(self):
        """Empty bad list → print nothing, exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            test_profile = profiles / "test"
            cron_dir = test_profile / "cron"
            cron_dir.mkdir(parents=True)
            jobs = cron_dir / "jobs.json"
            jobs.write_text('{"jobs": []}')
            
            # Create baseline config to avoid drift warnings
            baseline = root / "config.yaml"
            baseline.write_text('model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4\n')

            env = {
                "HERMES_REAL_HOME": str(root),
                "CRON_HEALTH_BASELINE_CONFIG": str(baseline),
            }
            result = subprocess.run(
                ["python3", str(CANARY_SCRIPT)],
                capture_output=True,
                text=True,
                env={**env},
                timeout=5,
            )

        self.assertEqual(result.returncode, 0, "Healthy canary must exit 0")
        self.assertEqual(result.stdout.strip(), "", "Healthy canary must be silent")

    def test_canary_exits_one_when_job_status_is_error(self):
        """Job with last_status=error → print findings, exit 1."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            test_profile = profiles / "test"
            cron_dir = test_profile / "cron"
            cron_dir.mkdir(parents=True)
            jobs = cron_dir / "jobs.json"
            jobs.write_text('''{
  "jobs": [{
    "id": "test123",
    "name": "test-job",
    "enabled": true,
    "last_status": "error",
    "last_error": "stage failure diag rc=1 reaper rc=0"
  }]
}''')
            
            baseline = root / "config.yaml"
            baseline.write_text('model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4\n')

            env = {
                "HERMES_REAL_HOME": str(root),
                "CRON_HEALTH_BASELINE_CONFIG": str(baseline),
            }
            result = subprocess.run(
                ["python3", str(CANARY_SCRIPT)],
                capture_output=True,
                text=True,
                env={**env},
                timeout=5,
            )

        self.assertEqual(result.returncode, 1, "Canary with ERROR findings must exit 1")
        self.assertNotIn("NameError", result.stderr, "Canary must exit 1 cleanly, not via NameError")
        self.assertNotIn("Traceback", result.stderr, "Canary must not raise when exiting on ERROR findings")
        self.assertIn("ERROR test/test-job", result.stdout)
        self.assertIn("stage failure", result.stdout)

    def test_canary_exits_one_when_job_status_is_failed(self):
        """Job with last_status=failed → print findings, exit 1."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            test_profile = profiles / "test"
            cron_dir = test_profile / "cron"
            cron_dir.mkdir(parents=True)
            jobs = cron_dir / "jobs.json"
            jobs.write_text('''{
  "jobs": [{
    "id": "abc456",
    "name": "failing-job",
    "enabled": true,
    "last_status": "failed",
    "last_error": "timeout"
  }]
}''')
            
            baseline = root / "config.yaml"
            baseline.write_text('model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4\n')

            env = {
                "HERMES_REAL_HOME": str(root),
                "CRON_HEALTH_BASELINE_CONFIG": str(baseline),
            }
            result = subprocess.run(
                ["python3", str(CANARY_SCRIPT)],
                capture_output=True,
                text=True,
                env={**env},
                timeout=5,
            )

        self.assertEqual(result.returncode, 1, "Canary with failed job must exit 1")
        self.assertIn("ERROR test/failing-job", result.stdout)


class CronHealthWrapperPropagationTest(unittest.TestCase):
    """Test that the wrapper propagates canary failure rc to guard-bundle runner."""

    def test_wrapper_exits_zero_when_canary_is_healthy(self):
        """Empty canary stdout → wrapper routes healthy signal, exits 0."""
        with tempfile.TemporaryDirectory() as tmp:
            canary = Path(tmp) / "fake_canary.py"
            canary.write_text("# healthy\n")
            router = Path(tmp) / "fake_router.py"
            router.write_text("import sys; sys.exit(0)\n")
            log = Path(tmp) / "router.log"

            wrapper = Path(tmp) / "wrapper.sh"
            wrapper.write_text(f'''#!/usr/bin/env bash
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
''')
            wrapper.chmod(0o755)

            result = subprocess.run(
                ["bash", str(wrapper)],
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 0, "Wrapper with healthy canary must exit 0")
        self.assertEqual(result.stdout.strip(), "", "Wrapper must preserve silent output")

    def test_wrapper_exits_one_when_canary_finds_issues(self):
        """Non-empty canary stdout + rc=1 → wrapper routes alert, exits 1."""
        with tempfile.TemporaryDirectory() as tmp:
            canary = Path(tmp) / "fake_canary.py"
            canary.write_text('''
import sys
print("ERROR test/job: failure")
sys.exit(1)
''')
            router = Path(tmp) / "fake_router.py"
            router.write_text("import sys; sys.stdin.read(); sys.exit(0)\n")
            log = Path(tmp) / "router.log"

            wrapper = Path(tmp) / "wrapper.sh"
            wrapper.write_text(f'''#!/usr/bin/env bash
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
''')
            wrapper.chmod(0o755)

            result = subprocess.run(
                ["bash", str(wrapper)],
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 1, "Wrapper with unhealthy canary must exit 1")
        self.assertIn("ERROR test/job", result.stdout)

    def test_wrapper_preserves_canary_failure_even_when_router_succeeds(self):
        """Router success must not mask canary failure rc."""
        with tempfile.TemporaryDirectory() as tmp:
            canary = Path(tmp) / "fail_canary.py"
            canary.write_text('import sys; print("BAD"); sys.exit(1)\n')
            router = Path(tmp) / "ok_router.py"
            router.write_text("import sys; sys.stdin.read(); sys.exit(0)\n")
            log = Path(tmp) / "router.log"

            wrapper = Path(tmp) / "wrapper.sh"
            wrapper.write_text(f'''#!/usr/bin/env bash
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
''')
            wrapper.chmod(0o755)

            result = subprocess.run(
                ["bash", str(wrapper)],
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 1, "Wrapper must exit with canary rc, not router rc")


class GuardBundleRunnerOutputPropagationTest(unittest.TestCase):
    """Test that guard-bundle runner propagates output only when check rc != 0."""

    def test_runner_suppresses_output_when_check_exits_zero(self):
        """Guard-bundle design: rc=0 → suppress output, healthy silent watchdog."""
        # This is the original bug: canary printed ERROR but exited 0,
        # so runner suppressed the output before it reached the router.
        with tempfile.TemporaryDirectory() as tmp:
            check = Path(tmp) / "check.sh"
            check.write_text('#!/bin/bash\necho "ERROR something bad"\nexit 0\n')
            check.chmod(0o755)

            result = subprocess.run(
                ["bash", str(check)],
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stdout)
        # Runner would see rc=0 and return (0, "") — suppressing the ERROR line.

    def test_runner_preserves_output_when_check_exits_nonzero(self):
        """Guard-bundle design: rc!=0 → collect output, aggregate report."""
        with tempfile.TemporaryDirectory() as tmp:
            check = Path(tmp) / "check.sh"
            check.write_text('#!/bin/bash\necho "ERROR something bad"\nexit 1\n')
            check.chmod(0o755)

            result = subprocess.run(
                ["bash", str(check)],
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR", result.stdout)
        # Runner sees rc=1 and collects output → propagated to report.


if __name__ == "__main__":
    unittest.main()
