"""Claim gate: stuck source-completion-pending must not spawn --finish-update
without an owned non-expired flight_claim for hermes-update.

SPEC: hermes-finish-update-claim-gate-DRAFT-20260926.md (Isolation HOLD).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cli import venv_sync


FLIGHT_SCHEMA = """
CREATE TABLE flight_claim (
  flight_key     TEXT PRIMARY KEY,
  owner_seat     TEXT NOT NULL,
  owner_pid      INTEGER,
  owner_card     TEXT,
  state          TEXT NOT NULL
                 CHECK (state IN ('owned','releasing','expired')),
  phase          TEXT NOT NULL DEFAULT 'finish-update'
                 CHECK (phase IN ('finish-update','web-build','done')),
  claimed_at     INTEGER NOT NULL,
  expires_at     INTEGER NOT NULL,
  renew_count    INTEGER NOT NULL DEFAULT 0,
  meta_json      TEXT NOT NULL DEFAULT '{}'
);
"""


def _bus_db(home: Path) -> Path:
    db = home / "session-bus" / "bus.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return db


def _init_bus(home: Path) -> Path:
    db = _bus_db(home)
    con = sqlite3.connect(db)
    con.executescript(FLIGHT_SCHEMA)
    con.close()
    return db


def _insert_claim(db: Path, *, state: str = "owned", expires_in: int = 600) -> None:
    now = int(time.time())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT OR REPLACE INTO flight_claim "
        "(flight_key, owner_seat, owner_pid, state, phase, claimed_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("hermes-update", "test-seat", os.getpid(), state, "finish-update", now, now + expires_in),
    )
    con.commit()
    con.close()


@pytest.fixture
def claim_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_ALLOW_UNCLAIMED_FINISH", raising=False)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    return home


def _self_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "install-stamp.json").write_text(
        json.dumps({"updateMechanism": "self"}), encoding="utf-8",
    )
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli" / "source_completion.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8",
    )
    return root


def _wire_prepare(monkeypatch, root: Path, *, current: bool = True):
    """Stub prepare_launch deps so only the finish/claim path is exercised."""
    import pm
    from hermes_cli import update_lock

    monkeypatch.setattr(venv_sync, "publish_launchers", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.steward.read_install_stamp",
        lambda r: {"updateMechanism": "self"},
    )
    monkeypatch.setattr(pm, "venv_is_current", lambda *, project_root: current)
    monkeypatch.setattr(
        "hermes_cli._launchers.resolve_store_python",
        lambda r: Path("/tmp/fake-store-python"),
    )

    class _Lock:
        acquired = True

        def acquire(self):
            return True

        def release(self):
            return None

    monkeypatch.setattr(update_lock, "UpdateLock", _Lock)
    monkeypatch.setattr(update_lock, "read_live_update", lambda: None)
    monkeypatch.setattr(
        venv_sync,
        "completion_pending_path",
        lambda r: root / "source-completion-pending",
    )


def test_owned_claim_helper_reads_bus_readonly(claim_home):
    db = _init_bus(claim_home)
    assert venv_sync._owned_hermes_update_claim() is False
    _insert_claim(db, state="owned", expires_in=600)
    assert venv_sync._owned_hermes_update_claim() is True
    _insert_claim(db, state="owned", expires_in=-10)  # expired
    assert venv_sync._owned_hermes_update_claim() is False


def test_pending_without_claim_does_not_spawn_finish(claim_home, tmp_path, monkeypatch, capsys):
    """(a) pending + no claim → no subprocess."""
    _init_bus(claim_home)  # empty flight_claim
    root = _self_checkout(tmp_path)
    pending = root / "source-completion-pending"
    pending.write_text("owed\n", encoding="utf-8")
    _wire_prepare(monkeypatch, root, current=True)

    calls: list = []

    def boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess.call must not run without claim")

    monkeypatch.setattr(venv_sync.subprocess, "call", boom)
    # Also guard the finish helper path via prepare_launch
    result = venv_sync.prepare_launch(root, ["status"])
    captured = capsys.readouterr()
    assert calls == []
    assert pending.is_file(), "pending must remain (no silent clear / tip-apply)"
    assert "refusing --finish-update" in captured.err
    assert "hermes-update" in captured.err
    # current + refused finish → still may return store python for relaunch
    assert result == Path("/tmp/fake-store-python")


def test_pending_with_owned_claim_allows_finish_path(claim_home, tmp_path, monkeypatch):
    """(b) pending + owned claim → finish allowed (subprocess mocked)."""
    db = _init_bus(claim_home)
    _insert_claim(db, state="owned", expires_in=600)
    root = _self_checkout(tmp_path)
    pending = root / "source-completion-pending"
    pending.write_text("owed\n", encoding="utf-8")
    _wire_prepare(monkeypatch, root, current=True)

    calls: list = []

    def fake_call(argv, **kwargs):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(venv_sync.subprocess, "call", fake_call)
    monkeypatch.setattr(
        "pm.environments.activation_environment",
        lambda r: dict(os.environ),
    )

    venv_sync.prepare_launch(root, ["status"])
    assert len(calls) == 1
    assert "--finish-update" in calls[0]
    assert not pending.exists(), "successful finish clears pending"


def test_no_pending_is_noop_for_finish(claim_home, tmp_path, monkeypatch):
    """(c) no pending + current → no finish subprocess (no-op)."""
    _init_bus(claim_home)
    root = _self_checkout(tmp_path)
    _wire_prepare(monkeypatch, root, current=True)

    calls: list = []
    monkeypatch.setattr(
        venv_sync.subprocess,
        "call",
        lambda *a, **k: calls.append(a) or 0,
    )
    result = venv_sync.prepare_launch(root, ["status"])
    assert calls == []
    assert result == Path("/tmp/fake-store-python")
