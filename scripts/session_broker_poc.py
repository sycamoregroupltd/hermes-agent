#!/usr/bin/env python3
"""Hermes session broker — proof of concept (INERT by default).

Reads structured task capsules from ONE managed Session Bus inbox, validates
them, takes an exclusive per-session lease, records ACK / terminal state through
the EXISTING guarded Session Bus helper, and hands the work to a DRY-RUN resume
adapter that never invokes the Claude or Grok CLI.

Why this shape
--------------
The Session Bus (``Orchestration/sessions/SESSION-BUS.md``, v1.2) is already the
fleet's coordination fabric: per-session ``inbox-<session-id>.md`` conversation
files plus a guarded event route onto the watchable ``orchestrator-sync``
card. This broker deliberately rides that existing protocol rather than opening
a parallel channel — a capsule is an ordinary bus message whose body carries a
fenced ``json`` block. Anything that cannot be parsed as a capsule is left alone
for a human/peer to read, exactly as today.

Scope guards (deliberate — do not relax without independent review)
-------------------------------------------------------------------
* **No provider CLI is ever spawned.** :class:`DryRunResumeAdapter` returns a
  plan describing what *would* run. There is no live adapter in this file, and
  :attr:`ResumePlan.executed` is hard-wired ``False``.
* **The bus is reached only through the existing guarded helper.** The default
  route is :class:`RecordingSessionBusRoute`, which writes nothing at all.
  :class:`GuardedSubprocessSessionBusRoute` shells out to ``session-bus.sh``
  and must be opted into explicitly; it is not exercised by the tests.
* **Actions are a closed allow-list.** :data:`ACTION_ALLOWLIST` gates every
  capsule. Anything outside it is ``REJECTED`` and never dispatched.
* **One managed session.** A capsule addressed to any other session is
  ``REJECTED`` as ``unknown_session``. The broker never acts for a session it
  does not own.
* **No approval authority.** This tool records coordination signals only. It
  makes no A2/A3 decision and carries no credentials (see
  ``Orchestration/approvals-registry.md``).

Run:
  python3 scripts/session_broker_poc.py --help
  python3 scripts/session_broker_poc.py \\
      --managed-session claude-poc0001 \\
      --inbox scripts/session_broker_poc.fixture-inbox.md \\
      --state-dir /tmp/session-broker-poc

Fixture + capsule schema: ``scripts/session_broker_poc.fixtures.md``
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

CAPSULE_VERSION = 1

# Closed allow-list. A capsule naming anything else is rejected, never run.
ACTION_ALLOWLIST = frozenset({"resume"})
PROVIDER_ALLOWLIST = frozenset({"claude-code", "grok"})

DEFAULT_LEASE_TTL_SECONDS = 900

# Canonical guarded helper. Never invoked unless the caller explicitly selects
# the subprocess route.
DEFAULT_BUS_HELPER = Path(
    "/home/frank/obsidian-fleet-vault/Orchestration/sessions/bin/session-bus.sh"
)

# `### <ts> · id:<id> · from:<x> · to:<y> · re:<topic> · ack:<requested|no>`
_HEADER_RE = re.compile(r"^###\s+(?P<rest>.+)$")
_JSON_FENCE_RE = re.compile(r"```json\s*\n(?P<payload>.*?)\n```", re.DOTALL)


class Decision(str, Enum):
    """Terminal classification for one delivered capsule."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    DEFERRED = "deferred"
    REJECTED = "rejected"


@dataclass(frozen=True)
class BusMessage:
    """One parsed block from a Session Bus inbox file."""

    message_id: str
    sender: str
    recipient: str
    body: str


@dataclass(frozen=True)
class Capsule:
    """A validated structured task capsule."""

    capsule_id: str
    session_id: str
    action: str
    provider: str
    task_ref: str
    message_id: str


@dataclass(frozen=True)
class ResumePlan:
    """What a live adapter *would* do. Nothing is executed."""

    provider: str
    session_id: str
    task_ref: str
    argv: tuple[str, ...]
    executed: bool = False


@dataclass(frozen=True)
class Outcome:
    """Result of brokering exactly one delivered capsule."""

    decision: Decision
    message_id: str
    capsule_id: str | None = None
    reason: str | None = None
    plan: ResumePlan | None = None


