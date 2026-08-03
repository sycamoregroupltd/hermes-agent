#!/usr/bin/env python3
"""Hermetic tests for frank_gate_premise_probe (jarvis-os/t_e911f789).

No live board access: every test builds a throwaway boards dir. The canary
test replays the exact t_4776f5c9 shape (cited-dead reviewer that kept
completing tasks) and asserts RETIRE-ELIGIBLE.

Run: python3 -m pytest scripts/test_frank_gate_premise_probe.py -q
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent
MODULE_PATH = REPO / "frank_gate_premise_probe.py"
spec = importlib.util.spec_from_file_location("frank_gate_premise_probe", MODULE_PATH)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)

NOW = 1_800_000_000
RAISE = NOW - 24 * 3600          # gate raised 24h ago
AFTER = RAISE + 3 * 3600         # activity 3h after the raise
BEFORE = RAISE - 3 * 3600        # activity 3h before the raise

SCHEMA = [
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
        block_kind TEXT, created_at INTEGER, completed_at INTEGER, result TEXT
    )""",
    "CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
    "author TEXT, body TEXT, created_at INTEGER)",
    "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
    "run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)",
]


# --------------------------------------------------------------------------
# fixtures / builders
# --------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path):
    boards = tmp_path / "boards"
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    for board in ("jarvis-os", "sycode-trading"):
        d = boards / board
        d.mkdir(parents=True)
        con = sqlite3.connect(d / "kanban.db")
        for stmt in SCHEMA:
            con.execute(stmt)
        con.commit()
        con.close()
    return {"boards": boards, "profiles": profiles}


def mkprofile(env, name: str) -> None:
    (env["profiles"] / name).mkdir(exist_ok=True)


def _con(env, board: str) -> sqlite3.Connection:
    return sqlite3.connect(env["boards"] / board / "kanban.db")


def add_gate(env, board: str, task_id: str, title: str, body: str,
             *, created_at: int = RAISE, block_kind: str = "frank_gate",
             status: str = "blocked", assignee: str = "operator") -> None:
    con = _con(env, board)
    con.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, title, body, assignee, status, block_kind, created_at, None, ""),
    )
    con.commit()
    con.close()


def add_done(env, board: str, task_id: str, assignee: str, completed_at,
             *, title: str = "work") -> None:
    con = _con(env, board)
    created = completed_at - 60 if isinstance(completed_at, int) else RAISE
    con.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, title, "", assignee, "done", "", created, completed_at, ""),
    )
    con.commit()
    con.close()


def add_open(env, board: str, task_id: str, assignee: str, status: str = "blocked") -> None:
    con = _con(env, board)
    con.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "stuck work", "", assignee, status, "", RAISE - 3600, None, ""),
    )
    con.commit()
    con.close()


def add_comment(env, board: str, task_id: str, body: str, created_at: int = AFTER) -> None:
    con = _con(env, board)
    con.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?,?,?,?)",
        (task_id, "someone", body, created_at),
    )
    con.commit()
    con.close()


def run(env, **kw):
    return probe.build_plans(
        boards=kw.pop("boards", ("jarvis-os", "sycode-trading")),
        boards_dir=env["boards"],
        profiles_dir=env["profiles"],
        **kw,
    )


def only(plans, task_id):
    return next(p for p in plans if p["task_id"] == task_id)


# --------------------------------------------------------------------------
# THE CANARY — would have caught t_4776f5c9
# --------------------------------------------------------------------------


T_4776F5C9_TITLE = (
    "OPERATOR: start trading-risk-reviewer gateway — sole cause of fleet BLOCK "
    "via sycode-trading/t_4b1902c7"
)
T_4776F5C9_BODY = """CEO ROUTE (elon governance cycle).

## Evidence
- unified-health-probe-39c29d42 @16:48 -> VERDICT: BLOCK, single ACTIVE cause = sycode-trading/t_4b1902c7 crashed while status=running.
- Merge blocked ONLY on independent verdict from trading-risk-reviewer on child t_24c405ba.
- `hermes profile list` this cycle: trading-risk-reviewer = STOPPED. Fleet-wide only jarvis, jarvis-os-pm, jarvis-voice are running.

## Gates
Runtime-service start only. No merge, deploy, DB/schema change, credential access, or live-trading action.
"""


