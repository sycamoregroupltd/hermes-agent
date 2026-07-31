#!/usr/bin/env python3
"""Issue one disposable CMUX-to-Claude interactive session binding.

This is an inert issuer.  It never invokes CMUX, Claude, a provider, a
dispatcher, or an executor.  It only consumes the already Mac-minted CMUX
receipt plus its canonical seat reservation and writes one tamper-evident
binding artifact for an *existing blocked* Hermes task.

The artifact is deliberately single-use at its path (O_EXCL): a replay cannot
replace, extend, or silently refresh an earlier binding.  The issuer accepts
only the exact pre-existing provider session UUID recorded in the CMUX seat
reservation; it cannot create or discover a Claude session.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


BINDING_KIND = "cmux-interactive-claude-session-binding"
BINDING_SCHEMA_VERSION = 1
MAX_RECEIPT_WINDOW_SECONDS = 600
MIN_BINDING_TTL_SECONDS = 30
MAX_BINDING_TTL_SECONDS = 600
OUTPUT_RELATIVE_PATH = Path("reservation/cmux-interactive-session-binding.json")
CALLER_PROOF_MARKER = "nonce-read-screen"
TASK_RE = re.compile(r"^t_[0-9a-f]{8}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
NONCE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Stable CMUX ref for a surface (the `ref` field system.tree reports); a
# reservation may pin the seat by ref instead of raw tree UUID.
STABLE_SURFACE_REF_RE = re.compile(r"^surface:[0-9]+$")


class Refuse(RuntimeError):
    """A contract precondition failed; no binding may be issued."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reservation_fingerprint(value: dict[str, Any]) -> str:
    clone = {key: item for key, item in value.items() if key != "reservation_fingerprint"}
    return "sha256:" + sha256_text(canonical_json(clone))


def receipt_fingerprint(value: dict[str, Any]) -> str:
    clone = {key: item for key, item in value.items() if key != "receipt_fingerprint"}
    return "sha256:" + sha256_text(compact_json(clone))


def artifact_fingerprint(value: dict[str, Any]) -> str:
    clone = {key: item for key, item in value.items() if key != "artifact_fingerprint"}
    return "sha256:" + sha256_text(compact_json(clone))


