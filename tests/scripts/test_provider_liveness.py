"""Behavior and executable-fixture tests for profile provider liveness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.provider_liveness import FixtureError, check_fixture, load_fixture


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "provider_liveness"


def _profile(report, name):
    return next(item for item in report["profiles"] if item["profile"] == name)


@pytest.mark.parametrize(
    ("fixture", "status", "profile", "classification", "expected_primary"),
    [
        ("primary_success.json", "green", "builder", "primary_success", "openrouter"),
        ("fallback.json", "non_green", "builder", "fallback", "nous"),
        (
            "unknown_collection.json",
            "non_green",
            "research",
            "unknown_collection",
            "anthropic",
        ),
        (
            "configured_dead.json",
            "non_green",
            "operator",
            "primary_success",
            "openai-codex",
        ),
    ],
)
def test_fixture_contract(fixture, status, profile, classification, expected_primary):
    report = load_fixture(FIXTURES / fixture)

    assert report["status"] == status
    profile_report = _profile(report, profile)
    assert profile_report["expected_primary"] == expected_primary
    assert profile_report["classification"] == classification


def test_inactive_profiles_and_unconfigured_providers_do_not_false_alarm():
    report = load_fixture(FIXTURES / "primary_success.json")

    reviewer = _profile(report, "reviewer")
    builder = _profile(report, "builder")
    unused = next(
        provider
        for provider in builder["providers"]
        if provider["provider"] == "unused-provider"
    )

    assert reviewer["classification"] == "inactive"
    assert reviewer["alarms"] == []
    assert unused["classification"] == "inactive"
    assert report["alarms"] == []


def test_fallback_is_reported_but_dead_primary_still_alarms():
    report = load_fixture(FIXTURES / "fallback.json")
    builder = _profile(report, "builder")

    assert builder["authenticated_provider"] == "openrouter"
    assert builder["classification"] == "fallback"
    assert builder["alarms"] == [
        {
            "code": "configured_route_dead",
            "profile": "builder",
            "provider": "nous",
        }
    ]


def test_dead_configured_fallback_alarms_even_when_primary_succeeds():
    report = load_fixture(FIXTURES / "configured_dead.json")

    assert report["status"] == "non_green"
    assert report["alarms"] == [
        {
            "code": "configured_route_dead",
            "profile": "operator",
            "provider": "openrouter",
        }
    ]


def test_model_listing_and_credential_presence_never_prove_authentication():
    report = load_fixture(FIXTURES / "unknown_collection.json")
    provider = _profile(report, "research")["providers"][0]

    assert provider["classification"] == "unknown_collection"
    assert provider["non_auth_evidence"] == [
        {"probe": "credential_presence", "result": "present"},
        {"probe": "http_model_listing", "result": "success"},
    ]
    assert report["alarms"][0]["code"] == "collection_unknown"


def test_missing_active_profile_config_is_rejected():
    fixture = {
        "schema_version": 1,
        "fixture_id": "invalid",
        "active_profiles": ["missing"],
        "profiles": {"present": {"config": {"model": {"provider": "nous"}}}},
        "observations": [],
    }

    with pytest.raises(FixtureError, match="active profile 'missing' is missing"):
        check_fixture(fixture)


@pytest.mark.parametrize(
    ("fixture", "expected_code"),
    [
        ("primary_success.json", 0),
        ("fallback.json", 1),
        ("unknown_collection.json", 1),
        ("configured_dead.json", 1),
    ],
)
def test_module_cli_executes_fixture_evidence(fixture, expected_code):
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.provider_liveness", str(FIXTURES / fixture)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == expected_code
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["status"] in {"green", "non_green"}
