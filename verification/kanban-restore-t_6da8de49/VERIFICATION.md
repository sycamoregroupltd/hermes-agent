# t_6da8de49 exact-source restore candidate

Status: staged only; not installed, not activated, not merged.
Project: control-plane
Board/tracker: jarvis-os
Cron: 93ced04b18bf (`fleet-kanban-integrity-backup-5boards`)
Named consumer: cron 93ced04b18bf / fleet-kanban-integrity-backup-5boards

## Source reconstruction

- Fresh isolated base: `origin/main` at `9516ac130eed1cfde699452bde56284d5fcbc736`.
- Worktree: `/home/frank/.hermes-worktrees/t_6da8de49`, branch `wt/t_6da8de49`.
- The candidate is the 1118-line hardening recovered from the reviewed post-rebuild lineage (`2b7182e74a`, exact blob SHA-256 `8e110743a6b8be02d042990c7f7e57f392f4a95a0c1f4fa78e03b25736b9fb48`).
- `git diff --no-index /tmp/t_6da8de49_candidate_source.py profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py` returned `DIFF_RC=0`.
- The prior reviewed/rebuild implementation `c18cb80bac` is the 511-line blob SHA-256 `e0c0a50c27ba16ac01079e064849342a33553674327b29921d5706a19a49fa26`; the live executed copy is still exactly that 511-line hash. It was not copied over the candidate.
- Current candidate path: `profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py`.

The candidate preserves newest integrity-ok backup selection, dispatcher `a9def8c365df` quiesce before swap, `PRAGMA wal_checkpoint(TRUNCATE)`, fsync + atomic `os.replace`, index-only `REINDEX`, page-only restore, unknown fail-closed behavior, lost-task delta in `FLEET_KANBAN_DB_RESTORED`, failed-post-check reentrancy guard, WAL-safe read-only backup, coverage-gap detection, restore-eligibility split, per-artifact verification, manifest/LATEST gating, and backup-health recovery evidence.

## Executed verification

Commands and observed results:

```text
python3 -m py_compile profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py
PY_COMPILE=PASS

python3 -m json.tool /home/frank/.hermes/profiles/jarvis/cron/jobs.json >/dev/null
CRON_STORE_JSON=VALID

python3 -m json.tool verification/kanban-restore-t_6da8de49/named-consumer-receipt.json >/dev/null
RECEIPT_JSON=VALID

python3 -m json.tool verification/kanban-restore-t_6da8de49/production-db-snapshot.json >/dev/null
SNAPSHOT_JSON=VALID

python3 verification/kanban-restore-t_6da8de49/restore_matrix.py
HARNESS_RC=0
TOTAL 22/22 passed
```

The matrix uses a temporary `/tmp/t_6da8de49_matrix_*` root only. It creates ten fixture boards, rejects a newer corrupt backup, physically probes page corruption, selects the newest integrity-ok backup, exercises the exact candidate `main()` restore path with a controlled check double, proves dispatcher pause/resume traces, atomic replacement, WAL hook, lost-task alert delta, reentrancy guard, manifest/LATEST fail-closed state, and no restore-temp residue.

The expected simulated board failure is visible in the harness output as `[FAIL] jarvis-os: database disk image is malformed`; it is the injected corruption input. The harness itself exits 0 and reports 22/22.

## Named-consumer controlled receipt

`verification/kanban-restore-t_6da8de49/named-consumer-receipt.json` is a test-double receipt, not an outbound alert. It records:

- exact candidate hash and line count;
- store liveness from throwaway SQLite plus read-only production snapshot;
- gateway/command-runner liveness with no network and no credentials;
- primary target `discord:#critical-alerts` simulated failure;
- fallback `whatsapp:Frank` simulated success;
- named cron consumer `93ced04b18bf`;
- lost-task delta `t_lost_delta_probe_6da8de49`;
- matrix row `restore dry-run against throwaway board DB: 22/22 PASS`;
- `auto_restore_enabled: false`, `live_db_mutated: false`.

No real outbound test was attempted.

## Production read-only proof

`verification/kanban-restore-t_6da8de49/production-db-snapshot.json` contains before/after SHA-256 and byte-size tuples for all ten protected live DB paths. The harness observed `unchanged: true` for all ten. The live executed script was separately read and measured as:

```text
e0c0a50c27ba16ac01079e064849342a33553674327b29921d5706a19a49fa26  /home/frank/.hermes/profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py
511 /home/frank/.hermes/profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py
```

The live cron JSON is valid and contains no enabling value for `KANBAN_AUTO_RESTORE_ENABLED`; the candidate default is OFF. No live DB, cron JSON, cron configuration, or live script was modified.

## A3 install / rollback packet (not executed)

Activation and installation remain an A3 live-infra gate for Frank and require independent `os-reviewer` approval. After approval only, from a clean checked-out candidate:

```bash
LIVE=/home/frank/.hermes/profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py
CANDIDATE=/home/frank/.hermes-worktrees/t_6da8de49/profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
cp -a "$LIVE" "$LIVE.bak-t_6da8de49-$STAMP"
install -m 0755 "$CANDIDATE" "$LIVE.tmp-t_6da8de49-$STAMP"
python3 -m py_compile "$LIVE.tmp-t_6da8de49-$STAMP"
cmp -s "$CANDIDATE" "$LIVE.tmp-t_6da8de49-$STAMP"
mv -f "$LIVE.tmp-t_6da8de49-$STAMP" "$LIVE"
```

Post-install verification, still with `KANBAN_AUTO_RESTORE_ENABLED` unset/off:

```bash
cmp -s "$CANDIDATE" "$LIVE"
python3 -m py_compile "$LIVE"
/home/frank/.local/bin/hermes -p jarvis cron run 93ced04b18bf
# read back the jarvis cron execution row; require status=completed/ok,
# last_error=null, last_delivery_error=null, and the exact candidate hash.
```

Rollback uses the timestamped backup made immediately before installation:

```bash
cp -a "$LIVE.bak-t_6da8de49-$STAMP" "$LIVE"
python3 -m py_compile "$LIVE"
cmp -s "$LIVE.bak-t_6da8de49-$STAMP" "$LIVE"
```

This packet does not authorize the commands and none were run in this task.

## Hotspot and review routing

`/home/frank/.hermes` is a severe shared dirty-tree hotspot. The live tree was read only; no checkout, switch, reset, stash, pull, merge, rebase, clean, install, cron edit, or production restore was performed. All candidate edits and verification artifacts are isolated to `wt/t_6da8de49`.

Independent checker required: `os-reviewer` using a different vendor/model. This task should enter review; it must not be self-certified or installed from this worker.
