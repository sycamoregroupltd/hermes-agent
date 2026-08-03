# Completion-gate classifier fixture matrix

Task: t_a916f335

Purpose: concise expected-behavior matrix for regression fixtures covering `agent-hooks/gate-kanban-complete-classifier.py` and `agent-hooks/gate-kanban-complete.fixtures.json`.

Tests should assert two layers when `expected_class` is present:
- classifier category: direct classifier stdout is one of `readonly_nonapp`, `web`, or `not_web`.
- completion gate decision: hook result is `allow` or `block` based on the required evidence.

| Fixture / representative task | Representative title/body shape | Expected classifier category | Expected verification gate / requirement | Expected hook decision without frontend running-app evidence | Why this belongs here |
|---|---|---|---|---|---|
| `task-type-cron-config-evidence-gate-maps-readonly-nonapp-allows` | `Verify cron/config bookkeeping completion gate mapping`; cron/config operational bookkeeping, before/after cron-store state, forced smoke output, explicit no app/runtime surface changed | `readonly_nonapp` | evidence-based task-specific verification: backup/readback/before-after state/smoke output; not frontend `verify-running-app.sh` | `allow` when the completion supplies task-specific evidence | Cron/config/bookkeeping/non-code operational work can mention `VERIFY_PASS` as an evidence label, but it does not touch a browser/app route and should not require the frontend running-app gate. |
| `t_f5f5385f-cron-config-repoint-allows` / `t_cbede0a3-provider-bookkeeping-allows` | Provider/model pin or provider-state bookkeeping; backup `jobs.json` or state; forced provider/cron smoke; explicit no provider routing or app surface changes | `readonly_nonapp` | evidence-based operational proof: backup path, before/after pin/state, forced smoke/probe output, no credentials or routing drift | `allow` when those proofs are present | These are real false-positive classes: operational state/bookkeeping tasks are verified by artifact/state evidence rather than by serving a frontend route. |
| `task-type-frontend-route-maps-web-running-app-gate-blocks` | `Build frontend completion-gate task-type dashboard route`; implement `apps/web` React/dashboard route/page/layout/component | `web` | existing frontend running-app verification gate: real `VERIFY_PASS` from `verify-running-app.sh` for the touched route/host | `block` until real running-app `VERIFY_PASS` evidence is supplied | True frontend/web/app work changes a route/page/component and must keep the hard running-app gate, even if the page renders classifier or cron/config status. |
| `baseline-frontend-with-verify-allows` | Build marketplace frontend page; completion summary includes `VERIFY_PASS /marketplace :: HTTP 200, real content` | `web` | existing frontend running-app verification gate with current route evidence | `allow` only with real `VERIFY_PASS` evidence | Positive control proving the frontend gate is not an unconditional block; it allows only when the required route evidence exists. |
| `task-type-ambiguous-fallback-maps-not-web-allows` | `Update completion checklist wording`; ambiguous/local checklist task with no frontend/web/app surface terms and non-app changed files | `not_web` | safe default: no frontend running-app gate; require ordinary task-appropriate command/readback evidence | `allow` when ordinary evidence is supplied | Ambiguous/fallback prose with no app-surface signal should not be over-classified as frontend. The intended safe default is `ALLOW_DEFAULT_NOT_WEB` / `not_web`, not `readonly_nonapp` via broad keywords and not `web`. |
| `task-type-ambiguous-body-with-app-changed-files-maps-web-blocks` | Ambiguous checklist/report-only prose, but completion metadata `changed_files` includes `apps/web/.../page.tsx` | `web` | existing frontend running-app verification gate; `changed_files` app-surface metadata wins over fallback prose | `block` until real running-app `VERIFY_PASS` evidence is supplied | Regression guard for the unsafe fallback edge: ambiguous body text cannot hide concrete frontend changed files. App-impacting metadata must map to `web`. |
| `cron-config-wording-paired-frontend-negative-blocks` / `t_11fb678a-paired-frontend-category-negative-blocks` | Frontend dashboard/page/route work that mentions cron/config, bookkeeping, classifier, or task-type categories | `web` | existing frontend running-app verification gate | `block` without real `VERIFY_PASS` | Paired negatives prevent allow-list wording for operational tasks from becoming a broad bypass for real app UI work. |
| `selftest-cli-script-nonweb-command-evidence-allows` | Non-web CLI/Python/shell worker-visibility preflight; py_compile/bash/pytest/temp JSON evidence; explicit no frontend/browser UI touched | `readonly_nonapp` or hook-level non-web allow | command evidence gate: py_compile, bash -n, tests, temp artifact readback | `allow` when command evidence is supplied | Non-code/non-web operational test tasks are verified by deterministic command output. Mentions of scripts/helpers should not trigger frontend route verification unless paired with actual frontend work. |
| `t_cdeab9c4-source-pr-review-guc-bypass-allows` | Paper-only source PR review whose body quotes the PostgreSQL GUC `app.bypass_append_only` (e.g. DataRetentionService append-only bypass fix), with `REVIEW_VERDICT` and no app-surface changed files | `readonly_nonapp` | source-PR-review lane: paper-only review evidence (verdict + task-evidence note), not frontend `verify-running-app.sh` | `allow` when the review verdict + evidence note are supplied | Real regression: a bare dotted SQL GUC/schema identifier (`app.<snake_ident>`) is a database namespace, NOT a frontend "app"; without the negation it trips `APP_IMPL_PATTERNS` ("fix ... app") and wrongly demands frontend VERIFY_PASS. |
| `t_cdeab9c4-paired-frontend-guc-bypass-negative-blocks` | Concrete `apps/web` React dashboard route/page that renders `app.bypass_append_only` GUC state; no VERIFY_PASS supplied | `web` | existing frontend running-app verification gate | `block` until real running-app `VERIFY_PASS` evidence is supplied | Paired negative for the GUC allow terms; real frontend route/page work must still require running-app VERIFY_PASS even when it mentions the GUC. |

