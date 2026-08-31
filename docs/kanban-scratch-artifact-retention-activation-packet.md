# Scratch artifact-retention activation packet

Task: `jarvis-os/t_90a453ed`
Disposition: design and disposable prototype only.

## Later implementation gates

1. Builder implements the additive manifest/atomic-staging contract in an isolated branch.
2. Changed-code tests and an independent different-provider checker prove all six required scenarios against temporary directories and synthetic SQLite only.
3. `os-reviewer` verifies that task CAS, attachment rows, recovery metadata, and current cleanup behavior remain compatible.
4. A maintainer reviews any schema migration/backfill plan. Existing attachment rows without digests remain `verification_status=pending`; no destructive rewrite is allowed.
5. Only after explicit approval may a separately scoped canary use a copied board fixture. Live board migration, scheduler changes, runtime activation, alerting, deployment, and rollback are separate gates.

## Rollback

Before any activation, retain the prior source revision and a reversible migration plan. If the guard rejects valid completions, disable only the new code path through the reviewed release mechanism and preserve staged manifests/attachments for recovery. Never delete the scratch workspace or attachment store as a rollback shortcut.

## Non-actions in this card

No live `kanban.db` read/write/copy/backfill, no cron or service action, no config/provider/credential change, no gateway activation, no deployment, no merge to main, and no dynamic worker spawning.
