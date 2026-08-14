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


    def test_update_job_add_script(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="Hello", schedule="every 1h")
        assert job.get("script") is None

        updated = update_job(job["id"], {"script": "/new/script.py"})
        assert updated["script"] == "/new/script.py"


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

    @pytest.mark.windows_only
    def test_windows_uv_venv_python_script_bypasses_launcher(self, cron_env, tmp_path, monkeypatch):
        # Windows-only: the fake ``sys.platform`` could not reproduce the
        # ``Scripts/python.exe`` launcher layout or the CREATE_NO_WINDOW
        # creationflags this branch exists for.
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

        monkeypatch.setattr(sched_mod.sys, "executable", str(venv_python))
        monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)

        success, output = _run_job_script("probe.py")

        assert success is True
        assert output == "ok"
        assert captured["argv"] == [str(base_python), str(script.resolve())]
        assert captured["kwargs"]["creationflags"] == sched_mod.windows_hide_flags()
        env = captured["kwargs"]["env"]
        assert env["VIRTUAL_ENV"] == str(venv)
        assert str(site_packages) in env["PYTHONPATH"]


    def test_non_windows_script_preserves_default_text_decoding(self, cron_env, monkeypatch):
        # No platform patching: the Linux CI host already takes this branch.
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "probe.py"
        script.write_text('print("ok")\n')

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

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

    def test_emoji_stdout_round_trips_through_script_capture(self, cron_env):
        """Emoji in script stdout must reach the caller intact (#42384).

        On Windows the fix is the utf-8 + errors='replace' popen kwargs
        (asserted above); on POSIX the UTF-8 locale default must already
        carry emoji through. Either way the delivery content is the real
        text, never an exception.
        """
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "emoji.py"
        script.write_text(
            'import sys\n'
            'sys.stdout.buffer.write("backup done \\N{PARTY POPPER} 日次".encode("utf-8"))\n',
            encoding="utf-8",
        )

        success, output = _run_job_script("emoji.py")

        assert success is True
        assert "backup done 🎉 日次" == output

    def test_invalid_utf8_stdout_does_not_raise(self, cron_env):
        """Truncated/invalid UTF-8 in script stdout must never escape as an
        exception (#47393) — a raised UnicodeDecodeError higher up would
        silently drop the whole delivery (#42384). The run may fail, but it
        must fail as a (False, message) result the scheduler can deliver.
        """
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "bad_bytes.py"
        # b'\xe6\x97' is the first two bytes of a three-byte CJK sequence —
        # a truncated write, exactly the shape reported in #47393.
        script.write_text(
            "import sys\n"
            "sys.stdout.buffer.write(b'partial \\xe6\\x97')\n",
            encoding="utf-8",
        )

        success, output = _run_job_script("bad_bytes.py")  # must not raise

        assert isinstance(success, bool)
        assert isinstance(output, str)
        assert output  # a message is always produced, never a silent drop


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

        # Dead-pin guard: the script file must exist on disk at create time.
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

        # Dead-pin guard: the new script must exist on disk at update time.
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

        # Dead-pin guard: the initial script must exist on disk at create time.
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

        # Dead-pin guard: the script must exist on disk at create time.
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

        # Dead-pin guard: the script file must exist on disk at create time.
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

        # Dead-pin guard: the initial script must exist on disk at create time.
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
    """Dead-pin guard (enable-time): a script file that does not exist on disk
    must be rejected at create/update time with a clear error.

    This closes the silent-failure class where a job is created pointing at a
    non-existent script and then fails every tick with no operator feedback.
    """

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
        # Clear, actionable error naming the dead-pin.
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
        """A missing script nested in a subdir must also be rejected."""
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
        """A script that exists on disk is accepted (no regression)."""
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
    """Unit-level dead-pin guard for the API-boundary validator.

    Complements TestDeadPinGuardEnableTime (which drives the full ``cronjob``
    tool end-to-end). These tests pin the exact contract of
    ``_validate_cron_script_path`` so a regression in the guard surfaces even
    if the tool's error plumbing changes. This is the ``_validate_cron_script_path``-
    equivalent named in the task spec for requirement (1).
    """

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

        # Empty / None / whitespace = clearing the field, always allowed.
        assert _validate_cron_script_path("") is None
        assert _validate_cron_script_path(None) is None
        assert _validate_cron_script_path("   ") is None


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


