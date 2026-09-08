"""Executable fixtures for lease-aware session-bus delivery resolution."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.session_bus_delivery import FixtureError, load_fixture, resolve_fixture


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "session_bus_delivery"


def _raw(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _only_recipient(report: dict) -> dict:
    assert len(report["recipients"]) == 1
    return report["recipients"][0]


def test_valid_fresh_process_bound_canary_resolves() -> None:
    report = load_fixture(FIXTURES / "valid_fresh_process_bound.json")
    recipient = _only_recipient(report)

    assert report["status"] == "green"
    assert report["available_recipients"] == ["builder-a"]
    assert recipient == {
        "recipient_id": "builder-a",
        "session_id": "session-builder-a",
        "request_id": "request-current",
        "message_id": "canary-current",
        "sent": True,
        "received": True,
        "owned": True,
        "acknowledged": True,
        "ignored_ack_count": 0,
        "available": True,
        "reasons": [],
    }


def test_stale_recipient_is_rejected_despite_complete_exchange() -> None:
    recipient = _only_recipient(load_fixture(FIXTURES / "stale_recipient.json"))

    assert recipient["sent"] is True
    assert recipient["received"] is True
    assert recipient["owned"] is True
    assert recipient["acknowledged"] is True
    assert recipient["available"] is False
    assert "recipient_stale" in recipient["reasons"]


def test_historical_ack_backlog_does_not_count_for_current_request() -> None:
    recipient = _only_recipient(load_fixture(FIXTURES / "historical_ack_backlog.json"))

    assert recipient["sent"] is True
    assert recipient["received"] is True
    assert recipient["owned"] is True
    assert recipient["acknowledged"] is False
    assert recipient["ignored_ack_count"] == 1
    assert recipient["reasons"] == ["ack_missing"]


def test_received_is_not_owned_and_ack_does_not_promote_it() -> None:
    recipient = _only_recipient(load_fixture(FIXTURES / "received_not_owned.json"))

    assert recipient["sent"] is True
    assert recipient["received"] is True
    assert recipient["owned"] is False
    assert recipient["acknowledged"] is True
    assert recipient["available"] is False
    assert recipient["reasons"] == ["owned_missing"]


def test_closed_and_unowned_recipients_are_rejected() -> None:
    report = load_fixture(FIXTURES / "rejected_recipients.json")
    recipients = {item["recipient_id"]: item for item in report["recipients"]}

    assert report["available_recipients"] == []
    assert "recipient_closed" in recipients["builder-closed"]["reasons"]
    assert "lease_unowned" in recipients["builder-unowned"]["reasons"]


def test_ack_requires_exact_request_and_message_correlation() -> None:
    fixture = _raw("valid_fresh_process_bound.json")
    ack = next(event for event in fixture["events"] if event["kind"] == "ack")
    ack["correlates_to"] = "different-canary"

    recipient = _only_recipient(resolve_fixture(fixture))

    assert recipient["acknowledged"] is False
    assert recipient["ignored_ack_count"] == 1
    assert recipient["reasons"] == ["ack_uncorrelated"]


def test_exact_ack_after_request_deadline_is_rejected() -> None:
    fixture = _raw("valid_fresh_process_bound.json")
    ack = next(event for event in fixture["events"] if event["kind"] == "ack")
    ack["at"] = 1000

    recipient = _only_recipient(resolve_fixture(fixture))

    assert recipient["acknowledged"] is False
    assert recipient["reasons"] == ["ack_deadline_missed"]


def test_ack_at_request_deadline_is_accepted() -> None:
    fixture = _raw("valid_fresh_process_bound.json")
    ack = next(event for event in fixture["events"] if event["kind"] == "ack")
    ack["at"] = fixture["probes"][0]["deadline_at"]
    ack["process"]["observed_at"] = ack["at"]

    recipient = _only_recipient(resolve_fixture(fixture))

    assert recipient["acknowledged"] is True
    assert recipient["available"] is True
    assert recipient["reasons"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lease_id", "other-lease"),
        ("pid", 7777),
        ("started_at", 799),
        ("state", "unknown"),
    ],
)
def test_ack_must_be_bound_to_exact_lease_and_process_instance(
    field: str, value: str | int
) -> None:
    fixture = _raw("valid_fresh_process_bound.json")
    ack = next(event for event in fixture["events"] if event["kind"] == "ack")
    if field == "lease_id":
        ack[field] = value
    else:
        ack["process"][field] = value

    recipient = _only_recipient(resolve_fixture(fixture))

    assert recipient["sent"] is True
    assert recipient["received"] is True
    assert recipient["owned"] is True
    assert recipient["acknowledged"] is False
    assert recipient["available"] is False
    assert recipient["reasons"] == ["ack_unbound"]


def test_lease_expiry_is_inclusive() -> None:
    fixture = _raw("valid_fresh_process_bound.json")
    fixture["recipients"][0]["lease"]["expires_at"] = fixture["observed_at"]

    recipient = _only_recipient(resolve_fixture(fixture))

    assert recipient["available"] is True
    assert "lease_inactive" not in recipient["reasons"]


def test_lease_is_inactive_immediately_after_expiry() -> None:
    fixture = _raw("valid_fresh_process_bound.json")
    fixture["recipients"][0]["lease"]["expires_at"] = fixture["observed_at"] - 0.1

    recipient = _only_recipient(resolve_fixture(fixture))

    assert recipient["available"] is False
    assert recipient["reasons"] == ["lease_inactive"]


def test_historical_request_cannot_establish_current_availability() -> None:
    fixture = copy.deepcopy(_raw("valid_fresh_process_bound.json"))
    fixture["observed_at"] = 1010

    recipient = _only_recipient(resolve_fixture(fixture))

    assert recipient["acknowledged"] is True
    assert recipient["available"] is False
    assert "request_stale" in recipient["reasons"]


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("process_not_running", "process_not_running"),
        ("lease_released", "lease_inactive"),
        ("lease_not_yet_acquired", "lease_not_yet_acquired"),
        ("sent_missing", "sent_missing"),
        ("received_missing", "received_missing"),
    ],
)
def test_remaining_unavailability_reason_codes(
    mutation: str, expected_reason: str
) -> None:
    fixture = _raw("valid_fresh_process_bound.json")
    if mutation == "process_not_running":
        fixture["recipients"][0]["process"]["state"] = "stopped"
    elif mutation == "lease_released":
        fixture["recipients"][0]["lease"]["released"] = True
    elif mutation == "lease_not_yet_acquired":
        fixture["recipients"][0]["lease"]["acquired_at"] = fixture["observed_at"] + 1
    elif mutation == "sent_missing":
        fixture["events"] = [
            event for event in fixture["events"] if event["kind"] != "sent"
        ]
    else:
        fixture["events"] = [
            event for event in fixture["events"] if event["kind"] != "received"
        ]

    recipient = _only_recipient(resolve_fixture(fixture))

    assert recipient["available"] is False
    assert expected_reason in recipient["reasons"]


def test_duplicate_lease_claimants_fail_safe() -> None:
    with pytest.raises(
        FixtureError, match=r"recipients\[1\]\.lease\.lease_id duplicates"
    ):
        load_fixture(FIXTURES / "duplicate_lease_claimants.json")


def test_malformed_or_unknown_input_fails_safe() -> None:
    with pytest.raises(FixtureError, match="unsupported value 'delivered-ish'"):
        load_fixture(FIXTURES / "malformed_unknown.json")

    fixture = _raw("valid_fresh_process_bound.json")
    fixture["probes"][0]["recipient_id"] = "unknown-worker"
    with pytest.raises(FixtureError, match="references unknown recipient"):
        resolve_fixture(fixture)

    fixture = _raw("valid_fresh_process_bound.json")
    fixture["observed_at"] = float("nan")
    with pytest.raises(FixtureError, match="observed_at must be finite"):
        resolve_fixture(fixture)


def test_undecodable_utf8_uses_malformed_input_envelope(tmp_path: Path) -> None:
    fixture_path = tmp_path / "undecodable.json"
    fixture_path.write_bytes(b'{"fixture_id": "\xff"}')

    with pytest.raises(FixtureError, match="cannot load fixture"):
        load_fixture(fixture_path)

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.session_bus_delivery", str(fixture_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert json.loads(completed.stderr)["status"] == "invalid"


@pytest.mark.parametrize(
    ("fixture", "expected_code", "stream", "status"),
    [
        ("valid_fresh_process_bound.json", 0, "stdout", "green"),
        ("stale_recipient.json", 1, "stdout", "unavailable"),
        ("historical_ack_backlog.json", 1, "stdout", "unavailable"),
        ("received_not_owned.json", 1, "stdout", "unavailable"),
        ("malformed_unknown.json", 2, "stderr", "invalid"),
        ("duplicate_lease_claimants.json", 2, "stderr", "invalid"),
    ],
)
def test_module_cli_executes_accounting_fixtures(
    fixture: str, expected_code: int, stream: str, status: str
) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.session_bus_delivery", str(FIXTURES / fixture)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == expected_code
    output = completed.stdout if stream == "stdout" else completed.stderr
    other = completed.stderr if stream == "stdout" else completed.stdout
    assert other == ""
    assert json.loads(output)["status"] == status
