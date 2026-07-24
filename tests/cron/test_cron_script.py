"""Tests for cron job script injection feature.

Tests cover:
- Script field in job creation / storage / update
- Script execution and output injection into prompts
- Error handling (missing script, timeout, non-zero exit)
- Path resolution (absolute, relative to HERMES_HOME/scripts/)
"""

import json
import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    (hermes_home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # Clear cached module-level paths
    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    return hermes_home


class TestJobScriptField:
    """Test that the script field is stored and retrieved correctly."""

    def test_create_job_with_script(self, cron_env):
        from cron.jobs import create_job, get_job

        job = create_job(
            prompt="Analyze the data",
            schedule="every 30m",
            script="/path/to/monitor.py",
        )
        assert job["script"] == "/path/to/monitor.py"

        loaded = get_job(job["id"])
        assert loaded["script"] == "/path/to/monitor.py"

    def test_create_job_without_script(self, cron_env):
        from cron.jobs import create_job

        job = create_job(prompt="Hello", schedule="every 1h")
        assert job.get("script") is None

    def test_create_job_empty_script_normalized_to_none(self, cron_env):
        from cron.jobs import create_job

        job = create_job(prompt="Hello", schedule="every 1h", script="  ")
        assert job.get("script") is None

    def test_update_job_add_script(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="Hello", schedule="every 1h")
        assert job.get("script") is None

        updated = update_job(job["id"], {"script": "/new/script.py"})
        assert updated["script"] == "/new/script.py"

    def test_update_job_clear_script(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="Hello", schedule="every 1h", script="/some/script.py")
        assert job["script"] == "/some/script.py"

        updated = update_job(job["id"], {"script": None})
        assert updated.get("script") is None


def test_cronjob_tool_rejects_stale_past_one_shot(cron_env, monkeypatch):
    from tools.cronjob_tools import cronjob

    now = datetime(2026, 3, 18, 4, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    stale = (now - timedelta(minutes=5)).isoformat()

    result = json.loads(cronjob(action="create", prompt="Too late", schedule=stale))

    assert result["success"] is False
    assert "past and cannot be scheduled" in result["error"]


class TestRunJobScript:
    """Test the _run_job_script() function."""

    def test_successful_script(self, cron_env):
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "test.py"
        script.write_text('print("hello from script")\n')

        success, output = _run_job_script(str(script))
        assert success is True
        assert output == "hello from script"

    def test_script_relative_path(self, cron_env):
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "relative.py"
        script.write_text('print("relative works")\n')

        success, output = _run_job_script("relative.py")
        assert success is True
        assert output == "relative works"

    def test_script_not_found(self, cron_env):
        from cron.scheduler import _run_job_script

        success, output = _run_job_script("nonexistent_script.py")
        assert success is False
        assert "not found" in output.lower()

    def test_script_nonzero_exit(self, cron_env):
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "fail.py"
        script.write_text(textwrap.dedent("""\
            import sys
            print("partial output")
            print("error info", file=sys.stderr)
            sys.exit(1)
        """))

        success, output = _run_job_script(str(script))
        assert success is False
        assert "exited with code 1" in output
        assert "error info" in output

    def test_script_subprocess_env_sanitized(self, cron_env, monkeypatch):
        """Cron scripts must not inherit Hermes provider env (SECURITY.md §2.3)."""
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST
        from cron.scheduler import _run_job_script

        # sorted() so the probed var is deterministic across runs
        # (frozenset iteration order varies with PYTHONHASHSEED).
        blocked_var = sorted(_HERMES_PROVIDER_ENV_BLOCKLIST)[0]
        monkeypatch.setenv(blocked_var, "must_not_leak")

        script = cron_env / "scripts" / "env_probe.py"
        script.write_text(
            textwrap.dedent(
                f"""\
                import os
                key = {blocked_var!r}
                print("PRESENT" if os.environ.get(key) else "ABSENT")
                """
            )
        )

        success, output = _run_job_script("env_probe.py")
        assert success is True
        assert output == "ABSENT"

    def test_windows_uv_venv_python_script_bypasses_launcher(self, cron_env, tmp_path, monkeypatch):
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "probe.py"
        script.write_text('print("ok")\n')

        venv = tmp_path / "venv"
        venv_scripts = venv / "Scripts"
        site_packages = venv / "Lib" / "site-packages"
        base = tmp_path / "base"
        venv_scripts.mkdir(parents=True)
        site_packages.mkdir(parents=True)
        base.mkdir()
        venv_python = venv_scripts / "python.exe"
        base_python = base / "python.exe"
        venv_python.write_text("", encoding="utf-8")
        base_python.write_text("", encoding="utf-8")
        (venv / "pyvenv.cfg").write_text(f"home = {base}\nuv = true\n", encoding="utf-8")

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        monkeypatch.setattr(sched_mod.sys, "platform", "win32")
        monkeypatch.setattr(sched_mod.sys, "executable", str(venv_python))
        monkeypatch.setattr(sched_mod, "windows_hide_flags", lambda: 0x08000000)
        monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)

        success, output = _run_job_script("probe.py")

        assert success is True
        assert output == "ok"
        assert captured["argv"] == [str(base_python), str(script.resolve())]
        assert captured["kwargs"]["creationflags"] == 0x08000000
        env = captured["kwargs"]["env"]
        assert env["VIRTUAL_ENV"] == str(venv)
        assert str(site_packages) in env["PYTHONPATH"]

    def test_windows_pythonw_script_uses_sibling_python_for_captured_output(self, cron_env, tmp_path, monkeypatch):
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "probe.py"
        script.write_text('print("ok")\n')

        venv = tmp_path / "venv"
        venv_scripts = venv / "Scripts"
        venv_scripts.mkdir(parents=True)
        pythonw = venv_scripts / "pythonw.exe"
        python = venv_scripts / "python.exe"
        pythonw.write_text("", encoding="utf-8")
        python.write_text("", encoding="utf-8")

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        monkeypatch.setattr(sched_mod.sys, "platform", "win32")
        monkeypatch.setattr(sched_mod.sys, "executable", str(pythonw))
        monkeypatch.setattr(sched_mod, "windows_hide_flags", lambda: 0x08000000)
        monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)

        success, output = _run_job_script("probe.py")

        assert success is True
        assert output == "ok"
        assert captured["argv"] == [str(python), str(script.resolve())]
        assert captured["kwargs"]["encoding"] == "utf-8"
        assert captured["kwargs"]["errors"] == "replace"

    def test_non_windows_script_preserves_default_text_decoding(self, cron_env, monkeypatch):
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "probe.py"
        script.write_text('print("ok")\n')

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        monkeypatch.setattr(sched_mod.sys, "platform", "linux")
        monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)

        success, output = _run_job_script("probe.py")

        assert success is True
        assert output == "ok"
        assert captured["argv"] == [sys.executable, str(script.resolve())]
        assert captured["kwargs"]["text"] is True
        assert "creationflags" not in captured["kwargs"]
        assert "encoding" not in captured["kwargs"]
        assert "errors" not in captured["kwargs"]

    def test_systemd_exec_context_wraps_script_with_user_service(self, cron_env, monkeypatch):
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "probe.py"
        script.write_text('print("ok")\n')

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        monkeypatch.setattr(sched_mod.sys, "platform", "linux")
        monkeypatch.setattr(sched_mod.shutil, "which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
        monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)

        success, output = _run_job_script(
            "probe.py",
            job_id="13c1f9279025",
            exec_context={
                "mode": "systemd-run-user-service",
                "unit_prefix": "hermes-cron-heavy-native",
                "properties": {"TasksMax": "4096", "Slice": "hermes-cron-heavy-native.slice"},
            },
        )

        assert success is True
        assert output == "ok"
        argv = captured["argv"]
        assert argv[:6] == [
            "/usr/bin/systemd-run",
            "--user",
            "--wait",
            "--pipe",
            "--collect",
            "--quiet",
        ]
        assert any(arg.startswith("--unit=hermes-cron-heavy-native-13c1f9279025-") for arg in argv)
        assert "--property=Slice=hermes-cron-heavy-native.slice" in argv
        assert "--property=TasksMax=4096" in argv
        assert argv[-2:] == [sys.executable, str(script.resolve())]
        assert captured["kwargs"]["cwd"] == str(script.parent)

    def test_systemd_exec_context_rejects_unsupported_property(self, cron_env):
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "probe.py"
        script.write_text('print("should not run")\n')

        success, output = _run_job_script(
            "probe.py",
            exec_context={
                "mode": "systemd-run-user-service",
                "properties": {"Environment": "SECRET=x"},
            },
        )

        assert success is False
        assert "unsupported cron exec_context systemd property" in output

    def test_systemd_exec_context_timeout_cleans_generated_unit(self, cron_env, monkeypatch):
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        monkeypatch.setattr(sched_mod, "_SCRIPT_TIMEOUT", 7)
        script = cron_env / "scripts" / "slow.py"
        script.write_text("import time; time.sleep(30)\n")

        calls = []

        def fake_which(name):
            return {
                "systemd-run": "/usr/bin/systemd-run",
                "systemctl": "/usr/bin/systemctl",
            }.get(name)

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "/usr/bin/systemd-run":
                raise sched_mod.subprocess.TimeoutExpired(cmd=argv, timeout=7)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sched_mod.sys, "platform", "linux")
        monkeypatch.setattr(sched_mod.shutil, "which", fake_which)
        monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)

        success, output = _run_job_script(
            "slow.py",
            job_id="13c1f9279025",
            exec_context={
                "mode": "systemd-run-user-service",
                "unit_prefix": "hermes-cron-heavy-native",
            },
        )

        assert success is False
        assert output == f"Script timed out after 7s: {script.resolve()}"
        systemd_run_call = calls[0]
        unit_arg = next(arg for arg in systemd_run_call if arg.startswith("--unit="))
        unit = unit_arg.removeprefix("--unit=")
        assert unit.startswith("hermes-cron-heavy-native-13c1f9279025-")
        assert calls[1:] == [
            ["/usr/bin/systemctl", "--user", "kill", "--kill-whom=all", "--signal=SIGKILL", unit],
            ["/usr/bin/systemctl", "--user", "stop", unit],
            ["/usr/bin/systemctl", "--user", "reset-failed", unit],
        ]

    def test_systemd_exec_context_timeout_surfaces_cleanup_failure(self, cron_env, monkeypatch):
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        monkeypatch.setattr(sched_mod, "_SCRIPT_TIMEOUT", 3)
        script = cron_env / "scripts" / "slow.py"
        script.write_text("import time; time.sleep(30)\n")

        def fake_which(name):
            return {
                "systemd-run": "/usr/bin/systemd-run",
                "systemctl": "/usr/bin/systemctl",
            }.get(name)

        def fake_run(argv, **kwargs):
            if argv[0] == "/usr/bin/systemd-run":
                raise sched_mod.subprocess.TimeoutExpired(cmd=argv, timeout=3)
            if argv[2] == "kill":
                return SimpleNamespace(returncode=1, stdout="", stderr="Access denied")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sched_mod.sys, "platform", "linux")
        monkeypatch.setattr(sched_mod.shutil, "which", fake_which)
        monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)

        success, output = _run_job_script(
            "slow.py",
            job_id="13c1f9279025",
            exec_context={"mode": "systemd-run-user-service"},
        )

        assert success is False
        assert output.startswith(f"Script timed out after 3s: {script.resolve()}")
        assert "Cron exec_context cleanup failed for unit hermes-cron-13c1f9279025-" in output
        assert "systemctl --user kill" in output
        assert "Access denied" in output

    def test_script_empty_output(self, cron_env):
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "empty.py"
        script.write_text("# no output\n")

        success, output = _run_job_script(str(script))
        assert success is True
        assert output == ""

    def test_script_timeout(self, cron_env, monkeypatch):
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        # Use a very short timeout
        monkeypatch.setattr(sched_mod, "_SCRIPT_TIMEOUT", 1)

        script = cron_env / "scripts" / "slow.py"
        script.write_text("import time; time.sleep(30)\n")

        success, output = _run_job_script(str(script))
        assert success is False
        assert "timed out" in output.lower()

    def test_script_json_output(self, cron_env):
        """Scripts can output structured JSON for the LLM to parse."""
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "json_out.py"
        script.write_text(textwrap.dedent("""\
            import json
            data = {"new_prs": [{"number": 42, "title": "Fix bug"}]}
            print(json.dumps(data, indent=2))
        """))

        success, output = _run_job_script(str(script))
        assert success is True
        parsed = json.loads(output)
        assert parsed["new_prs"][0]["number"] == 42


