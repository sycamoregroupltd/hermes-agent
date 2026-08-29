#!/usr/bin/env python3
"""kanban-signature-verify — periodic tamper check of the kanban event ledger.

WHY THIS EXISTS
    ed25519 signing of task_events has been writing since 2026-08-29, but nothing
    ever verified it: verify_sidecar() was reachable only as a manual CLI
    subcommand, run by no cron and no script. Signatures accumulated and were
    never checked, so the tamper-evidence property was not actually delivered.
    Found while auditing the module (t_5ecd5b0c).

EXIT CODE IS THE SIGNAL
    A `hermes cron` no-agent job NEVER parses stdout — only the exit code
    reaches the failure summary. So:
      exit 0  every signed event verified (or nothing to verify yet)
      exit 1  BAD or UNTRUSTED > 0 on any board  -> the ledger may be altered
      exit 0  infrastructure problems (missing sidecar, unreadable board)
              are reported on stdout and DO NOT fail the job — otherwise the
              check goes permanently red on a fresh install and gets ignored.
    STALE and UNSIGNED are not failures: events predating signing, or written by
    a build with no key, are expected.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT_OVERRIDE") or Path.home() / ".hermes")
BOARDS_DIR = ROOT / "kanban" / "boards"
SIDECAR = ROOT / "audit" / "kanban-event-signatures.db"
ALERT = ROOT / "scripts" / "fleet-alert-card.sh"
SIGNING = ROOT / "hermes-agent" / "hermes_cli" / "kanban_event_signing.py"
PYBIN = ROOT / "hermes-agent" / "venv" / "bin" / "python"

# Only boards that carry real fleet work. Throwaway/test boards are skipped so a
# stray fixture cannot turn the fleet's tamper check red.
REAL_BOARDS = {
    "jarvis-os", "sycode-trading", "sycode-ai", "upero",
    "yorkstone-supplies", "ai-restaurant", "ecohome", "orchestrator-sync",
}


def _alert(key: str, subject: str, body: str) -> None:
    """Raise a board card. Best effort: never let alerting fail the check."""
    if not ALERT.is_file():
        return
    try:
        subprocess.run([str(ALERT), key, subject, body], timeout=120,
                       capture_output=True, check=False)
    except Exception as exc:  # noqa: BLE001 - alerting must never fail the check
        print(f"SIG-VERIFY: alert card write failed (non-fatal): {exc}")


def main() -> int:
    if not SIDECAR.is_file():
        print(f"SIG-VERIFY: no sidecar at {SIDECAR} — signing not active yet; nothing to verify")
        return 0
    if not SIGNING.is_file() or not PYBIN.is_file():
        print(f"SIG-VERIFY: verifier or interpreter missing ({SIGNING}); skipping")
        return 0

    failures: list[str] = []
    checked = 0
    for board_dir in sorted(BOARDS_DIR.glob("*")):
        if board_dir.name not in REAL_BOARDS:
            continue
        db = board_dir / "kanban.db"
        if not db.is_file():
            continue
        try:
            p = subprocess.run(
                [str(PYBIN), str(SIGNING), "verify",
                 "--kanban-db", str(db), "--sidecar", str(SIDECAR)],
                capture_output=True, text=True, timeout=900, check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"SIG-VERIFY: {board_dir.name} TIMED OUT after 900s — not treated as tampering")
            continue
        checked += 1
        out = (p.stdout or "").strip()
        for line in out.splitlines():
            if line.startswith("EVENT-SIG-VERIFY:"):
                print(line)
        # rc==1 means the verifier itself found BAD/UNTRUSTED. Any other non-zero
        # is an infrastructure fault, not evidence of tampering.
        if p.returncode == 1:
            failures.append(f"{board_dir.name}: {(p.stderr or out).strip()[:400]}")
        elif p.returncode != 0:
            print(f"SIG-VERIFY: {board_dir.name} verifier error rc={p.returncode}: "
                  f"{(p.stderr or '').strip()[:200]}")

    if failures:
        detail = "\n".join(failures)
        print(f"SIG-VERIFY: FAIL on {len(failures)} board(s)\n{detail}", file=sys.stderr)
        # Stable key: one card per condition, superseded on recurrence. Never put
        # a count or timestamp in an alert key — that defeats the supersede and
        # floods the board (see the 157-duplicate stack-health incident).
        _alert(
            "kanban_event_signature_bad",
            "🚨 kanban event-signature verification FAILED",
            "The signed kanban event ledger did not verify. Either stored event "
            "content was altered after signing, or an event was signed by a key "
            "that is not in governance/allowed_signers.\n\n"
            f"{detail}\n\n"
            "Reproduce:\n"
            f"  {PYBIN} {SIGNING} verify --kanban-db "
            f"{BOARDS_DIR}/<board>/kanban.db --sidecar {SIDECAR}\n\n"
            "STALE/UNSIGNED are expected (events predating signing) and are NOT "
            "part of this failure.",
        )
        return 1

    print(f"SIG-VERIFY: OK — {checked} board(s) verified, no BAD or UNTRUSTED signatures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