def test_canary_t_4776f5c9_is_retire_eligible(env):
    """The reference incident: cited-dead reviewer kept completing tasks."""
    for name in ("trading-risk-reviewer", "jarvis", "jarvis-os-pm", "jarvis-voice"):
        mkprofile(env, name)
    add_gate(env, "jarvis-os", "t_4776f5c9", T_4776F5C9_TITLE, T_4776F5C9_BODY)

    # the cited-dead reviewer completed 21 tasks AFTER the raise
    for i in range(19):
        add_done(env, "sycode-trading", f"t_bu11{i:04d}", "trading-risk-reviewer", AFTER + i * 60)
    add_done(env, "sycode-trading", "t_0e693411", "trading-risk-reviewer", RAISE + 8 * 3600 + 131)
    # the two cited-stuck cards finished too
    add_done(env, "sycode-trading", "t_4b1902c7", "trading-devops", RAISE + 4 * 3600)
    add_done(env, "sycode-trading", "t_24c405ba", "trading-risk-reviewer", RAISE + 4 * 3600 + 600)
    # the cited BLOCK probe re-ran clean
    add_comment(env, "jarvis-os", "t_4776f5c9",
                "unified-health-probe-39c29d42 @20:18 now VERDICT: PASS (mechanism=GREEN)")

    plan = only(run(env), "t_4776f5c9")
    assert plan["disposition"] == "RETIRE-ELIGIBLE", plan["reason"]

    kinds = {p["kind"] for p in plan["premises"]}
    assert kinds == {"profile-down", "task-stuck", "probe-block"}
    assert all(p["contradicted"] for p in plan["premises"])

    blob = "\n".join(ev for p in plan["premises"] for ev in p["evidence"])
    assert "trading-risk-reviewer completed 21 task(s)" in blob
    assert "t_0e693411" in blob
    assert "t_4b1902c7 is status=done" in blob


def test_canary_running_profiles_are_not_read_as_down(env):
    """'only jarvis, jarvis-os-pm, jarvis-voice are running; X is stopped'.

    The running trio must NOT be extracted as down-premises just because a
    'stopped' cue appears later in the same sentence. This was a real false
    positive during development.
    """
    for name in ("trading-risk-reviewer", "jarvis", "jarvis-os-pm", "jarvis-voice"):
        mkprofile(env, name)
    add_gate(env, "jarvis-os", "t_4776f5c9", T_4776F5C9_TITLE, T_4776F5C9_BODY)
    plan = only(run(env), "t_4776f5c9")
    down = {p["subject"] for p in plan["premises"] if p["kind"] == "profile-down"}
    assert down == {"trading-risk-reviewer"}


def test_card_own_thread_is_not_independent_corroboration(env):
    """A card cannot corroborate itself.

    The t_4776f5c9 post-mortem explicitly warned that the 'probe flipped
    BLOCK->PASS' line was only ever visible in the card's OWN comment thread.
    A probe that accepts that as disproof is circular.
    """
    add_gate(env, "jarvis-os", "t_circ01", "unblock lane",
             "unified-health-probe-abc123 @16:48 -> VERDICT: BLOCK, cause unknown.")
    add_comment(env, "jarvis-os", "t_circ01",
                "unified-health-probe-abc123 now VERDICT: PASS (mechanism=GREEN)")
    plan = only(run(env), "t_circ01")
    assert plan["disposition"] == "PARTIAL-RESCOPE"
    assert not plan["premises"][0]["contradicted"]
    assert "cannot corroborate itself" in plan["premises"][0]["evidence"][0]


def test_probe_flip_on_another_card_is_independent(env):
    """The same verdict flip observed on a DIFFERENT card does count."""
    add_gate(env, "jarvis-os", "t_circ02", "unblock lane",
             "unified-health-probe-abc123 @16:48 -> VERDICT: BLOCK, cause unknown.")
    add_comment(env, "jarvis-os", "t_other99",
                "unified-health-probe-abc123 now VERDICT: PASS (mechanism=GREEN)")
    plan = only(run(env), "t_circ02")
    assert plan["disposition"] == "RETIRE-ELIGIBLE"
    assert plan["premises"][0]["independent"] is True


# --------------------------------------------------------------------------
# negative cases — the probe must NOT retire these
# --------------------------------------------------------------------------


def test_still_dead_profile_is_not_retired(env):
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_dead01", "start os-reviewer",
             "os-reviewer gateway is stopped and blocks the lane.")
    add_done(env, "jarvis-os", "t_old01", "os-reviewer", BEFORE)  # pre-raise only
    plan = only(run(env), "t_dead01")
    assert plan["disposition"] == "PARTIAL-RESCOPE"
    assert not plan["premises"][0]["contradicted"]


def test_below_threshold_activity_does_not_falsify(env):
    """One stray completion is noise, not proof the gateway is healthy."""
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_thresh", "start os-reviewer",
             "os-reviewer gateway is stopped.")
    add_done(env, "jarvis-os", "t_one", "os-reviewer", AFTER)
    plan = only(run(env), "t_thresh")
    assert plan["disposition"] == "PARTIAL-RESCOPE"
    plan2 = only(run(env, min_completions=1), "t_thresh")
    assert plan2["disposition"] == "RETIRE-ELIGIBLE"