class TestBuildJobPromptWithScript:
    """Test that script output is injected into the prompt."""

    def test_script_output_injected(self, cron_env):
        from cron.scheduler import _build_job_prompt

        script = cron_env / "scripts" / "data.py"
        script.write_text('print("new PR: #123 fix typo")\n')

        job = {
            "prompt": "Report any notable changes.",
            "script": str(script),
        }
        prompt = _build_job_prompt(job)
        assert "## Script Output" in prompt
        assert "new PR: #123 fix typo" in prompt
        assert "Report any notable changes." in prompt

    def test_script_error_injected(self, cron_env):
        from cron.scheduler import _build_job_prompt

        job = {
            "prompt": "Report status.",
            "script": "nonexistent_monitor.py",
        }
        prompt = _build_job_prompt(job)
        assert "## Script Error" in prompt
        assert "not found" in prompt.lower()
        assert "Report status." in prompt

    def test_no_script_unchanged(self, cron_env):
        from cron.scheduler import _build_job_prompt

        job = {"prompt": "Simple job."}
        prompt = _build_job_prompt(job)
        assert "## Script Output" not in prompt
        assert "Simple job." in prompt



class TestCronjobToolScript:
    """Test the cronjob tool's script parameter."""

    def test_create_with_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        (cron_env / "scripts" / "monitor.py").write_text('print("ok")\n')
        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="monitor.py",
        ))
        assert result["success"] is True
        assert result["job"]["script"] == "monitor.py"

    def test_update_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        create_result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
        ))
        job_id = create_result["job_id"]

        (cron_env / "scripts" / "new_script.py").write_text('print("ok")\n')
        update_result = json.loads(cronjob(
            action="update",
            job_id=job_id,
            script="new_script.py",
        ))
        assert update_result["success"] is True
        assert update_result["job"]["script"] == "new_script.py"

    def test_clear_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        (cron_env / "scripts" / "some_script.py").write_text('print("ok")\n')
        create_result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="some_script.py",
        ))
        job_id = create_result["job_id"]

        update_result = json.loads(cronjob(
            action="update",
            job_id=job_id,
            script="",
        ))
        assert update_result["success"] is True
        assert "script" not in update_result["job"]

    def test_list_shows_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        (cron_env / "scripts" / "data_collector.py").write_text('print("ok")\n')
        cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="data_collector.py",
        )

        list_result = json.loads(cronjob(action="list"))
        assert list_result["success"] is True
        assert len(list_result["jobs"]) == 1
        assert list_result["jobs"][0]["script"] == "data_collector.py"


