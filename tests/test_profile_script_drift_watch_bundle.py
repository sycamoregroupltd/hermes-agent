import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "profile_script_drift_watch.py"
RUNNER = Path(__file__).parents[1] / "profiles" / "jarvis" / "scripts" / "cron_guard_bundle_runner.py"
spec = importlib.util.spec_from_file_location("profile_script_drift_watch", SCRIPT)
assert spec is not None and spec.loader is not None
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class GuardBundleDriftTests(unittest.TestCase):
    def test_extracts_checks_and_pipelines_without_importing_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / "runner.py"
            runner.write_text(
                "CHECKS = {'a': {'script': 'check_a.py'}, 'b': {'script': 'check_b.sh'}}\n"
                "PIPELINES = [{'script': 'check_c.py'}]\n"
            )
            names, error = watch.bundle_script_names(runner)
        self.assertIsNone(error)
        self.assertEqual(names, {"check_a.py", "check_b.sh", "check_c.py"})

    def test_reports_profile_bundle_drift_and_missing_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            central = root / "scripts"
            profile = root / "profiles" / "jarvis" / "scripts"
            central.mkdir(parents=True)
            profile.mkdir(parents=True)
            (profile / "cron_guard_bundle_runner.py").write_text(
                "CHECKS = {'drift': {'script': 'drift.py'}, 'missing': {'script': 'missing.py'}, 'ok': {'script': 'ok.py'}}\n"
                "BUNDLES = {'5m': ['drift', 'missing', 'ok']}\n"
            )
            (central / "drift.py").write_text("central\n")
            (profile / "drift.py").write_text("profile fork\n")
            (central / "missing.py").write_text("central\n")
            (central / "ok.py").write_text("same\n")
            (profile / "ok.py").write_text("same\n")

            alerts = watch.inspect_guard_bundle_scripts(root)

        self.assertEqual(
            {(row["type"], row["script"]) for row in alerts},
            {
                ("GUARD_BUNDLE_SCRIPT_FORK_DRIFT", "drift.py"),
                ("GUARD_BUNDLE_SCRIPT_MISSING", "missing.py"),
            },
        )

    def test_ignores_profile_owned_bundle_script_without_central_counterpart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            central = root / "scripts"
            profile = root / "profiles" / "jarvis" / "scripts"
            central.mkdir(parents=True)
            profile.mkdir(parents=True)
            (profile / "cron_guard_bundle_runner.py").write_text(
                "CHECKS = {'local': {'script': 'profile_only.py'}}\n"
            )
            (profile / "profile_only.py").write_text("profile-owned\n")
            self.assertEqual(watch.inspect_guard_bundle_scripts(root), [])

    def test_alerts_when_runner_manifest_is_empty_or_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profiles" / "jarvis" / "scripts"
            (root / "scripts").mkdir(parents=True)
            profile.mkdir(parents=True)
            (profile / "cron_guard_bundle_runner.py").write_text(
                "RENAMED_CHECKS = {'freshness': {'script': 'probe.py'}}\n"
            )

            alerts = watch.inspect_guard_bundle_scripts(root)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "GUARD_BUNDLE_MANIFEST_EMPTY")

    def test_extracts_freshness_script_from_committed_live_shaped_runner(self):
        names, error = watch.bundle_script_names(RUNNER)

        self.assertIsNone(error)
        self.assertIn("dgx_data_freshness_probe.py", names)


if __name__ == "__main__":
    unittest.main()
