# REVIEW_VERDICT router fixture and expectation matrix

Task: t_643ec9d1 (consolidates prior fixture work from t_57fcc08e, t_bdac52bc, t_ce83a886, t_bc8a8ba6)

Purpose: deterministic, importable coverage for REVIEW_VERDICT router behavior without live kanban access.

Primary importable fixtures: `agent-hooks/verdict-router.fixtures.json`
Reference harness/self-test: `agent-hooks/verdict-router-harness.py` and `agent-hooks/verdict-router.selftest.sh`
Production-script isolation adapter: `python3 agent-hooks/verdict-router-harness.py --router-script scripts/verdict_router.py` creates a temporary kanban DB per fixture and redirects router note/log/state paths away from live boards.

## Harness contract implementation note

Concrete entry points:
- Production router: `scripts/verdict_router.py::main(argv)` scans boards from `boards()`, builds candidates with `candidates_for_board()`, parses reviewer comments with `parse_verdict()`, validates targets with `target_validation()`, classifies safety with `classify_scope()` / `frontend_app_without_verify_pass()`, chooses a `Decision` in `decide()`, and mutates only through `perform()` when apply mode is enabled.
- Test harness: `agent-hooks/verdict-router-harness.py::main(argv)` is the caller-facing harness entry point. Use `reference_plan(fixture, mode="dry-run"|"mutation-plan")` for the in-memory contract, `run_router_script(script_path, fixture)` to exercise the production script against an isolated temporary SQLite board, and `assert_plan(fixture, plan)` for the assertion surface.
- Fixture source: `agent-hooks/verdict-router.fixtures.json` provides the board/card/comment inputs and expected outputs. `agent-hooks/verdict-router.selftest.sh` runs both dry-run and mutation-planning reference checks.

Required caller inputs:
- `board`: slug used in target validation and idempotency keys.
- `task`: at minimum `id`, `status`, `title`, `body`, and `comments`; optional `assignee`, `block_reason`, `changed_files`, `existing_idempotency_keys`, `created_at`, `priority`, and `extra_tasks` for board peers.
- Each comment must provide `id`, `author`, `created_at`, and `body`. Only the latest non-router comment (`author` not in `verdict-router` / `cron:deterministic-verdict-router`) can supply a parseable `REVIEW_VERDICT[:=]...` token.
- `expect`: expected `verdict_value`, `target_validation`, `scope_class`, `action`, `result`, `mutations`, and optional comment/log assertions.

Dependencies that downstream tests must mock or replace in memory:
- SQLite board tables `tasks` and `task_comments`; do not read live `~/.hermes/kanban*.db` or board DBs. The harness `write_fixture_board()` creates the minimal schema needed for `--router-script` tests.
- Board discovery paths: override `VERDICT_ROUTER_ROOT`, `VERDICT_ROUTER_BOARDS_DIR`, `VERDICT_ROUTER_DEFAULT_DB`, `VERDICT_ROUTER_STATE_DIR`, `VERDICT_ROUTER_VAULT_ROOT`, and `VERDICT_ROUTER_NOTE_DIR` to temporary directories.
- Mutation APIs: production `perform()` shells out through `HERMES_BIN kanban --board <board> comment|complete|unblock`; unit tests should use mutation-planning output, a fake `HERMES_BIN`, or monkeypatch `run_cli()` / `perform()` rather than calling the live CLI.
- Side-effect sinks: `append_note()` / `today_note()` for the Obsidian shadow log, `append_run_log()` for `scripts/logs`, and lock/sentinel files under `VERDICT_ROUTER_STATE_DIR`.
- Idempotency state: prior router markers are represented by `existing_idempotency_keys` in fixtures or by router-authored rows in the temporary `task_comments` table.

Mode behavior:
- Dry-run is the production default unless `--apply`, `VERDICT_ROUTER_APPLY=1`, or the apply sentinel exists; `--dry-run` overrides apply. In dry-run, production emits decisions and appends shadow-log entries but must not call `perform()` or mutate cards.
- Mutation-planning mode is harness-only (`--mutation-planning`); it reports the same planned `mutations` without enabling live side effects.
- `--router-script scripts/verdict_router.py` still runs the production script with `--dry-run --json` against a temp DB and temp note/log/state dirs, so it is safe for regression tests without live board access.