# ---------------------------------------------------------------------------
# Session Bus routes
# ---------------------------------------------------------------------------


class SessionBusRoute(Protocol):
    """Minimal surface the broker needs from the Session Bus."""

    def event(self, author: str, text: str) -> None:  # pragma: no cover - protocol
        ...


@dataclass
class RecordingSessionBusRoute:
    """Inert default route: records calls in memory, writes nothing anywhere.

    This is what makes the POC safe to run on a live host — the broker's full
    decision path can be exercised without touching Hermes or Obsidian.
    """

    events: list[tuple[str, str]] = field(default_factory=list)

    def event(self, author: str, text: str) -> None:
        self.events.append((author, text))


@dataclass
class GuardedSubprocessSessionBusRoute:
    """Real route — delegates to the existing guarded ``session-bus.sh`` helper.

    Deliberately a thin shell-out: the helper already owns the shared
    ``.SESSION-BUS.lock``, the secret / R3-payload rejection, and the
    event-bridge that writes the watchable card. Reimplementing any of that here
    would fork a guard that review has already accepted.

    Not exercised by the test suite: the tests must never post to the live bus.
    """

    helper: Path = DEFAULT_BUS_HELPER
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess] = field(
        default=lambda argv: subprocess.run(argv, check=True, text=True)
    )

    def event(self, author: str, text: str) -> None:
        if not self.helper.exists():
            raise FileNotFoundError(f"session bus helper not found: {self.helper}")
        self.runner([str(self.helper), "event", "--author", author, "--text", text])


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------


