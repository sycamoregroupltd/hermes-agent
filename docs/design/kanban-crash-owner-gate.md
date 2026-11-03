# Kanban crash owner gate

Status: **proposal + reference implementation (draft PR)**. Not installed on the
fleet; a live install is a separate, gated step.

Base: `bf0203a7` (the upstream v0.22.5 commit the fleet runs). This branch is cut
from that exact commit.

Fleet card: `jarvis-os/t_862b9fb8` (fleet-engineer). Review card: `t_9cc5807e`
(platform-reviewer). Decision owner: os-architect.

## Problem

Probe 6 of the world-class suite ("a worker crash-loops") requires that **no open
card crashes 3+ times in 7 days**: after a card's 2nd crash, its *owner* steps in.

On `bf0203a7` the dispatcher already auto-blocks a card after `failure_limit` (2)
consecutive non-successes (`_record_task_failure` → status `blocked`, event
`gave_up`). What fails is everything after the block:

1. `unblock_task` (`hermes_cli/kanban_db.py`, ~L3560) sets
   `consecutive_failures = 0` on every unblock. Any actor that unblocks the card
   restarts the streak, so the card spends two more crashes before it is blocked
   again.
2. On the normal ready path, `unblock_task` writes the `unblocked` event with
   payload `None`. Nobody can tell from the board who released the card.
3. None of the three in-tree callers (CLI `hermes kanban unblock`, the agent tool
   `kanban_unblock`, dashboard drag-to-ready) checks who is unblocking. A PM
   autopilot, a dead-pid reclaim sweep or another agent can release it.
4. There are two quieter exits that do the same thing:
   - `promote_task` moves a card from blocked to ready.
   - A dashboard drag from blocked to todo is a raw status write. On the next
     tick `recompute_ready` moves the card from todo to ready, and nothing
     checks the crash streak on that path.
   - `recompute_ready` also auto-promotes a plain `gave_up` card once its counter
     drops below the limit, for example after `assign_task` resets the counter
     because the card was handed to a different profile.

Result on the live fleet (2026-11-03): one card crashed 12 times in 7 days through
gave_up → unblock cycles, and 17 open cards have 3+ crashes in 7 days.

## Decision summary

| Question | Recommendation |
|---|---|
| Owner | `tasks.created_by`, canonicalised. A creator that is a task id resolves to that task's assignee. Generic creators (`NULL`, `""`, `worker`, `kanban-service-gate`) mean *no owner* (legacy). |
| Actor identity | The bound profile on every path, never a caller-supplied string: CLI `current_profile_name()`, agent tool `_persisted_identity()`, dashboard the server's profile with `source="dashboard"`. |
| Policy | **Owner-only release with a reason** (option A), plus "no unblock resets the streak" and "a release grants exactly one retry" (the useful half of option B). |
| Notification | The existing `gave_up` wake event, now carrying `owner`, `owner_source` and `owner_notify_subs`. Refusals leave an `unblock_refused` audit event. Nothing new is added to the gateway. |
| Upstream | Compatible with the merged retry policy; supersedes #28379's reset-on-unblock; aligned with open #33150 / #127108 / #83067; conflicts with #102770 / #108942 auto-recovery (details below). |

## Q1: Trusted owner identity

### Who owns a card

The owner is **the card's creator** (`tasks.created_by`), not the assignee.

- The assignee is the profile whose worker is crashing, so it is not an independent
  check. Workers also cannot unblock: `kanban_unblock` is orchestrator-only, and
  `hermes kanban unblock` refuses inside `HERMES_KANBAN_TASK`.
- The creator is the principal that asked for the work and is already treated as
  accountable elsewhere. Creator-origin inheritance (`inherit_creator_origin`)
  subscribes the creator's chat to the card, so the creator is also the party the
  notifier already wakes.
- A board-level "PM" owner was considered and rejected. Boards have no owner
  field on `bf0203a7`, and on the fleet the PM autopilot is exactly the actor that
  kept re-releasing cards. Making the PM the owner would legitimise the loop.

Resolution (`crash_gate_owner`):

