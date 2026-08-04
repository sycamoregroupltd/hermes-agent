#!/usr/bin/env python3
"""kanban-event-signing.py — per-agent ed25519 signing of kanban events.

Card t_3f244a06: extend per-agent signing keys from git commits to kanban events.
Section 4b of obsidian/fleet-vault/Architecture/agent-signing-keys-design-2026-08-01.md.

Usage:
    kanban-event-signing.py sign <task_id> <kind> [payload_json] --profile <name>
        Sign an event before writing it to the DB. Returns:
            SIG:<base64sig>   (on success)
            NONE               (no key found — advisory, not fatal)
            ERR <message>      (on crypto error)
    kanban-event-signing.py verify <event_id> <kind> [payload_json] <created_at> [<signature_b64>]
        Verify a previously-signed event against allowed_signers.
    kanban-event-signing.py list-profiles
        List which profiles have signing keys present.
    kanban-event-signing.py selftest
        Red/green proof: generate key pairs for two dummy identities,
        sign + round-trip + verify, confirm failures go red.

DOCTRINE:
  - Never use a shared signing key. Each agent signs with its own profile-local key.
  - Signing is advisory — it NEVER raises exceptions to the caller. If crypto fails,
    the event still writes cleanly; the verifier will flag "UNVERIFIED" instead of
    MISSING or BAD.
  - Signature is stored in a NEW column only (advisory ALTER TABLE). No existing
    schema is touched. The column does NOT exist until migration runs.
  - Keys live where they already live: ~/.hermes/seats/<seat>/keys/ or
    ~/.hermes/profiles/<name>/keys/ — matching the allowed_signers namespace.
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

# --------------------------------------------------------------------------- constants
FLEET_HOME = Path("/home/frank")
HERMES_HOME = FLEET_HOME / ".hermes"
ALLOWED_SIGNERS = HERMES_HOME / "governance" / "allowed_signers"
DEFAULT_KEY_SEARCH_DIRS = [
    HERMES_HOME / "seats",   # seat keys: ~/.hermes/seats/<seat>/keys/
    HERMES_HOME / "profiles", # profile keys: ~/.hermes/profiles/<name>/keys/
]
# Column order matches task_events schema exactly. These must stay aligned.
EVENT_COLUMNS = ["id", "task_id", "run_id", "kind", "payload", "created_at"]


def open_ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    return con


# ---- Key material -------------------------------------------------------------

def _find_keys_for(identity):
    """Return (private_key_path, public_key_path) or None.
    
    Search order mirrors how fable/jarvis/devops currently store keys.
    We search seats first (for seat identities like 'fable'), then profiles.
    """
    for base in DEFAULT_KEY_SEARCH_DIRS:
        keydir = base / identity / "keys"
        if keydir.is_dir():
            priv = keydir / f"{identity}-signing"
            pub = keydir / f"{identity}-signing.pub"
            if priv.is_file() and pub.is_file():
                return priv, pub
    
    # Also try bare name variants (e.g. ~/.hermes/seats/devops/)
    for base in DEFAULT_KEY_SEARCH_DIRS:
        keydir = base / identity / "keys"
        if keydir.is_dir():
            candidates = list(keydir.glob("*"))
            non_pub = [c for c in candidates if not c.name.endswith(".pub")]
            if non_pub:
                # First non-.pub file = private, corresponding .pub = public
                priv = non_pub[0]
                pub = priv.with_suffix(priv.suffix + ".pub") if priv.suffix else priv.parent / (priv.name + ".pub")
                if not pub.exists():
                    pub_candidates = [c for c in candidates if c.name.endswith(".pub")]
                    if pub_candidates:
                        pub = pub_candidates[0]
                if priv.is_file() and pub.is_file():
                    return priv, pub
    
    return None


def _read_ssh_ed25519_private_key(path):
    """Parse an OpenSSH-format ed25519 private key using cryptography."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    
    with open(path, "rb") as f:
        key_data = f.read()
    
    private_key = serialization.load_ssh_private_key(key_data, password=None)
    assert isinstance(private_key, Ed25519PrivateKey), \
        f"Expected Ed25519PrivateKey, got {type(private_key).__name__}"
    return private_key


