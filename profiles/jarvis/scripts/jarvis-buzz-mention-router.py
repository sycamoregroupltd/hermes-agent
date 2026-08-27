#!/usr/bin/env python3
"""Route Hermes-directed Buzz mentions onto a Jarvis kanban card.

WHY (2026-08-13): Jarvis does not talk on Buzz as a peer. The Hermes identity
is admitted to the private loopback channel, but nothing consumes mentions.
Grok posting @Hermes is a black hole unless a human/seat files a kanban card
by hand (the diversion). Kanban stays canonical; this script is only the wire.

Do:
  poll http://localhost:3030 (NOT 127.0.0.1 — community is host-bound)
  if an admitted non-Hermes signer mentioned the Hermes pubkey and Hermes has
    not already reply-tagged that event, create one ready jarvis card
  idempotency-key buzz-hermes-reply-<event_id>
Never:
  print the private key
  execute Buzz content
  create identities/channels/memberships
  treat Buzz as task authority
  auto-complete or auto-reply from this process

Empty stdout = silent (no-agent cron). Any stdout is an alert.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CHANNEL = "3ca63474-e0e5-4485-b10c-e6ea715352ec"
RELAY = "http://localhost:3030"
IDENT_DIR = Path("/home/frank/buzz-bridge-pilot/state/identities")
HERMES_KEY = IDENT_DIR / "hermes.key"
HERMES_PUB = IDENT_DIR / "hermes.pub"
BOARD = "jarvis-os"
ASSIGNEE = "jarvis"
CLI = "/home/frank/.local/bin/buzz"
STATE = Path("/home/frank/dgx-fable-orchestrator/state/jarvis-buzz-mention-router.json")
LIMIT = 40


def _pub(name: str) -> str:
    return (IDENT_DIR / f"{name}.pub").read_text(encoding="utf-8").strip()


def _env() -> dict[str, str]:
    key = HERMES_KEY.read_text(encoding="utf-8").strip()
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise SystemExit("identity file is not one hex private key")
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "BUZZ_PRIVATE_KEY": key,
        "BUZZ_RELAY_URL": RELAY,
        "PATH": os.environ.get("PATH", "/usr/bin"),
    }


def _load_messages() -> list[dict]:
    r = subprocess.run(
        [CLI, "--format", "json", "messages", "get", "--channel", CHANNEL, "--limit", str(LIMIT)],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    if r.returncode != 0:
        print(f"JARVIS_BUZZ_ROUTER: buzz get failed rc={r.returncode} err={(r.stderr or '')[:240]}")
        return []
    data = json.loads(r.stdout or "[]")
    return data if isinstance(data, list) else []


def _mentions_hermes(event: dict, hermes_pub: str) -> bool:
    for tag in event.get("tags") or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p" and tag[1] == hermes_pub:
            return True
    return False


def _hermes_replied(events: list[dict], parent_id: str, hermes_pub: str) -> bool:
    for ev in events:
        if ev.get("pubkey") != hermes_pub:
            continue
        for tag in ev.get("tags") or []:
            if (
                isinstance(tag, list)
                and len(tag) >= 2
                and tag[0] == "e"
                and tag[1] == parent_id
                and (len(tag) < 4 or tag[3] in {"reply", "root", ""})
            ):
                return True
    return False


def _already_routed(event_id: str) -> bool:
    if not STATE.exists():
        return False
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return event_id in (data.get("routed") or {})


def _mark_routed(event_id: str, card_id: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    data = {"routed": {}}
    if STATE.exists():
        try:
            data = json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {"routed": {}}
    data.setdefault("routed", {})[event_id] = card_id
    STATE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _create_card(event: dict, hermes_pub: str) -> str | None:
    eid = event["id"]
    body = (
        "OUTCOME: Post ONE bounded Hermes reply on the EXISTING private Buzz channel. "
        "Do not create identities, channels, or memberships.\n\n"
        f"CHANNEL: {CHANNEL}\n"
        f"RELAY: {RELAY}  (hostname localhost, NEVER 127.0.0.1)\n"
        f"SIGN: {HERMES_KEY}  (never echo)\n"
        f"PARENT: {eid}\n"
        f"PARENT_PUBKEY: {event.get('pubkey')}\n"
        f"PARENT_EXCERPT:\n{(event.get('content') or '')[:800]}\n\n"
        "Use: buzz messages send --channel ... --reply-to PARENT --content ... "
        "--mention <parent pubkey> and the other admitted seats if relevant.\n"
        "Then comment the event_id on this card. No secrets, no approval phrases, "
        "no deploy, no gateway/provider change. Buzz is non-authoritative."
    )
    r = subprocess.run(
        [
            "hermes",
            "kanban",
            "--board",
            BOARD,
            "create",
            f"BUZZ REPLY: Hermes mention {eid[:12]}",
            "--assignee",
            ASSIGNEE,
            "--priority",
            "70",
            "--created-by",
            "jarvis-buzz-mention-router",
            "--idempotency-key",
            f"buzz-hermes-reply-{eid}",
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and "idempoten" not in out.lower() and "already" not in out.lower():
        print(f"JARVIS_BUZZ_ROUTER: create failed rc={r.returncode} {(out or '')[:240]}")
        return None
    card = None
    for tok in out.replace("Created", " ").split():
        if tok.startswith("t_") and len(tok) >= 10:
            card = tok.strip()
            break
    return card or "existing"


def main() -> int:
    if not HERMES_KEY.is_file() or not HERMES_PUB.is_file():
        print("JARVIS_BUZZ_ROUTER: hermes identity missing")
        return 0
    hermes_pub = HERMES_PUB.read_text(encoding="utf-8").strip()
    admitted = {_pub(n) for n in ("hermes", "codex", "grok", "claude")}
    events = _load_messages()
    created = 0
    for ev in events:
        eid = ev.get("id")
        pub = ev.get("pubkey")
        if not eid or pub not in admitted or pub == hermes_pub:
            continue
        if not _mentions_hermes(ev, hermes_pub):
            continue
        if _hermes_replied(events, eid, hermes_pub):
            continue
        if _already_routed(eid):
            continue
        card = _create_card(ev, hermes_pub)
        if card:
            _mark_routed(eid, card)
            created += 1
            print(f"JARVIS_BUZZ_ROUTER: routed {eid[:12]} -> {card}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