class TestScriptPathContainment:
    """Regression tests for path containment bypass in _run_job_script().

    Prior to the fix, absolute paths and ~-prefixed paths bypassed the
    scripts_dir containment check entirely, allowing arbitrary script
    execution through the cron system.
    """

    def test_absolute_path_outside_scripts_dir_blocked(self, cron_env):
        """Absolute paths outside ~/.hermes/scripts/ must be rejected."""
        from cron.scheduler import _run_job_script

        # Create a script outside the scripts dir
        outside_script = cron_env / "outside.py"
        outside_script.write_text('print("should not run")\n')

        success, output = _run_job_script(str(outside_script))
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()

    def test_absolute_path_tmp_blocked(self, cron_env):
        """Absolute paths to /tmp must be rejected."""
        from cron.scheduler import _run_job_script

        success, output = _run_job_script("/tmp/evil.py")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()

    def test_tilde_path_blocked(self, cron_env):
        """~ prefixed paths must be rejected (expanduser bypasses check)."""
        from cron.scheduler import _run_job_script

        success, output = _run_job_script("~/evil.py")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()

    def test_tilde_traversal_blocked(self, cron_env):
        """~/../../../tmp/evil.py must be rejected."""
        from cron.scheduler import _run_job_script

        success, output = _run_job_script("~/../../../tmp/evil.py")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()

    def test_relative_traversal_still_blocked(self, cron_env):
        """../../etc/passwd style traversal must still be blocked."""
        from cron.scheduler import _run_job_script

        success, output = _run_job_script("../../etc/passwd")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()

    def test_relative_path_inside_scripts_dir_allowed(self, cron_env):
        """Relative paths within the scripts dir should still work."""
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "good.py"
        script.write_text('print("ok")\n')

        success, output = _run_job_script("good.py")
        assert success is True
        assert output == "ok"

    def test_subdirectory_inside_scripts_dir_allowed(self, cron_env):
        """Relative paths to subdirectories within scripts/ should work."""
        from cron.scheduler import _run_job_script

        subdir = cron_env / "scripts" / "monitors"
        subdir.mkdir()
        script = subdir / "check.py"
        script.write_text('print("sub ok")\n')

        success, output = _run_job_script("monitors/check.py")
        assert success is True
        assert output == "sub ok"

    def test_absolute_path_inside_scripts_dir_allowed(self, cron_env):
        """Absolute paths that resolve WITHIN scripts/ should work."""
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "abs_ok.py"
        script.write_text('print("abs ok")\n')

        success, output = _run_job_script(str(script))
        assert success is True
        assert output == "abs ok"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlinks require elevated privileges on Windows",
    )
    def test_symlink_escape_blocked(self, cron_env, tmp_path):
        """Symlinks pointing outside scripts/ must be rejected."""
        from cron.scheduler import _run_job_script

        # Create a script outside the scripts dir
        outside = tmp_path / "outside_evil.py"
        outside.write_text('print("escaped")\n')

        # Create a symlink inside scripts/ pointing outside
        link = cron_env / "scripts" / "sneaky.py"
        link.symlink_to(outside)

        success, output = _run_job_script("sneaky.py")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()


