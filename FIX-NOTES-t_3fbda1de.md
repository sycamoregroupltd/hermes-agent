# FIX-NOTES — card t_3fbda1de (fix pass for PR #109, parent card t_89fe0b51)

Starting tip: `d205bc72` on `switchboard-seat-rebase-bf0203a7` (base `pin/bf0203a7`).
Maker's notes for Review to re-check. Review judges PASS or FAIL; this file doesn't.

## (a) P1 x2: the stop file is never cleared, and the reaper closes by seat name

**What was wrong**

1. The seat state dir was `<seats>/hermes-<profile>-<task>`, shared by every run of a card.
   `close_run_seat` touched `stop` and nothing removed it. So on a retry, the new
   supervisor saw the stale `stop` on its first tick and closed the seat without ever
   enrolling (switchboard_seat.py, `signal_stop`/`ensure_state_dir`).
2. `_close_seat_for_reaped_run` (kanban_db_dispatch.py ~L569) acts on a run that ended at
   least `TERMINAL_WORKER_REAP_GRACE_SECONDS` ago. It closed `hermes-<profile>-<task>` by
   name, so if the card was respawned within that window, the reaper closed the live
   retry's seat and stopped its supervisor. The same happened when the old run's
   supervisor woke up late: its `finally: close(session)` also closed by name.

**Fix: state is keyed by run, and every close is checked against the run that owns the seat**

* The new `run_state_dir(session, run_id)` gives `<seats>/<session>/run-<id>/`, which holds
  the doorbell, `stop`, the inbox spool, `supervisor.pid` and the log. A stop for run N
  can't reach run N+1.
* `ensure_state_dir(session, run_id=...)` creates the run dir and removes a stale `stop`
  (this covers a reused `run-none` dir). It then writes `<seats>/<session>/owner =
  run-<id>` atomically (tmp file + `os.replace`).
* `owns_seat(state_dir)` is false only when `owner` names a *different* run. If `owner` is
  missing, the old behaviour is kept and the close goes ahead.
* `close_run_seat` always sets this run's `stop`, but skips the bus `close --session`
  once a later run owns the seat.
* `keeper_main` stops with reason `superseded` when it no longer owns the seat. Its final
  close is skipped in that case (`_final_close`).
* The reaper closes `run_state_dir(session, row["id"])`, so the close is keyed by run id.
* `_note_seat_close` (run end) queues the run's own dir. If the supervisor fails to start,
  `release_seat` drops the claim, so an earlier run's supervisor can still close the seat
  it really holds.
* The bus seat name is unchanged (`hermes-<profile>-<task>`, which must match
  `kanban_bus_bridge`). Only local state and the close decision are keyed by run.

## (b) P2: a rolled-back run-end leaves a queued seat close

`_SEAT_CLOSE_PENDING` was a single global list. A `ROLLBACK`, or a `COMMIT` that ran out
of retries, left the entry there, and the next `write_txn` that committed on *any*
connection then closed the seat of a run that never ended.

Fix (kanban_db.py, kanban_db_connect.py `write_txn`):
* The queue is now `dict[id(conn)] -> list`, so one connection's rollback can't drop or
  drain another thread's entries.
* Top-level body exception → `ROLLBACK` → `_discard_pending_seat_closes(conn)`.
* Failed `COMMIT` → `ROLLBACK` → `_discard_pending_seat_closes(conn)`.
* A nested savepoint that rolls back cuts the queue back to the mark taken at `SAVEPOINT`
  (`_pending_seat_close_mark`). The outer txn's own closes still run.
* After a successful commit, the drain runs in a `finally` around
  `_check_file_length_invariant`. The txn did commit, so its closes must run even if that
  check raises.

## (c) Mutant G7 killed, plus mutants on every new guard

Method: for each mutant, a scratch copy of the tree with one edit, then
`scripts/run_tests.sh tests/hermes_cli/test_switchboard_seat.py`. rc != 0 means killed.