## Assertion names tests should use

- Operational evidence category: `readonly_nonapp` via `ALLOW_NONAPP_OVERRIDE_ONLY_WITHOUT_APP_IMPL` or `ALLOW_READONLY_EVIDENCE_ONLY_WITHOUT_WEB_SURFACE`; hook decision `allow`; required evidence label `evidence-based task-specific verification`.
- Frontend/app category: `web` via `BLOCK_APP_CHANGED_FILES_NEED_VERIFY_PASS` or `BLOCK_WEB_SURFACE_NEEDS_VERIFY_PASS`; hook decision `block` without `VERIFY_PASS`; required evidence label `running-app VERIFY_PASS`.
- Ambiguous/fallback category: `not_web` via `ALLOW_DEFAULT_NOT_WEB`; hook decision `allow` with ordinary task evidence; no frontend running-app requirement unless app changed_files or app-surface task wording appears.

## Maintenance rule

Every new operational/non-app allow rule must have paired frontend/app negative fixtures using similar wording plus concrete app-surface title/body and changed-files-aware coverage. At least one negative for the new allow class should prove `changed_files` containing an app surface (for example `apps/web/.../page.tsx`) maps to `web` and blocks without running-app `VERIFY_PASS`, so the suite proves the allow term cannot bypass the running-app gate.

## Quick start — exact commands

All commands run from `/home/frank/.hermes/agent-hooks/`.

### Full regression selftest (83+ cases)

```bash
cd /home/frank/.hermes/agent-hooks
./gate-kanban-complete.selftest.sh
```

Runs 65+ fixture-corpus gate-decision assertions + 18 hand-written DB-based cases + 16 `expected_class` classifier assertions. Creates an isolated temp kanban DB — never touches live boards. Exits with `"gate-kanban-complete self-test PASS"`.

### Regression probe (pre-fix vs HEAD)

```bash
git show 28d1eba^:agent-hooks/gate-kanban-complete-classifier.py > /tmp/classifier_pre_fix.py
python3 /home/frank/.hermes/agent-hooks/gate-kanban-complete.regression-probe.py
```

Compares 16 `expected_class` fixtures across Expected, Pre-fix (28d1eba^), and HEAD columns. Misclassifications are marked with `<--`.

### Preflight validation

```bash
python3 -m json.tool gate-kanban-complete.fixtures.json > /dev/null    # valid JSON
python3 -m py_compile gate-kanban-complete-classifier.py               # syntax
bash -n gate-kanban-complete.selftest.sh                               # shell syntax
```

### Selftest with custom hook path

```bash
./gate-kanban-complete.selftest.sh /home/frank/.hermes/agent-hooks/gate-kanban-complete.sh
```