1. Read `created_by` and strip it. If it is in `_OWNERLESS_CREATORS`
   (`""`, `worker`, `kanban-service-gate`) or NULL, the result is `(None, "none")`.
2. If it looks like a task id (`t_…`) and that task exists, the owner is that
   task's assignee: `(assignee, "creator_task")`. This covers a worker that
   filed a child card under its own task id, a case `complete_task`'s
   created-card check already recognises.
3. Otherwise the owner is the canonicalised creator: `(creator, "created_by")`.

The owner is resolved **when the gate is checked**, never read back from the event
payload. Editing an event row therefore cannot change who may release a card.

### How the unblocking actor is identified on each path

| Path | Actor | `source` | Trust basis |
|---|---|---|---|
| CLI `hermes kanban unblock` / `promote` | `current_profile_name()` (`_profile_author`) | `cli` | The profile the process runs as, the same identity that stamps comments and `created_by`. |
| Agent tool `kanban_unblock` | `_persisted_identity()` | `tool` | The bound profile, never tool args. This is the #19713 rule that already protects comment authorship. |
| Dashboard drag to ready/todo | dashboard server's profile | `dashboard` | Session-token-authenticated human console. Treated as an operator override, the same convention the dashboard's forced `done`/`review` verbs already use. |
| Out-of-tree Python callers (`kb.unblock_task(conn, tid)`) | `None` | `None` | Unidentified. These **fail closed** on a gated card. |

Known limits, accepted for this proposal and listed for the reviewer:

- An agent that runs `hermes kanban unblock` from its own terminal is identified as
  its own profile. That is correct, not a bypass: it is still not the owner unless
  it created the card.
- `hermes kanban create --created-by X` lets a caller name any creator at creation
  time. This is pre-existing. A hardening follow-up could restrict
  `--created-by` the same way #19713 restricted tool args.
- The dashboard override trusts whoever holds the dashboard session token.

### Legacy cards with no creator

An ownerless card (no usable `created_by`) can be released only from an
**operator surface**: the CLI with a `--reason`, or the dashboard. Agent tool
calls are refused. The CLI branch accepts any profile, because there is no owner
to compare against and a human with shell access is the realistic operator. The
release is recorded with `released_by: "operator"`.

## Q2: Unblock policy after a crash auto-block

### Options considered

**A. Only the owner (with a reason) may release.** Non-owners are refused.
- Pros: actually bounds crashes. Between owner interventions a card crashes at
  most twice. Release decisions are explicit and attributed.
- Cons: non-owner flows that used to work now get a refusal, for example a PM
  autopilot sweeping blocked cards. Ownerless legacy cards need an operator. A
  systemic wave (provider outage) gates many cards, each needing release; bulk
  `hermes kanban unblock id1 id2 … --reason` handles that.

**B. Anyone may unblock; a non-owner unblock keeps the streak.** The next crash
re-blocks immediately.
- Pros: nothing breaks, and every cycle costs one crash instead of two.
- Cons: **does not meet probe 6**. An autopilot that unblocks after every trip
  still produces one crash per cycle, unbounded. Twelve cycles a week is still
  twelve crashes. It only slows the loop down.

