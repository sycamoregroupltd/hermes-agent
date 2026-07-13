#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
import json, subprocess, os, datetime, sys
HOME = "/home/frank"
HERMES = "/home/frank/.local/bin/hermes"
LOG = os.path.join(HOME, ".hermes", "profiles", "jarvis", "cron", "output", "health_canary.jsonl")
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
env = {**os.environ, "SYSTEMD_PAGER": "cat", "PAGER": "cat"}
hermes_ok = subprocess.run([HERMES, "-p", "jarvis", "--version"], capture_output=True, text=True, env=env, timeout=30).returncode == 0
try:
    gateway_check = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway-jarvis.service"], capture_output=True, text=True, timeout=10, env=env)
    gateway = gateway_check.stdout.strip()
    gateway_running = gateway_check.returncode == 0 and gateway == "active"
except Exception:
    gateway = ""
    gateway_running = False
# Cross-check disabled platforms for stale gateway_state residue so the log
# captures what the jarvis_health MCP now reclassifies as disabled/stale.
# Non-fatal: residue on an OFF platform is NOT a health fault (the gateway IS up).
stale_residue = []
try:
    import re
    state_path = os.path.join(HOME, ".hermes", "profiles", "jarvis", "gateway_state.json")
    cfg_path = os.path.join(HOME, ".hermes", "profiles", "jarvis", "config.yaml")
    with open(state_path) as f:
        st = json.load(f)
    enabled = {}
    if os.path.exists(cfg_path):
        lines = open(cfg_path).read().splitlines()
        # locate gateway: block
        gw_idx = next((i for i, s in enumerate(lines) if re.match(r"^\s*gateway\s*:\s*$", s)), None)
        if gw_idx is not None:
            gw_indent = len(lines[gw_idx]) - len(lines[gw_idx].lstrip())
            plat_idx = None
            for i in range(gw_idx + 1, len(lines)):
                s = lines[i]
                if not s.strip() or s.lstrip().startswith("#"):
                    continue
                if (len(s) - len(s.lstrip())) <= gw_indent:
                    break
                if re.match(r"^\s*platforms\s*:\s*$", s):
                    plat_idx = i
                    break
            if plat_idx is not None:
                plat_indent = len(lines[plat_idx]) - len(lines[plat_idx].lstrip())
                for i in range(plat_idx + 1, len(lines)):
                    s = lines[i]
                    if not s.strip():
                        continue
                    pind = len(s) - len(s.lstrip())
                    if pind <= plat_indent:
                        break
                    if pind == plat_indent + 2:
                        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*:\s*(.*)$", s)
                        if m:
                            en = re.search(r"enabled\s*:\s*(true|false)", m.group(2))
                            enabled[m.group(1)] = en is None or en.group(1) == "true"
                    elif pind > plat_indent + 2:
                        last = next(reversed(enabled)) if enabled else None
                        if last:
                            en = re.search(r"enabled\s*:\s*(true|false)", s.strip())
                            if en:
                                enabled[last] = en.group(1) == "true"
    for k, v in (st.get("platforms") or {}).items():
        if enabled.get(k, True):
            continue
        stale_residue.append({"platform": k, "gateway_state": v.get("state"),
                              "error_message": v.get("error_message"),
                              "updated_at": v.get("updated_at")})
except Exception:
    pass

record = {"ts": now, "hermes_cli": hermes_ok, "gateway_running": gateway_running, "gateway_snippet": gateway[:200] if gateway else None, "stale_residue": stale_residue}
os.makedirs(os.path.dirname(LOG), exist_ok=True)
with open(LOG, "a") as f:
    f.write(json.dumps(record) + "\n")
# Rotation (2026-07-05 claude-seat audit): shared file also receives freshness-probe
# records and grew unbounded; keep the newest 5000 lines.
try:
    with open(LOG) as f:
        lines = f.readlines()
    if len(lines) > 5000:
        with open(LOG, "w") as f:
            f.writelines(lines[-5000:])
except Exception:
    pass
if not hermes_ok or not gateway_running:
    print("DGX Jarvis health issue detected", file=sys.stderr)
    sys.exit(1)
