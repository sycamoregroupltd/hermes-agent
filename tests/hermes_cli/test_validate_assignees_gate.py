"""Verification tests for the PM decomposition non-spawnable gate (t_680e72d2).

Covers rule (3) from the card acceptance: PM decomposition defaults exclude
non-spawnable assignees. The gate lives in
``skills/kanban-orchestrator/scripts/validate_assignees.py`` (t_40d8eaca).

Acceptance:
  (3a) NON_SPAWNABLE assignees are rejected (exit 1; names on stderr)
  (3b) real worker + terminal-lane assignees are accepted (exit 0)
  (3c) JSON decomposition structures extract nested assignees
  (3d) catalog drift WARN when catalog readable and a name is missing
  (3e) case-insensitive rejection
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Canonical script location (fleet skill, t_40d8eaca artifact).
SCRIPT = Path(
    "/home/frank/.hermes/skills/kanban-orchestrator/scripts/validate_assignees.py"
)


@pytest.fixture(scope="module")
def mod():
    """Load validate_assignees.py as a module via importlib."""
    assert SCRIPT.is_file(), f"script missing: {SCRIPT}"
    spec = importlib.util.spec_from_file_location("validate_assignees", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


NON_SPAWNABLE = {
    "workforce-scaler",
    "nim-deepseek",
    "nim-gemini3",
    "nim-glm52",
    "nim-qwen35",
}


def test_non_spawnable_constant_covers_catalog_reserved_set(mod):
    assert NON_SPAWNABLE <= {x.lower() for x in mod.NON_SPAWNABLE}


@pytest.mark.parametrize(
    "name",
    ["workforce-scaler", "nim-deepseek", "nim-gemini3", "nim-glm52", "nim-qwen35",
     "WORKFORCE-SCALER", "Nim-DeepSeek"],
)
def test_is_non_spawnable_case_insensitive(mod, name):
    assert mod._norm(name) in {mod._norm(n) for n in mod.NON_SPAWNABLE}


@pytest.mark.parametrize(
    "name", ["builder", "os-reviewer", "fable", "codex", "grok", "orion-cc"]
)
def test_workers_and_terminal_lanes_not_non_spawnable(mod, name):
    assert mod._norm(name) not in {mod._norm(n) for n in mod.NON_SPAWNABLE}


def test_validate_assignees_rejects_and_keeps_terminal_notes(mod):
    rejected, terminal = mod.validate_assignees(
        ["workforce-scaler", "builder", "fable", "nim-qwen35"]
    )
    assert {mod._norm(x) for x in rejected} >= {"workforce-scaler", "nim-qwen35"}
    assert "fable" in [mod._norm(t) for t in terminal]


def test_cli_assignees_reject_exit_code():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--assignees", "workforce-scaler", "builder"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "workforce-scaler" in proc.stderr


def test_cli_assignees_ok_exit_code():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--assignees", "builder", "os-reviewer", "fable"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "OK" in proc.stdout


def test_cli_json_decomposition_nested_assignees(tmp_path, monkeypatch):
    payload = {
        "tasks": [
            {"title": "A", "assignee": "builder"},
            {"title": "B", "assignee": "nim-deepseek"},
            {"children": [{"assignee": "workforce-scaler"}]},
        ]
    }
    p = tmp_path / "decomp.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    # Point the drift guard at an unreadable catalog so it never emits a
    # spurious WARN that changes nothing but keeps stderr clean.
    monkeypatch.setenv("PROFILE_CATALOG_PATH", str(tmp_path / "no-catalog.md"))
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", str(p)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "nim-deepseek" in proc.stderr
    assert "workforce-scaler" in proc.stderr


def test_extract_assignees_walks_nested_structures(mod):
    data = {"assignee": "a", "kids": [{"assignee": "b"}, {"x": {"assignee": "c"}}]}
    out: list[str] = []
    mod._assignees_from_decomposition(data, out)
    assert out == ["a", "b", "c"]


def test_catalog_drift_warns_when_name_absent(tmp_path, monkeypatch):
    catalog = tmp_path / "cat.md"
    catalog.write_text(
        "| workforce-scaler | x | y |\n| builder | x | y |\n", encoding="utf-8"
    )
    monkeypatch.setenv("PROFILE_CATALOG_PATH", str(catalog))
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--assignees", "builder"],
        capture_output=True, text=True,
    )
    # builder is fine (exit 0) and catalog is readable/non-empty, so any
    # NON_SPAWNABLE name absent from the catalog must WARN.
    assert proc.returncode == 0
    assert "WARN" in proc.stderr
