#!/usr/bin/env python3
"""Regression tests for the Hermes session broker POC.

Run:
  python3 scripts/test_session_broker_poc.py
  python3 -m pytest scripts/test_session_broker_poc.py -q

These tests never touch the live Session Bus, Hermes, Obsidian, or any provider
CLI. The broker is driven with an inert recording bus route and a spy adapter
that fails loudly if anything tries to execute.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_broker_poc import (  # noqa: E402
    Capsule,
    Decision,
    DryRunResumeAdapter,
    GuardedSubprocessSessionBusRoute,
    RecordingSessionBusRoute,
    ResumePlan,
    SessionBroker,
    SessionLease,
    parse_inbox,
)

SCRIPT = Path(__file__).with_name("session_broker_poc.py")
FIXTURE = Path(__file__).with_name("session_broker_poc.fixture-inbox.md")
MANAGED = "claude-poc0001"


def block(msg_id: str, to: str, payload: dict | None, *, body: str = "test block") -> str:
    """Build one Session Bus inbox block, optionally carrying a capsule."""
    parts = [
        f"### 2026-07-30T12:00:00Z · id:{msg_id} · from:jarvis · to:{to} · re:broker-poc · ack:requested",
        body,
    ]
    if payload is not None:
        parts.append("```json")
        parts.append(json.dumps(payload, indent=2))
        parts.append("```")
    parts.append("---")
    return "\n".join(parts) + "\n"


def capsule_payload(
    capsule_id: str = "cap-0001",
    session_id: str = MANAGED,
    action: str = "resume",
    provider: str = "claude-code",
    task_ref: str = "t_deadbeef",
) -> dict:
    return {
        "capsule_version": 1,
        "capsule_id": capsule_id,
        "session_id": session_id,
        "action": action,
        "provider": provider,
        "task_ref": task_ref,
        "issued_at": "2026-07-30T12:01:00Z",
    }


class SpyAdapter:
    """Records plan() calls; raises if anything ever tries to execute."""

    def __init__(self) -> None:
        self.calls: list[Capsule] = []

    def plan(self, capsule: Capsule) -> ResumePlan:
        self.calls.append(capsule)
        return DryRunResumeAdapter().plan(capsule)


class BrokerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.inbox = self.root / f"inbox-{MANAGED}.md"
        self.bus = RecordingSessionBusRoute()
        self.adapter = SpyAdapter()

    def write_inbox(self, *blocks: str) -> None:
        self.inbox.write_text("".join(blocks), encoding="utf-8")

    def make_broker(self, **kwargs) -> SessionBroker:
        return SessionBroker(
            managed_session=MANAGED,
            inbox_path=self.inbox,
            state_dir=self.state,
            bus=self.bus,
            adapter=self.adapter,
            **kwargs,
        )

    def bus_texts(self, prefix: str) -> list[str]:
        return [text for _author, text in self.bus.events if text.startswith(prefix)]


class SingleCapsuleTests(BrokerTestBase):
    def test_one_capsule_produces_exactly_one_ack_and_one_lease_decision(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload()))

        outcomes = self.make_broker().process_inbox()

        self.assertEqual([o.decision for o in outcomes], [Decision.ACCEPTED])
        # Exactly one ACK — this is the core acceptance criterion.
        self.assertEqual(len(self.bus_texts("ACK")), 1)
        self.assertEqual(len(self.bus_texts("DONE")), 1)
        self.assertEqual(len(self.bus_texts("CONFLICT")), 0)
        self.assertEqual(len(self.bus_texts("BLOCKED")), 0)
        # Exactly one dispatch decision reached the adapter.
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0].capsule_id, "cap-0001")

    def test_lease_is_released_after_a_successful_broker_run(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload()))
        broker = self.make_broker()
        broker.process_inbox()
        self.assertFalse(broker.lease_path.exists(), "lease must not leak after completion")

    def test_non_capsule_message_is_ignored_entirely(self) -> None:
        self.write_inbox(block("m1", MANAGED, None, body="just a human note"))

        outcomes = self.make_broker().process_inbox()

        self.assertEqual(outcomes, [])
        self.assertEqual(self.bus.events, [])
        self.assertEqual(self.adapter.calls, [])


class IdempotencyTests(BrokerTestBase):
    def test_duplicate_delivery_in_same_run_is_ignored(self) -> None:
        payload = capsule_payload()
        self.write_inbox(block("m1", MANAGED, payload), block("m2", MANAGED, payload))

        outcomes = self.make_broker().process_inbox()

        self.assertEqual(
            [o.decision for o in outcomes], [Decision.ACCEPTED, Decision.DUPLICATE]
        )
        self.assertEqual(len(self.bus_texts("ACK")), 1, "duplicate must not produce a second ACK")
        self.assertEqual(len(self.adapter.calls), 1, "duplicate must not reach the adapter")

    def test_duplicate_delivery_across_runs_is_ignored(self) -> None:
        payload = capsule_payload()
        self.write_inbox(block("m1", MANAGED, payload))
        self.make_broker().process_inbox()

        # Second, independent broker over the same state dir.
        self.bus = RecordingSessionBusRoute()
        self.adapter = SpyAdapter()
        outcomes = self.make_broker().process_inbox()

        self.assertEqual([o.decision for o in outcomes], [Decision.DUPLICATE])
        self.assertEqual(self.bus_texts("ACK"), [])
        self.assertEqual(self.adapter.calls, [])


class LeaseTests(BrokerTestBase):
    def test_busy_leased_session_is_deferred_not_executed(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload()))
        broker = self.make_broker()

        # Simulate a peer already holding the session.
        holder = SessionLease(broker.lease_path, holder="someone-else", ttl_seconds=900)
        self.assertTrue(holder.acquire())

        outcomes = broker.process_inbox()

        self.assertEqual([o.decision for o in outcomes], [Decision.DEFERRED])
        self.assertEqual(outcomes[0].reason, "session_leased")
        self.assertEqual(self.bus_texts("ACK"), [], "a deferred capsule must never be ACKed")
        self.assertEqual(len(self.bus_texts("CONFLICT")), 1)
        self.assertEqual(self.adapter.calls, [], "a deferred capsule must not reach the adapter")
        # The peer's lease must survive untouched.
        self.assertTrue(broker.lease_path.exists())

    def test_deferred_capsule_is_retried_once_the_session_frees_up(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload()))
        broker = self.make_broker()
        holder = SessionLease(broker.lease_path, holder="someone-else", ttl_seconds=900)
        holder.acquire()

        self.assertEqual(broker.process_inbox()[0].decision, Decision.DEFERRED)

        holder.release()
        self.bus = RecordingSessionBusRoute()
        self.adapter = SpyAdapter()
        outcomes = self.make_broker().process_inbox()

        self.assertEqual([o.decision for o in outcomes], [Decision.ACCEPTED])
        self.assertEqual(len(self.bus_texts("ACK")), 1)

    def test_expired_lease_is_taken_over_so_a_crash_cannot_wedge_a_session(self) -> None:
        clock = {"t": 1000.0}
        self.write_inbox(block("m1", MANAGED, capsule_payload()))
        broker = self.make_broker(now=lambda: clock["t"])

        stale = SessionLease(
            broker.lease_path, holder="crashed", ttl_seconds=60, now=lambda: clock["t"]
        )
        self.assertTrue(stale.acquire())

        clock["t"] += 3600  # lease TTL long past
        outcomes = broker.process_inbox()

        self.assertEqual([o.decision for o in outcomes], [Decision.ACCEPTED])

    def test_exclusive_acquire_is_atomic(self) -> None:
        path = self.state / "lease.json"
        first = SessionLease(path, holder="a", ttl_seconds=900)
        second = SessionLease(path, holder="b", ttl_seconds=900)

        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire(), "second holder must not get the lease")

        second.release()  # must be a no-op; b never held it
        self.assertTrue(path.exists(), "release by a non-holder must not delete the lease")

        first.release()
        self.assertFalse(path.exists())


class RejectionTests(BrokerTestBase):
    def test_unknown_session_in_capsule_is_rejected_and_never_dispatched(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload(session_id="grok-someone-else")))

        outcomes = self.make_broker().process_inbox()

        self.assertEqual([o.decision for o in outcomes], [Decision.REJECTED])
        self.assertEqual(outcomes[0].reason, "unknown_session")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.bus_texts("ACK"), [])

    def test_message_addressed_to_another_session_is_rejected(self) -> None:
        self.write_inbox(block("m1", "claude-other", capsule_payload(session_id="claude-other")))

        outcomes = self.make_broker().process_inbox()

        self.assertEqual([o.decision for o in outcomes], [Decision.REJECTED])
        self.assertEqual(outcomes[0].reason, "unknown_session")
        self.assertEqual(self.adapter.calls, [])

    def test_forbidden_action_is_rejected_and_never_dispatched(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload(action="deploy")))

        outcomes = self.make_broker().process_inbox()

        self.assertEqual([o.decision for o in outcomes], [Decision.REJECTED])
        self.assertEqual(outcomes[0].reason, "forbidden_action")
        self.assertEqual(self.adapter.calls, [], "a forbidden action must never reach the adapter")
        self.assertEqual(self.bus_texts("ACK"), [])

    def test_forbidden_provider_is_rejected(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload(provider="rogue-cli")))

        outcomes = self.make_broker().process_inbox()

        self.assertEqual(outcomes[0].reason, "forbidden_provider")
        self.assertEqual(self.adapter.calls, [])

    def test_schema_violations_are_rejected(self) -> None:
        cases = {
            "unsupported_capsule_version": {**capsule_payload(), "capsule_version": 99},
            "missing_or_invalid_field:capsule_id": {**capsule_payload(), "capsule_id": ""},
            "missing_or_invalid_field:task_ref": {
                k: v for k, v in capsule_payload().items() if k != "task_ref"
            },
        }
        for expected_reason, payload in cases.items():
            with self.subTest(reason=expected_reason):
                self.setUp()
                self.write_inbox(block("m1", MANAGED, payload))
                outcomes = self.make_broker().process_inbox()
                self.assertEqual([o.decision for o in outcomes], [Decision.REJECTED])
                self.assertEqual(outcomes[0].reason, expected_reason)
                self.assertEqual(self.adapter.calls, [])

    def test_malformed_json_is_rejected_not_crashed_on(self) -> None:
        self.inbox.write_text(
            "### 2026-07-30T12:00:00Z · id:m1 · from:jarvis · to:%s · re:x · ack:no\n"
            "```json\n{not valid json,,,}\n```\n---\n" % MANAGED,
            encoding="utf-8",
        )
        outcomes = self.make_broker().process_inbox()
        self.assertEqual(outcomes[0].reason, "malformed_json")
        self.assertEqual(self.adapter.calls, [])


class NoExecutionTests(BrokerTestBase):
    def test_adapter_plans_but_never_executes(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload()))
        outcomes = SessionBroker(
            managed_session=MANAGED,
            inbox_path=self.inbox,
            state_dir=self.state,
            bus=self.bus,
            adapter=DryRunResumeAdapter(),
        ).process_inbox()

        plan = outcomes[0].plan
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertFalse(plan.executed, "the POC must never execute a provider CLI")
        self.assertEqual(plan.argv[0], "claude")

    def test_grok_provider_plans_a_grok_resume_without_running_it(self) -> None:
        self.write_inbox(block("m1", MANAGED, capsule_payload(provider="grok")))
        outcomes = SessionBroker(
            managed_session=MANAGED,
            inbox_path=self.inbox,
            state_dir=self.state,
            bus=self.bus,
            adapter=DryRunResumeAdapter(),
        ).process_inbox()

        plan = outcomes[0].plan
        assert plan is not None
        self.assertEqual(plan.argv[0], "grok")
        self.assertFalse(plan.executed)

    def test_live_bus_route_is_not_invoked_by_default(self) -> None:
        """The real route must be explicitly constructed; it is never the default."""
        calls: list[list[str]] = []
        route = GuardedSubprocessSessionBusRoute(
            helper=Path("/nonexistent/session-bus.sh"),
            runner=lambda argv: calls.append(list(argv)),  # type: ignore[arg-type,return-value]
        )
        # Missing helper must fail closed rather than silently no-op.
        with self.assertRaises(FileNotFoundError):
            route.event("x", "y")
        self.assertEqual(calls, [])


class ParserTests(unittest.TestCase):
    def test_parses_headers_and_bodies(self) -> None:
        text = block("m1", MANAGED, None, body="hello") + block("m2", "other", None, body="world")
        messages = parse_inbox(text)
        self.assertEqual([m.message_id for m in messages], ["m1", "m2"])
        self.assertEqual([m.recipient for m in messages], [MANAGED, "other"])
        self.assertIn("hello", messages[0].body)

    def test_ignores_leading_prose_before_the_first_block(self) -> None:
        text = "# Inbox\n\nsome preamble\n\n" + block("m1", MANAGED, None)
        self.assertEqual([m.message_id for m in parse_inbox(text)], ["m1"])


class FixtureEndToEndTests(unittest.TestCase):
    """Drives the documented fixture through the real CLI entrypoint."""

    def test_documented_fixture_behaves_as_its_readme_claims(self) -> None:
        self.assertTrue(FIXTURE.exists(), f"fixture missing: {FIXTURE}")
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--managed-session",
                    MANAGED,
                    "--inbox",
                    str(FIXTURE),
                    "--state-dir",
                    str(Path(tmp) / "state"),
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)

        self.assertFalse(report["live_bus"], "fixture run must not touch the live bus")
        decisions = [(o["decision"], o["capsule_id"], o["reason"]) for o in report["outcomes"]]
        self.assertEqual(
            decisions,
            [
                ("accepted", "cap-0001", None),
                ("duplicate", "cap-0001", "already_handled"),
                ("rejected", "cap-0002", "unknown_session"),
                ("rejected", "cap-0003", "forbidden_action"),
            ],
        )
        acks = [e for e in report["recorded_bus_events"] if e.startswith("ACK")]
        self.assertEqual(len(acks), 1, "exactly one ACK across the whole fixture")
        accepted = report["outcomes"][0]
        self.assertFalse(accepted["executed"])
        self.assertEqual(accepted["planned_argv"][0], "claude")


if __name__ == "__main__":
    unittest.main(verbosity=2)