class TestCronjobToolScriptValidation:
    """Test API-boundary validation of cron script paths in cronjob_tools."""

    def test_create_with_absolute_script_rejected(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="/home/user/evil.py",
        ))
        assert result["success"] is False
        assert "relative" in result["error"].lower() or "absolute" in result["error"].lower()

    def test_create_with_tilde_script_rejected(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="~/monitor.py",
        ))
        assert result["success"] is False
        assert "relative" in result["error"].lower() or "absolute" in result["error"].lower()

    def test_create_with_traversal_script_rejected(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="../../etc/passwd",
        ))
        assert result["success"] is False
        assert "escapes" in result["error"].lower() or "traversal" in result["error"].lower()

    def test_create_with_relative_script_allowed(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        (cron_env / "scripts" / "monitor.py").write_text('print("ok")\n')
        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="monitor.py",
        ))
        assert result["success"] is True
        assert result["job"]["script"] == "monitor.py"

    def test_update_with_absolute_script_rejected(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        create_result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
        ))
        job_id = create_result["job_id"]

        update_result = json.loads(cronjob(
            action="update",
            job_id=job_id,
            script="/tmp/evil.py",
        ))
        assert update_result["success"] is False
        assert "relative" in update_result["error"].lower() or "absolute" in update_result["error"].lower()

    def test_update_clear_script_allowed(self, cron_env, monkeypatch):
        """Clearing a script (empty string) should always be permitted."""
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        (cron_env / "scripts" / "monitor.py").write_text('print("ok")\n')
        create_result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="monitor.py",
        ))
        job_id = create_result["job_id"]

        update_result = json.loads(cronjob(
            action="update",
            job_id=job_id,
            script="",
        ))
        assert update_result["success"] is True
        assert "script" not in update_result["job"]

    def test_windows_absolute_path_rejected(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="C:\\Users\\evil\\script.py",
        ))
        assert result["success"] is False


