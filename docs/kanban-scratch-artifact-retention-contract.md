# Scratch completion artifact-retention contract

Status: source-only design proposal; not installed or active.
Task: `jarvis-os/t_90a453ed`
Baseline inspected: Hermes `87f12e99ba`.

## Invariant

For a managed `workspace_kind=scratch` task, completion may remove the workspace only after every declared deliverable has a durable attachment row and a verified digest. If the declaration, copy, verification, or durable commit is uncertain, completion remains in-flight and the scratch workspace remains available for retry.

This contract protects only declared deliverables. A worker with no deliverables must make an explicit `artifact_policy: none` declaration; omission is not equivalent to none.

## API shape (additive)

Keep `kanban_complete(artifacts=[...])` as the ergonomic shorthand. The tool boundary translates each path into this internal, versioned manifest before calling the completion kernel:

```json
{
  "artifact_manifest": {
    "schema_version": 1,
    "policy": "retain",
    "entries": [
      {
        "source_path": "/managed/scratch/t_x/report.md",
        "filename": "report.md",
        "size": 1234,
        "sha256": "<64 lowercase hex>",
        "expected_sha256": "<optional second assertion>"
      }
    ]
  }
}
```

The normalized kernel contract requires `size` and `sha256` on every `retain` entry. `expected_sha256` is an optional compatibility assertion; when present it is independently format-validated and must match the same source snapshot. The kernel always computes its own source and destination digest rather than trusting caller metadata.

A no-deliverable completion must send:

```json
{"artifact_manifest": {"schema_version": 1, "policy": "none", "entries": []}}
```

`policy=retain` requires one or more entries. Paths must be regular files strictly below the managed scratch workspace. Existing external attachment paths remain supported for non-scratch tasks, but must not be used to satisfy the scratch retention invariant.

## Lifecycle protocol

1. **Normalize and validate, before task mutation.** Require the manifest for managed scratch completion. Validate schema version, policy, path containment, basename, regular-file status, integer size, the maximum artifact size, and every supplied SHA-256 value as exactly 64 lowercase hexadecimal characters. Compute `source_sha256` and `source_size` immediately before staging. Declared size and digest must match that source snapshot.
2. **Stage atomically, outside the scratch workspace.** Create a task-scoped attachment staging directory outside the scratch workspace. Copy each source to a unique `.partial` file in bounded chunks while hashing the bytes actually read. Flush and `fsync` the file, compare byte count and digest to the source snapshot, re-read the source and require the same size/digest (detecting source mutation during staging), then atomically rename the temporary file to its final attachment name. Any exception removes all files from this attempt and raises `ArtifactPreservationError`.
3. **Commit the task and manifest in one existing `write_txn`.** Re-check the task status and `expected_run_id` with the current CAS. If the CAS loses (including repeated completion), discard the staged files and return `False`; do not create duplicate rows. On success, insert the existing `task_attachments` row plus additive digest/verification columns, persist the exact manifest in run metadata and the `completed` event, and commit.
4. **Cleanup only after commit.** Call the existing scratch cleanup after the durable commit. Cleanup failure does not erase attachments or roll the task back; record `cleanup_status=deferred`, a non-sensitive error type, and recovery metadata for the normal cleanup/recovery sweep. Successful cleanup records `cleanup_status=completed`.
5. **Recovery is deterministic and non-destructive.** A startup/GC recovery pass reads task-scoped manifests with `state=staged` or `cleanup_status=deferred`. It verifies final attachment paths and digests, removes only orphan `.partial` files belonging to that manifest, and never deletes a scratch workspace unless the committed manifest is complete. Unknown or contradictory states are quarantined for review, not guessed away.

## Additive storage

Prefer additive nullable columns on `task_attachments` for compatibility:

- `sha256 TEXT NULL` — lowercase SHA-256 of stored bytes.
- `verified_at INTEGER NULL` — epoch when stored bytes were re-read and matched.
- `verification_status TEXT NULL` — `verified`, `pending`, or `recovery_required` (business state, not a kanban status).
- `artifact_manifest_version INTEGER NULL` — manifest schema version.

Keep the full immutable manifest/recovery object in the existing `task_runs.metadata` and completion event payload. Do not replace `task_attachments` or introduce a competing attachment API.

## Fail-closed cases

- missing manifest on managed scratch completion;
- `retain` with no entries or `none` with entries;
- missing/non-regular/out-of-workspace source;
- missing/invalid digest, invalid/oversize size, source digest/size, or expected digest mismatch;
- source mutation during staging;
- partial copy, fsync, rename, or destination re-read failure;
- attachment row/manifest commit failure;
- ambiguous recovery state.

In every case: no `done` transition, no scratch deletion, no partial attachment exposed to consumers, and a structured error/event that identifies the retryable stage without leaking secrets. The sole exception is a post-commit cleanup failure: the task remains `done`, attachments remain durable, and recovery metadata records `cleanup_status=deferred`.

## Compatibility and rollout

- Non-scratch `dir`/`worktree` completion keeps current semantics; only existing attachment handling is reused.
- For a bounded compatibility period, the tool may translate legacy `artifacts=[...]` to the normalized `retain` manifest, computing size and digest at the tool boundary; it must not infer `policy=none` from omission for managed scratch tasks.
- The legacy prose-discovery helper becomes advisory evidence only. It may suggest paths in an error, never silently manufacture a declaration.
- No config, provider, credential, scheduler, live-board migration, runtime activation, or deployment is part of this proposal.

## Acceptance

The disposable prototype and fresh checker cover: valid retention, missing declarations, expected/declared-hash mismatch, invalid digest format, oversize declaration, injected partial-copy failure, source mutation during staging, repeated completion, cleanup failure with deferred recovery metadata, and receipt persistence. The checker recomputes status, attachment count, file existence, cleanup behavior, and digest evidence independently from the maker's test helpers.
