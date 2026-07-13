#!/usr/bin/env python3
"""fleet-improver-scan.py — the DETERMINISTIC half of the weekly fleet-improver cron.

Builds an "already-have" manifest of the current Hermes fleet so the agent half NEVER
proposes something we already have (the anti-rabbit-hole guard), plus fresh external signal
(newest Hermes release, recent NousResearch activity hints) and a live health snapshot.
Its stdout becomes the agent's context (per automate-with-cron.md: script does mechanical
work, agent does reasoning).

Self-contained, stdlib only. Never throws (a cron script that crashes delivers nothing).
"""
import json, os, subprocess, glob, urllib.request, datetime

HOME = "/home/frank"
HERMES = f"{HOME}/.hermes"

def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as e:
        return f"(error: {e})"

def safe(fn, default):
    try: return fn()
    except Exception: return default

# --- 1. already-have manifest (the guard) ---
manifest = {}
manifest["skills_global"] = safe(lambda: sorted(os.listdir(f"{HERMES}/skills")), [])
manifest["skills_optional"] = safe(lambda: sorted(os.listdir(f"{HERMES}/hermes-agent/optional-skills")), [])
manifest["profiles"] = safe(lambda: sorted(d for d in os.listdir(f"{HERMES}/profiles") if os.path.isdir(f"{HERMES}/profiles/{d}")), [])
# cron job names (global store)
cron_out = sh("hermes cron list")
manifest["crons"] = [l.split("Name:")[1].strip() for l in cron_out.splitlines() if "Name:" in l]
manifest["known_fixes"] = safe(lambda: sorted(os.listdir(f"{HERMES.replace('.hermes','uaa-rules')}/known-fixes")), [])
# past proposals (don't re-propose) — both the ledger dir and prior kanban improvement cards
manifest["past_proposals"] = safe(lambda: sorted(os.listdir(f"{HOME}/uaa-rules/proposals")), [])
# kanban cards across boards (titles only) so we don't re-propose in-flight/done work
kanban_titles = []
for b in ["jarvis-os", "sycode-trading", "sycode-ai", "upero"]:
    out = sh(f"hermes kanban --board {b} list")
    for l in out.splitlines():
        if l.strip().startswith(("✓","◻","⊘","●")) or " t_" in l:
            kanban_titles.append(l.strip()[:120])
manifest["kanban_cards"] = kanban_titles[:200]

# --- 2. live health snapshot ---
health = {}
health["cron_errors"] = sum(1 for l in cron_out.splitlines() if "Last run:" in l and "error" in l)
health["cron_total"] = len(manifest["crons"])
health["gateway_active"] = sh("systemctl --user is-active hermes-gateway")
health["voice_gateway_active"] = sh("systemctl --user is-active hermes-voice-gateway")
# blocked tasks across boards
blocked = 0
for b in ["jarvis-os", "sycode-trading", "upero", "sycode-ai"]:
    blocked += sh(f"hermes kanban --board {b} list").count("blocked")
health["blocked_task_lines"] = blocked

# --- 3. fresh external signal: newest Hermes release (GitHub API, no auth needed for public) ---
ext = {}
try:
    req = urllib.request.Request(
        "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest",
        headers={"User-Agent": "Hermes-FleetImprover/1.0", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rel = json.load(r)
        ext["latest_release"] = rel.get("tag_name")
        ext["latest_release_name"] = rel.get("name")
        ext["latest_release_date"] = rel.get("published_at")
        ext["latest_release_notes_excerpt"] = (rel.get("body") or "")[:1500]
except Exception as e:
    ext["latest_release"] = f"(github fetch failed: {e})"
# installed version for comparison
ext["installed_version"] = sh("hermes version 2>/dev/null || hermes --version 2>/dev/null").splitlines()[0] if sh("hermes version 2>/dev/null || hermes --version 2>/dev/null") else "unknown"

stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
print("=== FLEET-IMPROVER SCAN ===")
print(f"timestamp: {stamp}")
print()
print("## ALREADY-HAVE MANIFEST (do NOT propose anything already in here)")
print(json.dumps(manifest, indent=1))
print()
print("## LIVE HEALTH SNAPSHOT")
print(json.dumps(health, indent=1))
print()
print("## FRESH EXTERNAL SIGNAL (Hermes upstream)")
print(json.dumps(ext, indent=1))
print()
print("=== END SCAN ===")