Expected assertion surface for downstream tests:
- Parsed verdict: `verdict_value`, source comment id/author, token-count behavior for malformed or multiple verdicts, and latest-comment ordering including corrupt numeric fields.
- Target validation: `same-card`, `missing-target`, `cross-target`, `multi-target`, or `not-applicable`.
- Safety classification: `source_docs_spec_test_only`, `operator_gated`, `ambiguous`, or `unknown`, including frontend/app `VERIFY_PASS` gating.
- Planned mutations: exact `mutations` list (`[]`, `["comment"]`, `["complete"]`, or `["comment", "unblock"]`) plus forbidden mutation checks for fail-closed cases.
- Emitted comments: prefix/content for `NEEDS-PM`, `NEEDS-OPERATOR`, and `verdict-router: REWORK_REQUIRED`; router comments must not contain parseable `REVIEW_VERDICT[:=]` tokens.
- Completion actions: `action=complete`, `result=would_complete`, non-empty `reason`, idempotency key, and completion metadata fields when produced by the planner/adapter.
- Unblock actions: `action=unblock_rework`, `result=would_unblock`, quoted blocking finding, idempotency key, and `["comment", "unblock"]` mutation plan.
- Ignored/no-op results: stale non-latest verdicts, router-authored echoes, non-blocked cards, and existing idempotency keys must produce `action=skip` with `result=skipped` or `skipped_idempotent` and no mutations.
- Errors/failures: board scan errors appear in production JSON `failures` and shadow-log `scan_error` entries; harness execution errors are returned as per-fixture `errors` and should fail the run without touching live boards.

The JSON fixtures use an in-memory board-card model. Each fixture has:
- `board`: board slug used for target validation/idempotency keys.
- `task`: representative blocked/non-blocked card state, comments, optional changed files, and optional existing idempotency keys.
- `expect`: expected parse/classification/action/mutation contract asserted by the harness.

Mode-specific expected outputs consumed by automated tests:
- Dry-run mode (`agent-hooks/verdict-router-harness.py --json`) must return `mode="dry-run"` and the fixture `expect` contract for every scenario. Any `mutations` listed under `expect` are planned-only assertions in dry-run; the harness must not touch a live kanban DB or execute completion/unblock/comment commands.
- Planned-mutation mode (`--mutation-planning --json`) must return `mode="mutation-plan"` and the same fixture `expect` contract, including `mutations`, `forbid_mutations`, `comment_prefix`, and `comment_contains`. This mode proves what would be planned without executing the mutation.
- Production-script regression mode (`--router-script scripts/verdict_router.py --json`) must run only against a temporary fixture DB and map script decisions back into the same expected fields. Any expected comments, unblock actions, completion actions, idempotent skips, malformed-verdict failures, and no-op/ignored results are asserted from the JSON fixture, not from live board IDs.

