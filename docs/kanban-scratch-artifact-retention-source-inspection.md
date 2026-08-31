# Scratch completion artifact retention — source inspection

Task: `jarvis-os/t_90a453ed`

This is a source-only inspection of the current Hermes substrate at baseline `87f12e99ba`.
It does not open or mutate a live kanban database.

## Existing path

- `tools/kanban_tools.py::_handle_complete` accepts top-level `artifacts`, normalises paths, and merges them into `metadata["artifacts"]`.
- `hermes_cli/kanban_db.py::complete_task` performs the task status CAS, calls `_persist_scratch_completion_artifacts`, inserts rows into the existing `task_attachments` table, records a `completed` event, and then calls `_cleanup_workspace`.
- `_persist_scratch_completion_artifacts` copies managed scratch files to `task_attachments_dir(task_id)`, rejects missing files and copy failures, and raises `ArtifactPreservationError`; the tool keeps the task in-flight on that error.
- The existing attachment schema stores filename, path, size, uploader, and timestamp, but no content digest or verification state.

## Verified gaps

1. `metadata["artifacts"]` is optional. A scratch completion with a real deliverable but no declaration can still reach cleanup; the legacy prose-discovery helper is best-effort and cannot be a trust boundary.
2. The copy is streamed directly into the final destination. It has a size cap and rollback cleanup, but no source-before/source-after digest equality, fsync, atomic rename, or durable staged-manifest state.
3. A caller cannot supply an expected digest, so a source mutation during copying is not explicitly detected.
4. Recovery metadata is limited to the ordinary attachment row and completion event. There is no immutable per-entry record that says which source bytes were verified, which destination bytes were verified, or whether cleanup completed.
5. The status transaction and filesystem copy are coupled procedurally rather than through an explicit two-phase contract. The desired ordering is present in intent (copy before cleanup), but the lifecycle invariant is not represented as a versioned protocol.

## Design implication

Extend the existing attachment path; do not create a second delivery system. Add a versioned, fail-closed artifact manifest and a staged-copy protocol around `complete_task`. Preserve the current post-commit scratch cleanup and existing attachment consumers.

The disposable rework prototype now makes the normalized manifest explicit: retain entries require an integer size and 64-character lowercase SHA-256, source size/digest are re-read after staging to detect mutation, and the default copy is bounded. Cleanup is intentionally outside the simulated commit: an injected cleanup failure leaves the task done and attachments durable while persisting `cleanup_status=deferred` for recovery.
