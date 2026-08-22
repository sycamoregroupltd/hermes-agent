"""Tests for the systemd ExecStopPost cgroup reaper (issue #37454)."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from gateway import cgroup_cleanup


class TestOwnCgroupPath:
    def test_parses_v2_cgroup_path(self, tmp_path, monkeypatch):
        proc_self = tmp_path / "cgroup"
        proc_self.write_text("0::/user.slice/user-1000.slice/hermes-gateway.service\n")
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: proc_self if p == "/proc/self/cgroup" else Path(p),
        )

        assert cgroup_cleanup._own_cgroup_path() == "/user.slice/user-1000.slice/hermes-gateway.service"


class TestReapCgroup:


    def test_noop_when_procs_file_missing(self, tmp_path, monkeypatch):
        cgroup_path = "/missing.slice/hermes-gateway.service"
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: tmp_path / "does-not-exist" if "cgroup.procs" in p else Path(p),
        )

        def _explode(*_a, **_kw):
            pytest.fail("os.kill must not be called when cgroup.procs is unreadable")

        monkeypatch.setattr(cgroup_cleanup.os, "kill", _explode)
        assert cgroup_cleanup.reap_cgroup(cgroup_path) == 0


class TestKanbanWorkerSkip:
    """t_022cb698: in-flight kanban dispatcher workers must survive the
    ExecStopPost cgroup teardown (bulk teardown race)."""

    def test_is_kanban_worker_true_when_env_has_task(self, tmp_path, monkeypatch):
        proc_env = tmp_path / "environ"
        proc_env.write_bytes(
            b"PATH=/usr/bin\x00HERMES_HOME=/home/frank/.hermes\x00"
            b"HERMES_KANBAN_TASK=t_1234\x00HERMES_PROFILE=default"
        )
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: proc_env if str(p) == f"/proc/9999/environ" else Path(p),
        )
        assert cgroup_cleanup._is_kanban_worker(9999) is True

    def test_is_kanban_worker_false_without_task(self, tmp_path, monkeypatch):
        proc_env = tmp_path / "environ"
        proc_env.write_bytes(b"PATH=/usr/bin\x00HERMES_PROFILE=default")
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: proc_env if str(p) == f"/proc/9998/environ" else Path(p),
        )
        assert cgroup_cleanup._is_kanban_worker(9998) is False

    def test_is_kanban_worker_false_when_env_unreadable(self, tmp_path, monkeypatch):
        # A missing /proc/<pid>/environ must NOT be treated as a worker: we
        # never refuse to reap on a permissions quirk.
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: tmp_path / "missing" if "environ" in str(p) else Path(p),
        )
        assert cgroup_cleanup._is_kanban_worker(9997) is False

    def test_reap_cgroup_skips_kanban_worker_but_kills_others(
        self, tmp_path, monkeypatch,
    ):
        procs_file = tmp_path / "cgroup.procs"
        # pid 100 = own pid (skipped), 200 = kanban worker (skipped), 300 = orphan (killed)
        procs_file.write_text("100\n200\n300\n")
        worker_env = tmp_path / "env200"
        worker_env.write_bytes(b"HERMES_KANBAN_TASK=t_5678\x00")
        orphan_env = tmp_path / "env300"
        orphan_env.write_bytes(b"PATH=/usr/bin\x00")

        real_path = Path

        def fake_path(p):
            p = str(p)
            if p == "/sys/fs/cgroup/some.slice/gateway.service/cgroup.procs":
                return procs_file
            if p == "/proc/200/environ":
                return worker_env
            if p == "/proc/300/environ":
                return orphan_env
            return real_path(p)

        monkeypatch.setattr(cgroup_cleanup, "Path", fake_path)
        killed = []
        monkeypatch.setattr(
            cgroup_cleanup.os,
            "kill",
            lambda pid, sig: killed.append((pid, sig)),
        )
        monkeypatch.setattr(cgroup_cleanup.os, "getpid", lambda: 100)

        count = cgroup_cleanup.reap_cgroup("/some.slice/gateway.service")
        assert count == 1
        assert killed == [(300, signal.SIGKILL)], (
            f"only the orphan (300) should be killed, got {killed}"
        )