class TestDeadPinGuardEnableTime:
    """Dead-pin guard (enable-time): missing scripts are rejected at create/update."""

    def test_create_rejects_nonexistent_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="does_not_exist.py",
        ))
        assert result["success"] is False
        err = result["error"].lower()
        assert "not found" in err or "not exist" in err or "dead-pin" in err

    def test_update_rejects_nonexistent_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        create_result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
        ))
        job_id = create_result["job_id"]

        update_result = json.loads(cronjob(
            action="update",
            job_id=job_id,
            script="vanished.py",
        ))
        assert update_result["success"] is False
        err = update_result["error"].lower()
        assert "not found" in err or "not exist" in err or "dead-pin" in err

    def test_update_rejects_nonexistent_subdir_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        create_result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
        ))
        job_id = create_result["job_id"]

        update_result = json.loads(cronjob(
            action="update",
            job_id=job_id,
            script="monitors/vanished.py",
        ))
        assert update_result["success"] is False

    def test_create_allows_existing_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        (cron_env / "scripts" / "exists.py").write_text('print("ok")\n')
        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="exists.py",
        ))
        assert result["success"] is True
        assert result["job"]["script"] == "exists.py"


class TestValidateCronScriptPathDeadPin:
    """Unit-level dead-pin guard for the API-boundary validator."""

    def test_rejects_nonexistent_script(self, cron_env):
        from tools.cronjob_tools import _validate_cron_script_path

        err = _validate_cron_script_path("does_not_exist.py")
        assert err is not None
        low = err.lower()
        assert "not found" in low or "dead-pin" in low

    def test_rejects_nonexistent_subdir_script(self, cron_env):
        from tools.cronjob_tools import _validate_cron_script_path

        err = _validate_cron_script_path("monitors/vanished.py")
        assert err is not None

    def test_allows_existing_script(self, cron_env):
        from tools.cronjob_tools import _validate_cron_script_path

        (cron_env / "scripts" / "exists.py").write_text('print("ok")\n')
        assert _validate_cron_script_path("exists.py") is None

    def test_empty_script_is_none(self, cron_env):
        from tools.cronjob_tools import _validate_cron_script_path

        assert _validate_cron_script_path("") is None
        assert _validate_cron_script_path(None) is None
        assert _validate_cron_script_path("   ") is None