| Fixture | Input card state | Comment / verdict ordering | Expected parsing result | Safety classification | Planned action | Expected mutation behavior |
|---|---|---|---|---|---|---|
| `approved-source-card-completes` | Blocked source/docs/spec/test-only card `t_a0000001`, review-required block reason. | Latest non-router comment `101` says `REVIEW_VERDICT=APPROVED` and targets `jarvis-os/t_a0000001`. | `verdict_value=APPROVED`, `target_validation=same-card`. | `source_docs_spec_test_only`. | `complete`. | Mutates only by completing the source card; no router comment required. |
| `approved-runtime-a3-needs-operator` | Blocked A3/runtime cron activation card `t_a0000002`. | Latest non-router comment `102` says `REVIEW_VERDICT: APPROVED` and targets same card. | `verdict_value=APPROVED`, `target_validation=same-card`. | `operator_gated` because title/body include A3/runtime/service restart terms. | `needs_operator`. | Adds NEEDS-OPERATOR comment only; must not complete or unblock. |
| `approved-db-live-scope-needs-operator` | Blocked production DB migration/live deploy card `t_a0000003`. | Latest non-router comment `103` says `REVIEW_VERDICT=APPROVED` and targets same card. | `verdict_value=APPROVED`, `target_validation=same-card`. | `operator_gated` because scope includes deploy/live/DB/schema/gateway restart terms. | `needs_operator`. | Adds NEEDS-OPERATOR comment only; must not complete or unblock. |
| `changes-requested-unblocks-with-quoted-finding` | Blocked source-only parser card `t_a0000004`. | Latest non-router comment `104` says `REVIEW_VERDICT: CHANGES_REQUESTED`, includes blocking finding, and targets same card. | `verdict_value=CHANGES_REQUESTED`, `target_validation=same-card`; finding excerpt is preserved. | `source_docs_spec_test_only`. | `unblock_rework`. | Adds REWORK_REQUIRED comment containing quoted finding, then unblocks for rework. |
| `ambiguous-malformed-verdict-fails-closed` | Blocked source-only card `t_a0000005`. | Latest non-router comment `105` says unsupported `REVIEW_VERDICT=APPROVED_WITH_NOTES`. | `verdict_value=APPROVED_WITH_NOTES`, `target_validation=same-card`. | `ambiguous`. | `needs_pm`. | Adds NEEDS-PM fail-closed comment only; must not complete or unblock. |
| `multiple-verdict-tokens-fail-closed` | Blocked source-only card `t_a0000017`. | Latest non-router comment `118` contains both `REVIEW_VERDICT=APPROVED` and `REVIEW_VERDICT=CHANGES_REQUESTED`. | Multiple verdict tokens are treated as unparseable; `target_validation=not-applicable`. | `ambiguous`. | `needs_pm`. | Adds NEEDS-PM fail-closed comment only; must not complete or unblock. |
| `off-target-approved-fails-closed` | Blocked source-only current card `t_a0000006`. | Latest non-router comment `106` says `REVIEW_VERDICT: APPROVED` but targets `t_b0000006`. | `verdict_value=APPROVED`, `target_validation=cross-target`. | `ambiguous`. | `needs_pm`. | Adds NEEDS-PM fail-closed comment only; must not complete or unblock. |
| `non-latest-verdict-ignored` | Blocked source-only card `t_a0000007`. | Older comment `107` has APPROVED for same card; newer non-router comment `108` has no verdict. | No latest parseable non-router verdict: `verdict_value=null`, `target_validation=not-applicable`. | `unknown`. | `skip`. | No mutation; stale/non-latest verdict is ignored. |
| `repeated-run-idempotent-skips-existing-key` | Blocked source-only card `t_a0000008` with existing complete idempotency key. | Latest non-router comment `109` says APPROVED and targets same card. | `verdict_value=APPROVED`, `target_validation=same-card`. | `source_docs_spec_test_only`. | `skip`. | No mutation; existing complete idempotency key suppresses duplicate completion. |
| `repeated-needs-pm-idempotent-skips-existing-key` | Blocked source-only card `t_a0000009` with existing needs_pm idempotency key. | Latest non-router comment `111` says APPROVED but targets another card. | `verdict_value=APPROVED`, `target_validation=cross-target`. | `ambiguous`. | `skip`. | No mutation; existing fail-closed PM idempotency key suppresses duplicate comment. |
| `repeated-needs-operator-idempotent-skips-existing-key` | Blocked A3/live-runtime card `t_a0000010` with existing needs_operator idempotency key. | Latest non-router comment `112` says APPROVED and targets same card. | `verdict_value=APPROVED`, `target_validation=same-card`. | `operator_gated`. | `skip`. | No mutation; existing operator idempotency key suppresses duplicate comment. |
| `cross-target-operator-gated-approval-needs-operator` | Blocked production DB deploy card `t_a0000011`. | Latest non-router comment `113` says APPROVED but targets `t_b0000011`. | `verdict_value=APPROVED`, `target_validation=cross-target`. | `operator_gated`. | `needs_operator`. | Adds NEEDS-OPERATOR comment only; operator-gated cards are not downgraded to completion/unblock. |
| `approved-with-no-task-id-fails-closed` | Blocked source-only card `t_a0000012`. | Latest non-router comment `114` says APPROVED but names no task id. | `verdict_value=APPROVED`, `target_validation=missing-target`. | `ambiguous`. | `needs_pm`. | Adds NEEDS-PM fail-closed comment only; explicit target is required for APPROVED. |
| `changes-requested-cross-target-fails-closed` | Blocked source-only card `t_a0000013`. | Latest non-router comment `115` says CHANGES_REQUESTED but targets another card. | `verdict_value=CHANGES_REQUESTED`, `target_validation=cross-target`. | `ambiguous`. | `needs_pm`. | Adds NEEDS-PM fail-closed comment only; must not unblock current card. |
| `custom-changes-requested-for-fails-closed` | Blocked source-only card `t_a0000014`. | Latest non-router comment `116` says unsupported `CHANGES_REQUESTED_FOR_DOCS`. | `verdict_value=CHANGES_REQUESTED_FOR_DOCS`, `target_validation=same-card`. | `ambiguous`. | `needs_pm`. | Adds NEEDS-PM fail-closed comment only; must not complete or unblock. |
| `frontend-app-without-verify-pass-needs-pm` | Blocked frontend/app card `t_a0000015` with `apps/web/...` changed file and no positive `VERIFY_PASS`. | Latest non-router comment `117` says APPROVED and targets same card, but explicitly notes missing VERIFY_PASS. | `verdict_value=APPROVED`, `target_validation=same-card`. | `ambiguous` because running-app evidence is missing. | `needs_pm`. | Adds NEEDS-PM fail-closed comment containing `frontend/app work without VERIFY_PASS`; must not complete or unblock. |
| `changes-requested-operator-gated-needs-operator` | Blocked deploy/DB/live card `t_a0000018`. | Latest non-router comment `119` says `REVIEW_VERDICT=CHANGES_REQUESTED` targeting same card with blocking finding about missing rollback verification. | `verdict_value=CHANGES_REQUESTED`, `target_validation=same-card`. | `operator_gated` because card scope includes deploy/DB/live/rollback terms. | `needs_operator`. | Adds NEEDS-OPERATOR comment only; CHANGES_REQUESTED on operator-gated cards must not unblock. |
| `router-authored-verdict-value-comment-ignored` | Blocked source-only card `t_a0000016`. | Only comment `110` is authored by `verdict-router` and contains `verdict_value=APPROVED`, not a source `REVIEW_VERDICT`. | No latest non-router verdict: `verdict_value=null`, `target_validation=not-applicable`. | `unknown`. | `skip`. | No mutation; router-authored echo/comment is ignored as a verdict source. |
| `nonnumeric-older-created-at-does-not-outrank-newer-numeric-verdict` | Blocked source-only card `t_a0000019` with corrupt `created_at="%s"` on the older verdict. | Older comment `120` has `REVIEW_VERDICT=CHANGES_REQUESTED` but `created_at="%s"` (sorts as 0); newer comment `121` has `REVIEW_VERDICT=APPROVED` with numeric `created_at`. | `verdict_value=APPROVED`, `target_validation=same-card`; corrupt `%s` timestamp sorts as 0 so latest numeric verdict wins. | `source_docs_spec_test_only`. | `complete`. | Mutates only by completing the source card; corrupt older timestamps do not outrank newer numeric verdicts. |
| `nonnumeric-comment-id-percent-s-is-skipped-with-log` | Blocked source-only card `t_a0000020` with corrupt `id="%s"`. | Only comment has `id="%s"` with `REVIEW_VERDICT=APPROVED`. | Non-numeric comment id is skipped; `verdict_value=null`, `target_validation=not-applicable`. | `unknown`. | `skip`. | No mutation; router log contains `skip-nonnumeric-int context=jarvis-os/t_a0000020:task_comments.id value='%s'`. |
|| `scan-continues-after-nonnumeric-comment-id` | Blocked source-only card `t_a0000021` after a corrupt peer `t_a0000022` with `id="%s"`. | Valid comment `122` has `REVIEW_VERDICT=APPROVED` for `t_a0000021`; corrupt peer is in `extra_tasks`. | `verdict_value=APPROVED`, `target_validation=same-card`; corrupt peer skipped with log but scan continues. | `source_docs_spec_test_only`. | `complete`. | Mutates by closing valid card; router log from peer scan contains `skip-nonnumeric-int context=jarvis-os/t_a0000022:task_comments.id value='%s'`. |
|| `c1-gate-denial-reviewer-prose-approved-completes` | Blocked source-only card `t_c1a0b1e2`. | Latest non-router comment `124` says `REVIEW_VERDICT=APPROVED` and denies A3/credential/prod/DB gates in reviewer prose only. | `verdict_value=APPROVED`, `target_validation=same-card`. | `source_docs_spec_test_only` because title/body do not carry deploy/DB/live/A3 scope. | `complete`. | Reviewer gate-denial nouns must not operator-gate a source/docs/spec/test-only card. |
|| `c3-genuine-prod-db-credential-operator-gated-needs-operator` | Blocked deploy/DB/live/A3 card `t_c3operator1`. | Latest non-router comment `125` says `REVIEW_VERDICT=APPROVED` but the task title/body genuinely indicate production DB migration, live deploy, schema write, and gateway restart. | `verdict_value=APPROVED`, `target_validation=same-card`. | `operator_gated`. | `needs_operator`. | Adds NEEDS-OPERATOR comment only; must not complete or unblock. |

