#!/usr/bin/env bash
# native-hermes-backup (no_agent cron wrapper)
# Idempotent, non-destructive critical-state snapshot.
# `hermes backup --quick --label weekly` writes to a NEW timestamped dir under
# profiles/jarvis/state-snapshots/ each run — it never overwrites an existing
# backup, so re-running is safe and cannot corrupt prior backups.
#
# 2026-08-29 (t_b2474c19, fable-db-architect) — THIS JOB DOES NOT BACK UP BOARDS.
# It advertised a "full state snapshot" and reported ok nightly, while all 19
# snapshots on disk contained kanban.db at exactly 8192 bytes with ZERO tables
# and no kanban/boards/ directory at all.
#
# ROOT CAUSE (measured, not inferred): hermes_cli/backup.py::_QUICK_STATE_FILES
# DOES list "kanban.db" and "kanban/boards" — but they are resolved relative to
# $HERMES_HOME, and this job runs under HERMES_HOME=/home/frank/.hermes/profiles/jarvis
# (verified: /proc/<gateway pid>/environ). The real boards live in the ROOT home at
# /home/frank/.hermes/kanban/boards/, so under the jarvis profile:
#   $HERMES_HOME/kanban/boards  -> does not exist -> silently skipped
#   $HERMES_HOME/kanban.db      -> an empty 8192-byte file from 2026-06-30
# Fleet memory "Fleet runs under profiles, not root" is exactly this class.
#
# DECISION: do NOT widen this job to include boards. They are already backed up
# hourly, with integrity + task-count verification and 48-deep per-board
# retention, by fleet-kanban-integrity-backup-5boards
# (jarvis_os_kanban_integrity_backup.py). Pulling ~330MB of boards into 20 kept
# snapshots would duplicate that at ~6.4GB for no recovery benefit. Instead this
# wrapper now STATES its true coverage and ASSERTS the manifest CONTENT, so the
# "ok" it reports means something. A backup that silently omits the thing you
# would most want to restore is worse than no backup: it stops people looking.
set -uo pipefail

HERMES=/home/frank/.local/bin/hermes
TS=$(date -u +%Y-%m-%dT%H:%MZ)

# Guard: don't stack backups if the binary is missing.
if [ ! -x "$HERMES" ]; then
  echo "[SILENT] hermes binary missing at $HERMES; backup skipped $TS"
  exit 0
fi

# Run the actual backup. The 300s in-band cap keeps the cron slot bounded and
# ensures a slow/failed copy FAILS LOUDLY (rc=124) instead of relying on the
# scheduler's hard _DEFAULT_SCRIPT_TIMEOUT=3600s SIGKILL, which previously
# produced silent "Script timed out after 3600s" errors (RCA t_2b90eaa8).
# Without this, `hermes backup --quick` had no internal bound and could run
# past an hour while copying the ~8.6 GB jarvis state.db under live gateway
# WAL write-load. 300s is ~20-30x the normal copy time (~10-15s), so it only
# trips on genuine stalls.
TIMEOUT_CAP_S=300
OUT=$(timeout "${TIMEOUT_CAP_S}" "$HERMES" backup --quick --label weekly 2>&1)
RC=$?
if [ "$RC" -eq 124 ]; then
  echo "BACKUP TIMED OUT (>${TIMEOUT_CAP_S}s) at $TS — state.db copy stalled (likely live gateway WAL contention)."
  echo "$OUT" | tail -20
  exit 124
fi

if [ $RC -ne 0 ]; then
  # Surface the failure (delivered) instead of silently succeeding.
  echo "BACKUP FAILED (rc=$RC) at $TS"
  echo "$OUT" | tail -20
  exit 1
fi

# Report the produced snapshot id + disk size so the run is verifiable.
# `hermes backup` prints: "State snapshot created: <ID>" and stores under the
# active profile's state-snapshots/ dir (resolved at cron-fire time).
SNAP_ID=$(echo "$OUT" | grep -oE "State snapshot created: [A-Za-z0-9._-]+" | awk '{print $4}')
SNAP_DIR=$( [ -n "$SNAP_ID" ] && find /home/frank/.hermes/profiles -maxdepth 3 -type d -name "$SNAP_ID" 2>/dev/null | head -1 )
SIZE=$( [ -n "$SNAP_DIR" ] && du -sh "$SNAP_DIR" 2>/dev/null | cut -f1 )
echo "Hermes weekly quick backup OK at $TS"
echo "Snapshot: ${SNAP_ID:-unknown}"
echo "Path: ${SNAP_DIR:-unknown}"
echo "Size: ${SIZE:-unknown}"
echo "Restore: hermes snapshot restore ${SNAP_ID:-<id>}"