| Mutant | rc | Result | Killed by |
|---|---|---|---|
| G7 reaper close ignores the kill switch (remove `seat_enabled()` guard in `_close_seat_for_reaped_run`) | 1 | KILLED | test_reaper_close_honours_the_kill_switch |
| M1 `close_run_seat` skips the owner check | 1 | KILLED | test_reaper_close_of_an_old_run_spares_the_retry_holding_the_seat |
| M2 reaper closes the seat dir, not the run dir | 1 | KILLED | test_reaper_close_of_an_old_run_spares_the_retry_holding_the_seat |
| M3 keeper's final close skips the owner check | 1 | KILLED | test_old_supervisor_waking_late_never_closes_the_retry_seat, test_superseded_keeper_stops_beating_without_a_stop_flag |
| M4 `ensure_state_dir` never claims the seat | 1 | KILLED | reaper-spares-retry, old-supervisor-late |
| M5 state dir not keyed by run | 1 | KILLED | test_spawn_sets_seat_env_and_starts_supervisor + 2 more |
| M6 ROLLBACK keeps queued closes | 1 | KILLED | test_rolled_back_run_end_leaves_the_seat_open |
| M7 failed COMMIT keeps queued closes | 1 | KILLED | test_failed_commit_of_a_run_end_leaves_the_seat_open |
| M8 savepoint rollback keeps its closes | 1 | KILLED | test_rolled_back_savepoint_drops_only_its_own_seat_close |
| M9 `release_seat` no-op on supervisor failure | 1 | KILLED | test_supervisor_failure_never_fails_the_spawn |
| M10 keeper ignores a later run's claim | 1 | KILLED | test_superseded_keeper_stops_beating_without_a_stop_flag |
| M11 stale stop flag not cleared at run start | 1 | KILLED | test_reused_run_dir_starts_without_the_previous_stop_flag |

No mutant on the new enrol/close guards survived. P3 survivors G2/G3/G6/G9 were not
touched, as instructed (see Follow-ups).

## (d) The two red tests

* `test_spawn_sets_seat_env_and_starts_supervisor`: it failed with `StopIteration`
  because it looked for the worker via `cmd[0] == "hermes"`. Since the rebase onto
  bf0203a7, the worker argv is `[sys.executable, "-m", "hermes_cli.kanban_worker_client"]`
  (`_worker_argv`), so no command matched. This is a stale test, not systemd/dbus or the
  host environment as the PR body said. It now finds the worker by
  `hermes_cli.kanban_worker_client` and asserts the per-run state dir (`run-7`).
* `test_standalone_dispatcher_keeps_direct_worker_spawn`: the fake `Popen` did
  `captured_cmd.extend(cmd)` for every spawn, so the seat supervisor's `keeper` argv was
  appended after the worker argv. It now ignores the `keeper` spawn, the same way the
  sibling tests in that file already do.

Both fail on d205bc72 (StopIteration / list mismatch) and pass on the new tip.

## Red on d205bc72, green on the new tip

The final test files were run against the d205bc72 code: **10 failed** (below), 24 passed.
On the new tip all of them pass.

test_spawn_sets_seat_env_and_starts_supervisor, test_supervisor_failure_never_fails_the_spawn,
test_retry_of_a_card_gets_a_supervisor_the_old_stop_flag_cannot_stop (a1),
test_reaper_close_of_an_old_run_spares_the_retry_holding_the_seat (a2),
test_old_supervisor_waking_late_never_closes_the_retry_seat (a2),
test_superseded_keeper_stops_beating_without_a_stop_flag (a2),
test_reused_run_dir_starts_without_the_previous_stop_flag (a1),
test_rolled_back_run_end_leaves_the_seat_open (b), test_failed_commit_of_a_run_end_leaves_the_seat_open (b),
test_rolled_back_savepoint_drops_only_its_own_seat_close (b).
G7's test passes on unmutated d205bc72, as a mutant-killer should, and fails with G7 applied.

## Suite results on the new tip (`scripts/run_tests.sh`)

* `tests/hermes_cli/test_switchboard_seat.py` + `test_kanban_gateway_restart_handoff.py`:
  **33 passed, 0 failed, 1 skipped** (the skip is a `platforms` marker).
