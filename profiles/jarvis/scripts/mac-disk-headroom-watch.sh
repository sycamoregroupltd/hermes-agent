#!/usr/bin/env bash
# mac-disk-headroom-watch.sh — no-agent Hermes cron (Pulse 2026-09-12).
# Alert when Mac Data used% >= WARN_PCT or free GiB < MIN_FREE_GIB. Silent when ok.
# Optional reclaim: RECLAIM=1 — rebuildable caches (Library/Caches + app Cache piles + aged /tmp YSS/build trees)
# + /tmp/claude-* only when age>=48h AND thin lsof shows no open fds (Pulse 19:24 bind).
# Never touches: Downloads, credentials, WhatsApp, Claude vm_bundles / Profiles, user profiles, young/live claude tmp.
set -euo pipefail

HOST="${MAC_DISK_SSH_HOST:-mac}"
WARN_PCT="${MAC_DISK_WARN_PCT:-97}"
MIN_FREE="${MAC_DISK_MIN_FREE_GIB:-12}"
RECLAIM="${RECLAIM:-0}"
DRY_RUN="${DRY_RUN:-0}"
LOG="${MAC_DISK_HEADROOM_LOG:-/home/frank/.hermes/cron/state/mac-disk-headroom-watch.log}"
mkdir -p "$(dirname "$LOG")"

raw=$(ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
  "df -g /System/Volumes/Data 2>/dev/null | awk 'NR==2{print \$2,\$3,\$4,\$5}'" 2>/dev/null || true)
if [ -z "${raw:-}" ]; then
  raw=$(ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
    "df -g ~ 2>/dev/null | awk 'NR==2{print \$2,\$3,\$4,\$5}'" 2>/dev/null || true)
fi
if [ -z "${raw:-}" ]; then
  echo "🔴 MAC DISK: ssh $HOST df failed"
  exit 0
fi

set -- $raw
avail_g="$3"
usep="$4"
usep_n="${usep%%%}"
usep_n="${usep_n%%\%}"

alert=0
if [ "${usep_n:-0}" -ge "$WARN_PCT" ]; then alert=1; fi
if [ "${avail_g:-0}" -lt "$MIN_FREE" ]; then alert=1; fi

if [ "$alert" -eq 0 ]; then
  exit 0
fi

msg="⚠️ MAC DISK HEADROOM: ${avail_g}G free / ${usep} used on Data (thresholds free<${MIN_FREE}G or used>=${WARN_PCT}%). Backup preflight at risk."
echo "$msg"
echo "$(date -u +%FT%TZ) $msg" >> "$LOG"

if [ "$RECLAIM" != "1" ]; then
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
    'ls -d ~/Library/Caches/Homebrew ~/Library/Application\ Support/Caches ~/Library/Application\ Support/Claude/Cache ~/Library/Application\ Support/Code/CachedExtensionVSIXs 2>/dev/null || true'
  echo "MAC DISK RECLAIM dry-run only"
  exit 0
fi

ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
HOME_DIR="${HOME}"
CACHE="$HOME_DIR/Library/Caches"
AS="$HOME_DIR/Library/Application Support"
before=$(df -g /System/Volumes/Data 2>/dev/null | awk 'NR==2{print $4}')

# --- Library/Caches allowlist ---
for t in \
  "$CACHE/Homebrew" \
  "$CACHE/ms-playwright" \
  "$CACHE/Playwright" \
  "$CACHE/com.openai.codex" \
  "$CACHE/com.google.GoogleUpdater" \
  "$CACHE/Google"
do
  [ -e "$t" ] || continue
  rm -rf "$t" || true
done
find "$CACHE" -maxdepth 1 -type d \( -name '*ShipIt*' -o -name '*Updater*' \) -print0 2>/dev/null \
  | xargs -0 -I{} rm -rf {} 2>/dev/null || true

# --- App Support rebuildable piles (Pulse 16:22) ---
# Claude desktop caches (not Profiles / vm_bundles)
for t in \
  "$AS/Claude/Cache" \
  "$AS/Claude/Code Cache" \
  "$AS/Claude/GPUCache" \
  "$AS/Claude/DawnGraphiteCache" \
  "$AS/Claude/DawnWebGPUCache" \
  "$AS/Caches" \
  "$AS/Code/CachedExtensionVSIXs" \
  "$AS/Code/Cache" \
  "$AS/Code/CachedData" \
  "$AS/Code/Code Cache" \
  "$AS/Code/GPUCache" \
  "$AS/Google/Chrome/OptGuideOnDeviceModel" \
  "$AS/Google/Chrome/ShaderCache" \
  "$AS/Google/Chrome/GraphiteDawnCache" \
  "$AS/Google/Chrome/GrShaderCache"
do
  [ -e "$t" ] || continue
  rm -rf "$t" || true
done

# Chrome Service Worker / Component caches (rebuildable)
find "$AS/Google/Chrome" -maxdepth 2 -type d \( \
  -name 'Service Worker' -o -name 'Component Crx Cache' -o -name 'optimization_guide*' \
\) -print0 2>/dev/null | xargs -0 -I{} rm -rf {} 2>/dev/null || true

