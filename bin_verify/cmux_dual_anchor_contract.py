#!/usr/bin/env python3
"""Shared fail-closed contract for dual-anchor CMUX reservations/receipts.

The provider anchor names the existing interactive Claude seat. The
mint-control anchor names the separate local Mac terminal which owns the CMUX
socket and proves its tty by a nonce/read-screen round trip. DGX consumers do
not probe CMUX; they validate this short-lived, fingerprinted schema as data.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any


RESERVATION_KIND = "cmux-manual-seat-reservation"
RESERVATION_SCHEMA_VERSION = 2
RECEIPT_KIND = "mac-cmux-reservation-receipt"
RECEIPT_SCHEMA_VERSION = 3
EXPECTED_PROVIDER = "claude-code"
EXPECTED_PROVIDER_KIND = "cmux-interactive-claude-max"
EXPECTED_BUNDLE_ID = "com.cmuxterm.app"
CALLER_PROOF_MARKER = "nonce-read-screen"
MAX_RECEIPT_WINDOW_SECONDS = 600

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
WORKSPACE_REF_RE = re.compile(r"^workspace:[0-9]+$")
SURFACE_REF_RE = re.compile(r"^surface:[0-9]+$")
NONCE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractRefuse(RuntimeError):
    """The cryptographic/schema contract is incomplete or inconsistent."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_prefixed(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def reservation_fingerprint(value: dict[str, Any]) -> str:
    clone = {key: item for key, item in value.items() if key != "reservation_fingerprint"}
    return _sha256_prefixed(canonical_json(clone))


def receipt_fingerprint(value: dict[str, Any]) -> str:
    clone = {key: item for key, item in value.items() if key != "receipt_fingerprint"}
    return _sha256_prefixed(compact_json(clone))


def anchor_fingerprint(value: dict[str, Any]) -> str:
    return _sha256_prefixed(compact_json(value))


def _require_nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractRefuse(f"{name} missing or malformed")
    return value


def _require_anchor_id(value: Any, *, kind: str, name: str) -> str:
    value = _require_nonempty(value, name)
    ref_re = WORKSPACE_REF_RE if kind == "workspace" else SURFACE_REF_RE
    if not UUID_RE.fullmatch(value) and not ref_re.fullmatch(value):
        raise ContractRefuse(f"{name} is neither a raw CMUX tree id nor a {kind} ref")
    return value


def _require_resolved_id(value: Any, name: str) -> str:
    value = _require_nonempty(value, name)
    if not UUID_RE.fullmatch(value):
        raise ContractRefuse(f"{name} is not a resolved raw CMUX tree id")
    return value


def _require_resolved_matches(anchor_value: str, resolved_value: str, name: str) -> None:
    if UUID_RE.fullmatch(anchor_value) and anchor_value.lower() != resolved_value.lower():
        raise ContractRefuse(f"{name} does not match the raw reservation anchor")