def test_pure_authorization_gate_is_hold_no_premise(env):
    """Deploy/credential asks are judgement, not falsifiable claims."""
    add_gate(env, "sycode-trading", "t_auth01",
             "Frank/A3 runtime gate: install/repoint/deploy PR #595 consumers",
             "Requires Frank explicit authorization before merge PR #595 and repoint.")
    plan = only(run(env), "t_auth01")
    assert plan["disposition"] == "HOLD-NO-PREMISE"
    assert plan["premises"] == []


def test_independent_authorization_survives_falsified_premise(env):
    """Falsifying the factual half must not open an unrelated Frank gate."""
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_mixed",
             "unblock the review lane",
             "os-reviewer gateway is stopped. Separately this requires Frank "
             "explicit authorization to chmod the profiles directory.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_act{i}", "os-reviewer", AFTER + i * 60)
    plan = only(run(env), "t_mixed")
    assert plan["disposition"] == "HOLD-AUTHORIZATION"
    assert "chmod" in plan["reason"]


def test_authorization_on_the_falsified_subject_does_not_block_retirement(env):
    """'start the gateway that is down' is moot once 'it is down' is false."""
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_moot",
             "OPERATOR: start os-reviewer gateway",
             "os-reviewer gateway is stopped. Requires Frank to start os-reviewer.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_mo{i}", "os-reviewer", AFTER + i * 60)
    plan = only(run(env), "t_moot")
    assert plan["disposition"] == "RETIRE-ELIGIBLE"


def test_denial_prose_does_not_create_an_authorization_gate(env):
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_denial", "start os-reviewer",
             "os-reviewer gateway is stopped. No credential access, no live "
             "trading, no production deploy is involved.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_dn{i}", "os-reviewer", AFTER + i * 60)
    plan = only(run(env), "t_denial")
    assert plan["disposition"] == "RETIRE-ELIGIBLE"


def test_cited_stuck_card_still_stuck_is_not_retired(env):
    add_gate(env, "jarvis-os", "t_st01", "unblock lane",
             "sycode-trading/t_f102ed is crashed while status=running.")
    add_open(env, "sycode-trading", "t_f102ed", "trading-devops")
    plan = only(run(env), "t_st01")
    assert plan["disposition"] == "PARTIAL-RESCOPE"


def test_card_completed_before_the_raise_does_not_falsify(env):
    """Only activity AFTER the raise can contradict the premise."""
    add_gate(env, "jarvis-os", "t_pre01", "unblock lane",
             "sycode-trading/t_ea111a is crashed while status=running.")
    add_done(env, "sycode-trading", "t_ea111a", "trading-devops", BEFORE)
    plan = only(run(env), "t_pre01")
    assert plan["disposition"] == "PARTIAL-RESCOPE"
    assert "NOT contradicted" in plan["premises"][0]["evidence"][0]


def test_self_reference_is_not_a_premise(env):
    add_gate(env, "jarvis-os", "t_self01", "this card t_self01 is blocked",
             "t_self01 is stuck and blocked.")
    plan = only(run(env), "t_self01")
    assert plan["premises"] == []


def test_non_frank_gate_cards_are_not_scanned(env):
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_nk01", "x", "os-reviewer gateway is stopped.",
             block_kind="needs_input")
    assert run(env) == []


# --------------------------------------------------------------------------
# corruption tolerance (real live-board condition)
# --------------------------------------------------------------------------


def test_text_completed_at_neither_crashes_nor_fakes_evidence(env):
    """jarvis-os has rows with completed_at = the literal string '%s'.

    SQLite sorts TEXT above every INTEGER, so an unfiltered '> raise' compare
    would count those as post-raise completions and manufacture a retirement.
    """
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_corrupt", "start os-reviewer",
             "os-reviewer gateway is stopped.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_bad{i}", "os-reviewer", "%s")
    plan = only(run(env), "t_corrupt")
    assert plan["disposition"] == "PARTIAL-RESCOPE"
    assert "only 0 completion(s)" in plan["premises"][0]["evidence"][0]


def test_epoch_of_coerces_dirty_values():
    assert probe.epoch_of(123) == 123
    assert probe.epoch_of("456") == 456
    assert probe.epoch_of("%s") == 0
    assert probe.epoch_of(None) == 0


# --------------------------------------------------------------------------
# emitted text safety
# --------------------------------------------------------------------------


