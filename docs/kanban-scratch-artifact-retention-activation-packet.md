# Scratch artifact-retention activation packet

Task: `jarvis-os/t_90a453ed`
Disposition: design and disposable prototype only; not installed, scheduled, activated, or live.

## Mechanism and ownership

- Existing substrate extended: `complete_task` → `_persist_scratch_completion_artifacts` → existing `task_attachments` store and post-commit `_cleanup_workspace`.
- Proposed durable store: existing per-task attachment directory returned by `task_attachments_dir(task_id)`; the prototype uses `<temporary-root>/<task-id>/` to model that location. Task/run metadata and the completion event remain the manifest authority.
- Future consumer/delivery target: the existing Kanban attachment download/API surface and the normal startup/GC recovery sweep. No new delivery system or core model tool is proposed.
- Source-only liveness declaration: gateway, scheduler, service, live-board, and runtime liveness are **not applicable** to this disposable prototype. No live process was changed or used as evidence.
- Owner for implementation: Hermes kanban completion/attachment maintainer; independent gate: `os-reviewer`; later schema/rollout owner: maintainer-approved implementation card, not this design card.

## Later implementation gates

1. Builder implements the additive manifest/atomic-staging contract in an isolated branch.
2. Changed-code tests and an independent different-provider checker prove all required scenarios against temporary directories and synthetic SQLite only.
3. `os-reviewer` verifies task CAS, attachment rows, recovery metadata, and current cleanup behavior remain compatible.
4. A maintainer reviews any schema migration/backfill plan. Existing attachment rows without digests remain `verification_status=pending`; no destructive rewrite is allowed.
5. Only after explicit approval may a separately scoped canary use a copied board fixture. Live board migration, scheduler changes, runtime activation, alerting, deployment, and rollback are separate gates.

## Verification matrix (source-only)

| Requirement | Evidence / exact command | Result |
|---|---|---|
| valid attachment, digest and cleanup | `python3 tests/test_scratch_artifact_guard.py`; `python3 tests/independent_scratch_artifact_checker.py` | maker and checker pass after rework |
| explicit no-deliverable policy | same two direct commands | first completion with `policy=none, entries=[]` cleans up with zero attachment rows |
| missing declaration, invalid digest, oversize | same two direct commands | fail-closed cases covered |
| hash mismatch, partial copy, source mutation | same two direct commands | no `done`, workspace retained, no leaked final state |
| expected digest mismatch | same two direct commands | wrong `expected_sha256` fails closed and retains workspace |
| repeated completion | same two direct commands | CAS/idempotence behavior covered |
| cleanup failure and recovery metadata | same two direct commands | `done` retained with `cleanup_status=deferred` |
| post-commit receipt-write failure | same two direct commands | no post-commit exception; a partial refresh cannot truncate the pending receipt; committed attachments remain and reconciliation is deferred |
| source syntax | `python3 -m py_compile prototypes/scratch_artifact_guard.py tests/test_scratch_artifact_guard.py tests/independent_scratch_artifact_checker.py` | required before handoff |
| repository hygiene | `git diff --check` and `sha256sum -c SHA256SUMS` | required before handoff |
| gateway/scheduler/service liveness | no command; N/A rationale above | not applicable to disposable source-only model |

## Exact files/copies executed in this rework

- Edited only the isolated source worktree files: `prototypes/scratch_artifact_guard.py`, `tests/test_scratch_artifact_guard.py`, `tests/independent_scratch_artifact_checker.py`, this packet, the contract, and source inspection.
- The prototype copies synthetic temporary-directory files to its task-scoped temporary attachment store; it does not copy or mutate a live Kanban store.
- Direct-script import bootstrap was added so the exact commands above run from the repository root without an undocumented `PYTHONPATH` override.

## Rollback

Before any activation, retain the prior source revision and a reversible migration plan. If the guard rejects valid completions, disable only the new code path through the reviewed release mechanism and preserve staged manifests/attachments for recovery. Never delete the scratch workspace or attachment store as a rollback shortcut.

## Non-actions in this card

No live `kanban.db` read/write/copy/backfill, no cron or service action, no config/provider/credential change, no gateway activation, no deployment, no merge to main, and no dynamic worker spawning.
