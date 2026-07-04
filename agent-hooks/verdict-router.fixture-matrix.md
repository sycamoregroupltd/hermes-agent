# REVIEW_VERDICT router fixture and expectation matrix

Task: t_bdac52bc

Purpose: deterministic, importable coverage for REVIEW_VERDICT router behavior without live kanban access.

Primary importable fixtures: `agent-hooks/verdict-router.fixtures.json`
Reference harness/self-test: `agent-hooks/verdict-router-harness.py` and `agent-hooks/verdict-router.selftest.sh`
Production-script isolation adapter: `python3 agent-hooks/verdict-router-harness.py --router-script scripts/verdict_router.py` creates a temporary kanban DB per fixture and redirects router note/log/state paths away from live boards.

The JSON fixtures use an in-memory board-card model. Each fixture has:
- `board`: board slug used for target validation/idempotency keys.
- `task`: representative blocked/non-blocked card state, comments, optional changed files, and optional existing idempotency keys.
- `expect`: expected parse/classification/action/mutation contract asserted by the harness.

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
| `router-authored-verdict-value-comment-ignored` | Blocked source-only card `t_a0000016`. | Only comment `110` is authored by `verdict-router` and contains `verdict_value=APPROVED`, not a source `REVIEW_VERDICT`. | No latest non-router verdict: `verdict_value=null`, `target_validation=not-applicable`. | `unknown`. | `skip`. | No mutation; router-authored echo/comment is ignored as a verdict source. |

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
# Reference fixture contract; no live board access or mutation.
/home/frank/.hermes/agent-hooks/verdict-router.selftest.sh

# Regression check for an implementation; the harness writes an isolated temporary kanban DB
# and redirects verdict-router note/log/state paths away from live boards.
python3 /home/frank/.hermes/agent-hooks/verdict-router-harness.py \
  --fixtures /home/frank/.hermes/agent-hooks/verdict-router.fixtures.json \
  --router-script /path/to/scripts/verdict_router.py
```