**C. Time-based auto-recovery or a cooldown probe** (upstream #102770 / #108942).
- Pros: self-heals transient failures.
- Cons: also unbounded over 7 days, and nobody is ever made to step in. This is
  the opposite of the probe's intent.

### Recommendation: A, with B's streak rule underneath

1. **Gate**: a card whose newest `gave_up` / `unblocked` / `promoted_manual`
   event is a `gave_up` is *gated* (`open_crash_gate`). `unblock_task` and
   `promote_task` call `_check_crash_gate`, which applies
   `crash_gate_decision`:
   - `source == "dashboard"`: release as `operator`.
   - No owner: release as `operator` only from `cli`/`dashboard` with a reason.
   - Actor is the owner and gave a reason: release as `owner`.
   - Anything else: refuse. Write an `unblock_refused` event in the same
     transaction (so it commits), then raise `CrashGateRefused` (a
     `ValueError`). The tool layer reports it as a structured tool error. The CLI
     prints it and exits 1, and writes no misleading `UNBLOCK:` comment.
     `promote_task` returns `(False, message)`.
2. **No unblock resets the crash streak.** `consecutive_failures` is cleared only
   by `complete_task`, the same rule upstream already applies to
   `block_recurrences` (#52848) and `reopen_review_task` (#35072). A card that
   crashed once, blocked for input, and was unblocked therefore trips on its next
   crash: that is the 2nd crash since its last success.
3. **A release grants exactly one retry**: `consecutive_failures` is set to
   `max(trip_limit, DEFAULT_FAILURE_LIMIT) - 1`. If the owner's fix did not work,
   the next crash re-gates the card and goes straight back to the owner. Without
   this, each release would buy two more crashes.
4. **`recompute_ready` treats an open gate as sticky** (`_has_sticky_block`). A
   counter reset from `assign_task` or a raised `failure_limit` no longer
   silently releases a gated card.
5. **Dashboard drag from blocked to `todo`** now goes through `unblock_task`, the
   same as drag to `ready`. The gate records the override, and the card still
   lands in the column it was dropped in.

Worst case per card per 7 days without owner action: **2 crashes**. Each owner
release adds at most **1**, and every one of those releases is attributed to an
actor and a reason.

## Q3: Verifiable owner notification

This uses only mechanisms that already exist:

- **Event.** `gave_up` is already in the gateway notifier's `_WAKE_KINDS`
  (`gateway/kanban_watchers_notifier.py`). Every `kanban_notify_subs` row on the
  card wakes its session. That includes the creator-origin subscription
  inherited at creation, which is how the owner's chat is reached.
- **Payload.** The `gave_up` payload now records:
  - `owner_gate: true`
  - `owner`
  - `owner_source`
  - `owner_notify_subs`: the subscription count at trip time.

  An auditor can now check from the event log alone that every trip named an
  owner and whether anyone was subscribed. `owner_notify_subs == 0` means no
  ping went out. Such cards (typically CLI-created) must be found from the
  board: `hermes kanban list --status blocked`, or the SQL in Migration.
- **Refusal audit.** `unblock_refused` (`actor`, `source`, `reason`, `via`,
  `owner`, `why`) shows exactly which actor tried to bypass the owner.
- **Release audit.** `unblocked` / `promoted_manual` carry `crash_gate`:
  `released_by`, `owner`, `failures_before`, `failures_after`.

**Not** done: reassigning the card to the owner. The owner is usually an
orchestrator or human, not a worker profile. Reassigning would spawn the owner's
profile as a worker on the crashing card and would reset the per-profile streak.

Follow-up, not in this PR, to keep the change inside kanban code paths: teach
`_fmt_gave_up` to print "held for <owner>" (a gateway i18n string), and make
`hermes kanban show` surface the gate.

## Q4: Related upstream NousResearch/hermes-agent PRs

These were found through the GitHub search API, which returns only titles and
the opening part of each description. Diffs were not read.

**Merged (the policy on `bf0203a7`)**

| PR | Effect | Compatibility |
|---|---|---|
| [#20410](https://github.com/NousResearch/hermes-agent/pull/20410) | Unifies spawn/timeout/crash into `consecutive_failures`. | Built on. |
| [#21330](https://github.com/NousResearch/hermes-agent/pull/21330) | Per-task `max_retries`, then `kanban.failure_limit`, then `DEFAULT_FAILURE_LIMIT`. | Built on. Release uses the trip's `effective_limit`. |
| [#28379](https://github.com/NousResearch/hermes-agent/pull/28379) | `unblock_task` resets `consecutive_failures`. | **Superseded.** This is the bug. |
| [#28994](https://github.com/NousResearch/hermes-agent/pull/28994) (#28712) | Worker/operator `blocked` is sticky; `gave_up` left auto-promotable. | **Changed**: `gave_up` is now sticky until released. |
| [#35220](https://github.com/NousResearch/hermes-agent/pull/35220) (#35072) | `recompute_ready` keeps the counter; at-limit cards stay blocked. | Aligned. Extended to unblock. |
| [#52848](https://github.com/NousResearch/hermes-agent/pull/52848) | `block_recurrences` survives unblock; names the counter reset as the loop cause. | Aligned. Same rule applied to `consecutive_failures`. |
| [#64353](https://github.com/NousResearch/hermes-agent/pull/64353), [#114904](https://github.com/NousResearch/hermes-agent/pull/114904) | Violation-budget trips stay held (`sticky`). | Compatible. Now also owner-gated. |
| [#111446](https://github.com/NousResearch/hermes-agent/pull/111446) | Never-spawned claims count; operator `reclaim_task` resets the counter. | Compatible. Reclaim applies to running cards, not blocked ones. |
| [#114886](https://github.com/NousResearch/hermes-agent/pull/114886) | Host-refused spawns don't count and never trip. | Compatible. No `gave_up`, so no gate. |
| [#115594](https://github.com/NousResearch/hermes-agent/pull/115594) | Terminal provider error parks the card after one spawn. | Compatible. Its `gave_up` is gated. |
| [#28680](https://github.com/NousResearch/hermes-agent/pull/28680), [#28683](https://github.com/NousResearch/hermes-agent/pull/28683), [#38223](https://github.com/NousResearch/hermes-agent/pull/38223) | Stale reclaim, blocker-auth and rate-limit paths don't count as failures. | Compatible. |

**Open, competing**

- **Aligned:**
  - [#33150](https://github.com/NousResearch/hermes-agent/pull/33150) "keep gave_up tasks blocked"
  - [#127108](https://github.com/NousResearch/hermes-agent/pull/127108) "make exhausted retries terminal"
  - [#83067](https://github.com/NousResearch/hermes-agent/pull/83067) "systemic gave_up stays blocked until explicit unblock"

  All three hold `gave_up` until an explicit unblock. This proposal is a strict
  superset: it says *whose* unblock counts. If one of them lands first, rebase
  `_has_sticky_block` onto it.
- **Conflicting:**
  - [#102770](https://github.com/NousResearch/hermes-agent/pull/102770) auto-recovers stale `gave_up` blocks after `kanban.gave_up_stale_hours`.
  - [#108942](https://github.com/NousResearch/hermes-agent/pull/108942) runs one probe retry after a cooldown.

  Both release without an owner, so they would reopen probe 6. If adopted, gate
  them behind config defaulting off for this fleet, or route their release
  through `_check_crash_gate` as `source="auto"` (refused by default). #102770's
  "unblock audit trail" half is compatible with this proposal's event payload.
- **Partly conflicting:** [#74364](https://github.com/NousResearch/hermes-agent/pull/74364) lets human unblocks reset `block_recurrences`. That is the same question for the other counter. The consistent answer here: an owner or operator release resets to *one retry left*, not a full budget.
- **Overlapping:** [#133294](https://github.com/NousResearch/hermes-agent/pull/133294) makes the budget per task instead of wiping it on `assign_task`. Compatible. This proposal already stops reassignment from releasing a gated card, and #133294 would close the remaining reassign-then-crash path on ungated cards.
- **Actor attribution:**
  - [#81221](https://github.com/NousResearch/hermes-agent/pull/81221) caller-identity check + `blocked_by`
  - [#89137](https://github.com/NousResearch/hermes-agent/pull/89137) optional actor on transitions

  Same direction. Field names should be reconciled (`actor`/`source` here) when
  either lands.
- **Conflicting:** [#109956](https://github.com/NousResearch/hermes-agent/pull/109956) lets workers unblock with a self-unblock guard. It is compatible only if the gate check runs on that path too. A worker is never the owner of its own crash.
- **Neutral:** [#122307](https://github.com/NousResearch/hermes-agent/pull/122307) lets unblock take a card out of `triage`. The gate check would apply unchanged.

## Change set (reference implementation)

The change stays inside kanban, unblock and dispatcher code paths:

- `hermes_cli/kanban_db.py`:
  - `crash_gate_owner`, `open_crash_gate`, `crash_gate_trip_payload`,
    `crash_gate_decision`, `_check_crash_gate`, `CrashGateRefused`.
  - `unblock_task(conn, tid, *, actor, source, reason)`: records the actor
    payload on every `unblocked` event, enforces the gate, and no longer resets
    the streak.
  - `promote_task` is gated.
  - `_has_sticky_block` holds open gates.
- `hermes_cli/kanban_db_dispatch.py`: the `gave_up` payload gains the owner and
  notify fields.
- `hermes_cli/kanban.py`: CLI unblock always passes the profile and
  `source="cli"`, reports refusals, and writes the `UNBLOCK:` comment only after
  a successful transition.
- `tools/kanban_tools.py` / `tools/kanban_tools_schemas.py`: the tool passes
  `_persisted_identity()` and `source="tool"`. It gains an optional `reason` arg,
  required by the owner to release.
- `plugins/kanban/dashboard/plugin_api.py`: drag to ready/todo from
  blocked/scheduled goes through `unblock_task` with `source="dashboard"`.
- `tests/hermes_cli/test_kanban_crash_owner_gate.py`: 20 tests on the real DB +
  dispatcher funnel and the CLI, tool and dashboard surfaces.
- Existing tests updated for the intended policy change:
  - `test_kanban_worker_exit_trailer.py::test_plain_budget_trip_still_auto_recovers`
    is renamed to `…_waits_for_its_owner`. It pinned the #28994 auto-recovery
    this design removes.
  - That file's operator-unblock test, and `test_kanban_review_lifecycle_complete.py`'s
    breaker test, now pass an operator actor. A bare `unblock_task(conn, tid)` on
    a gated card fails closed.

There are no schema changes and no config keys. The gate is derived entirely
from the existing `task_events` log.

## Migration for existing blocked cards

- **No DB migration is needed.** The gate is computed from events. Every card
  that is `blocked` right now with a `gave_up` as its newest
  `gave_up`/`unblocked`/`promoted_manual` event is gated as soon as the new code
  is installed. Its owner is resolved from `created_by` as usual. Old `gave_up`
  payloads that lack `owner`/`effective_limit` are handled: the release falls
  back to `DEFAULT_FAILURE_LIMIT`.
- **No retroactive notification.** Those cards' `gave_up` already fired before
  the install. At install, post one list per owner, from this query:

  ```sql
  SELECT t.id, t.title, t.created_by, t.assignee, t.consecutive_failures
    FROM tasks t
   WHERE t.status IN ('blocked','scheduled')
     AND (SELECT kind FROM task_events e
           WHERE e.task_id = t.id AND e.kind IN ('gave_up','unblocked','promoted_manual')
           ORDER BY e.id DESC LIMIT 1) = 'gave_up'
   ORDER BY t.created_by, t.id;
  ```

  Ownerless rows (`created_by` NULL/`worker`) go to the operator (Frank).
- **Open cards already past 3 crashes but not blocked** (the 17 in the probe):
  their counters were reset by earlier unblocks, so they get a fresh 2-crash
  budget from install. That still satisfies the probe going forward: the 7-day
  window clears by itself, and no new card can exceed 2 + (owner releases).
  An optional one-off backfill could set `consecutive_failures` to the number
  of crash/timeout/spawn_failed events since each card's last `completed`. It
  is **not** included, because it would re-gate cards on the first crash after
  install without an event explaining why.
- **Out-of-tree callers** (fleet PM autopilot, dead-pid reclaim plugins) that call
  `kb.unblock_task(conn, tid)` will start getting `CrashGateRefused` on gated
  cards. That is intended. They should catch it and route to the owner (or
  stop), and pass `actor=`/`source=` on ordinary unblocks so their releases are
  attributed.
- **Rollback**: revert the commit. The events written (`unblock_refused`, extra
  payload keys) are ignored by older code.

## Validation

- New: `tests/hermes_cli/test_kanban_crash_owner_gate.py` covers:
  - Owner release with and without a reason.
  - Non-owner refusal plus its audit event.
  - Unidentified callers failing closed.
  - Reassign not releasing the gate; promote not being a bypass.
  - Exactly one retry after a release.
  - The probe-6 bound: repeated autopilot attempts produce no third crash.
  - Ownerless and legacy cards, including old-format `gave_up` events.
  - The plain-unblock payload, and the streak being preserved.
  - CLI, tool and dashboard surfaces.
- Existing kanban suite: see the PR description for the run and any expected
  behaviour updates.
