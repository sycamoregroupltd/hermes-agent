"""Fixture-driven, profile-aware provider liveness classification.

This module deliberately consumes exported JSON only.  It does not read Hermes
configuration, credentials, or provider endpoints, so it is safe to use for
offline evidence review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
AUTH_PROBE = "authenticated_inference"
NON_AUTH_PROBES = frozenset({"credential_presence", "http_model_listing"})
KNOWN_PROBES = NON_AUTH_PROBES | {AUTH_PROBE}


class FixtureError(ValueError):
    """Raised when a liveness fixture does not meet the input contract."""


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FixtureError(f"{path} must be an object")
    return value


def _name(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureError(f"{path} must be a non-empty string")
    return value.strip().lower()


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise FixtureError(f"{path} must be an array")
    result = [_name(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise FixtureError(f"{path} contains duplicates")
    return result


def _configured_routes(profile: str, raw_profile: Any) -> tuple[str, list[str]]:
    profile_data = _object(raw_profile, f"profiles.{profile}")
    config = _object(profile_data.get("config"), f"profiles.{profile}.config")
    model = _object(config.get("model"), f"profiles.{profile}.config.model")
    primary = _name(
        model.get("provider"), f"profiles.{profile}.config.model.provider"
    )

    raw_fallbacks = config.get("fallback_providers", [])
    if not isinstance(raw_fallbacks, list):
        raise FixtureError(
            f"profiles.{profile}.config.fallback_providers must be an array"
        )
    fallbacks: list[str] = []
    for index, raw_fallback in enumerate(raw_fallbacks):
        fallback = _object(
            raw_fallback,
            f"profiles.{profile}.config.fallback_providers[{index}]",
        )
        provider = _name(
            fallback.get("provider"),
            f"profiles.{profile}.config.fallback_providers[{index}].provider",
        )
        if provider != primary and provider not in fallbacks:
            fallbacks.append(provider)
    return primary, fallbacks


def _observations(
    raw: Any, profiles: set[str]
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], list[dict[str, str]]]]:
    if not isinstance(raw, list):
        raise FixtureError("observations must be an array")

    authenticated: dict[tuple[str, str], str] = {}
    supporting: dict[tuple[str, str], list[dict[str, str]]] = {}
    for index, raw_observation in enumerate(raw):
        path = f"observations[{index}]"
        observation = _object(raw_observation, path)
        profile = _name(observation.get("profile"), f"{path}.profile")
        if profile not in profiles:
            raise FixtureError(f"{path}.profile references unknown profile {profile!r}")
        provider = _name(observation.get("provider"), f"{path}.provider")
        probe = _name(observation.get("probe"), f"{path}.probe")
        result = _name(observation.get("result"), f"{path}.result")
        if probe not in KNOWN_PROBES:
            raise FixtureError(f"{path}.probe has unsupported value {probe!r}")

        route = (profile, provider)
        if probe == AUTH_PROBE:
            if result not in {"success", "failure", "unknown"}:
                raise FixtureError(
                    f"{path}.result must be success, failure, or unknown for {AUTH_PROBE}"
                )
            if route in authenticated:
                raise FixtureError(
                    f"duplicate {AUTH_PROBE} observation for {profile}/{provider}"
                )
            authenticated[route] = result
        else:
            supporting.setdefault(route, []).append(
                {"probe": probe, "result": result}
            )
    return authenticated, supporting


def check_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    """Classify provider liveness from one validated JSON fixture.

    Only ``authenticated_inference`` with ``result=success`` proves that a
    route authenticated.  Model-list HTTP responses and credential presence
    are retained as non-auth evidence but never influence health.
    """

    fixture = _object(fixture, "fixture")
    if fixture.get("schema_version") != SCHEMA_VERSION:
        raise FixtureError(f"schema_version must be {SCHEMA_VERSION}")
    fixture_id = _name(fixture.get("fixture_id"), "fixture_id")
    profiles_data = _object(fixture.get("profiles"), "profiles")
    if not profiles_data:
        raise FixtureError("profiles must not be empty")

    normalized_profiles: dict[str, Any] = {}
    for raw_name, profile_data in profiles_data.items():
        name = _name(raw_name, "profiles key")
        if name in normalized_profiles:
            raise FixtureError(f"duplicate normalized profile name {name!r}")
        normalized_profiles[name] = profile_data

    active_profiles = _string_list(fixture.get("active_profiles"), "active_profiles")
    for profile in active_profiles:
        if profile not in normalized_profiles:
            raise FixtureError(f"active profile {profile!r} is missing from profiles")
    active = set(active_profiles)

    routes = {
        profile: _configured_routes(profile, profile_data)
        for profile, profile_data in normalized_profiles.items()
    }
    authenticated, supporting = _observations(
        fixture.get("observations"), set(normalized_profiles)
    )

    reports: list[dict[str, Any]] = []
    all_alarms: list[dict[str, str]] = []
    for profile in sorted(normalized_profiles):
        primary, fallbacks = routes[profile]
        configured = [primary, *fallbacks]
        provider_reports: list[dict[str, Any]] = []
        profile_alarms: list[dict[str, str]] = []

        if profile not in active:
            for provider in configured:
                provider_reports.append(
                    {
                        "provider": provider,
                        "role": "primary" if provider == primary else "fallback",
                        "classification": "inactive",
                    }
                )
            classification = "inactive"
            authenticated_provider = None
        else:
            primary_result = authenticated.get((profile, primary))
            fallback_successes: list[str] = []
            for provider in configured:
                role = "primary" if provider == primary else "fallback"
                result = authenticated.get((profile, provider))
                non_auth = supporting.get((profile, provider), [])
                if result == "success":
                    route_classification = (
                        "primary_success" if role == "primary" else "fallback"
                    )
                    if role == "fallback":
                        fallback_successes.append(provider)
                elif result == "failure":
                    route_classification = "configured_dead"
                    profile_alarms.append(
                        {
                            "code": "configured_route_dead",
                            "profile": profile,
                            "provider": provider,
                        }
                    )
                else:
                    route_classification = "unknown_collection"
                    profile_alarms.append(
                        {
                            "code": "collection_unknown",
                            "profile": profile,
                            "provider": provider,
                        }
                    )

                provider_report: dict[str, Any] = {
                    "provider": provider,
                    "role": role,
                    "classification": route_classification,
                }
                if non_auth:
                    provider_report["non_auth_evidence"] = non_auth
                provider_reports.append(provider_report)

            if primary_result == "success":
                classification = "primary_success"
                authenticated_provider = primary
            elif fallback_successes:
                classification = "fallback"
                authenticated_provider = fallback_successes[0]
            elif all(authenticated.get((profile, provider)) == "failure" for provider in configured):
                classification = "configured_dead"
                authenticated_provider = None
            else:
                classification = "unknown_collection"
                authenticated_provider = None

        configured_set = set(configured)
        observed_providers = {
            provider
            for observed_profile, provider in set(authenticated) | set(supporting)
            if observed_profile == profile and provider not in configured_set
        }
        for provider in sorted(observed_providers):
            provider_report = {
                "provider": provider,
                "role": "unconfigured",
                "classification": "inactive",
            }
            non_auth = supporting.get((profile, provider), [])
            if non_auth:
                provider_report["non_auth_evidence"] = non_auth
            provider_reports.append(provider_report)

        reports.append(
            {
                "profile": profile,
                "active": profile in active,
                "expected_primary": primary,
                "classification": classification,
                "authenticated_provider": authenticated_provider,
                "providers": provider_reports,
                "alarms": profile_alarms,
            }
        )
        all_alarms.extend(profile_alarms)

    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_id": fixture_id,
        "status": "green" if not all_alarms else "non_green",
        "profiles": reports,
        "alarms": all_alarms,
    }


def load_fixture(path: str | Path) -> dict[str, Any]:
    """Load and classify a UTF-8 JSON fixture from ``path``."""

    fixture_path = Path(path)
    try:
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot load fixture {fixture_path}: {exc}") from exc
    return check_fixture(raw)


def exit_code(report: dict[str, Any]) -> int:
    """Return 0 for green evidence and 1 for any non-green classification."""

    return 0 if report.get("status") == "green" else 1