# --------------------------------------------------------------------------
# ASSERT THE ARTIFACT, NOT THE JOB.
# `hermes backup` exiting 0 is not evidence the snapshot holds anything. This
# reads the snapshot's own manifest.json and checks that each critical store is
# present AND non-zero on disk. Anything wrong reaches the EXIT CODE, because
# for a --no-agent cron job stdout is never parsed and rc is the only signal.
# --------------------------------------------------------------------------
if [ -z "$SNAP_DIR" ] || [ ! -d "$SNAP_DIR" ]; then
  echo "BACKUP UNVERIFIABLE: hermes reported success but no snapshot directory was found for id='${SNAP_ID:-}'."
  exit 1
fi

SNAP_DIR="$SNAP_DIR" python3 - <<'PY'
import json, os, sys

snap = os.environ["SNAP_DIR"]
manifest_path = os.path.join(snap, "manifest.json")

# Stores whose loss is unrecoverable from anywhere else in the fleet.
CRITICAL = [
    "config.yaml", ".env", "auth.json",
    "cron/jobs.json", "cron/executions.db",
    "projects.db", "response_store.db", "verification_evidence.db",
]

try:
    with open(manifest_path, encoding="utf-8") as fh:
        m = json.load(fh)
except Exception as exc:
    print(f"BACKUP UNVERIFIABLE: cannot read {manifest_path}: {exc}")
    sys.exit(1)

files = m.get("files") or {}
skipped = m.get("oversized_skipped") or []
failed = m.get("failed_dbs") or []
problems = []

# 1) every critical entry is listed, non-zero, and actually on disk at that size
for rel in CRITICAL:
    size = files.get(rel)
    if size is None:
        problems.append(f"missing from manifest: {rel}")
        continue
    if size <= 0:
        problems.append(f"zero bytes: {rel}")
        continue
    on_disk = os.path.join(snap, rel)
    if not os.path.isfile(on_disk):
        problems.append(f"manifest lists {rel} ({size}B) but it is not on disk")
    elif os.path.getsize(on_disk) != size:
        problems.append(
            f"size mismatch {rel}: manifest {size}B vs on-disk {os.path.getsize(on_disk)}B"
        )

# 2) state.db must be captured, or explicitly recorded as size-skipped
if "state.db" not in files and "state.db" not in skipped:
    problems.append("state.db neither captured nor recorded in oversized_skipped")

# 3) any DB the snapshotter could not copy is a failure
for f in failed:
    problems.append(f"snapshotter failed to copy: {f}")

total = sum(v for v in files.values() if isinstance(v, int))
print()
print(f"Manifest: {len(files)} files, {total / 1048576:.1f} MiB claimed, verified against disk.")

# 4) state the TRUE board coverage, every run, in both directions.
board_entries = [k for k in files if k.startswith("kanban/boards")]
root_kanban = files.get("kanban.db")
if board_entries:
    print(f"Boards INCLUDED: {len(board_entries)} kanban/boards entries.")
else:
    print("COVERAGE — boards NOT included in this snapshot (expected, not a fault):")
    print("  $HERMES_HOME here is profiles/jarvis; the fleet's boards live in the ROOT")
    print("  home at /home/frank/.hermes/kanban/boards/, so the manifest's 'kanban/boards'")
    print("  entry resolves to a path that does not exist and is silently skipped.")
    if root_kanban is not None:
        print(f"  The 'kanban.db' entry above ({root_kanban}B) is the profile-local placeholder,")
        print("  NOT a board — it has zero tables. Do not mistake it for board data.")
    print("  Boards are backed up hourly, integrity- and task-count-verified, by cron job")
    print("  fleet-kanban-integrity-backup-5boards -> ~/.hermes/kanban/backups/integrity-check/")
    print("  (see that store's LATEST + MANIFEST.json for the authoritative board coverage).")

if skipped:
    print(f"NOTE: skipped for size (snapshot is INCOMPLETE for these): {skipped}")

if problems:
    print()
    print("=== SNAPSHOT CONTENT VERIFICATION FAILED ===")
    for p in problems:
        print(f"  {p}")
    print("HERMES_QUICK_BACKUP_CONTENT_FAIL: the job exited 0 but the artifact is not whole.")
    sys.exit(1)

print("Snapshot content verified: every critical store present and non-zero on disk.")
PY
exit $?
