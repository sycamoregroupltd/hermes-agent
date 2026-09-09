"""Tests for the shared local/external Kanban assignee admission gate."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr(
        kb, "_canonical_assignee", lambda value: str(value).strip().lower())
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name == "local-worker")
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"external_assignees": ["fable", "external-seat"]}},
    )


def test_external_seat_is_admitted_by_canonical_allowlist(config):
    assert kb.validate_assignee_name(" FABLE ") == "fable"
    assert kb.validate_assignee_name("external-seat") == "external-seat"
    assert kb.validate_assignee_name("local-worker") == "local-worker"


def test_unknown_and_placeholder_names_fail_closed(config):
    with pytest.raises(ValueError, match="not an installed profile"):
        kb.validate_assignee_name("reviewer")
    with pytest.raises(ValueError, match="placeholder"):
        kb.validate_assignee_name("<reviewer>")
    with pytest.raises(ValueError, match="whitespace-only"):
        kb.validate_assignee_name("   ")


def test_malformed_external_allowlist_is_visible(config, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"external_assignees": "fable"}},
    )
    with pytest.raises(ValueError, match="must be a list"):
        kb.validate_assignee_name("fable")