class SessionLease:
    """Exclusive per-session lease backed by an atomic ``O_CREAT|O_EXCL`` file.

    A lease that has outlived its TTL is treated as abandoned and may be taken
    over, so a crashed broker cannot wedge a session forever. That mirrors the
    fail-closed-but-recoverable posture the fleet already uses for locks.
    """

    def __init__(
        self,
        path: Path,
        holder: str,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.holder = holder
        self.ttl_seconds = ttl_seconds
        self._now = now
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_exclusive(self) -> bool:
        payload = json.dumps(
            {
                "holder": self.holder,
                "pid": os.getpid(),
                "acquired_at": self._now(),
                "expires_at": self._now() + self.ttl_seconds,
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return True

    def acquire(self) -> bool:
        """Return True if this broker now holds the lease."""
        if self._write_exclusive():
            self._held = True
            return True

        current = self._read()
        if current is None:
            # Unreadable lease file: treat as held by an unknown owner and defer
            # rather than stomping it.
            return False

        expires_at = current.get("expires_at")
        if isinstance(expires_at, (int, float)) and self._now() >= expires_at:
            # Abandoned lease — take it over.
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            if self._write_exclusive():
                self._held = True
                return True
        return False

    def release(self) -> None:
        if not self._held:
            return
        current = self._read()
        if current is not None and current.get("holder") != self.holder:
            # Someone else's lease — never delete it.
            self._held = False
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._held = False


# ---------------------------------------------------------------------------
# Idempotency ledger
# ---------------------------------------------------------------------------


class SeenLedger:
    """Durable record of terminally-handled capsule ids.

    Only terminal decisions are recorded. A ``DEFERRED`` capsule is intentionally
    NOT recorded so a later run can pick it up once the session frees up.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seen: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            entries = raw.get("seen", {})
            if isinstance(entries, dict):
                self._seen = {str(k): str(v) for k, v in entries.items()}

    def has(self, capsule_id: str) -> bool:
        return capsule_id in self._seen

    def record(self, capsule_id: str, decision: Decision) -> None:
        self._seen[capsule_id] = decision.value
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"seen": self._seen}, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


# ---------------------------------------------------------------------------
# Adapter (dry-run only)
# ---------------------------------------------------------------------------


class ResumeAdapter(Protocol):
    def plan(self, capsule: Capsule) -> ResumePlan:  # pragma: no cover - protocol
        ...


class DryRunResumeAdapter:
    """Builds the argv a live adapter would use — and never runs it.

    Kept as a separate seam so a future live adapter can be reviewed and gated
    on its own merits rather than smuggled in behind this POC.
    """

    def plan(self, capsule: Capsule) -> ResumePlan:
        if capsule.provider == "claude-code":
            argv: tuple[str, ...] = (
                "claude",
                "--resume",
                capsule.session_id,
                "--task",
                capsule.task_ref,
            )
        elif capsule.provider == "grok":
            argv = ("grok", "resume", "--session", capsule.session_id, "--task", capsule.task_ref)
        else:  # pragma: no cover - guarded upstream by PROVIDER_ALLOWLIST
            raise ValueError(f"provider not allow-listed: {capsule.provider}")
        return ResumePlan(
            provider=capsule.provider,
            session_id=capsule.session_id,
            task_ref=capsule.task_ref,
            argv=argv,
            executed=False,
        )


# ---------------------------------------------------------------------------
# Inbox parsing
# ---------------------------------------------------------------------------


def _parse_header(rest: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for chunk in rest.split("·"):
        chunk = chunk.strip()
        if ":" not in chunk:
            continue
        key, _, value = chunk.partition(":")
        fields[key.strip().lower()] = value.strip()
    return fields


def parse_inbox(text: str) -> list[BusMessage]:
    """Parse Session Bus message blocks out of an inbox file."""
    messages: list[BusMessage] = []
    current: dict[str, str] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is None:
            return
        messages.append(
            BusMessage(
                message_id=current.get("id", ""),
                sender=current.get("from", ""),
                recipient=current.get("to", ""),
                body="\n".join(body).strip(),
            )
        )

    for line in text.splitlines():
        header = _HEADER_RE.match(line)
        if header:
            flush()
            current = _parse_header(header.group("rest"))
            body = []
            continue
        if current is None:
            continue
        if line.strip() == "---":
            flush()
            current = None
            body = []
            continue
        body.append(line)

    flush()
    return messages


def extract_capsule(message: BusMessage) -> tuple[Capsule | None, str | None]:
    """Return ``(capsule, None)`` or ``(None, reason)``.

    A block with no fenced json payload is not an error — it is an ordinary
    human bus message and is skipped with reason ``not_a_capsule``.
    """
    match = _JSON_FENCE_RE.search(message.body)
    if not match:
        return None, "not_a_capsule"
    try:
        raw = json.loads(match.group("payload"))
    except ValueError:
        return None, "malformed_json"
    if not isinstance(raw, dict):
        return None, "malformed_json"

    if raw.get("capsule_version") != CAPSULE_VERSION:
        return None, "unsupported_capsule_version"

    required = ("capsule_id", "session_id", "action", "provider", "task_ref")
    for key in required:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, f"missing_or_invalid_field:{key}"

    return (
        Capsule(
            capsule_id=raw["capsule_id"].strip(),
            session_id=raw["session_id"].strip(),
            action=raw["action"].strip(),
            provider=raw["provider"].strip(),
            task_ref=raw["task_ref"].strip(),
            message_id=message.message_id,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------


class SessionBroker:
    """Brokers capsules for exactly one managed session."""

    def __init__(
        self,
        managed_session: str,
        inbox_path: Path,
        state_dir: Path,
        bus: SessionBusRoute,
        adapter: ResumeAdapter | None = None,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.managed_session = managed_session
        self.inbox_path = inbox_path
        self.state_dir = state_dir
        self.bus = bus
        self.adapter = adapter or DryRunResumeAdapter()
        self.lease_ttl_seconds = lease_ttl_seconds
        self._now = now
        self.ledger = SeenLedger(state_dir / f"seen-{managed_session}.json")
        self.lease_path = state_dir / f"lease-{managed_session}.json"

    # -- helpers ---------------------------------------------------------

    def _lease(self) -> SessionLease:
        return SessionLease(
            self.lease_path,
            holder=f"broker:{self.managed_session}:{os.getpid()}",
            ttl_seconds=self.lease_ttl_seconds,
            now=self._now,
        )

    def _reject(self, message: BusMessage, capsule: Capsule | None, reason: str) -> Outcome:
        capsule_id = capsule.capsule_id if capsule else None
        if capsule_id:
            self.ledger.record(capsule_id, Decision.REJECTED)
        self.bus.event(
            self.managed_session,
            f"BLOCKED re:{message.message_id} capsule={capsule_id or 'none'} reason={reason}",
        )
        return Outcome(Decision.REJECTED, message.message_id, capsule_id, reason)

    # -- main entry ------------------------------------------------------

    def process_inbox(self) -> list[Outcome]:
        text = self.inbox_path.read_text(encoding="utf-8")
        outcomes: list[Outcome] = []
        for message in parse_inbox(text):
            outcome = self.process_message(message)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def process_message(self, message: BusMessage) -> Outcome | None:
        capsule, reason = extract_capsule(message)

        if capsule is None:
            if reason == "not_a_capsule":
                # Ordinary human message; not this broker's business.
                return None
            return self._reject(message, None, reason or "invalid_capsule")

        # --- ownership: never act for a session we do not manage ---------
        if message.recipient != self.managed_session:
            return self._reject(message, capsule, "unknown_session")
        if capsule.session_id != self.managed_session:
            return self._reject(message, capsule, "unknown_session")

        # --- closed action / provider allow-lists ------------------------
        if capsule.action not in ACTION_ALLOWLIST:
            return self._reject(message, capsule, "forbidden_action")
        if capsule.provider not in PROVIDER_ALLOWLIST:
            return self._reject(message, capsule, "forbidden_provider")

        # --- idempotency: a re-delivered capsule is silently ignored ------
        if self.ledger.has(capsule.capsule_id):
            return Outcome(Decision.DUPLICATE, message.message_id, capsule.capsule_id, "already_handled")

        # --- exclusive lease ---------------------------------------------
        lease = self._lease()
        if not lease.acquire():
            # Busy session. No ACK (an ACK means accepted), no ledger entry, so
            # the capsule stays eligible for a later run.
            self.bus.event(
                self.managed_session,
                f"CONFLICT re:{message.message_id} capsule={capsule.capsule_id} reason=session_leased",
            )
            return Outcome(Decision.DEFERRED, message.message_id, capsule.capsule_id, "session_leased")

        try:
            self.bus.event(
                self.managed_session,
                f"ACK re:{message.message_id} capsule={capsule.capsule_id} action={capsule.action}",
            )
            plan = self.adapter.plan(capsule)
            self.ledger.record(capsule.capsule_id, Decision.ACCEPTED)
            self.bus.event(
                self.managed_session,
                (
                    f"DONE re:{message.message_id} capsule={capsule.capsule_id} "
                    f"mode=dry-run executed={str(plan.executed).lower()}"
                ),
            )
            return Outcome(Decision.ACCEPTED, message.message_id, capsule.capsule_id, None, plan)
        finally:
            lease.release()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hermes session broker POC (inert: no CLI is invoked, bus route off by default).",
    )
    parser.add_argument("--managed-session", required=True, help="the single session this broker owns")
    parser.add_argument("--inbox", required=True, type=Path, help="path to inbox-<session>.md")
    parser.add_argument(
        "--state-dir",
        required=True,
        type=Path,
        help="directory for the lease + idempotency ledger",
    )
    parser.add_argument(
        "--lease-ttl-seconds",
        type=int,
        default=DEFAULT_LEASE_TTL_SECONDS,
        help=f"lease TTL before it is considered abandoned (default {DEFAULT_LEASE_TTL_SECONDS})",
    )
    parser.add_argument(
        "--emit-to-live-bus",
        action="store_true",
        help=(
            "route ACK/terminal events through the real guarded session-bus.sh helper. "
            "OFF by default; the POC is inert without it."
        ),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    bus: SessionBusRoute
    if args.emit_to_live_bus:
        bus = GuardedSubprocessSessionBusRoute()
    else:
        bus = RecordingSessionBusRoute()

    broker = SessionBroker(
        managed_session=args.managed_session,
        inbox_path=args.inbox,
        state_dir=args.state_dir,
        bus=bus,
        lease_ttl_seconds=args.lease_ttl_seconds,
    )
    outcomes = broker.process_inbox()

    report = {
        "managed_session": args.managed_session,
        "inbox": str(args.inbox),
        "live_bus": bool(args.emit_to_live_bus),
        "outcomes": [
            {
                "decision": o.decision.value,
                "message_id": o.message_id,
                "capsule_id": o.capsule_id,
                "reason": o.reason,
                "planned_argv": list(o.plan.argv) if o.plan else None,
                "executed": o.plan.executed if o.plan else None,
            }
            for o in outcomes
        ],
    }
    if isinstance(bus, RecordingSessionBusRoute):
        report["recorded_bus_events"] = [text for _author, text in bus.events]

    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
