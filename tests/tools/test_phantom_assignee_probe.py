"""Behavioral tests for the installed phantom-assignee probe."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROBE_PATHS = (
    Path("/home/frank/.hermes/scripts/phantom_assignee_probe.py"),
    Path("/home/frank/.hermes/profiles/jarvis/scripts/phantom_assignee_probe.py"),
)


def load_probe(path: Path):
    spec = importlib.util.spec_from_file_location("phantom_assignee_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", PROBE_PATHS)
def test_probe_uses_canonical_external_seats(monkeypatch, path):
    probe = load_probe(path)
    monkeypatch.setattr(probe, "_canonical_external_assignees", lambda: frozenset({"peer"}))

    assert probe.is_phantom("peer", {"local"}, probe._canonical_external_assignees()) is False
    assert probe.is_phantom("external-unconfigured", {"local"}, probe._canonical_external_assignees()) is True


@pytest.mark.parametrize("path", PROBE_PATHS)
def test_probe_fails_closed_when_registry_unavailable(monkeypatch, path):
    probe = load_probe(path)
    monkeypatch.setattr(probe.importlib, "import_module", lambda name: (_ for _ in ()).throw(ImportError(name)))

    seats = probe._canonical_external_assignees()
    assert seats == frozenset()
    assert probe.is_phantom("peer", {"local"}, seats) is True
    assert probe.is_phantom("local", {"local"}, seats) is False


@pytest.mark.parametrize("path", PROBE_PATHS)
def test_installed_probe_selftest(path):
    assert path.is_file()
    probe = load_probe(path)
    assert probe.selftest() == 0
