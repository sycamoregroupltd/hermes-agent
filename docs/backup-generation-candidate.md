# Backup generation verifier candidate

This candidate adds source-only tooling for declaring, validating, and fixture-restoring one
backup generation. It does not take live snapshots, install a job, transfer data, change a
schedule, or advance a `LATEST` pointer.

## Interface

The executable uses only the Python standard library:

```bash
python3 scripts/backup_generation_verify.py generate GENERATION \
  --active-boards-manifest ACTIVE_BOARDS.json
python3 scripts/backup_generation_verify.py verify GENERATION \
  --active-boards-manifest ACTIVE_BOARDS.json
python3 scripts/backup_generation_verify.py restore GENERATION \
  --active-boards-manifest ACTIVE_BOARDS.json --destination EMPTY_DIRECTORY
```

`generate` validates every payload and atomically writes
`GENERATION/backup-generation-manifest.json` last. Re-running it over unchanged inputs produces
identical manifest bytes. `verify` requires the on-disk artifact set to equal the manifest set;
unmanifested files fail just like missing files. `restore` first verifies the source, copies and
re-verifies it under `EMPTY_DIRECTORY/generation`, then manually extracts each archive under
`EMPTY_DIRECTORY/extracted/<archive-path>.tar.gz/`. The destination must already exist, be a real
directory, and be empty.

## Manifest schemas

The supplied active-boards manifest is the source of truth for snapshot coverage. It uses the
fleet's canonical version-1 shape: `boards` is keyed by board slug, every board has a lifecycle
`state`, and other per-board metadata is allowed. The top-level `version` and `boards` keys are
required; additional top-level fleet metadata such as `updated`, `generated_at`, `generator`,
`generated_from`, `doc`, and `states` is allowed:

```json
{
  "version": 1,
  "updated": "2026-09-06T12:00:00Z",
  "generator": "fleet-board-registry",
  "boards": {
    "board-a": {"state": "active", "owner": "operations"},
    "board-b": {"state": "dormant", "reason": "paused"},
    "board-c": {"state": "denied", "reason": "policy"}
  }
}
```

The only accepted states are `active`, `dormant`, and `denied`. Each active board requires the
root-level snapshot `kanban-<slug>.db`; paths are derived by the verifier and cannot be supplied
by the manifest or normalized. Dormant and denied boards require no snapshot. Slugs must be 1–64 lowercase
ASCII alphanumeric, hyphen, or underscore characters, start with an alphanumeric, and produce a
safe portable relative filename. Duplicate JSON keys and duplicate or impossible derived
coverage are rejected.

The generated completion manifest has this shape:

```json
{
  "active_boards": {
    "boards": [
      {"slug": "board-a", "snapshot": "kanban-board-a.db"}
    ],
    "source_sha256": "<sha256 of the supplied active-boards manifest bytes>"
  },
  "artifacts": [
    {
      "kind": "sqlite",
      "path": "kanban-board-a.db",
      "sha256": "<artifact sha256>",
      "size": 8192
    },
    {
      "kind": "tar.gz",
      "path": "archives/state.tar.gz",
      "sha256": "<artifact sha256>",
      "size": 1234
    }
  ],
  "schema_version": 1
}
```

Arrays are sorted and JSON is emitted with sorted keys. Every active board must have exactly one
declared `sqlite` snapshot; every `.tar.gz` is an archive; other regular files use `file`. At least
one archive is required. Verification rejects missing/unmanifested, symlinked, non-regular,
zero-byte, size-mismatched, or SHA-256-mismatched artifacts. It also runs SQLite
`PRAGMA integrity_check` through an immutable read-only connection, reads gzip streams through
their CRC/footer, requires tar end markers, rejects unsafe/duplicate/link/device archive members,
and reads every archived regular file to its declared size.

## Source lineage and operational boundary

This is the restorable-generation companion to the existing rsync hardening lineage at commit
`633dd68d5f3c3602cffa6d36244fd197ed45c258` (`test: cover nightly backup rsync failure telemetry`,
with its bounded retry/exit-code behavior originating in its parent change). That shell behavior
is intentionally not copied here. A future operator-owned integration can call this verifier
before and after that transport boundary.

No scheduler, cron store, remote host, credential, live Hermes home, deployment, or installation
is touched by this candidate. In particular, these tests do **not** prove that a scheduled
off-host generation runs, transfers, verifies remotely, or is restorable from the remote system.

Rollback is source-only: remove
`scripts/backup_generation_verify.py`, `scripts/tests/test_backup_generation_verify.py`, and this
document. No runtime rollback or data migration is needed. A future installation must be a
separately approved operation with its own isolated end-to-end evidence.

## PREPARED A3 packet

- **Candidate:** kanban task `t_d2f9fa75`; source-only, prepared for review, not installed or merged.
- **Safety contract:** manifest written last; current active-board bytes and normalized coverage
  must agree; exact artifact coverage; content hashes/sizes; SQLite and archive integrity; no
  symlink following; manual traversal-safe extraction into an empty caller-owned fixture.
- **Review surface:** one executable module, one focused stdlib test module, and this contract.
- **Prepared evidence:** `python -m py_compile scripts/backup_generation_verify.py
  scripts/tests/test_backup_generation_verify.py`; `python -m unittest -v
  scripts.tests.test_backup_generation_verify`; `git diff --check`.
- **Required follow-up before any operational claim:** independent review, explicit install
  authority, an operator-defined active-boards manifest producer, integration with generation and
  transport, and a real isolated restore drill from the off-host copy.
- **Non-claim:** scheduled/off-host backup generation and recovery are not proven by this packet.