Invariants asserted by the harness:
- All fixture runs are dry-run/in-memory and perform no live board reads or writes.
- `--mutation-planning` reuses the same fixture expectations while asserting planned mutations without enabling live mutations.
- `--router-script` executes the production script only against temp fixture DBs and reports pass/fail deltas without live board side effects.
- Router output comments must not contain parseable `REVIEW_VERDICT[:=]` tokens.
- Every mutation/comment action must include an idempotency key.
- Every structured plan/log entry must include a non-empty `reason` alongside `action` and `result` so dry-run output has auditable skip/action reasons.
- Forbidden mutation lists prevent completion/unblock on operator-gated, ambiguous, malformed, off-target, stale, and idempotent cases.

Runnable commands:

```bash
# Full agent-hooks selftest suite (recommended — runs all 3 harnesses)
/home/frank/.hermes/agent-hooks/run-selftests.sh

# Reference fixture contract; no live board access or mutation.
/home/frank/.hermes/agent-hooks/verdict-router.selftest.sh

# Regression check for an implementation; the harness writes an isolated temporary kanban DB
# and redirects verdict-router note/log/state paths away from live boards.
python3 /home/frank/.hermes/agent-hooks/verdict-router-harness.py \
  --fixtures /home/frank/.hermes/agent-hooks/verdict-router.fixtures.json \
  --router-script /path/to/scripts/verdict_router.py
```
