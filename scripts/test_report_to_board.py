#!/usr/bin/env python3
"""Regression tests for report-to-board.py's identical-condition re-fire
suppression (kanban t_4ed34e09).

BUG: dgx-unified-health-probe fires roughly every 13-15min. Whenever the
mechanism-liveness matrix is RED, report-to-board.py filed a brand-new
"[report] dgx-unified-health-probe" card even when the underlying dead_keys
set was byte-identical to the previous card AND that card had already been
independently classified/routed and archived by a human/worker. Since the
BLOCK body embeds a fresh timestamp + volatile ready-backlog ages on every
run, the raw-text digest used for de-dup never repeated, and state[key] is
popped the instant a card closes/archives — so the very next tick (still
the same unchanged condition) had nothing to compare against and filed a
duplicate. 20+ near-duplicate cards in a few hours resulted.

FIX PINNED HERE:
  1. dedup_fingerprint(key, out) extracts only the STABLE cause identifiers
     (mechanism dead_keys, infra check names, crash/forced-release BLOCK
     cause) from dgx-unified-health-probe's BLOCK body, ignoring volatile
     timestamps/ages. Every other RTB_KEY is unaffected (raw text passthrough).
  2. A tombstone state entry (f"{key}:fp") survives card close/archive and
     is only cleared when the condition genuinely goes clean (empty stdout).
  3. When RTB_QUIET_WINDOW_SEC > 0 (opt-in per job) and the fingerprint is
     unchanged within the window, filing/commenting is suppressed entirely
     — no hermes kanban call is made at all.
  4. A CHANGED fingerprint (new/different dead_keys, infra check, or BLOCK
     cause) always fires immediately regardless of the window elapsed —
     fail-visible semantics are never debounced for genuinely new
     information.
  5. RTB_QUIET_WINDOW_SEC defaults to 0 (fully off) so every job that has
     not opted in behaves byte-identically to pre-fix.

Nothing here touches the live board, the deployed script, jobs.json, or any
real Hermes runtime. subprocess.run (the wrapped RTB_SCRIPT invocation) and
the module's `hermes()` kanban-CLI wrapper are both monkeypatched to fakes;
STATE points at a throwaway tmp file per test.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT = Path(__file__).resolve().with_name("report-to-board.py")


def _load_module(rtb_state: Path):
    """Load a fresh module instance with RTB_STATE repointed at rtb_state.

    Fresh per test so module-level STATE (computed once from os.environ at
    import time) never leaks between cases.
    """
    spec = importlib.util.spec_from_file_location("report_to_board_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "os.environ", {"RTB_STATE": str(rtb_state)}, clear=False
    ):
        spec.loader.exec_module(mod)
    return mod


class FakeBoard:
    """Minimal in-memory stand-in for `hermes kanban ...` used by report-to-board.py.

    Tracks every create/comment call so tests can assert exactly how many
    cards/comments were produced — the thing this bug produced too many of.
    """

    def __init__(self):
        self.cards: dict[str, dict] = {}
        self._next_id = 1
        self.create_calls: list[str] = []
        self.comment_calls: list[str] = []

    def _new_id(self) -> str:
        cid = f"t_{self._next_id:08x}"
        self._next_id += 1
        return cid

    def dispatch(self, *a, timeout=90):
        # Mirrors report_to_board.hermes()'s return shape: (rc, combined_out)
        assert a[0] == "kanban"
        assert a[1] == "--board"
        board = a[2]
        verb = a[3]
        if verb == "create":
            title = a[4]
            body_idx = a.index("--body")
            self.create_calls.append(title)
            cid = self._new_id()
            self.cards[cid] = {"status": "blocked", "board": board, "title": title}
            return 0, f"Created {cid}"
        if verb == "show":
            cid = a[4]
            card = self.cards.get(cid)
            if card is None:
                return 1, "not found"
            return 0, f"status: {card['status']}\n"
        if verb == "comment":
            cid = a[-2] if a[-3] != "--author" else a[-2]
            # args: kanban --board B comment --author X <id> <text>
            cid = a[a.index("--author") + 2]
            self.comment_calls.append(cid)
            return 0, f"Comment added to {cid}"
        if verb == "complete":
            cid = a[4]
            if cid in self.cards:
                self.cards[cid]["status"] = "done"
            return 0, "ok"
        if verb == "archive":
            cid = a[4]
            if cid in self.cards:
                self.cards[cid]["status"] = "archived"
            return 0, "ok"
        raise AssertionError(f"unexpected verb {verb!r}")


BLOCK_BODY_TEMPLATE = (
    "\U0001f534 UNIFIED FLEET HEALTH — {ts} — VERDICT: BLOCK\n"
    "\n"
    "## Infra checks failed\n"
    "  - (none)\n"
    "\n"
    "## Mechanism matrix RED\n"
    "  - overall=RED dead=3 warn=0 keys={keys}\n"
    "\n"
    "## Ready backlog telemetry (observability-only, NOT a BLOCK cause)\n"
    "  - jarvis-os: ready_total=12 age_days={age}\n"
    "\n"
    "Single escalation path. Classify + route via devops/os-reviewer.\n"
)


def block_body(keys: list[str], ts: str | None = None, age: float = 1.23) -> str:
    return BLOCK_BODY_TEMPLATE.format(
        ts=ts or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        keys=repr(sorted(keys)),
        age=age,
    )


def run_main(state_path: Path, board: FakeBoard, out: str, rc: int = 1,
             env_extra: dict | None = None):
    """Drive a FRESH module load + main() with subprocess.run/hermes() faked.

    Reloads the module per call (RTB_STATE={state_path} plus env_extra, e.g.
    RTB_QUIET_WINDOW_SEC) because RTB_QUIET_WINDOW_SEC is a module-level
    constant read once at import time — correct in production, where every
    cron tick spawns a brand-new `python3 report-to-board.py` process, so
    faithfully reproducing that here means reloading rather than mutating
    env under an already-imported module.
    """
    env = {
        "RTB_STATE": str(state_path),
        "RTB_SCRIPT": "/fake/script.py",
        "RTB_KEY": "dgx-unified-health-probe",
        "RTB_TITLE": "dgx-unified-health-probe",
        "RTB_BOARD": "jarvis-os",
    }
    if env_extra:
        env.update(env_extra)
    with mock.patch.dict("os.environ", env, clear=False):
        mod = _load_module(state_path)
    fake_completed = SimpleNamespace(stdout=out, returncode=rc)
    with mock.patch.dict("os.environ", env, clear=False), \
         mock.patch.object(mod.subprocess, "run", return_value=fake_completed), \
         mock.patch.object(mod, "hermes", side_effect=board.dispatch):
        return mod.main()


class DedupFingerprintTests(unittest.TestCase):
    """Layer A: the pure fingerprint extraction function in isolation."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="rtb-test-")) / "state.json"
        self.mod = _load_module(tmp)

    def test_same_dead_keys_different_timestamp_same_fingerprint(self):
        a = block_body(["leak-guard", "black-hole-weekly"], ts="2026-09-10T00:00:00+00:00")
        b = block_body(["black-hole-weekly", "leak-guard"], ts="2026-09-10T00:15:00+00:00", age=9.87)
        fp_a = self.mod.dedup_fingerprint("dgx-unified-health-probe", a)
        fp_b = self.mod.dedup_fingerprint("dgx-unified-health-probe", b)
        self.assertEqual(fp_a, fp_b, "unchanged dead_keys set must fingerprint identically")

    def test_changed_dead_keys_different_fingerprint(self):
        a = block_body(["leak-guard", "black-hole-weekly"])
        b = block_body(["leak-guard", "black-hole-weekly", "registered-implies-ticking"])
        fp_a = self.mod.dedup_fingerprint("dgx-unified-health-probe", a)
        fp_b = self.mod.dedup_fingerprint("dgx-unified-health-probe", b)
        self.assertNotEqual(fp_a, fp_b, "a genuinely new dead key must change the fingerprint")

    def test_other_keys_use_raw_text_passthrough(self):
        # Any job that hasn't opted in gets EXACTLY today's behavior: no
        # extraction, the fingerprint IS the raw text.
        out = "arbitrary non-BLOCK output\n"
        self.assertEqual(self.mod.dedup_fingerprint("some-other-job", out), out)

    def test_unrecognized_shape_fails_closed_to_raw_text(self):
        out = "not a BLOCK body at all, no mechanism matrix section\n"
        fp = self.mod.dedup_fingerprint("dgx-unified-health-probe", out)
        self.assertEqual(fp, out, "unparseable body must never be treated as a known-stable fingerprint")

    # os-reviewer round-1 (t_4ed34e09): dedup_fingerprint() used to collapse
    # the crash/forced-release BLOCK-cause signal to a bare boolean
    # (crash=1/forced=1), so a DIFFERENT crashed task or an escalating crash
    # count -- while dead_keys/infra names happened to stay the same --
    # hashed identically and would have been silently suppressed for the
    # whole quiet window. These three tests pin the fix: the fingerprint
    # must change whenever the crashed-task SET or COUNT changes.
    def _crash_body(self, crash_count: int, task_ids: list[str]) -> str:
        lines = "\n".join(
            f"  - sycode-trading/{t}:crashed (status=ready)" for t in task_ids
        )
        return (
            "BLOCK\n\n"
            "## Infra checks failed\n  - (none)\n\n"
            "## Mechanism matrix RED\n  - overall=RED dead=1 warn=0 keys=['leak-guard']\n\n"
            f"## Kanban ACTIVE crashes (last 60m): {crash_count}  <-- BLOCK cause\n{lines}\n"
        )

    def test_same_crash_count_same_task_set_suppressed_as_before(self):
        a = self._crash_body(1, ["t_edf4f31e"])
        b = self._crash_body(1, ["t_edf4f31e"])
        fp_a = self.mod.dedup_fingerprint("dgx-unified-health-probe", a)
        fp_b = self.mod.dedup_fingerprint("dgx-unified-health-probe", b)
        self.assertEqual(fp_a, fp_b, "identical crash count/task set must still fingerprint identically")

    def test_different_crashed_task_same_count_must_not_suppress(self):
        a = self._crash_body(1, ["t_edf4f31e"])
        b = self._crash_body(1, ["t_aaaaaaaa"])
        fp_a = self.mod.dedup_fingerprint("dgx-unified-health-probe", a)
        fp_b = self.mod.dedup_fingerprint("dgx-unified-health-probe", b)
        self.assertNotEqual(
            fp_a, fp_b,
            "a different crashed task id at the same count must never fingerprint "
            "identically -- this was the os-reviewer round-1 bug",
        )

    def test_crash_count_escalation_must_not_suppress(self):
        a = self._crash_body(1, ["t_edf4f31e"])
        b = self._crash_body(5, ["t_edf4f31e", "t_b", "t_c", "t_d", "t_e"])
        fp_a = self.mod.dedup_fingerprint("dgx-unified-health-probe", a)
        fp_b = self.mod.dedup_fingerprint("dgx-unified-health-probe", b)
        self.assertNotEqual(
            fp_a, fp_b,
            "an escalating crash count (1 -> 5 active crashes) must never fingerprint "
            "identically to the single-crash condition",
        )


