"""Tests for cron/script_tracking.py — author-time git-tracking validation.

Covers both MODE 1 (never committed) and MODE 2 (committed to unmerged branch).
Also covers out-of-repo paths being silently accepted.

Run from repo root after pip install -e . or python setup.py develop."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from cron.script_tracking import (
    check_all,
    get_hermes_cron_stores,
    is_tracked,
    resolve_script_path,
    validate_script_for_creation,
    ScriptViolation,
)


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@local"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"], check=True
    )


class TestResolveScriptPath:

    def test_relative_inside(self):
        home = Path(tempfile.mkdtemp()) / "jarvis"
        scripts = home / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "test.py").write_text("# ok\n")
        resolved, reason = resolve_script_path(home, "test.py")
        assert reason is None
        assert resolved == scripts / "test.py"

    def test_absolute_inside(self):
        home = Path(tempfile.mkdtemp()) / "jarvis"
        scripts = home / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "test.py").write_text("# ok\n")
        abs_path = str(scripts / "test.py")
        resolved, reason = resolve_script_path(home, abs_path)
        assert reason is None
        assert resolved == scripts / "test.py"

    def test_escaping_absolute(self):
        home = Path(tempfile.mkdtemp()) / "jarvis"
        outside = Path(tempfile.mkdtemp()) / "escape.py"
        outside.write_text("# escape\n")
        resolved, reason = resolve_script_path(home, str(outside))
        assert reason == "SCHEDULER-BLOCKED"
        assert resolved is None

    def test_missing_file(self):
        home = Path(tempfile.mkdtemp()) / "jarvis"
        resolved, reason = resolve_script_path(home, "does_not_exist.py")
        assert reason == "MISSING"
        assert resolved is None


class TestIsTracked:

    def test_tracked_file_returns_true(self):
        tmp = Path(tempfile.mkdtemp())
        _git_init(tmp)
        f = tmp / "good.py"
        f.write_text("# tracked\n")
        subprocess.run(["git", "-C", str(tmp), "add", "good.py"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-q", "-m", "init"], check=True
        )
        assert is_tracked("good.py", tmp)

    def test_untracked_file_returns_false(self):
        tmp = Path(tempfile.mkdtemp())
        _git_init(tmp)
        f = tmp / "evil.py"
        f.write_text("# evil\n")
        assert not is_tracked("evil.py", tmp)


class TestValidateScriptForCreation:
    """Test the author-time API used by create_job()."""

    def _make_fixture_repo(self):
        """Create a minimal repo layout: repo-root, profiles/jarvis/{scripts,cron}."""
        tmp = Path(tempfile.mkdtemp())
        _git_init(tmp)
        home = tmp / "profiles" / "jarvis"
        scripts = home / "scripts"
        cron_dir = home / "cron"
        scripts.mkdir(parents=True)
        cron_dir.mkdir(parents=True)
        return tmp, home, scripts, cron_dir

    def test_no_agent_untracked_refuses(self):
        """MODE 1: Author writes file + registers job via Python API, never commits."""
        tmp, home, scripts, cron_dir = self._make_fixture_repo()

        # Write store referencing a script, mark a marker file
        (cron_dir / "jobs.json").write_text(json.dumps({
            "jobs": [{"id": "a", "name": "test-job", "script": "never_committed.py", "enabled": True}]
        }))
        (tmp / ".marker").write_text("")

        # Commit everything that exists NOW — scripts/ is empty so nothing there gets added
        subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-q", "-m", "setup"], check=True
        )

        # Create the script AFTER commit — truly untracked
        (scripts / "never_committed.py").write_text("# written but never staged\n")

        rel_path = str((scripts / "never_committed.py").relative_to(tmp))
        assert not is_tracked(rel_path, tmp), \
            f"Setup error: {rel_path} should be untracked"

        try:
            validate_script_for_creation(
                "never_committed.py", home, tmp, is_no_agent=True
            )
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "NOT tracked" in str(e)
            assert "git add" in str(e)

    def test_agent_untracked_warns(self):
        """Untracked script + agent job => warns, does NOT raise."""
        tmp, home, scripts, cron_dir = self._make_fixture_repo()

        (cron_dir / "jobs.json").write_text(json.dumps({
            "jobs": [{"id": "w", "name": "warn-job", "script": "warn.py", "enabled": True}]
        }))
        (tmp / ".marker").write_text("")
        subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-q", "-m", "setup"], check=True
        )

        (scripts / "warn.py").write_text("# warning only\n")

        rel_path = str((scripts / "warn.py").relative_to(tmp))
        assert not is_tracked(rel_path, tmp)

        ok, msgs = validate_script_for_creation(
            "warn.py", home, tmp, is_no_agent=False
        )
        assert not ok, f"Expected ok=False, got ok={ok}, msgs={msgs}"
        assert len(msgs) >= 1
        assert "NOT tracked" in msgs[0]

    def test_tracked_script_succeeds(self):
        """Tracked script => passes for both no_agent and agent jobs."""
        tmp, home, scripts, cron_dir = self._make_fixture_repo()

        (scripts / "good.py").write_text("# good\n")
        subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-q", "-m", "init"], check=True
        )

        ok, msgs = validate_script_for_creation(
            "good.py", home, tmp, is_no_agent=True
        )
        assert ok
        assert msgs == []

    def test_out_of_repo_accepted(self):
        """A script whose PROFILE_HOME is outside the repo — silently accepted.

        If the profile directory itself is outside the git repo, any script
        path within it will naturally resolve outside the repo and be accepted.
        """
        repo = Path(tempfile.mkdtemp())
        _git_init(repo)
        (repo / "marker.txt").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True
        )

        # Profile home is OUTSIDE the repo
        external_home = Path(tempfile.mkdtemp()) / "profiles" / "jarvis"
        ext_scripts = external_home / "scripts"
        ext_scripts.mkdir(parents=True)
        (ext_scripts / "ext.py").write_text("# external\n")
        subprocess.run(["git", "-C", str(repo), "rm", "--cached", ".", "--force"],
                       check=False, capture_output=True)

        ok, msgs = validate_script_for_creation(
            "ext.py", external_home, repo, is_no_agent=True
        )
        assert ok, f"out-of-repo profile should be accepted. got ok={ok}, msgs={msgs}"
        assert msgs == []


class TestGetHermesCronStores:

    def test_discovers_profiles_and_root(self):
        tmp = Path(tempfile.mkdtemp())
        _git_init(tmp)

        for name in ("jarvis", "devops"):
            p = tmp / "profiles" / name / "cron"
            p.mkdir(parents=True)
            (p / "jobs.json").write_text(json.dumps({"jobs": []}))

        (tmp / "cron").mkdir(parents=True)
        (tmp / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))

        stores = get_hermes_cron_stores(tmp)
        labels = [label for _, label, _ in stores]
        assert "jarvis" in labels
        assert "devops" in labels
        assert "<root>" in labels


class TestCheckAllAgreementWithDetector:

    def test_agreement_on_simulated_reality(self):
        tmp = Path(tempfile.mkdtemp())
        _git_init(tmp)

        for name in ("jarvis", "jarvis-os-pm"):
            p = tmp / "profiles" / name
            (p / "scripts").mkdir(parents=True)
            (p / "cron").mkdir(parents=True)

        (tmp / "profiles" / "jarvis" / "cron" / "jobs.json").write_text(
            json.dumps({
                "jobs": [
                    {"id": "a1", "name": "tracked-job", "script": "tracked.py", "enabled": True},
                    {"id": "b1", "name": "untracked-job", "script": "untracked.py", "enabled": True},
                ]
            })
        )
        (tmp / "profiles" / "jarvis-os-pm" / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": []})
        )

        (tmp / ".marker").write_text("x")
        subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-q", "-m", "init"], check=True
        )

        # Create tracked.py AND commit it
        (tmp / "profiles" / "jarvis" / "scripts" / "tracked.py").write_text("# ok\n")
        subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-q", "-m", "add-tracked"], check=True
        )

        # Create untracked.py AFTER commit
        (tmp / "profiles" / "jarvis" / "scripts" / "untracked.py").write_text("# bad\n")

        assert is_tracked("profiles/jarvis/scripts/tracked.py", tmp)
        assert not is_tracked("profiles/jarvis/scripts/untracked.py", tmp)

        viols, errors = check_all(tmp)
        assert not errors
        assert len(viols) == 1
        assert viols[0].job_id == "b1"
        assert "untracked" in viols[0].reason


class TestBothModes:

    def _make_fixture_repo(self):
        """Create a minimal repo layout."""
        tmp = Path(tempfile.mkdtemp())
        _git_init(tmp)
        home = tmp / "profiles" / "jarvis"
        scripts = home / "scripts"
        cron_dir = home / "cron"
        scripts.mkdir(parents=True)
        cron_dir.mkdir(parents=True)
        return tmp, home, scripts, cron_dir

    def test_mode1_never_committed(self):
        """Author creates script + registers job via Python API, never commits."""
        tmp, home, scripts, cron_dir = self._make_fixture_repo()

        (cron_dir / "jobs.json").write_text(json.dumps({
            "jobs": [{"id": "m1", "name": "mode1-job", "script": "never_committed.py", "enabled": True}]
        }))
        (tmp / ".marker").write_text("")
        subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-q", "-m", "setup"], check=True
        )

        (scripts / "never_committed.py").write_text("# written but never staged\n")

        rel_path = str((scripts / "never_committed.py").relative_to(tmp))
        assert not is_tracked(rel_path, tmp)

        # no_agent=True raises ValueError (refusal)
        raised = False
        try:
            validate_script_for_creation(
                "never_committed.py", home, tmp, is_no_agent=True
            )
        except ValueError as e:
            raised = True
            assert "NOT tracked" in str(e)
        assert raised, "Expected ValueError for untracked no_agent script"

    def test_mode2_unmerged_branch_simulation(self):
        """Simulate Mode 2: file committed on another branch, absent on HEAD.

        Simulated by committing then removing from index (as if switching branches).
        """
        tmp = Path(tempfile.mkdtemp())
        _git_init(tmp)

        home = tmp / "profiles" / "jarvis"
        scripts = home / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "branch_only.py").write_text("# only on another branch\n")

        subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-q", "-m", "add-branch-only"], check=True
        )

        # Remove from index — simulates branch switch removing it
        subprocess.run(
            ["git", "-C", str(tmp), "rm", "--cached",
             str((scripts / "branch_only.py").relative_to(tmp))],
            check=True, capture_output=True
        )

        rel_path = str((scripts / "branch_only.py").relative_to(tmp))
        assert not is_tracked(rel_path, tmp), \
            "File removed from index should be treated as untracked"
        assert (scripts / "branch_only.py").exists(), \
            "File should still exist on disk"


if __name__ == "__main__":
    import sys
    total = 0
    passed = 0
    failed = 0
    results = []

    test_classes = [
        TestResolveScriptPath,
        TestIsTracked,
        TestValidateScriptForCreation,
        TestGetHermesCronStores,
        TestCheckAllAgreementWithDetector,
        TestBothModes,
    ]

    for cls in test_classes:
        instance = cls()
        for attr_name in sorted(dir(instance)):
            if not attr_name.startswith("test_"):
                continue
            total += 1
            try:
                getattr(instance, attr_name)()
                passed += 1
                results.append(f"PASS {cls.__name__}.{attr_name}")
            except Exception as e:
                failed += 1
                results.append(f"FAIL {cls.__name__}.{attr_name}: {e}")

    print("\n".join(results))
    print(f"\n=== product self-test summary: {passed}/{total} passed ===")
    sys.exit(1 if failed else 0)