* Every test file the PR touches, plus all `tests/hermes_cli/test_kanban*.py`,
  `tests/gateway/test_kanban*.py` and `tests/plugins/test_kanban*.py` (110 files):
  **767 passed, 0 failed, 5 skipped**.
* All of `tests/hermes_cli/` + `tests/plugins/` did not finish inside a 1500 s local
  timeout, so it's not reported as a result.

## `python scripts/check --base origin/pin/bf0203a7`

* Fixed in this pass (all in this PR's own `switchboard_seat.py`, and already failing on
  d205bc72): **footguns** (BOM-tolerant reads use `utf-8-sig`; `os.kill(pid, 0)` →
  `psutil.pid_exists`) and **no-tmp** (`/tmp` literals → `tempfile.gettempdir()`). Ruff
  is clean. These now pass.
* Still failing, same findings as on d205bc72 (none new; the `keeper_main` CC finding my
  first draft added was removed by extracting `_exit_reason` / `_final_close`):
  **health**. FILE_LINES on `hermes_cli/kanban_db.py`, `hermes_cli/kanban_db_dispatch.py`
  and `tests/hermes_cli/test_kanban_db.py`, and BLE001/S110 on the deliberate "a seat
  must never fail a spawn" broad excepts. Fixing this means moving the seat code into a
  `kanban_db_*` sibling. That's out of scope for this fix pass; see Follow-ups.
* The `blob-size`/`health` "not a valid object name pin/bf0203a7" errors only appear
  without the `origin/` prefix, because there's no local branch of that name.

## (e) CI on the new tip

Fix commit `6fa09eaf` (pushed 30 Oct 21:16 GMT). Workflows **did trigger** on the push
(`pull_request` synchronize):

| Workflow | Run id | State at 21:35 GMT |
|---|---|---|
| CI (`.github/workflows/ci.yaml`) | 41129814961 | queued; first job "Detect affected areas" never got a runner |
| Docker Build, Test, and Publish (`docker.yml`) | 41129814820 | queued; "Resolve release phase" never got a runner |
| Nix flake check (`nix.yml`) | 41129814799 | queued; "Detect affected areas" never got a runner |

**There is no CI result yet, and there won't be one without a human change.** No job has
started, so these runs aren't evidence for or against the code. Local evidence is the
suite results above.

**Exact reason:** every job in the workflows carried by base `pin/bf0203a7` (and so by
this PR's merge ref `refs/pull/109/merge`) has `runs-on: blacksmith-4vcpu-ubuntu-2404`.
That's a Blacksmith third-party runner label from upstream; 50 workflow files under
`.github/workflows` use Blacksmith labels. `sycamoregroupltd/hermes-agent` has no
Blacksmith runners: the job API shows `runner_id: 0` and an empty runner group. Fork
`main`'s workflows (`ci.yml`, `docker.yml`) use `ubuntu-latest` and contain no
`blacksmith` label at all, which is why PR #108 (base `main`) gets CI and PR #109 (base
`pin/bf0203a7`) doesn't. The same thing already happened on PR #105: its Docker and Nix
runs were created 24 Oct 18:26 and cancelled 25 Oct 18:26, exactly GitHub's 24 h
queued-job timeout. These three runs will end the same way (about 31 Oct 21:16 GMT)
unless a runner appears.

I couldn't determine why `d205bc72` had 0 runs. The fork's Actions is enabled and the new
push did create runs. The most likely cause is that the push or PR-open event came from a
credential whose events don't trigger workflows (e.g. a workflow `GITHUB_TOKEN`). That's
moot now, because the new tip does trigger.