class TestRunJobDeadPinFireTime:
    """Fire-time dead-pin behavior: a missing script auto-pauses the job;
    a transient failure (non-zero exit) alerts but does NOT auto-pause.

    These exercise the real ``run_job`` entry point (no_agent path) so the
    acceptance criteria are proven end-to-end, not just the helper.
    """

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
        from cron.jobs import update_job, get_job
        update_job(job_id, {"script": script, "no_agent": no_agent})
        job = get_job(job_id)
        assert job is not None
        return job

    def test_missing_script_autopauses_no_agent(self, cron_env, monkeypatch):
        from cron.scheduler import run_job
        from cron.jobs import get_job

        job = self._make_job(cron_env, monkeypatch, "vanished.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert "Script not found" in err

        paused = get_job(job_id)
        assert paused["enabled"] is False
        assert paused["state"] == "paused"
        assert paused["paused_reason"].startswith("dead-pin: script not found:")
        # Schedule is untouched — only the broken job is paused.
        assert paused["schedule"] is not None

    def test_missing_script_delivers_alert_no_agent(self, cron_env, monkeypatch):
        """Requirement (2a): a missing script must deliver a #critical-alerts ping.

        Auto-pause alone is not enough — the incident class this closes is the
        *silent* failure. We assert the alert was actually emitted, not merely
        that the job got paused.
        """
        import cron.scheduler as sched_mod
        from cron.scheduler import run_job
        from cron.jobs import get_job

        alerts = []
        monkeypatch.setattr(sched_mod, "_alert_critical_alerts", alerts.append)

        job = self._make_job(cron_env, monkeypatch, "vanished.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert "Script not found" in err

        # The alert was actually delivered (the structural fix for the silent
        # dead-pin: the operator is paged, not left guessing).
        assert alerts, "expected a #critical-alerts ping for a missing script"
        assert any("dead-pin" in a.lower() for a in alerts)
        # And the job is paused, as required by (2b).
        paused = get_job(job_id)
        assert paused["enabled"] is False
        assert paused["paused_reason"].startswith("dead-pin: script not found:")

    def test_transient_failure_no_deadpin_alert(self, cron_env, monkeypatch):
        """Requirement (3): a non-missing failure must NOT raise a dead-pin alert.

        A script that exists but crashes this tick must keep firing — a false
        auto-pause would mask a real error behind a paused job and hide it the
        same way the dead-pin class does. We assert no dead-pin alert fires and
        the job stays enabled.
        """
        import cron.scheduler as sched_mod
        from cron.scheduler import run_job
        from cron.jobs import get_job

        alerts = []
        monkeypatch.setattr(sched_mod, "_alert_critical_alerts", alerts.append)

        script = cron_env / "scripts" / "boom.py"
        script.write_text("import sys\nsys.exit(3)\n")
        job = self._make_job(cron_env, monkeypatch, "boom.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert "exited with code 3" in err

        # Transient failure must NOT trigger the dead-pin alert.
        assert not any("dead-pin" in a.lower() for a in alerts), \
            "transient failure must not trigger the dead-pin alert"
        still_enabled = get_job(job_id)
        assert still_enabled["enabled"] is True
        assert still_enabled["state"] != "paused"

    def test_not_a_file_autopauses(self, cron_env, monkeypatch):
        from cron.scheduler import run_job
        from cron.jobs import get_job

        # Create a directory at the target path so it "exists but is not a file".
        (cron_env / "scripts" / "adir").mkdir()
        job = self._make_job(cron_env, monkeypatch, "adir", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert "not a file" in err

        paused = get_job(job_id)
        assert paused["enabled"] is False
        assert paused["state"] == "paused"

    def test_transient_failure_does_not_autopause(self, cron_env, monkeypatch):
        from cron.scheduler import run_job
        from cron.jobs import get_job

        script = cron_env / "scripts" / "boom.py"
        script.write_text("import sys\nsys.exit(3)\n")
        job = self._make_job(cron_env, monkeypatch, "boom.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert "exited with code 3" in err

        still_enabled = get_job(job_id)
        # Transient failure must NOT auto-pause: the job keeps firing.
        assert still_enabled["state"] != "paused"
        assert still_enabled["enabled"] is True

    def test_existing_script_printing_dangerous_substrings_no_autopause(
        self, cron_env, monkeypatch
    ):
        """Regression: an existing script that exits non-zero while printing
        'not a file' or 'script not found' in its output must NOT be
        misclassified as a dead-pin and auto-paused.

        Before the exact-prefix fix, ``_is_missing_script_error`` used broad
        substring matching over the entire script output (stderr+stdout),
        so a non-missing script that happened to print those words would be
        silently auto-paused — masking a real error.
        """
        import cron.scheduler as sched_mod
        from cron.scheduler import run_job
        from cron.jobs import get_job

        alerts = []
        monkeypatch.setattr(sched_mod, "_alert_critical_alerts", alerts.append)

        script = cron_env / "scripts" / "dangerous.py"
        script.write_text(
            'import sys\nprint("Error: not a file")\nprint("Hint: script not found in path")\nsys.exit(2)\n'
        )
        job = self._make_job(cron_env, monkeypatch, "dangerous.py", no_agent=True)
        job_id = job["id"]

        success, doc, response, err = run_job(job)
        assert success is False
        assert "exited with code 2" in err
        assert "not a file" in err
        assert "script not found" in err

        # No dead-pin alert, and the job must stay enabled.
        assert not any("dead-pin" in a.lower() for a in alerts), (
            "script emitting dangerous substrings must not trigger the dead-pin alert"
        )
        still_enabled = get_job(job_id)
        assert still_enabled["enabled"] is True
        assert still_enabled["state"] != "paused"

    def test_missing_script_autopauses_llm_path(self, cron_env, monkeypatch):
        """LLM path (no_agent=False) also auto-pauses on a missing script."""
        from cron.scheduler import run_job
        from cron.jobs import get_job

        job = self._make_job(cron_env, monkeypatch, "gone.py", no_agent=False)
        job_id = job["id"]

        # LLM path will fail downstream (no model), but the dead-pin guard
        # must have already fired during the pre-check script run.
        try:
            run_job(job)
        except Exception:
            pass

        paused = get_job(job_id)
        assert paused["enabled"] is False
        assert paused["state"] == "paused"
        assert paused["paused_reason"].startswith("dead-pin: script not found:")
