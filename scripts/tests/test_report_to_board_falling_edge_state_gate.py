#!/usr/bin/env python3
"""RED/GREEN acceptance harness for t_06b884a5: report-to-board.py must not
treat falling-edge silence as a clear.

Defect: report-to-board.py's close path (`if not out:`) treated ANY empty
stdout from the wrapped job as "condition cleared" and completed+archived
the keyed card, including blocked cards. nous_balance_watchdog.py (and the
general falling-edge shape shared by position_age_watchdog.py /
dgx_data_freshness_probe.py) alerts once on the falling edge, then goes
SILENT while the bad condition persists (default 24h re-remind dedup).
Empty stdout during that dedup window is "still low", not "healthy" — but
the old code could not tell the difference and auto-closed anyway.

Live false-clear 2026-09-05T16:25:01Z: RTB archived t_141d9320 while the
watchdog's own state file still had usable=0.0 / threshold=5.0. Voice-line
spend-gate dropped.

This harness is HERMETIC. It never touches a live board or the live RTB
state file. It fakes the `hermes` CLI as a small Python script on PATH that
reads/writes a temp JSON "board" so report-to-board.py's real subprocess
calls exercise the real close-path logic against a controlled fixture.

It loads TWO module copies:
  * the pre-change backup (scripts/backups/*.prechange-t_06b884a5-*) -> RED
  * the current live-tree-shaped script (report-to-board.py in this
    worktree)                                                        -> GREEN
and runs each against byte-identical fixtures, so the RED evidence is a real
observed failure of the old code, not a claim about it.

Scenarios
  A  silent still-low     falling-edge state file present (usable<threshold);
                           empty stdout. RED completes+archives the blocked
                           card. GREEN posts a STILL ACTIVE comment and
                           leaves the card blocked, untouched.
  B  genuine recovery      state file absent (clear_state() ran); empty
                           stdout. Both RED and GREEN close+archive — the
                           fix must not break real recovery.
  C  JWT/usable-None hiccup  same as A: nous_balance_watchdog.py's own
                           contract is to PRESERVE the state file (not
                           clear it) on a transient JWT/account-info read
                           failure, so from RTB's perspective it is
                           indistinguishable from "still low" and must not
                           close either.
  D  no RTB_STATE_FILE set  job that never opts in keeps ORIGINAL
                           close-on-silence behavior unchanged (regression
                           guard on jobs.json's 40+ other RTB shims that
                           don't set RTB_STATE_FILE).
"""
import importlib.machinery
import importlib.util
import glob
import json
import os
import stat
import sys
import tempfile
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
LIVE = SCRIPTS / "report-to-board.py"
PRECHANGE = sorted(glob.glob(str(SCRIPTS / "backups" / "*.prechange-t_06b884a5-*")))

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
STORE = os.environ["FAKE_KANBAN_STORE"]
LOG = os.environ["FAKE_HERMES_LOG"]
store = json.load(open(STORE)) if os.path.exists(STORE) else {}
args = sys.argv[1:]
with open(LOG, "a") as f:
    f.write(json.dumps(args) + "\\n")

def board_idx(a):
    return a.index("--board") + 1 if "--board" in a else None

if args[:1] == ["kanban"]:
    bi = board_idx(args)
    board = args[bi] if bi is not None else None
    rest = args[bi + 1:] if bi is not None else args[1:]
    verb = rest[0]
    if verb == "show":
        card_id = rest[1]
        card = store.get(card_id)
        if card is None:
            print("not found", file=sys.stderr)
            sys.exit(1)
        print(f"status: {card['status']}")
        sys.exit(0)
    elif verb == "complete":
        card_id = rest[1]
        if card_id in store:
            store[card_id]["status"] = "done"
        json.dump(store, open(STORE, "w"))
        sys.exit(0)
    elif verb == "archive":
        card_id = rest[1]
        if card_id in store:
            store[card_id]["status"] = "archived"
        json.dump(store, open(STORE, "w"))
        sys.exit(0)
    elif verb == "comment":
        # comment --author X <card_id> <text>
        card_id = rest[2]
        store.setdefault(card_id, {}).setdefault("comments", []).append(rest[3])
        json.dump(store, open(STORE, "w"))
        sys.exit(0)
    elif verb == "create":
        print("t_deadbeef created")
        sys.exit(0)