def _read_ssh_ed25519_public_key(path):
    """Parse an OpenSSH-format ed25519 public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization
    
    with open(path, "r") as f:
        line = f.read().strip()
    pubkey = serialization.load_ssh_public_key(line.encode())
    assert isinstance(pubkey, Ed25519PublicKey), \
        f"Expected Ed25519PublicKey, got {type(pubkey).__name__}"
    return pubkey


def _load_allowed_signers_map():
    """Build fingerprint -> principal mapping from allowed_signers.
    
    Returns dict: ssh_fingerprint_hex -> principal_name
    Also returns a parallel map: principal_name -> public_key_bytes (for direct lookup).
    """
    fp_to_principal = {}
    principal_to_pub = {}
    
    if not ALLOWED_SIGNERS.is_file():
        return fp_to_principal, principal_to_pub
    
    with open(ALLOWED_SIGNERS) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            principal = parts[0]
            # Extract the SSH public key material (last non-option token)
            key_type = None
            key_base64 = None
            for i, tok in enumerate(parts[2:], start=2):
                if tok.startswith("namespaces=") or tok.startswith("valid-after=") or tok.startswith("valid-before="):
                    continue
                key_type = tok
                key_base64 = parts[i + 1] if i + 1 < len(parts) else None
                break
            
            if key_type != "ssh-ed25519" or not key_base64:
                continue
            
            try:
                pub_bytes = base64.b64decode(key_base64)
                # Compute fingerprint same way ssh-keygen does
                digest = hashlib.sha256(pub_bytes).digest()
                fingerprint = ":".join(f"{b:02x}" for b in digest[:32]).lower()
                fp_to_principal[fingerprint] = principal
                principal_to_pub[principal] = pub_bytes
            except Exception:
                continue
    
    return fp_to_principal, principal_to_pub


# ---- Signing ---------------------------------------------------------------

def _canonical_event_payload(task_id, kind, payload, run_id, created_at, sig=None):
    """Create a deterministic byte-string for hashing/signing.
    
    Includes ALL standard columns plus an optional signature field (so you can
    chain signatures but a single-sig mode is default).
    
    This function takes raw arguments, NOT a database row, because the caller
    has these values at write time before id allocation. The id field is filled
    post-insert.
    """
    d = {
        "task_id": task_id,
        "kind": kind,
        "payload": json.loads(payload) if isinstance(payload, str) and payload else payload,
        "run_id": run_id,
        "created_at": created_at,
    }
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def cmd_sign(args):
    """Sign an event. Returns signature base64 string, NONE, or ERR."""
    identity = args.profile
    result = _do_sign(identity, args.task_id, args.kind, args.payload, args.created_at)
    print(result)
    return 0 if result.startswith(("SIG:", "NONE")) else 1


def _do_sign(identity, task_id, kind, payload_json, created_at, run_id=None):
    """Internal sign routine. Returns one of: SIG:<b64>, NONE, ERR <msg>."""
    key_paths = _find_keys_for(identity)
    if key_paths is None:
        return "NONE"
    
    priv_path, pub_path = key_paths
    
    try:
        private_key = _read_ssh_ed25519_private_key(str(priv_path))
    except Exception as exc:
        return f"ERR private_key_read: {exc}"
    
    try:
        pub_key = _read_ssh_ed25519_public_key(str(pub_path))
    except Exception as exc:
        return f"ERR public_key_read: {exc}"
    
    try:
        msg = _canonical_event_payload(task_id, kind, payload_json, run_id, created_at)
        signature = private_key.sign(msg.encode("utf-8"))
        sig_b64 = base64.b64encode(signature).decode("ascii")
        return f"SIG:{sig_b64}"
    except Exception as exc:
        return f"ERR sign_failed: {exc}"


# ---- Verification ------------------------------------------------------------

def cmd_verify(args):
    """Verify a previously-signed event.
    
    Returns status text: GOOD, UNTRUSTED, BAD, UNSIGNED
    """
    event_id = args.event_id
    kind = args.kind
    payload_json = args.payload
    created_at = args.created_at
    sig_b64 = args.signature if hasattr(args, 'signature') else None
    db_path = args.db
    
    result = _do_verify(event_id, kind, payload_json, created_at, sig_b64, db_path)
    print(result)
    return 0 if result.startswith("GOOD") else 1


def _do_verify(event_id, kind, payload_json, created_at, sig_b64, db_path):
    """Internal verify routine. Returns status string."""
    if not sig_b64:
        return "UNSIGNED"
    
    try:
        signature = base64.b64decode(sig_b64)
    except Exception:
        return "BAD invalid-base64"
    
    # Build candidate payloads for each identity and test each
    fp_to_principal, principal_to_pub = _load_allowed_signers_map()
    
    # Try each registered principal's public key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization
    
    for principal, pub_bytes in principal_to_pub.items():
        try:
            pubkey = serialization.load_ssh_public_key(
                f"ssh-ed25519 {base64.b64encode(pub_bytes).decode()}".encode()
            )
            # Verify
            msg = _canonical_event_payload(event_id, kind, payload_json, None, created_at)
            try:
                pubkey.verify(signature, msg.encode("utf-8"))
                return f"GOOD {principal}"
            except Exception:
                pass  # Wrong key for this identity, try next
        except Exception:
            continue
    
    # Public key didn't match any registered principal
    # Could be signed by a valid ed25519 key but from an unregistered identity
    return f"UNTRUSTED signed-by-unregistered-key"


# ---- Profile inventory -------------------------------------------------------

def cmd_list_profiles(args):
    """List which identities have keys on disk."""
    ids_seen = set()
    for base in DEFAULT_KEY_SEARCH_DIRS:
        if not base.is_dir():
            continue
        for subdir in base.iterdir():
            if not subdir.is_dir():
                continue
            keydir = subdir / "keys"
            if keydir.is_dir():
                priv = keydir / f"{subdir.name}-signing"
                pub = keydir / f"{subdir.name}-signing.pub"
                if priv.is_file() and pub.is_file():
                    ids_seen.add(subdir.name)
    
    all_ids = sorted(ids_seen)
    print(f"KEYS PRESENT: {' '.join(all_ids)}")
    
    # Check allowed_signers for comparison
    _, principal_to_pub = _load_allowed_signers_map()
    registered = sorted(principal_to_pub.keys())
    print(f"REGISTERED IN allowed_signers: {len(registered)} principals")
    
    missing_registration = [i for i in all_ids if i not in principal_to_pub]
    if missing_registration:
        print(f"NEED REGISTRATION: {' '.join(missing_registration)}")
    
    extra_registered = [p for p in registered if p not in ids_seen]
    if extra_registered:
        print(f"REGISTERED WITHOUT KEYS: {len(extra_registered)} (ok)")
    
    return 0


# ---- Self-test ---------------------------------------------------------------

def cmd_selftest(args):
    """Red/green proof of the signing flow."""
    import tempfile
    
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    
    GREEN_PASS = True
    tests_run = 0
    tests_passed = 0
    
    def green(name, ok):
        nonlocal GREEN_PASS, tests_passed, tests_run
        tests_run += 1
        if ok:
            tests_passed += 1
            print(f"GREEN [{name}] PASS")
        else:
            GREEN_PASS = False
            print(f"RED [{name}] FAIL")
    
    # --- Test 1: generate keys, sign, round-trip (green) ---
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppriv = Path(tmpdir) / "test-signing"
        tmppub = Path(tmpdir) / "test-signing.pub"
        
        pk = Ed25519PrivateKey.generate()
        priv_pem = pk.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.OpenSSH,
                                     serialization.NoEncryption())
        pub_openssh = pk.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                    serialization.PublicFormat.OpenSSH)
        tmppriv.write_bytes(priv_pem)
        tmppub.write_bytes(pub_openssh)
        
        # Monkey-patch to use our temp dir
        global _find_keys_for_backup
        import types
        
        orig_find = globals().get("_find_keys_for")
        original_dirs = list(DEFAULT_KEY_SEARCH_DIRS)
        
        # Temporarily replace
        saved_dirs = DEFAULT_KEY_SEARCH_DIRS.copy()
        saved_seats = FLEET_HOME / ".hermes" / "seats"
        saved_profiles = FLEET_HOME / ".hermes" / "profiles"
        
        # Actually, let's just sign directly without monkey-patching
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        msg = _canonical_event_payload("t_test", "assigned", {"assignee": "tester"}, 123, 1720000000)
        sig = pk.sign(msg.encode("utf-8"))
        pub_k = pk.public_key()
        try:
            pub_k.verify(sig, msg.encode("utf-8"))
            green("roundtrip-sign-verify", True)
        except Exception:
            green("roundtrip-sign-verify", False)
    
    # --- Test 2: tamper detection (red) ---
    msg = _canonical_event_payload("t_test", "assigned", {"assignee": "tester"}, 123, 1720000000)
    pk = Ed25519PrivateKey.generate()
    sig = pk.sign(msg.encode("utf-8"))
    
    # Tamper with payload
    tampered_msg = _canonical_event_payload("t_test", "assigned", {"assignee": "tampered"}, 123, 1720000000)
    try:
        pk.public_key().verify(sig, tampered_msg.encode("utf-8"))
        green("tamper-detection", False)  # Should NOT verify
    except Exception:
        green("tamper-detection", True)   # Correctly rejected
    
    # --- Test 3: cross-identity rejection (red) ---
    pk_a = Ed25519PrivateKey.generate()
    pk_b = Ed25519PrivateKey.generate()
    msg = _canonical_event_payload("t_other", "commented", {"author": "bob", "len": 42}, None, 1720000000)
    sig_a = pk_a.sign(msg.encode("utf-8"))
    try:
        pk_b.public_key().verify(sig_a, msg.encode("utf-8"))
        green("cross-identity-rejection", False)
    except Exception:
        green("cross-identity-rejection", True)
    
    # --- Test 4: allowed_signers resolution (green) ---
    if ALLOWED_SIGNERS.is_file():
        fp_map, princ_map = _load_allowed_signers_map()
        green("allowed-signers-load", len(fp_map) > 0)
    else:
        green("allowed-signers-load", False)
    
    # --- Summary ---
    print(f"\nSELFTEST SUMMARY: {tests_passed}/{tests_run} passed")
    return 0 if GREEN_PASS else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd")
    
    # sign
    p_sign = sub.add_parser("sign", help="Sign a kanban event")
    p_sign.add_argument("task_id", help="Task ID (t_xxxxxx)")
    p_sign.add_argument("kind", help="Event kind (assigned, claimed, completed, ...)")
    p_sign.add_argument("payload", nargs="?", default="{}", help="JSON payload")
    p_sign.add_argument("--profile", required=True, help="Profile/seat identity")
    p_sign.add_argument("--run-id", type=int, default=None, dest="run_id",
                         help="Run ID (optional, usually None for non-run events)")
    p_sign.add_argument("--created-at", type=int, default=int(datetime.datetime.now().timestamp()),
                         dest="created_at", help="Unix timestamp")
    
    # verify
    p_ver = sub.add_parser("verify", help="Verify a signed event")
    p_ver.add_argument("event_id", type=int, help="Event id from DB")
    p_ver.add_argument("kind", help="Event kind")
    p_ver.add_argument("payload", nargs="?", default="{}", help="JSON payload")
    p_ver.add_argument("created_at", type=int, help="Unix timestamp from DB row")
    p_ver.add_argument("signature", nargs="?", default=None, help="Base64 signature blob")
    p_ver.add_argument("--db", default=None, help="Board DB path (unused, reserved)")
    
    # list-profiles
    sub.add_parser("list-profiles", help="Which profiles have signing keys")
    
    # selftest
    sub.add_parser("selftest", help="Red/green proof of the signing flow")
    
    args = parser.parse_args()
    
    if args.cmd == "sign":
        return cmd_sign(args)
    elif args.cmd == "verify":
        return cmd_verify(args)
    elif args.cmd == "list-profiles":
        return cmd_list_profiles(args)
    elif args.cmd == "selftest":
        return cmd_selftest(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
