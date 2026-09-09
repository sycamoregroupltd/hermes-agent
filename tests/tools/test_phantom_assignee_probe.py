"""Behavioral tests for the tracked phantom-assignee probe."""
from __future__ import annotations

import importlib.util
import sqlite3
import types
from pathlib import Path

import pytest


PROBE_PATH = Path(__file__).parents[2] / "scripts" / "phantom_assignee_probe.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("phantom_assignee_probe", PROBE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_board(tmp_path: Path, assignees: list[str]) -> Path:
    board = tmp_path / "boards" / "test"
    board.mkdir(parents=True)
    db = board / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, "
        "status TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?, 'ready', '2026-09-01')",
        [(f"t_{i}", "test", assignee) for i, assignee in enumerate(assignees)],
    )
    conn.commit()
    conn.close()
    return tmp_path / "boards"


def test_probe_uses_canonical_external_seats_through_scan(monkeypatch, tmp_path):
    probe = load_probe()
    canonical = types.SimpleNamespace(external_assignees=lambda: frozenset({"peer"}))
    monkeypatch.setattr(probe.importlib, "import_module", lambda name: canonical)

    hits = probe.probe(make_board(tmp_path, ["peer", "unknown"]), tmp_path / "profiles")

    assert [hit["assignee"] for hit in hits] == ["unknown"]


def test_probe_fails_closed_when_registry_unavailable_through_scan(monkeypatch, tmp_path):
    probe = load_probe()
    monkeypatch.setattr(
        probe.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError(name)),
    )

    hits = probe.probe(make_board(tmp_path, ["peer"]), tmp_path / "profiles")

    assert [hit["assignee"] for hit in hits] == ["peer"]


def test_probe_preserves_local_profile_admission(tmp_path):
    probe = load_probe()
    profiles = tmp_path / "profiles"
    (profiles / "local").mkdir(parents=True)

    hits = probe.probe(make_board(tmp_path, ["local", "unknown"]), profiles, frozenset())

    assert [hit["assignee"] for hit in hits] == ["unknown"]


def test_tracked_probe_selftest():
    assert PROBE_PATH.is_file()
    assert load_probe().selftest() == 0
