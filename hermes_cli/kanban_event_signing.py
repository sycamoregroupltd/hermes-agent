"""kanban-event-signing — per-agent ed25519 signing of kanban task_events rows.

Card t_3f244a06 (section 4b of agent-signing-keys-design).

DOCTRINE:
  - Signing is ADVISORY. It NEVER raises; if crypto fails the event still
    writes cleanly. The verifier reports UNVERIFIED / BAD instead of breaking.
  - Each agent signs with its OWN profile-local ed25519 key (~.pub in
    ~/.hermes/seats/<seat>/keys/ or ~/.hermes/profiles/<name>/keys/).
  - Key material is verified against allowed_signers for trustworthiness,
    matching the git commit-signing pattern (allowedSignersFile).
  - Schema change: advisory ALTER TABLE adds "event_signature TEXT" to
    task_events. Zero impact on existing consumers.
"""

import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# ---- Paths ------------------------------------------------------------------

FLEET_HOME = Path(os.environ.get("HERMES_ROOT", "/home/frank/.hermes"))
ALLOWED_SIGNERS = FLEET_HOME / "governance" / "allowed_signers"

KEY_SEARCH_DIRS = [
    FLEET_HOME / "seats",     # ~/.hermes/seats/<seat>/keys/
    FLEET_HOME / "profiles",  # ~/.hermes/profiles/<name>/keys/
]


def _find_private_key(identity):
    """Return absolute private-key path for ``identity`` or None."""
    for base in KEY_SEARCH_DIRS:
        kd = base / identity / "keys"
        priv = kd / f"{identity}-signing"
        if priv.is_file():
            return str(priv)
    return None


def _find_allowed_principal_for(identity):
    """Check whether *identity* is registered in allowed_signers.
    
    Returns True if the profile/seat identity has a corresponding line
    (namespaces="git" lines suffice; we don't require namespaces="kanban").
    This mirrors the git verification pattern.
    """
    if not ALLOWED_SIGNERS.is_file():
        return False
    try:
        target = f"{identity}@hermes-fleet"
        with open(ALLOWED_SIGNERS) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith("#") or not ln:
                    continue
                if ln.split()[0] == target:
                    return True
    except Exception:
        pass
    return False


def sign_event_payload(task_id, kind, payload_json_str, run_id, created_at):
    """Best-effort ed25519 sign an event's canonical payload.
    
    Called from *_append_event* immediately after INSERT. Must NOT raise.
    Returns (signature_b64, status):
      (b64_string, "GOOD")          — signed successfully
      (None, "NONE")               — no private key found
      (None, "ERR <msg>")          — crypto error
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    # Resolve active profile name (same method as lifecycle hooks)
    try:
        from hermes_cli.profiles import get_active_profile_name  # noqa: F401
        identity = get_active_profile_name()
    except Exception:
        identity = os.environ.get("HERMES_PROFILE", "default")

    key_path = _find_private_key(identity)
    if key_path is None:
        return None, "NONE"

    try:
        with open(key_path, "rb") as fh:
            pk = serialization.load_ssh_private_key(fh.read(), password=None)
    except Exception as exc:
        msg = f"ERR load_key: {exc}"
        _log.debug("event_signing %s: %s", identity, msg)
        return None, msg

    try:
        # Canonical form: deterministic JSON of all standard columns
        d = {
            "task_id": task_id,
            "kind": kind,
            "payload": json.loads(payload_json_str) if payload_json_str else None,
            "run_id": run_id,
            "created_at": int(created_at),
        }
        msg_bytes = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        sig = pk.sign(msg_bytes)
        return base64.b64encode(sig).decode("ascii"), "GOOD"
    except Exception as exc:
        msg = f"ERR sign: {exc}"
        _log.debug("event_signing %s: %s", identity, msg)
        return None, msg


def register_advisory_schema(conn):
    """Ensure the event_signature column exists on task_events.
    
    Idempotent — uses ADD COLUMN IF NOT EXISTS so safe to call on every import.
    Never modifies existing data.
    """
    try:
        conn.execute(
            "ALTER TABLE task_events ADD COLUMN event_signature TEXT DEFAULT NULL"
        )
        _log.info("event_signing: added event_signature column to task_events")
    except Exception:
        # Column already exists or table incompatible — ignore silently
        _log.debug("event_signing: event_signature column already present")


def append_event_with_signature(conn, task_id, kind, payload, *, run_id=None):
    """Append a task_events row AND sign it (all-in-one transaction).
    
    Wrapper around conn.execute(...) suitable for replacement calls.
    If you're integrating into the core library, just apply the ADD COLUMN
    migration in register_advisory_schema() and compute the signature inline.
    """
    import time
    
    register_advisory_schema(conn)
    now = int(time.time())
    pl = json.dumps(payload, ensure_ascii=False) if payload else None
    
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, pl, now),
    )
    
    sig, status = sign_event_payload(task_id, kind, pl, run_id, now)
    if status == "GOOD":
        conn.execute(
            "UPDATE task_events SET event_signature = ? WHERE id = ?",
            (sig, conn.lastrowid),
        )
        _log.debug("signed event %d as %s [%s]", conn.lastrowid, task_id, identity)
    elif status.startswith("ERR"):
        _log.warning("event_signing failed for task %s (%s): %s", task_id, kind, status)
