"""Read-only, fixture-driven session-bus delivery resolution.

The resolver consumes an exported JSON snapshot.  It deliberately does not
open Hermes state, inspect the local process table, or contact a gateway.  The
snapshot contract carries the two process-identity fields Hermes already uses
for durable ownership (PID plus process start time), and lease release is
identity checked in the same spirit as the gateway turn lease.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EVENT_KINDS = frozenset({"sent", "received", "owned", "ack"})
PROCESS_STATES = frozenset({"running", "stopped", "unknown"})
RECIPIENT_STATES = frozenset({"open", "closed"})


class FixtureError(ValueError):
    """Raised when a delivery snapshot cannot be interpreted safely."""


def _object(
    value: Any,
    path: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FixtureError(f"{path} must be an object")
    optional = optional or set()
    missing = required - set(value)
    if missing:
        raise FixtureError(f"{path} missing field(s): {', '.join(sorted(missing))}")
    unknown = set(value) - required - optional
    if unknown:
        raise FixtureError(f"{path} has unknown field(s): {', '.join(sorted(unknown))}")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise FixtureError(f"{path} must be an array")
    return value


def _name(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureError(f"{path} must be a non-empty string")
    return value.strip()


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixtureError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise FixtureError(f"{path} must be finite")
    return result


def _positive_number(value: Any, path: str) -> float:
    result = _number(value, path)
    if result <= 0:
        raise FixtureError(f"{path} must be greater than zero")
    return result


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FixtureError(f"{path} must be a positive integer")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise FixtureError(f"{path} must be a boolean")
    return value


def _process(value: Any, path: str) -> dict[str, Any]:
    raw = _object(
        value,
        path,
        required={"pid", "started_at", "state", "observed_at"},
    )
    state = _name(raw["state"], f"{path}.state")
    if state not in PROCESS_STATES:
        raise FixtureError(f"{path}.state has unsupported value {state!r}")
    return {
        "pid": _positive_int(raw["pid"], f"{path}.pid"),
        "started_at": _number(raw["started_at"], f"{path}.started_at"),
        "state": state,
        "observed_at": _number(raw["observed_at"], f"{path}.observed_at"),
    }


def _recipient(value: Any, path: str) -> dict[str, Any]:
    raw = _object(
        value,
        path,
        required={
            "recipient_id",
            "session_id",
            "state",
            "last_seen_at",
            "process",
            "lease",
        },
    )
    recipient_id = _name(raw["recipient_id"], f"{path}.recipient_id")
    state = _name(raw["state"], f"{path}.state")
    if state not in RECIPIENT_STATES:
        raise FixtureError(f"{path}.state has unsupported value {state!r}")
    process = _process(raw["process"], f"{path}.process")
    lease_raw = _object(
        raw["lease"],
        f"{path}.lease",
        required={
            "lease_id",
            "owner_recipient_id",
            "owner_pid",
            "owner_started_at",
            "acquired_at",
            "expires_at",
            "released",
        },
    )
    lease = {
        "lease_id": _name(lease_raw["lease_id"], f"{path}.lease.lease_id"),
        "owner_recipient_id": _name(
            lease_raw["owner_recipient_id"],
            f"{path}.lease.owner_recipient_id",
        ),
        "owner_pid": _positive_int(lease_raw["owner_pid"], f"{path}.lease.owner_pid"),
        "owner_started_at": _number(
            lease_raw["owner_started_at"], f"{path}.lease.owner_started_at"
        ),
        "acquired_at": _number(lease_raw["acquired_at"], f"{path}.lease.acquired_at"),
        "expires_at": _number(lease_raw["expires_at"], f"{path}.lease.expires_at"),
        "released": _boolean(lease_raw["released"], f"{path}.lease.released"),
    }
    return {
        "recipient_id": recipient_id,
        "session_id": _name(raw["session_id"], f"{path}.session_id"),
        "state": state,
        "last_seen_at": _number(raw["last_seen_at"], f"{path}.last_seen_at"),
        "process": process,
        "lease": lease,
    }


def _probe(value: Any, path: str) -> dict[str, Any]:
    raw = _object(
        value,
        path,
        required={
            "request_id",
            "message_id",
            "sender_id",
            "recipient_id",
            "sent_at",
            "deadline_at",
        },
    )
    sent_at = _number(raw["sent_at"], f"{path}.sent_at")
    deadline_at = _number(raw["deadline_at"], f"{path}.deadline_at")
    if deadline_at < sent_at:
        raise FixtureError(f"{path}.deadline_at must not precede sent_at")
    return {
        "request_id": _name(raw["request_id"], f"{path}.request_id"),
        "message_id": _name(raw["message_id"], f"{path}.message_id"),
        "sender_id": _name(raw["sender_id"], f"{path}.sender_id"),
        "recipient_id": _name(raw["recipient_id"], f"{path}.recipient_id"),
        "sent_at": sent_at,
        "deadline_at": deadline_at,
    }


def _event(value: Any, path: str) -> dict[str, Any]:
    common = {"event_id", "kind", "request_id", "message_id", "from_id", "to_id", "at"}
    if not isinstance(value, dict):
        raise FixtureError(f"{path} must be an object")
    kind = _name(value.get("kind"), f"{path}.kind")
    if kind not in EVENT_KINDS:
        raise FixtureError(f"{path}.kind has unsupported value {kind!r}")
    extra = {
        "received": {"process"},
        "owned": {"process", "lease_id"},
        "ack": {"process", "lease_id", "correlates_to"},
    }.get(kind, set())
    raw = _object(value, path, required=common | extra)
    event = {
        "event_id": _name(raw["event_id"], f"{path}.event_id"),
        "kind": kind,
        "request_id": _name(raw["request_id"], f"{path}.request_id"),
        "message_id": _name(raw["message_id"], f"{path}.message_id"),
        "from_id": _name(raw["from_id"], f"{path}.from_id"),
        "to_id": _name(raw["to_id"], f"{path}.to_id"),
        "at": _number(raw["at"], f"{path}.at"),
    }
    if "process" in extra:
        event["process"] = _process(raw["process"], f"{path}.process")
    if "lease_id" in extra:
        event["lease_id"] = _name(raw["lease_id"], f"{path}.lease_id")
    if "correlates_to" in extra:
        event["correlates_to"] = _name(raw["correlates_to"], f"{path}.correlates_to")
    return event


def _unique(items: list[dict[str, Any]], key: str, path: str) -> None:
    seen: set[str] = set()
    for index, item in enumerate(items):
        value = item[key]
        if value in seen:
            raise FixtureError(f"{path}[{index}].{key} duplicates {value!r}")
        seen.add(value)


def _same_process(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left["pid"] == right["pid"] and left["started_at"] == right["started_at"]


def _event_process_matches(
    event: dict[str, Any],
    recipient_process: dict[str, Any],
    *,
    freshness: float,
) -> bool:
    event_process = event["process"]
    observation_age = event["at"] - event_process["observed_at"]
    return (
        _same_process(event_process, recipient_process)
        and event_process["state"] == "running"
        and 0 <= observation_age <= freshness
    )


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _base_reasons(
    recipient: dict[str, Any],
    *,
    observed_at: float,
    recipient_freshness: float,
) -> list[str]:
    reasons: list[str] = []
    process = recipient["process"]
    lease = recipient["lease"]

    if recipient["state"] != "open":
        _add_reason(reasons, "recipient_closed")
    age = observed_at - recipient["last_seen_at"]
    process_age = observed_at - process["observed_at"]
    if (
        age < 0
        or age > recipient_freshness
        or process_age < 0
        or process_age > recipient_freshness
    ):
        _add_reason(reasons, "recipient_stale")
    if process["state"] != "running":
        _add_reason(reasons, "process_not_running")
    if lease["released"] or observed_at > lease["expires_at"]:
        _add_reason(reasons, "lease_inactive")
    if lease["acquired_at"] > observed_at:
        _add_reason(reasons, "lease_not_yet_acquired")
    if (
        lease["owner_recipient_id"] != recipient["recipient_id"]
        or lease["owner_pid"] != process["pid"]
        or lease["owner_started_at"] != process["started_at"]
    ):
        _add_reason(reasons, "lease_unowned")
    return reasons


def _resolve_probe(
    probe: dict[str, Any],
    recipient: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    observed_at: float,
    recipient_freshness: float,
    request_freshness: float,
) -> dict[str, Any]:
    reasons = _base_reasons(
        recipient,
        observed_at=observed_at,
        recipient_freshness=recipient_freshness,
    )
    process = recipient["process"]
    lease = recipient["lease"]

    request_age = observed_at - probe["sent_at"]
    if request_age < 0 or request_age > request_freshness:
        _add_reason(reasons, "request_stale")

    sent_events = [
        event
        for event in events
        if event["kind"] == "sent"
        and event["request_id"] == probe["request_id"]
        and event["message_id"] == probe["message_id"]
        and event["from_id"] == probe["sender_id"]
        and event["to_id"] == probe["recipient_id"]
        and event["at"] == probe["sent_at"]
    ]
    sent = bool(sent_events)
    if not sent:
        _add_reason(reasons, "sent_missing")

    received_events = [
        event
        for event in events
        if event["kind"] == "received"
        and event["request_id"] == probe["request_id"]
        and event["message_id"] == probe["message_id"]
        and event["from_id"] == probe["sender_id"]
        and event["to_id"] == probe["recipient_id"]
        and _event_process_matches(event, process, freshness=recipient_freshness)
        and probe["sent_at"] <= event["at"] <= observed_at
    ]
    received = sent and bool(received_events)
    if not received:
        _add_reason(reasons, "received_missing")
    received_at = min((event["at"] for event in received_events), default=None)

    owned_events = [
        event
        for event in events
        if event["kind"] == "owned"
        and event["request_id"] == probe["request_id"]
        and event["message_id"] == probe["message_id"]
        and event["from_id"] == probe["sender_id"]
        and event["to_id"] == probe["recipient_id"]
        and event["lease_id"] == lease["lease_id"]
        and _event_process_matches(event, process, freshness=recipient_freshness)
        and received_at is not None
        and max(received_at, lease["acquired_at"]) <= event["at"] <= observed_at
    ]
    owned = received and bool(owned_events)
    if not owned:
        _add_reason(reasons, "owned_missing")

    relevant_acks = [
        event
        for event in events
        if event["kind"] == "ack"
        and event["from_id"] == probe["recipient_id"]
        and event["to_id"] == probe["sender_id"]
        and probe["sent_at"] <= event["at"] <= observed_at
    ]
    correlated_acks = [
        event
        for event in relevant_acks
        if event["request_id"] == probe["request_id"]
        and event["correlates_to"] == probe["message_id"]
    ]
    bound_acks = [
        event
        for event in correlated_acks
        if event["lease_id"] == lease["lease_id"]
        and _event_process_matches(event, process, freshness=recipient_freshness)
        and event["at"] >= lease["acquired_at"]
    ]
    timely_acks = [
        event
        for event in bound_acks
        if received_at is not None
        and received_at <= event["at"] <= probe["deadline_at"]
    ]
    acknowledged = received and bool(timely_acks)
    if not acknowledged:
        if bound_acks or (not relevant_acks and observed_at > probe["deadline_at"]):
            _add_reason(reasons, "ack_deadline_missed")
        elif correlated_acks:
            _add_reason(reasons, "ack_unbound")
        elif relevant_acks:
            _add_reason(reasons, "ack_uncorrelated")
        else:
            _add_reason(reasons, "ack_missing")

    ignored_ack_count = sum(
        1
        for event in events
        if event["kind"] == "ack"
        and event["from_id"] == probe["recipient_id"]
        and event["to_id"] == probe["sender_id"]
        and event not in bound_acks
    )
    available = not reasons
    return {
        "recipient_id": recipient["recipient_id"],
        "session_id": recipient["session_id"],
        "request_id": probe["request_id"],
        "message_id": probe["message_id"],
        "sent": sent,
        "received": received,
        "owned": owned,
        "acknowledged": acknowledged,
        "ignored_ack_count": ignored_ack_count,
        "available": available,
        "reasons": reasons,
    }


def resolve_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    """Validate and resolve fresh process-bound recipients from a snapshot."""

    raw = _object(
        fixture,
        "fixture",
        required={
            "schema_version",
            "fixture_id",
            "observed_at",
            "freshness",
            "recipients",
            "probes",
            "events",
        },
    )
    if (
        isinstance(raw["schema_version"], bool)
        or raw["schema_version"] != SCHEMA_VERSION
    ):
        raise FixtureError(f"schema_version must be {SCHEMA_VERSION}")
    fixture_id = _name(raw["fixture_id"], "fixture_id")
    observed_at = _number(raw["observed_at"], "observed_at")
    freshness_raw = _object(
        raw["freshness"],
        "freshness",
        required={"recipient_seconds", "request_seconds"},
    )
    recipient_freshness = _positive_number(
        freshness_raw["recipient_seconds"], "freshness.recipient_seconds"
    )
    request_freshness = _positive_number(
        freshness_raw["request_seconds"], "freshness.request_seconds"
    )

    recipients = [
        _recipient(value, f"recipients[{index}]")
        for index, value in enumerate(_array(raw["recipients"], "recipients"))
    ]
    probes = [
        _probe(value, f"probes[{index}]")
        for index, value in enumerate(_array(raw["probes"], "probes"))
    ]
    events = [
        _event(value, f"events[{index}]")
        for index, value in enumerate(_array(raw["events"], "events"))
    ]
    if not recipients:
        raise FixtureError("recipients must not be empty")
    if not probes:
        raise FixtureError("probes must not be empty")

    _unique(recipients, "recipient_id", "recipients")
    claimed_lease_ids: set[str] = set()
    for index, recipient in enumerate(recipients):
        lease_id = recipient["lease"]["lease_id"]
        if lease_id in claimed_lease_ids:
            raise FixtureError(
                f"recipients[{index}].lease.lease_id duplicates {lease_id!r}"
            )
        claimed_lease_ids.add(lease_id)
    _unique(probes, "request_id", "probes")
    _unique(probes, "message_id", "probes")
    _unique(probes, "recipient_id", "probes")
    _unique(events, "event_id", "events")

    recipients_by_id = {item["recipient_id"]: item for item in recipients}
    for index, probe in enumerate(probes):
        if probe["recipient_id"] not in recipients_by_id:
            raise FixtureError(
                f"probes[{index}].recipient_id references unknown recipient "
                f"{probe['recipient_id']!r}"
            )

    reports = [
        _resolve_probe(
            probe,
            recipients_by_id[probe["recipient_id"]],
            events,
            observed_at=observed_at,
            recipient_freshness=recipient_freshness,
            request_freshness=request_freshness,
        )
        for probe in probes
    ]
    available = [report["recipient_id"] for report in reports if report["available"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_id": fixture_id,
        "status": "green" if available else "unavailable",
        "available_recipients": available,
        "recipients": reports,
    }


def load_fixture(path: str | Path) -> dict[str, Any]:
    """Load and resolve a UTF-8 JSON snapshot without accessing live state."""

    fixture_path = Path(path)
    try:
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot load fixture {fixture_path}: {exc}") from exc
    return resolve_fixture(raw)


def exit_code(report: dict[str, Any]) -> int:
    """Return zero only when the snapshot has a resolvable recipient."""

    return 0 if report.get("status") == "green" else 1
