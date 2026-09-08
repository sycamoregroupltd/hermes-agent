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
        (
            "silent_downgrade.json",
            "non_green",
            "operator",
            "silent_downgrade",
            "openai-codex",
        ),
        (
            "no_active_profiles.json",
            "non_green",
            "dormant",
            "inactive",
            "anthropic",
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


def test_empty_active_profiles_is_valid_and_fails_closed():
    report = load_fixture(FIXTURES / "no_active_profiles.json")

    assert report["status"] == "non_green"
    assert report["alarms"] == [{"code": "no_active_profiles"}]


def test_silent_downgrade_identifies_the_served_provider_and_alarms():
    report = load_fixture(FIXTURES / "silent_downgrade.json")
    operator = _profile(report, "operator")

    assert operator["classification"] == "silent_downgrade"
    assert operator["authenticated_provider"] == "openrouter"
    assert operator["providers"] == [
        {
            "provider": "openai-codex",
            "role": "primary",
            "classification": "silent_downgrade",
            "served_provider": "openrouter",
        }
    ]
    assert report["alarms"] == [
        {
            "code": "route_served_by_other_provider",
            "profile": "operator",
            "provider": "openai-codex",
            "served_provider": "openrouter",
        }
    ]


def test_success_without_served_provider_fails_closed_as_unknown_collection():
    fixture = json.loads(
        (FIXTURES / "primary_success.json").read_text(encoding="utf-8")
    )
    fixture["observations"][0].pop("served_provider")

    report = check_fixture(fixture)
    builder = _profile(report, "builder")

    assert report["status"] == "non_green"
    assert builder["classification"] == "unknown_collection"
    assert builder["authenticated_provider"] is None
    assert builder["providers"][0]["classification"] == "unknown_collection"
    assert report["alarms"] == [
        {
            "code": "collection_unknown",
            "profile": "builder",
            "provider": "openrouter",
        }
    ]


@pytest.mark.parametrize(
    ("result", "classification", "alarm"),
    [
        ("failure", "configured_dead", "configured_route_dead"),
        ("unknown", "unknown_collection", "collection_unknown"),
    ],
)
def test_non_success_observations_do_not_require_a_served_provider(
    result, classification, alarm
):
    fixture = json.loads(
        (FIXTURES / "primary_success.json").read_text(encoding="utf-8")
    )
    fixture["observations"][0] = {
        "probe": "authenticated_inference",
        "profile": "builder",
        "provider": "openrouter",
        "result": result,
    }

    report = check_fixture(fixture)
    builder = _profile(report, "builder")

    assert builder["classification"] == classification
    assert "served_provider" not in builder["providers"][0]
    assert report["alarms"][0]["code"] == alarm


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
        ("silent_downgrade.json", 1),
        ("no_active_profiles.json", 1),
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