sys.exit(1)
"""

failures = []


def check(label, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(label)
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None, f"could not build a module spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        hermes_path = self.bin_dir / "hermes"
        hermes_path.write_text(FAKE_HERMES)
        hermes_path.chmod(hermes_path.stat().st_mode | stat.S_IEXEC)
        self.store_path = tmp / "board.json"
        self.log_path = tmp / "hermes_calls.log"
        self.noop_script = tmp / "noop.py"
        self.noop_script.write_text("#!/usr/bin/env python3\n")  # silent, exit 0
        self.rtb_state_path = tmp / "report-to-board.json"

    def seed_board(self, card_id, status):
        json.dump({card_id: {"status": status}}, open(self.store_path, "w"))

    def seed_rtb_rec(self, key, card_id, board="jarvis-os"):
        json.dump({key: {"card_id": card_id, "digest": "x", "board": board, "at": "t0"}},
                   open(self.rtb_state_path, "w"))

    def board(self):
        return json.load(open(self.store_path)) if self.store_path.exists() else {}

    def calls(self):
        if not self.log_path.exists():
            return []
        return [json.loads(l) for l in self.log_path.read_text().splitlines() if l.strip()]

    def run(self, mod, key, state_file=None, board="jarvis-os"):
        env = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "RTB_SCRIPT": str(self.noop_script),
            "RTB_KEY": key,
            "RTB_TITLE": key,
            "RTB_BOARD": board,
            "RTB_STATE": str(self.rtb_state_path),
            "FAKE_KANBAN_STORE": str(self.store_path),
            "FAKE_HERMES_LOG": str(self.log_path),
        }
        if state_file is not None:
            env["RTB_STATE_FILE"] = str(state_file)
        old_env = dict(os.environ)
        old_argv = sys.argv[:]
        try:
            os.environ.clear()
            os.environ.update(env)
            sys.argv = ["report-to-board.py"]
            # STATE is resolved from os.environ at MODULE IMPORT time (module
            # loaded once, reused across scenarios), so re-point it explicitly
            # per run instead of relying on a stale import-time value.
            mod.STATE = Path(env["RTB_STATE"])
            return mod.main()
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            sys.argv = old_argv


def scenario_A_silent_still_low(mod, label):
    """State file present (still low/JWT-preserved) + blocked card + empty
    stdout must NOT complete/archive."""
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td))
        card_id = "t_141d9320"
        h.seed_board(card_id, "blocked")
        h.seed_rtb_rec("nous-balance-watchdog", card_id)
        state_file = Path(td) / "nous_balance_watchdog.first_seen.json"
        state_file.write_text(json.dumps({"usable": 0.0, "threshold": 5.0}))
        h.run(mod, "nous-balance-watchdog", state_file=state_file)
        status = h.board()[card_id]["status"]
        closed = status in ("done", "archived")
        check(f"{label}: scenario A silent-still-low does NOT close blocked card",
              not closed, f"status={status}")
        return not closed


def scenario_B_genuine_recovery(mod, label):
    """State file absent (clear_state() ran) + blocked card + empty stdout
    MUST still close+archive — the fix must not break real recovery."""
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td))
        card_id = "t_recover01"
        h.seed_board(card_id, "blocked")
        h.seed_rtb_rec("nous-balance-watchdog", card_id)
        state_file = Path(td) / "nous_balance_watchdog.first_seen.json"
        # deliberately NOT created: clear_state() unlinked it
        h.run(mod, "nous-balance-watchdog", state_file=state_file)
        status = h.board()[card_id]["status"]
        closed = status == "archived"
        check(f"{label}: scenario B genuine-recovery DOES close+archive",
              closed, f"status={status} calls={h.calls()}")
        return closed


def scenario_C_jwt_hiccup_preserves_state(mod, label):
    """usable-None JWT hiccup: nous_balance_watchdog.py's own contract is to
    leave the state file untouched (not clear it). From RTB's perspective
    this is identical to scenario A: state file present => do not close."""
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td))
        card_id = "t_jwthiccup"
        h.seed_board(card_id, "blocked")
        h.seed_rtb_rec("nous-balance-watchdog", card_id)
        state_file = Path(td) / "nous_balance_watchdog.first_seen.json"
        # Prior low-balance state preserved verbatim by the watchdog's own
        # sys.exit(0) fast path on usable is None.
        state_file.write_text(json.dumps({"usable": 0.0, "threshold": 5.0,
                                           "last_seen": "prior-hiccup"}))
        h.run(mod, "nous-balance-watchdog", state_file=state_file)
        status = h.board()[card_id]["status"]
        closed = status in ("done", "archived")
        check(f"{label}: scenario C JWT/usable-None silence does NOT close",
              not closed, f"status={status}")
        return not closed


def scenario_D_no_state_file_opt_in(mod, label):
    """A job that never sets RTB_STATE_FILE keeps the ORIGINAL
    close-on-silence behavior — regression guard for the other 40+ RTB
    shims that don't opt into the falling-edge hook."""
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td))
        card_id = "t_legacyjob"
        h.seed_board(card_id, "blocked")
        h.seed_rtb_rec("some-other-job", card_id)
        h.run(mod, "some-other-job", state_file=None)
        status = h.board()[card_id]["status"]
        closed = status == "archived"
        check(f"{label}: scenario D no-opt-in keeps legacy close-on-silence",
              closed, f"status={status}")
        return closed