**Minimal fix (for a human; I didn't edit workflow files, as instructed).** Pick one:
1. Give the fork Blacksmith runners: install the Blacksmith GitHub App on
   `sycamoregroupltd`. No code change; the queued runs would then start.
2. Or, in one human-authored commit on this PR branch (or on `pin/bf0203a7`), replace
   `runs-on: blacksmith-4vcpu-ubuntu-2404` (and the other `blacksmith-*` labels) with
   `ubuntu-latest` in at least `ci.yaml`, `tests.yml`, `lint.yml`, `docker.yml` and
   `nix.yml`, then push. This matches fork `main`.
3. Or cancel the stuck runs, and Review accepts the local suite + `scripts/check` results
   above in place of CI for this base.

## Round 2 (card t_fab72502, os-reviewer execution-lens comment 1612)

Tip at start of round 2: `f07818df25`. Read-only evidence: `bus.db` (message log, seat
table), `session-bus/kanban-bridge/bridge.log`, and the upero board's `task_events`.

### R1: root cause for run 1070 (t_1a6b594f): proven, and the earlier "no seat" reading was wrong

* Run 1070 started 09:54:56Z and died at 10:32:59Z after 2283 s, exit 1. The worker output
  begins `Kanban owner refused: Gateway connection fa…`. The source comment is truncated there,
  so the exact tail of that message is not quoted here.
* It HAD a seat for its whole life. The interim bridge enrolled it at 09:55:45Z
  (`v2-enrol state=degraded pid=28934 card=upero/t_1a6b594f#1070`) and heartbeated it five
  times until 10:25:48Z.
* The earlier "NO_SEAT_ROW" for 1070 came from the seat table, which is upserted by session
  name. Retry run 1071 overwrote the row at 10:33:54Z, so the row no longer shows 1070.
* So run 1070 is not a seat-gap run. Its crash is a gateway-connection failure, and the seat
  was alive until the worker died. The seat-gap runs are 1014, 1062 and 1065.
* The three seat-gap runs are proven by timing. Each ran 65–107 s and the bridge logs show
  no tick inside its window (`ticks inside window: []` for 1014, 1062, 1065). The next tick
  came 3–4 min after the run ended, so the bridge never saw them. The enrol path itself wrote
  nothing to any log, which is the gap R2 closes.
* Residual, not fixed here: every bridge seat is `state=degraded` with `listener=None` and
  `doorbell=None` (missing `listener-or-drain`, `doorbell-handle`). The seats are observed,
  but nothing drains mail to a worker. The spawn-time keeper in this PR is the supervised
  listener; the degraded state is the interim bridge's honest report.

### R2: enrol and supervisor failures are board-visible

* `switchboard_seat.record_seat_event(board_db, card, run_id, kind, payload)` writes one
  `task_events` row under the run (a single `INSERT` with the same shape as
  `kanban_db._append_event`) and logs a warning. It is best-effort and never raises. It
  returns False when there is no board, no run, or the write fails. It is deliberately
  self-contained (raw sqlite, no `hermes_cli` import): the keeper is a standalone file, and
  an editable install can shadow `hermes_cli`, so an import would silently lose the event.
  The first draft imported `hermes_cli`; the standalone probe showed the import failing,
  which is why it was replaced.
* Keeper (`keeper_main`): an enrol failure is recorded as `switchboard_seat_enrol_failed`
  once per run (not once per retry tick). A keeper exception is recorded as
  `switchboard_seat_supervisor_failed`, with the error type and message.
* Dispatcher (`_start_seat_supervisor`): a supervisor start that raises, or returns no pid,
  is recorded as `switchboard_seat_supervisor_failed` on the run. This was
  `_kb._log.debug`, and is now `warning` plus the event.
* Tests added to `tests/hermes_cli/test_switchboard_seat.py` (4):
  `test_keeper_enrol_failure_is_recorded_once_per_run`,
  `test_keeper_supervisor_crash_is_recorded_on_the_run`,
  `test_record_seat_event_never_raises_without_a_board`,
  `test_spawn_supervisor_failure_is_recorded_on_the_run`.
* Results (`env -i`, scrubbed, `/usr/bin/python3 -m pytest`):
  `test_switchboard_seat.py` + `test_kanban_dispatch_tick_hook.py` = **36 passed**. Nine
  adjacent kanban files plus those two = **178 passed, 1 skipped, 0 failed**.
* Environment note: in an unscrubbed shell, 4 tests in `test_kanban_boards.py`,
  `test_kanban_core_functionality.py` and `test_kanban_db.py` fail with `cannot create
  restart-safe systemd scope`. They fail the same way on a pristine `git archive` of HEAD,
  so they are environmental, not caused by this change. They pass under `env -i`.

### R3: mechanism contract

| Element | Declaration |
|---|---|
| Store | Bus: `bus.db` `seat` table (`session`, `card`, `pid`, `state`, `listener`, `doorbell`, `enrolled_at`, `heartbeat_at`, `closed_at`). Session = `hermes-<profile>-<task>`, which must match `kanban_bus_bridge`. Local: `switchboard-seats/<session>/run-<id>/` (`doorbell`, `stop`, `inbox.ndjson`, `supervisor.log`) plus `owner`. Board: `task_events` rows `switchboard_seat_enrol_failed` and `switchboard_seat_supervisor_failed`, keyed by `run_id`. |
| Liveness | The keeper beats every `heartbeat_seconds()`. A seat is alive while `heartbeat_at` advances. The keeper ends when the worker pid dies, and then closes the seat. A heartbeat after close is refused (T10) and stops the keeper. |
| Executed copy | The keeper runs `python <abspath of hermes_cli/switchboard_seat.py> keeper ...` (`supervisor_argv` uses `os.path.abspath(__file__)`). That is the same file the dispatcher imported, so the executed copy is the deployed hermes-agent checkout at the release that lands this PR. Nothing runs from the live tree edit. |
| Consumer | Session Bus readers (`busctl` scan/inbox, the switchboard dashboard). The interim `kanban_bus_bridge` is the fallback during rollout and must not mint a second seat for the same session. The board reads the `switchboard_seat_*` events on a run. |
| Kill switch | `seat_enabled()` guards every seat call, including the reaper close. `test_kill_switch_disables_every_seat_call` covers it. |
| Rollback | Revert the merge commit. No schema change and no data migration (`task_events.kind` is free text). Before revert, the bridge still seats on its ~3-min tick. |
| Verification matrix | (1) unit: the 36 tests above. (2) live: `post_deploy_seat_check.py <deploy_epoch> 10 120` must print `RESULT 10/10 seated` (this script is in fleet-engineer scratch, not in the repo, and must be moved into the repo or its output recorded). (3) visibility: a run with a failed seat shows a `switchboard_seat_*` event under its run in `task_events`. |

### R4: card record

* Canonical PR for this fix: **#109**, head `switchboard-seat-rebase-bf0203a7` (round-2
  commit on top of `f07818df25`), base `pin/bf0203a7`.