class TestRunJobDeadPinFireTime:
    """Fire-time missing script alerts #critical-alerts and auto-pauses only dead pins."""

    def _make_job(self, cron_env, monkeypatch, script, no_agent=True):
        import json as _json
        from tools.cronjob_tools import cronjob

        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        # Create time uses the enable-time guard, so seed an existing script.
        (cron_env / "scripts" / "seed.py").write_text('print("ok")\n')
        created = _json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="probe",
            script="seed.py",
            no_agent=no_agent,
        ))
        job_id = created["job_id"]
        # Now point the job at the (possibly missing) script directly in the
        # persisted store, bypassing the enable-time guard, and read it back.
        from cron.jobs import get_job, update_job

        update_job(job_id, {"script": script, "no_agent": no_agent})
        job = get_job(job_id)
        assert job is not None
        return job

    def test_missing_script_autopauses_no_agent(self, cron_env, monkeypatch):
        from cron.jobs import get_job
        from cron.scheduler import run_job

        job = self._make_job(cron_env, monkeypatch, "vanished.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert err is not None
        assert "Script not found" in err

        paused = get_job(job_id)
        assert paused is not None
        assert paused["enabled"] is False
        assert paused["state"] == "paused"
        assert paused["paused_reason"].startswith("dead-pin: script not found:")
        # Schedule is untouched — only the broken job is paused.
        assert paused["schedule"] is not None

    def test_missing_script_delivers_alert_no_agent(self, cron_env, monkeypatch):
        import cron.scheduler as sched_mod
        from cron.jobs import get_job
        from cron.scheduler import run_job

        alerts = []
        monkeypatch.setattr(sched_mod, "_alert_critical_alerts", alerts.append)

        job = self._make_job(cron_env, monkeypatch, "vanished.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert err is not None
        assert "Script not found" in err

        assert alerts, "expected a #critical-alerts ping for a missing script"
        assert any("dead-pin" in a.lower() for a in alerts)
        paused = get_job(job_id)
        assert paused is not None
        assert paused["enabled"] is False
        assert paused["paused_reason"].startswith("dead-pin: script not found:")

    def test_transient_failure_no_deadpin_alert(self, cron_env, monkeypatch):
        import cron.scheduler as sched_mod
        from cron.jobs import get_job
        from cron.scheduler import run_job

        alerts = []
        monkeypatch.setattr(sched_mod, "_alert_critical_alerts", alerts.append)

        script = cron_env / "scripts" / "boom.py"
        script.write_text("import sys\nsys.exit(3)\n")
        job = self._make_job(cron_env, monkeypatch, "boom.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert err is not None
        assert "exited with code 3" in err

        assert not any("dead-pin" in a.lower() for a in alerts), (
            "transient failure must not trigger the dead-pin alert"
        )
        still_enabled = get_job(job_id)
        assert still_enabled is not None
        assert still_enabled["enabled"] is True
        assert still_enabled["state"] != "paused"

    def test_not_a_file_autopauses(self, cron_env, monkeypatch):
        from cron.jobs import get_job
        from cron.scheduler import run_job

        (cron_env / "scripts" / "adir").mkdir()
        job = self._make_job(cron_env, monkeypatch, "adir", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert err is not None
        assert "not a file" in err

        paused = get_job(job_id)
        assert paused is not None
        assert paused["enabled"] is False
        assert paused["state"] == "paused"

    def test_transient_failure_does_not_autopause(self, cron_env, monkeypatch):
        from cron.jobs import get_job
        from cron.scheduler import run_job

        script = cron_env / "scripts" / "boom.py"
        script.write_text("import sys\nsys.exit(3)\n")
        job = self._make_job(cron_env, monkeypatch, "boom.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert err is not None
        assert "exited with code 3" in err

        still_enabled = get_job(job_id)
        assert still_enabled is not None
        assert still_enabled["state"] != "paused"
        assert still_enabled["enabled"] is True

    def test_missing_script_autopauses_llm_path(self, cron_env, monkeypatch):
        from cron.jobs import get_job
        from cron.scheduler import run_job

        job = self._make_job(cron_env, monkeypatch, "gone.py", no_agent=False)
        job_id = job["id"]

        # LLM path may fail downstream (no model), but the dead-pin guard must
        # have already fired during the pre-check script run.
        try:
            run_job(job)
        except Exception:
            pass

        paused = get_job(job_id)
        assert paused is not None
        assert paused["enabled"] is False
        assert paused["state"] == "paused"
        assert paused["paused_reason"].startswith("dead-pin: script not found:")


class TestRunJobEnvVarCleanup:
    """Test that run_job() env vars are cleaned up even on early failure."""

    def test_env_vars_cleaned_on_early_error(self, cron_env, monkeypatch):
        """Origin env vars must be cleaned up even if run_job fails early."""
        # Ensure env vars are clean before test
        for key in (
            "HERMES_SESSION_PLATFORM",
            "HERMES_SESSION_CHAT_ID",
            "HERMES_SESSION_CHAT_NAME",
        ):
            monkeypatch.delenv(key, raising=False)

        # Build a job with origin info that will fail during execution
        # (no valid model, no API key — will raise inside try block)
        job = {
            "id": "test-envleak",
            "name": "env-leak-test",
            "prompt": "test",
            "schedule_display": "every 1h",
            "origin": {
                "platform": "telegram",
                "chat_id": "12345",
                "chat_name": "Test Chat",
            },
        }

        from cron.scheduler import run_job

        # Expect it to fail (no model/API key), but env vars must be cleaned
        try:
            run_job(job)
        except Exception:
            pass

        # Verify env vars were cleaned up by the finally block
        assert os.environ.get("HERMES_SESSION_PLATFORM") is None
        assert os.environ.get("HERMES_SESSION_CHAT_ID") is None
        assert os.environ.get("HERMES_SESSION_CHAT_NAME") is None