def run_all(mod, label):
    results = []
    results.append(scenario_A_silent_still_low(mod, label))
    results.append(scenario_B_genuine_recovery(mod, label))
    results.append(scenario_C_jwt_hiccup_preserves_state(mod, label))
    results.append(scenario_D_no_state_file_opt_in(mod, label))
    return results


def main():
    print("=== GREEN: current worktree report-to-board.py ===")
    green_mod = load(LIVE, "rtb_green")
    green = run_all(green_mod, "GREEN")

    if PRECHANGE:
        print("\n=== RED: pre-change backup (should demonstrate the defect) ===")
        red_mod = load(Path(PRECHANGE[-1]), "rtb_red")
        # RED is EXPECTED to fail scenarios A and C (that IS the defect this
        # card fixes) and expected to pass B and D (those paths were never
        # broken) — so don't feed RED's A/C results into the pass/fail tally
        # the same way GREEN's are; assert the expected RED shape instead.
        red = run_all(red_mod, "RED")
        # Discard the scenario A/C FAIL entries logged inside run_all for RED
        # (expected) but keep B/D (must still pass on RED).
        failures[:] = [f for f in failures if not (
            f.startswith("RED: scenario A") or f.startswith("RED: scenario C"))]
        red_a_defect_reproduced = not red[0]  # scenario A closed -> defect present
        red_c_defect_reproduced = not red[2]
        check("RED reproduces scenario A defect (old code DOES close)",
              red_a_defect_reproduced)
        check("RED reproduces scenario C defect (old code DOES close)",
              red_c_defect_reproduced)
        check("RED still passes scenario B (recovery unaffected)", red[1])
        check("RED still passes scenario D (legacy jobs unaffected)", red[3])
    else:
        print("\n(no pre-change backup found under scripts/backups/ — "
              "skipping RED comparison; GREEN-only run)")

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed: {failures}")
        return 1
    print("PASS: all checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