def parse_utc(value: Any, name: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise Refuse(f"{name} missing or malformed")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refuse(f"{name} missing or malformed") from exc
    if parsed.tzinfo is None:
        raise Refuse(f"{name} must be timezone-aware")
    return parsed.astimezone(dt.timezone.utc)


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refuse(f"{label} unreadable or malformed") from exc
    if not isinstance(value, dict):
        raise Refuse(f"{label} must be a JSON object")
    return value


def require_identifier(value: str, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Refuse(f"{label} malformed")
    return value


def validate_output_rel(output_rel: str | Path | None, task_id: str) -> Path:
    """Return the only permitted task-local binding destination.

    The historical default remains the legacy global reservation location.
    An override is intentionally one exact file name below the named task's
    reservation artifact directory. This prevents a new issuance from
    replacing a prior task's binding or escaping via a normalized traversal.
    """
    if output_rel is None:
        return OUTPUT_RELATIVE_PATH
    raw = str(output_rel)
    if raw == str(OUTPUT_RELATIVE_PATH):
        return OUTPUT_RELATIVE_PATH
    expected = Path("reservation") / "task-artifacts" / task_id / "cmux-interactive-session-binding.json"
    candidate = Path(raw)
    if candidate.is_absolute() or "\\" in raw or candidate != expected:
        raise Refuse("binding output override must be exactly " + str(expected))
    return candidate


def validate_caller_context(receipt: dict[str, Any], seat: dict[str, Any]) -> None:
    """Defence in depth (t_a6365be3): re-assert the receipt's caller claim.

    The Mac mint's C9 proves — by nonce read-screen — that the caller IS the
    exact reserved surface before any receipt exists. DGX treats the receipt
    strictly as data, so this issuer re-checks that claim structurally: a
    caller_context that is absent, malformed, foreign to the reservation
    seat, or re-signed around a foreign caller refuses. When the reservation
    pins the seat by stable ref, the receipt keeps the ref verbatim at top
    level while caller_context records the tree ID the mint resolved on the
    Mac; DGX cannot resolve refs (it never touches a CMUX socket), so that
    field must then be exactly a well-formed raw tree ID — never a ref."""
    caller = receipt.get("mint_control_context")
    if not isinstance(caller, dict):
        raise Refuse("CMUX receipt caller_context missing or malformed")
    if caller.get("proof") != CALLER_PROOF_MARKER:
        raise Refuse("CMUX receipt caller_context proof marker is not nonce-read-screen")
    nonce = caller.get("nonce_sha256")
    if not isinstance(nonce, str) or not NONCE_SHA256_RE.fullmatch(nonce):
        raise Refuse("CMUX receipt caller_context nonce digest is not sha256 hex form")
    tty = caller.get("tty")
    if not isinstance(tty, str) or not tty.strip():
        raise Refuse("CMUX receipt caller_context tty missing or malformed")
    if caller.get("workspace_id") != seat["cmux_workspace_id"]:
        raise Refuse("CMUX receipt caller_context workspace does not match reservation seat")
    surface = caller.get("surface_id")
    if STABLE_SURFACE_REF_RE.fullmatch(seat["cmux_surface_id"]):
        if not isinstance(surface, str) or not UUID_RE.fullmatch(surface):
            raise Refuse("CMUX receipt caller_context surface is not a resolved raw tree id")
    elif surface != seat["cmux_surface_id"]:
        raise Refuse("CMUX receipt caller_context surface does not match reservation seat")


def validate_contract(
    *,
    reservation: dict[str, Any],
    receipt: dict[str, Any],
    task_id: str,
    session_id: str,
    now: dt.datetime,
) -> tuple[dict[str, Any], dict[str, Any], dt.datetime, dt.datetime]:
    """Validate the already-published Mac provenance, never probing a provider."""
    if reservation.get("record_kind") != "cmux-manual-seat-reservation" or reservation.get("schema_version") != 2:
        raise Refuse("reservation must be dual-anchor schema_version 2")
    if reservation.get("reservation_fingerprint") != reservation_fingerprint(reservation):
        raise Refuse("reservation fingerprint mismatch")
    seat = reservation.get("seat")
    mint = reservation.get("mint_control")
    if not isinstance(seat, dict) or not isinstance(mint, dict):
        raise Refuse("reservation seat or mint_control malformed")
    if seat.get("provider") != "claude-code" or seat.get("kind") != "cmux-interactive-claude-max":
        raise Refuse("reservation is not the Claude interactive CMUX seat")
    for field in ("cmux_workspace_id", "cmux_surface_id", "cmux_daemon_version"):
        if not isinstance(seat.get(field), str) or not str(seat[field]).strip():
            raise Refuse(f"reservation seat {field} missing")
    for field in ("cmux_workspace_id", "cmux_surface_id"):
        if not isinstance(mint.get(field), str) or not str(mint[field]).strip():
            raise Refuse(f"reservation mint_control {field} missing")
    reserved_session = require_identifier(
        str(seat.get("provider_session_uuid", "")), "reservation provider_session_uuid", UUID_RE
    )
    if session_id != reserved_session:
        raise Refuse("session id does not equal the pre-existing reserved provider session")

    if receipt.get("receipt_kind") != "mac-cmux-reservation-receipt":
        raise Refuse("CMUX receipt has wrong receipt_kind")
    if receipt.get("receipt_fingerprint") != receipt_fingerprint(receipt):
        raise Refuse("CMUX receipt fingerprint mismatch")
    if receipt.get("canary_task") != task_id:
        raise Refuse("CMUX receipt is bound to a different Hermes task")
    if receipt.get("cmux_workspace_id") != seat["cmux_workspace_id"]:
        raise Refuse("CMUX receipt workspace does not match provider reservation")
    if receipt.get("cmux_surface_id") != seat["cmux_surface_id"]:
        raise Refuse("CMUX receipt surface does not match provider reservation")
    control = receipt.get("control_socket")
    if not isinstance(control, dict) or control.get("cmux_daemon_version") != seat["cmux_daemon_version"]:
        raise Refuse("CMUX receipt daemon identity does not match reservation")
    validate_caller_context(receipt, mint)
    issued = parse_utc(receipt.get("minted_at_utc"), "CMUX receipt minted_at_utc")
    expires = parse_utc(receipt.get("expires_at_utc"), "CMUX receipt expires_at_utc")
    if issued > now:
        raise Refuse("CMUX receipt is future-dated")
    if expires <= now:
        raise Refuse("CMUX receipt is expired")
    if (expires - issued).total_seconds() > MAX_RECEIPT_WINDOW_SECONDS:
        raise Refuse("CMUX receipt validity window is too long")
    return seat, control, issued, expires


def verify_blocked_task(board_db: Path, task_id: str) -> None:
    """Read-only anchor: bind only one named, existing, unrun blocked task."""
    try:
        connection = sqlite3.connect(f"file:{board_db}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            runs = connection.execute(
                "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise Refuse("Hermes board cannot be read-only verified") from exc
    if row != ("blocked",):
        raise Refuse("named Hermes task is not an existing blocked task")
    if runs is None or int(runs[0]) != 0:
        raise Refuse("named Hermes task already has runs; replay/continuation forbidden")


def issue_binding(
    *,
    worktree: Path,
    board_db: Path,
    reservation_path: Path,
    receipt_path: Path,
    task_id: str,
    session_id: str,
    declared_by: str,
    ttl_seconds: int,
    output_rel: str | Path | None = None,
    now: dt.datetime | None = None,
) -> Path:
    """Validate and atomically issue one binding. No CMUX/provider call occurs."""
    task_id = require_identifier(task_id, "task id", TASK_RE)
    output_rel = validate_output_rel(output_rel, task_id)
    session_id = require_identifier(session_id, "session id", UUID_RE)
    if not isinstance(declared_by, str) or not declared_by.strip() or len(declared_by) > 200:
        raise Refuse("declared_by missing or malformed")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not (
        MIN_BINDING_TTL_SECONDS <= ttl_seconds <= MAX_BINDING_TTL_SECONDS
    ):
        raise Refuse("binding ttl outside 30..600 seconds")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise Refuse("issuer now must be timezone-aware")
    now = now.astimezone(dt.timezone.utc)
    if not worktree.is_dir():
        raise Refuse("declared worktree does not exist")
    for marker in ("bin_verify/mint_cmux_receipt.py", "bin_verify/dispatch_gate_v2.py"):
        if not (worktree / marker).is_file():
            raise Refuse("declared worktree marker missing")
    reservation = load_json(reservation_path, "seat reservation")
    receipt = load_json(receipt_path, "CMUX receipt")
    seat, control, receipt_issued, receipt_expires = validate_contract(
        reservation=reservation, receipt=receipt, task_id=task_id,
        session_id=session_id, now=now,
    )
    verify_blocked_task(board_db, task_id)
    expires = min(receipt_expires, now + dt.timedelta(seconds=ttl_seconds))
    if (expires - now).total_seconds() < MIN_BINDING_TTL_SECONDS:
        raise Refuse("CMUX receipt expires too soon for a minimum binding window")
    output = (worktree / output_rel).resolve()
    root = worktree.resolve()
    expected_parent = (root / output_rel.parent).resolve()
    if output.parent != expected_parent or not str(output).startswith(str(root) + os.sep):
        raise Refuse("binding output escapes permitted worktree-relative destination")
    binding: dict[str, Any] = {
        "binding_kind": BINDING_KIND,
        "schema_version": BINDING_SCHEMA_VERSION,
        "provider": "claude-code",
        "task_id": task_id,
        "board_sha256": sha256_text(str(board_db.resolve())),
        "session_id": session_id,
        "declared_by": declared_by.strip(),
        "issued_at_utc": now.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": expires.isoformat().replace("+00:00", "Z"),
        "cmux_seat": {
            "workspace_id": seat["cmux_workspace_id"],
            "surface_id": seat["cmux_surface_id"],
            "daemon_version": seat["cmux_daemon_version"],
            "provider_session_uuid": seat["provider_session_uuid"],
        },
        "mac_receipt": {
            "receipt_fingerprint": receipt["receipt_fingerprint"],
            "minted_at_utc": receipt["minted_at_utc"],
            "expires_at_utc": receipt["expires_at_utc"],
            "control_socket_bundle_id": control.get("bundle_identifier"),
        },
        "reservation_fingerprint": reservation["reservation_fingerprint"],
        # The consumer compares this to the issuer source supplied at its own
        # gate and at dispatch time.  A binding cannot silently survive a
        # changed issuer implementation.
        "issuer_source_sha256": sha256_file(Path(__file__).resolve()),
    }
    binding["artifact_fingerprint"] = artifact_fingerprint(binding)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise Refuse("binding artifact already exists; replay or replacement forbidden") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(binding))
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        raise
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--board-db", type=Path, required=True)
    parser.add_argument("--reservation", type=Path, required=True)
    parser.add_argument("--cmux-receipt", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--declared-by", required=True)
    parser.add_argument("--ttl", type=int, default=300)
    parser.add_argument("--output-rel", default=None,
                        help="legacy default unchanged; override only to reservation/task-artifacts/<task-id>/cmux-interactive-session-binding.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = issue_binding(
            worktree=args.worktree, board_db=args.board_db,
            reservation_path=args.reservation, receipt_path=args.cmux_receipt,
            task_id=args.task_id, session_id=args.session_id,
            declared_by=args.declared_by, ttl_seconds=args.ttl,
            output_rel=args.output_rel,
        )
        report = {"verdict": "ISSUED", "artifact": str(output)}
        rc = 0
    except Refuse as exc:
        report = {"verdict": "REFUSE", "reason": str(exc)}
        rc = 2
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"{report['verdict']}: {report.get('artifact') or report.get('reason')}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