def parse_utc(value: Any, name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ContractRefuse(f"{name} missing or malformed")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractRefuse(f"{name} missing or malformed") from exc
    if parsed.tzinfo is None:
        raise ContractRefuse(f"{name} must be timezone-aware")
    return parsed.astimezone(dt.timezone.utc)


def validate_reservation(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if value.get("record_kind") != RESERVATION_KIND:
        raise ContractRefuse("reservation has wrong record_kind")
    if value.get("schema_version") != RESERVATION_SCHEMA_VERSION:
        raise ContractRefuse("reservation must be dual-anchor schema_version 2")
    if value.get("reservation_fingerprint") != reservation_fingerprint(value):
        raise ContractRefuse("reservation fingerprint mismatch")
    seat = value.get("seat")
    mint = value.get("mint_control")
    if not isinstance(seat, dict) or not isinstance(mint, dict):
        raise ContractRefuse("reservation seat or mint_control malformed")
    if seat.get("provider") != EXPECTED_PROVIDER or seat.get("kind") != EXPECTED_PROVIDER_KIND:
        raise ContractRefuse("reservation is not the Claude interactive CMUX seat")
    provider_ws = _require_anchor_id(
        seat.get("cmux_workspace_id"), kind="workspace", name="reservation seat workspace")
    provider_surface = _require_anchor_id(
        seat.get("cmux_surface_id"), kind="surface", name="reservation seat surface")
    _require_nonempty(seat.get("cmux_daemon_version"), "reservation seat daemon version")
    session = _require_nonempty(
        seat.get("provider_session_uuid"), "reservation provider_session_uuid")
    if not UUID_RE.fullmatch(session):
        raise ContractRefuse("reservation provider_session_uuid malformed")
    mint_ws = _require_anchor_id(
        mint.get("cmux_workspace_id"), kind="workspace", name="reservation mint_control workspace")
    mint_surface = _require_anchor_id(
        mint.get("cmux_surface_id"), kind="surface", name="reservation mint_control surface")
    if (provider_ws, provider_surface) == (mint_ws, mint_surface):
        raise ContractRefuse("provider and mint_control anchors must be distinct")
    if value.get("provider_anchor_fingerprint") != anchor_fingerprint(seat):
        raise ContractRefuse("reservation provider anchor fingerprint mismatch")
    if value.get("mint_control_anchor_fingerprint") != anchor_fingerprint(mint):
        raise ContractRefuse("reservation mint_control anchor fingerprint mismatch")
    return seat, mint


def validate_receipt(
    receipt: dict[str, Any],
    reservation: dict[str, Any],
    *,
    task_id: str | None,
    now: dt.datetime,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dt.datetime, dt.datetime]:
    seat, mint = validate_reservation(reservation)
    if receipt.get("receipt_kind") != RECEIPT_KIND:
        raise ContractRefuse("CMUX receipt has wrong receipt_kind")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ContractRefuse("CMUX receipt must be dual-anchor schema_version 3")
    if receipt.get("receipt_fingerprint") != receipt_fingerprint(receipt):
        raise ContractRefuse("CMUX receipt fingerprint mismatch")
    if task_id and receipt.get("canary_task") != task_id:
        raise ContractRefuse("CMUX receipt is bound to a different Hermes task")
    if receipt.get("reservation_fingerprint") != reservation["reservation_fingerprint"]:
        raise ContractRefuse("CMUX receipt reservation fingerprint mismatch")
    if receipt.get("provider_anchor_fingerprint") != reservation["provider_anchor_fingerprint"]:
        raise ContractRefuse("CMUX receipt provider anchor fingerprint mismatch")
    if receipt.get("mint_control_anchor_fingerprint") != reservation["mint_control_anchor_fingerprint"]:
        raise ContractRefuse("CMUX receipt mint_control anchor fingerprint mismatch")
    if receipt.get("cmux_workspace_id") != seat["cmux_workspace_id"]:
        raise ContractRefuse("CMUX receipt workspace does not match provider reservation")
    if receipt.get("cmux_surface_id") != seat["cmux_surface_id"]:
        raise ContractRefuse("CMUX receipt surface does not match provider reservation")

    caller = receipt.get("mint_control_context")
    if not isinstance(caller, dict):
        raise ContractRefuse("CMUX receipt mint_control_context missing or malformed")
    if caller.get("workspace_id") != mint["cmux_workspace_id"]:
        raise ContractRefuse("CMUX receipt mint_control workspace does not match reservation")
    if caller.get("surface_id") != mint["cmux_surface_id"]:
        raise ContractRefuse("CMUX receipt mint_control surface does not match reservation")
    resolved_ws = _require_resolved_id(
        caller.get("resolved_workspace_id"), "CMUX receipt resolved mint_control workspace")
    resolved_surface = _require_resolved_id(
        caller.get("resolved_surface_id"), "CMUX receipt resolved mint_control surface")
    _require_resolved_matches(mint["cmux_workspace_id"], resolved_ws, "resolved mint_control workspace")
    _require_resolved_matches(mint["cmux_surface_id"], resolved_surface, "resolved mint_control surface")
    if caller.get("proof") != CALLER_PROOF_MARKER:
        raise ContractRefuse("CMUX receipt mint_control proof marker is not nonce-read-screen")
    nonce = caller.get("nonce_sha256")
    if not isinstance(nonce, str) or not NONCE_SHA256_RE.fullmatch(nonce):
        raise ContractRefuse("CMUX receipt mint_control nonce digest is not sha256 hex form")
    _require_nonempty(caller.get("tty"), "CMUX receipt mint_control tty")

    control = receipt.get("control_socket")
    if not isinstance(control, dict):
        raise ContractRefuse("CMUX receipt control_socket missing or malformed")
    if control.get("bundle_identifier") != EXPECTED_BUNDLE_ID:
        raise ContractRefuse("CMUX receipt bundle identity mismatch")
    if control.get("cmux_daemon_version") != seat["cmux_daemon_version"]:
        raise ContractRefuse("CMUX receipt daemon identity does not match reservation")

    if now.tzinfo is None:
        raise ContractRefuse("receipt validation clock must be timezone-aware")
    now = now.astimezone(dt.timezone.utc)
    issued = parse_utc(receipt.get("minted_at_utc"), "CMUX receipt minted_at_utc")
    expires = parse_utc(receipt.get("expires_at_utc"), "CMUX receipt expires_at_utc")
    if issued > now:
        raise ContractRefuse("CMUX receipt is future-dated")
    if expires <= now:
        raise ContractRefuse("CMUX receipt is expired")
    if expires <= issued:
        raise ContractRefuse("CMUX receipt validity window is non-positive")
    if (expires - issued).total_seconds() > MAX_RECEIPT_WINDOW_SECONDS:
        raise ContractRefuse("CMUX receipt validity window is too long")
    return seat, mint, control, issued, expires
