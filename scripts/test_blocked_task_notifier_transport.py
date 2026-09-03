#!/usr/bin/env python3
"""Hermetic transport tests for blocked_task_notifier.py."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(os.environ.get("NOTIFIER_PATH", Path(__file__).with_name("blocked_task_notifier.py")))


def load_notifier():
    spec = importlib.util.spec_from_file_location("blocked_task_notifier_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profile_runtime_home_is_preserved(monkeypatch):
    notifier = load_notifier()
    seen = {}

    class Result:
        returncode = 0
        stdout = '{"success": true}'
        stderr = ""

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs["env"]
        return Result()

    monkeypatch.setenv("HERMES_HOME", "/home/frank/.hermes/profiles/jarvis")
    with patch.object(notifier.subprocess, "run", side_effect=fake_run):
        assert notifier._run_hermes_send("discord:#critical-alerts", "subject", "fixture", 1) == (0, '{"success": true}')

    assert seen["env"]["HERMES_HOME"] == "/home/frank/.hermes/profiles/jarvis"
    assert seen["args"][:2] == [notifier.HERMES, "send"]


def test_unset_runtime_home_defaults_to_jarvis_profile(monkeypatch):
    notifier = load_notifier()
    seen = {}

    class Result:
        returncode = 1
        stdout = ""
        stderr = "fixture failure"

    def fake_run(_args, **kwargs):
        seen["env"] = kwargs["env"]
        return Result()

    monkeypatch.delenv("HERMES_HOME", raising=False)
    with patch.object(notifier.subprocess, "run", side_effect=fake_run):
        rc, detail = notifier._run_hermes_send("discord:#critical-alerts", "subject", "fixture", 1)

    assert rc == 1
    assert detail == "fixture failure"
    assert seen["env"]["HERMES_HOME"] == "/home/frank/.hermes/profiles/jarvis"


def test_primary_and_fallback_failure_stays_fail_closed(monkeypatch):
    notifier = load_notifier()
    calls = []
    responses = iter([
        (1, "Could not resolve '#critical-alerts' on discord"),
        (1, "Could not resolve '#critical-alerts' on discord"),
        (1, "WhatsApp bridge error (connection refused)"),
    ])

    def fake_send(target, subject, message, timeout):
        calls.append((target, subject, message, timeout))
        return next(responses)

    monkeypatch.setattr(notifier, "_run_hermes_send", fake_send)
    ok, detail = notifier.send_alert("hermetic RED fixture")

    assert ok is False
    assert len(calls) == 3
    assert calls[0][0] == notifier.ALERT_TARGET
    assert calls[2][0] == notifier.WA_FALLBACK
    assert "attempt 2/2: rc=1" in detail
    assert "wa-fallback=failed" in detail
    assert "connection refused" in detail


def test_successful_primary_is_green_without_fallback(monkeypatch):
    notifier = load_notifier()
    calls = []

    def fake_send(target, subject, message, timeout):
        calls.append(target)
        return 0, '{"success": true}'

    monkeypatch.setattr(notifier, "_run_hermes_send", fake_send)
    ok, detail = notifier.send_alert("hermetic GREEN fixture")

    assert ok is True
    assert calls == [notifier.ALERT_TARGET]
    assert detail == 'rc=0 {"success": true}'