* PR **#111** is superseded. It is a 19,887-file DIRTY main-based PR. Its spawn-time commits
  `128a3c60` and `db188062` are not the fix vehicle. fleet-engineer did not close it;
  os-reviewer decides on closure.
* Still pending, and these are landing gates rather than rework: merge through the Merge
  lane, CI (no Blacksmith runner on this fork), and the post-deploy 10/10 bus proof.

## Follow-ups (written list only, not done here)

* P3 mutant survivors **G2, G3, G6, G9** from Review FINAL: need killing tests.
* code-health ratchet: move `_note_seat_close` / `_drain_pending_seat_closes` /
  `_discard_*` / `switchboard_seats_root` out of `kanban_db.py`, and the seat helpers out
  of `kanban_db_dispatch.py`, into a `kanban_db_seats.py` sibling (FILE_LINES). Annotate
  or narrow the deliberate broad excepts (BLE001/S110).
* Residual race, not fixed here: the owner check and the bus close are two steps. If a
  respawn claims the seat *and* its supervisor enrols between them, the old close still
  lands, and the new supervisor then exits on rc 4 (closed elsewhere). The new run then
  has no seat, though it never gets a wrong one. The window is one busctl subprocess call.
  For the run-end path a respawn needs a later claim txn plus a spawn, so this is unlikely
  but not impossible. The full fix is on the bus side: `busctl close --card <board>/<task>#<run>`
  that closes only when the row's card still names that run. That's a busctl change,
  outside this repo.

---
Card: jarvis-os/t_3fbda1de · Lane: Claude cloud (sss, Opus) via Jarvis infrastructure push 30 Oct · Session: session_01PFFexPxLKHfpHsz9QogwcG · Fix pass (a)-(e) for hermes-agent PR #109, push to existing branch, no merge.