class QuietWindowSuppressionTests(unittest.TestCase):
    """Layer B: full main() driven twice, hermes()/subprocess.run faked."""

    def setUp(self):
        self.state_path = Path(tempfile.mkdtemp(prefix="rtb-test-")) / "state.json"
        self.board = FakeBoard()

    def test_identical_condition_within_window_files_only_one_card(self):
        out1 = block_body(["leak-guard", "black-hole-weekly"], ts="2026-09-10T00:00:00+00:00")
        rc1 = run_main(self.state_path, self.board, out1, env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})
        self.assertEqual(len(self.board.create_calls), 1, "first fire must file exactly one card")
        self.assertEqual(rc1, 1)

        # Re-run repeatedly (simulating the 13-15min cron cadence) with the
        # SAME dead_keys but a fresh timestamp/age each time, as the real
        # probe emits every tick.
        for i in range(5):
            out_n = block_body(
                ["leak-guard", "black-hole-weekly"],
                ts=f"2026-09-10T00:{15 + i * 13:02d}:00+00:00",
                age=1.0 + i,
            )
            run_main(self.state_path, self.board, out_n, env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})

        self.assertEqual(
            len(self.board.create_calls), 1,
            f"quiet window must suppress all re-fires of the unchanged condition, "
            f"got {len(self.board.create_calls)} cards",
        )
        self.assertEqual(len(self.board.comment_calls), 0,
                          "unchanged condition must not even post a comment while suppressed")

    def test_identical_condition_survives_human_archiving_interim_card(self):
        """This is the EXACT root cause: state[key] is popped on archive, so
        the next tick had nothing to compare against pre-fix. The tombstone
        must survive that pop and still suppress the next identical fire."""
        out1 = block_body(["leak-guard"], ts="2026-09-10T00:00:00+00:00")
        run_main(self.state_path, self.board, out1, env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})
        self.assertEqual(len(self.board.create_calls), 1)
        card_id = next(iter(self.board.cards))
        # Human/worker archives the card mid-incident (observed real pattern:
        # classified as duplicate/already-tracked and archived directly).
        self.board.cards[card_id]["status"] = "archived"

        out2 = block_body(["leak-guard"], ts="2026-09-10T00:15:00+00:00")
        run_main(self.state_path, self.board, out2, env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})
        self.assertEqual(
            len(self.board.create_calls), 1,
            "an archived card for an unchanged condition must NOT trigger a new card "
            "within the quiet window — this is the exact t_4ed34e09 regression",
        )

    def test_changed_dead_keys_fires_immediately_even_inside_window(self):
        out1 = block_body(["leak-guard"], ts="2026-09-10T00:00:00+00:00")
        run_main(self.state_path, self.board, out1, env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})
        self.assertEqual(len(self.board.create_calls), 1)
        card_id = next(iter(self.board.cards))
        self.board.cards[card_id]["status"] = "archived"

        # A genuinely NEW key appears seconds later — must not be debounced.
        out2 = block_body(["leak-guard", "registered-implies-ticking"],
                           ts="2026-09-10T00:00:05+00:00")
        run_main(self.state_path, self.board, out2, env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})
        self.assertEqual(
            len(self.board.create_calls), 2,
            "a changed dead_keys set must file a new card immediately, "
            "regardless of the quiet window",
        )

    def test_feature_disabled_by_default_preserves_legacy_duplicate_behavior(self):
        """RTB_QUIET_WINDOW_SEC unset (0): must reproduce the ORIGINAL bug
        behavior exactly, proving this fix is strictly opt-in and does not
        change any job that hasn't been switched on."""
        out1 = block_body(["leak-guard"], ts="2026-09-10T00:00:00+00:00")
        run_main(self.state_path, self.board, out1)  # no RTB_QUIET_WINDOW_SEC -> defaults to 0
        self.assertEqual(len(self.board.create_calls), 1)
        card_id = next(iter(self.board.cards))
        self.board.cards[card_id]["status"] = "archived"

        out2 = block_body(["leak-guard"], ts="2026-09-10T00:15:00+00:00")
        run_main(self.state_path, self.board, out2)
        self.assertEqual(
            len(self.board.create_calls), 2,
            "with the feature off, an archived card for the same condition "
            "must reproduce the pre-fix duplicate-filing behavior",
        )

    def test_condition_clearing_drops_tombstone_so_recurrence_refires(self):
        out1 = block_body(["leak-guard"], ts="2026-09-10T00:00:00+00:00")
        run_main(self.state_path, self.board, out1, env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})
        self.assertEqual(len(self.board.create_calls), 1)
        card_id = next(iter(self.board.cards))
        self.board.cards[card_id]["status"] = "done"

        # Condition genuinely clears: empty stdout, rc=0.
        rc_clean = run_main(self.state_path, self.board, "", rc=0,
                             env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})
        self.assertEqual(rc_clean, 0)

        state = json.loads(self.state_path.read_text())
        self.assertNotIn("dgx-unified-health-probe:fp", state,
                          "a clean run must drop the tombstone so a later recurrence "
                          "of the SAME dead_keys is not wrongly suppressed as stale")

        # Same condition recurs later (still "inside" what would have been
        # the old window) — must fire, because the tombstone was cleared.
        out3 = block_body(["leak-guard"], ts="2026-09-10T00:30:00+00:00")
        run_main(self.state_path, self.board, out3, env_extra={"RTB_QUIET_WINDOW_SEC": "14400"})
        self.assertEqual(
            len(self.board.create_calls), 2,
            "a genuine recurrence after the condition cleared must file a new card",
        )


if __name__ == "__main__":
    unittest.main()