def test_generated_comment_never_carries_the_auto_unblock_token(env):
    """apply_approvals() LIKE-matches that token anywhere in any comment."""
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_tok01", "start os-reviewer",
             "os-reviewer gateway is stopped.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_tk{i}", "os-reviewer", AFTER + i * 60)
    plan = only(run(env), "t_tok01")
    body = probe.retirement_comment(plan, authorized_by="t_e911f789")
    assert not probe.APPROVAL_TOKEN_RE.search(body)
    assert probe.RETIRED_MARKER in body
    assert probe.MARKER in body


def test_approval_token_guard_raises(env):
    with pytest.raises(probe.ApprovalTokenLeak):
        probe.assert_no_approval_token("prose ... REVIEW_VERDICT=APPROVED ... prose")


def test_consolidated_note_is_single_and_lists_every_bucket(env):
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_r1", "start os-reviewer", "os-reviewer gateway is stopped.")
    add_gate(env, "sycode-trading", "t_h1", "Frank gate",
             "Requires Frank explicit authorization to deploy to prod.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_c{i}", "os-reviewer", AFTER + i * 60)
    plans = run(env)
    note = probe.consolidated_note(plans, generated_at="2026-08-03T00:00:00+00:00")
    assert note.count("FALSIFIED-FRANK_GATE RETIREMENT SWEEP") == 1
    assert "t_r1" in note and "t_h1" in note
    assert "retire_eligible=1" in note


# --------------------------------------------------------------------------
# write gating
# --------------------------------------------------------------------------


def test_writes_require_authorization(env, capsys):
    rc = probe.main(["--boards-dir", str(env["boards"]),
                     "--profiles-dir", str(env["profiles"]),
                     "--apply-retire"])
    assert rc == 2
    assert "--authorized-by" in capsys.readouterr().err


def test_dry_run_mutates_nothing(env):
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_dry", "start os-reviewer", "os-reviewer gateway is stopped.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_dc{i}", "os-reviewer", AFTER + i * 60)
    probe.main(["--boards-dir", str(env["boards"]),
                "--profiles-dir", str(env["profiles"]), "--json"])
    con = _con(env, "jarvis-os")
    assert con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 0
    assert con.execute("SELECT status FROM tasks WHERE id='t_dry'").fetchone()[0] == "blocked"
    con.close()


def test_apply_comments_does_not_change_status(env):
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_cm", "start os-reviewer", "os-reviewer gateway is stopped.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_cc{i}", "os-reviewer", AFTER + i * 60)
    plan = only(run(env), "t_cm")
    assert probe.apply_plan(plan, boards_dir=env["boards"],
                            authorized_by="t_e911f789", retire=False) == "comment-added"
    con = _con(env, "jarvis-os")
    assert con.execute("SELECT status FROM tasks WHERE id='t_cm'").fetchone()[0] == "blocked"
    assert con.execute("SELECT COUNT(*) FROM task_comments WHERE task_id='t_cm'").fetchone()[0] == 1
    con.close()


def test_apply_retire_sets_done_with_marker_and_is_idempotent(env):
    mkprofile(env, "os-reviewer")
    add_gate(env, "jarvis-os", "t_rt", "start os-reviewer", "os-reviewer gateway is stopped.")
    for i in range(5):
        add_done(env, "jarvis-os", f"t_rc{i}", "os-reviewer", AFTER + i * 60)
    plan = only(run(env), "t_rt")
    assert probe.apply_plan(plan, boards_dir=env["boards"],
                            authorized_by="t_e911f789", retire=True) == "retired"
    con = _con(env, "jarvis-os")
    status, result = con.execute(
        "SELECT status, result FROM tasks WHERE id='t_rt'").fetchone()
    assert status == "done"
    assert probe.RETIRED_MARKER in result
    assert con.execute("SELECT COUNT(*) FROM task_comments WHERE task_id='t_rt'").fetchone()[0] == 1
    con.close()

    # second pass must not double-comment
    assert probe.apply_plan(plan, boards_dir=env["boards"],
                            authorized_by="t_e911f789", retire=True) == "already-present"
    con = _con(env, "jarvis-os")
    assert con.execute("SELECT COUNT(*) FROM task_comments WHERE task_id='t_rt'").fetchone()[0] == 1
    con.close()


def test_hold_cards_are_never_mutated_even_when_applying(env):
    add_gate(env, "sycode-trading", "t_hold9", "Frank gate",
             "Requires Frank explicit authorization to deploy to prod.")
    plan = only(run(env), "t_hold9")
    assert probe.apply_plan(plan, boards_dir=env["boards"],
                            authorized_by="t_e911f789", retire=True) == "skipped-not-eligible"
    con = _con(env, "sycode-trading")
    assert con.execute("SELECT status FROM tasks WHERE id='t_hold9'").fetchone()[0] == "blocked"
    assert con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 0
    con.close()