# --- Electron/app Cache piles (Pulse 17:35) ---
# Rebuildable Cache/Code Cache/GPUCache only — never Profiles / IndexedDB / Local Storage / Login Data
for app in \
  ClickUp Windsurf Slack discord "Ledger Live" Antigravity Replit Cypress Manus obsidian Hermes Comet \
  "cross-the-ages-launcher"
do
  for sub in Cache "Code Cache" GPUCache DawnGraphiteCache DawnWebGPUCache; do
    t="$AS/$app/$sub"
    [ -e "$t" ] || continue
    rm -rf "$t" || true
  done
done

# --- /tmp aged YSS/build/staging trees (Pulse 19:24) ---
# Rebuildable scratch only; age>=24h; never claude-* (may be live), cookies, credentials.
python3 - <<'TMPPY'
import os, time, shutil, subprocess
from pathlib import Path
now=time.time()
ALLOW_PREFIXES=(
  "rel-","card","build-","wt-","yss-","ng","api-staging","pub-staging","pr-yss-",
  "staging_","staging-","ngbuild","codex-min-",
)
ALLOW_EXACT={"api-staging","pub-staging","build-boot-staging","yss-board.json","staging-orders.ts","node-compile-cache"}
deny=("claude","chrome-cookie","cookie","whatsapp","1password","ssh","gnupg",".npmrc","credentials","login data")
for p in Path("/tmp").iterdir():
    name=p.name
    try:
        age_h=(now-p.stat().st_mtime)/3600
    except Exception:
        continue
    if age_h < 24:
        continue
    if any(d in name.lower() for d in deny):
        continue
    ok = name in ALLOW_EXACT or any(name.startswith(pref) for pref in ALLOW_PREFIXES)
    if name.endswith((".log",".txt",".ts",".json",".sh")) and any(k in name for k in ("rel-","yss","ngbuild","staging","build","wt-","pr-","api-","admin-","pub-","nestbuild")):
        ok=True
    if not ok:
        continue
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)
    except Exception:
        pass
TMPPY

# --- /tmp/claude-* age+fd gate (Jarvis bind Pulse 19:24) ---
# Only reclaim when mtime age >= CLAUDE_TMP_MIN_AGE_H (default 48) AND thin lsof has no open path under it.
# Fail closed on lsof errors. Does not touch Library/Application Support/Claude (vm_bundles/Profiles).
python3 - <<'CLAUDETMP'
import os, time, shutil, subprocess
from pathlib import Path
now=time.time()
min_age_h=float(os.environ.get("CLAUDE_TMP_MIN_AGE_H", "48"))
lsof_timeout=int(os.environ.get("CLAUDE_TMP_LSOF_TIMEOUT_SEC", "120"))

def thin_open_prefixes():
    """One lsof -nP -w pass; return set of /tmp/claude-* top dirs with open fds. Fail closed -> None."""
    try:
        r=subprocess.run(["lsof","-nP","-w"], capture_output=True, text=True, timeout=lsof_timeout)
    except Exception:
        return None
    prefs=set()
    for ln in (r.stdout or "").splitlines():
        if "/tmp/claude-" not in ln:
            continue
        for part in ln.split():
            if part.startswith("/tmp/claude-"):
                bits=part.split("/")
                if len(bits)>=3:
                    prefs.add("/".join(bits[:3]))
    return prefs

open_prefs=thin_open_prefixes()
if open_prefs is None:
    print("CLAUDE_TMP skip: lsof failed (fail-closed)")
else:
    for p in sorted(Path("/tmp").glob("claude-*")):
        try:
            st=p.stat()
            age_h=(now-st.st_mtime)/3600.0
        except Exception:
            continue
        if age_h < min_age_h:
            print(f"CLAUDE_TMP keep young age_h={age_h:.1f} path={p}")
            continue
        key=str(p)
        busy = key in open_prefs or any(op == key or op.startswith(key + "/") for op in open_prefs)
        if busy:
            print(f"CLAUDE_TMP keep open_fds age_h={age_h:.1f} path={p}")
            continue
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink(missing_ok=True)
            print(f"CLAUDE_TMP reclaimed age_h={age_h:.1f} path={p}")
        except Exception as e:
            print(f"CLAUDE_TMP err path={p} err={e}")
CLAUDETMP

# npm rebuildable cacache (not ~/.npmrc)
[ -d "$HOME_DIR/.npm/_cacache" ] && rm -rf "$HOME_DIR/.npm/_cacache" || true

# old logs
find "$HOME_DIR/Library/Logs" -type f -mtime +14 -delete 2>/dev/null || true
find "$HOME_DIR/Library/Application Support/Cursor/logs" -type f -mtime +7 -delete 2>/dev/null || true

after=$(df -g /System/Volumes/Data 2>/dev/null | awk 'NR==2{print $4}')
echo "MAC DISK RECLAIM free_before=${before}G free_after=${after}G"
REMOTE
exit 0
