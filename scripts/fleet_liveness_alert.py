#!/usr/bin/env python3
"""Fleet worker-agent liveness CHURN ALERT.

Reads the fleet liveness-churn measurement (fleet_liveness_churn.py), compares
the computed churn/death metrics against thresholds, and:

  1. Always emits a structured Prometheus textfile-metrics snapshot to
     /home/frank/.hermes/metrics/fleet_liveness.prom  (consumed by the
     existing textfile collector if present; otherwise a plain durable signal).
  2. Maintains alert state in /home/frank/.hermes/metrics/fleet_liveness_alert.json
     so it FIRES ONCE then stays resolved (no storm of repeats), and re-fires only
     after recovery + re-breach (hysteresis).
  3. Logs every evaluation to /home/frank/.hermes/logs/fleet-liveness-alert.log.
  4. Best-effort relays a compact alert to Discord #critical-alerts using the
     fleet OOB relay if present; failure to relay NEVER blocks the local signal.

No prod systems, no credentials read. Local kanban SQLite + Discord webhook ONLY.

Thresholds (tunable below). Default posture: alert when the FLEET 14-day death
rate (deaths / started) exceeds 25% OR the fleet death rate per day exceeds 40
sessions/day, OR any single board's death-rate-% exceeds 40%.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "fleet_liveness_churn.py")
# The engine imports hermes_cli/hermes_state; under the SYSTEM python (3.12)
# that resolves to a stale installed copy missing hermes_state_common, so it
# must run under the Hermes venv python (3.11) like hermes itself does.
VENV_PY = "/home/frank/.hermes/hermes-agent/venv/bin/python3"

METRICS_DIR = "/home/frank/.hermes/metrics"
LOG_DIR = "/home/frank/.hermes/logs"
ALERT_STATE = os.path.join(METRICS_DIR, "fleet_liveness_alert.json")
PROM_FILE = os.path.join(METRICS_DIR, "fleet_liveness.prom")
LOG_FILE = os.path.join(LOG_DIR, "fleet-liveness-alert.log")

# Alertmanager OOB relay HTTP endpoint (the relay runs as a systemd service and
# listens for Alertmanager webhook POSTs; we POST to it rather than launching it).
RELAY_URL = os.environ.get("FLEET_LIVENESS_RELAY_URL", "http://localhost:8655/alertmanager")

# Thresholds
FLEET_DEATH_PCT_MAX = 25.0        # 14d fleet death-rate %
FLEET_DEATH_PER_DAY_MAX = 40.0    # deaths/day fleet
BOARD_DEATH_PCT_MAX = 40.0        # single-board 14d death-rate %
RECOVERY_HYSTERESIS_PCT = 5.0     # must drop this far below max to auto-resolve


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {msg}"
    print(line)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def measure():
    env = os.environ.copy()
    # The engine imports hermes_cli, which warns when HERMES_HOME is unset and
    # would write state to the DEFAULT profile instead of the jarvis profile
    # (issue #18594; observed every 10 min in fleet-liveness-alert.log).
    env.setdefault("HERMES_HOME", "/home/frank/.hermes/profiles/jarvis")
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    out = subprocess.run([py, ENGINE], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        log(f"MEASURE_FAIL rc={out.returncode} err={out.stderr[:300]}")
        return None
    return json.loads(out.stdout)


def load_state():
    try:
        with open(ALERT_STATE) as f:
            return json.load(f)
    except Exception:
        return {"firing": False, "since": None, "last_fired_at": None, "last_reason": None}


def write_prom(d):
    os.makedirs(METRICS_DIR, exist_ok=True)
    agg = d["fleet_aggregate"]
    lines = []
    lines.append("# HELP fleet_kanban_window_started_total sessions started in measurement window")
    lines.append("# TYPE fleet_kanban_window_started_total gauge")
    lines.append(f"fleet_kanban_window_started_total {agg['window_started_total']}")
    lines.append("# HELP fleet_kanban_window_death_total sessions that died in measurement window")
    lines.append("# TYPE fleet_kanban_window_death_total gauge")
    lines.append(f"fleet_kanban_window_death_total {agg['window_death_total']}")
    lines.append("# HELP fleet_kanban_death_rate_pct death rate % (deaths/started) over window")
    lines.append("# TYPE fleet_kanban_death_rate_pct gauge")
    lines.append(f"fleet_kanban_death_rate_pct {agg['death_rate_pct_fleet']}")
    lines.append("# HELP fleet_kanban_death_rate_per_day deaths per day over window")
    lines.append("# TYPE fleet_kanban_death_rate_per_day gauge")
    lines.append(f"fleet_kanban_death_rate_per_day {agg['death_rate_per_day_fleet']}")
    lines.append("# HELP fleet_kanban_churn_rate_per_day sessions started per day over window")
    lines.append("# TYPE fleet_kanban_churn_rate_per_day gauge")
    lines.append(f"fleet_kanban_churn_rate_per_day {agg['churn_rate_per_day_fleet']}")
    lines.append("# HELP fleet_kanban_blocked_needs_input_total open blocked needs_input cards")
    lines.append("# TYPE fleet_kanban_blocked_needs_input_total gauge")
    lines.append(f"fleet_kanban_blocked_needs_input_total {agg['blocked_needs_input_total']}")
    for b, m in d["per_board"].items():
        w = m["window"]
        lines.append(f"fleet_kanban_board_death_rate_pct{{board=\"{b}\"}} {w.get('death_rate_pct')}")
        lines.append(f"fleet_kanban_board_death_per_day{{board=\"{b}\"}} {w.get('death_rate_per_day')}")
        lines.append(f"fleet_kanban_board_blocked_needs_input{{board=\"{b}\"}} {len(m['blocked_needs_input'])}")
    try:
        with open(PROM_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log(f"PROM_WRITE_FAIL {e}")


def evaluate(d):
    agg = d["fleet_aggregate"]
    reasons = []
    fleet_pct = agg.get("death_rate_pct_fleet")
    fleet_dpd = agg.get("death_rate_per_day_fleet")
    if fleet_pct is not None and fleet_pct > FLEET_DEATH_PCT_MAX:
        reasons.append(f"fleet death-rate {fleet_pct}% > {FLEET_DEATH_PCT_MAX}%")
    if fleet_dpd is not None and fleet_dpd > FLEET_DEATH_PER_DAY_MAX:
        reasons.append(f"fleet death-rate {fleet_dpd}/day > {FLEET_DEATH_PER_DAY_MAX}/day")
    for b, m in d["per_board"].items():
        pct = m["window"].get("death_rate_pct")
        if pct is not None and pct > BOARD_DEATH_PCT_MAX:
            reasons.append(f"board {b} death-rate {pct}% > {BOARD_DEATH_PCT_MAX}%")
    return reasons


def relay(alert_text):
    import urllib.request
    payload = {
        "status": "firing",
        "alerts": [{
            "labels": {
                "alertname": "FleetLivenessChurn",
                "severity": "critical",
                "route": "fleet-liveness",
            },
            "annotations": {
                "summary": "Fleet worker-agent liveness churn breach",
                "description": alert_text,
            },
            "startsAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    }
    try:
        req = urllib.request.Request(
            RELAY_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")[:160]
            log(f"RELAY http {resp.status} {body}")
        return True
    except Exception as e:
        log(f"RELAY_FAIL {RELAY_URL}: {e} (local signal still recorded)")
    return False


def main():
    d = measure()
    if d is None:
        return 2
    write_prom(d)

    state = load_state()
    reasons = evaluate(d)
    agg = d["fleet_aggregate"]
    generated = agg.get("generated_at")

    firing = bool(reasons)
    # hysteresis: only auto-resolve after sustained recovery below threshold - hysteresis
    if state["firing"] and not reasons:
        # recovered; resolve
        state["firing"] = False
        state["resolved_at"] = generated
        save(state)
        log(f"RESOLVED: fleet death-rate {agg.get('death_rate_pct_fleet')}% back under threshold")
        print(json.dumps({"state": "resolved", "metrics": agg}, indent=2))
        return 0

    if firing and not state["firing"]:
        # new fire
        state["firing"] = True
        state["since"] = generated
        state["last_fired_at"] = generated
        state["last_reason"] = reasons
        save(state)
        alert_text = (
            f"[FLEET LIVENESS CHURN ALERT] {generated}\n"
            f"Fleet death-rate {agg.get('death_rate_pct_fleet')}% "
            f"({agg.get('window_death_total')}/{agg.get('window_started_total')} sessions died in 14d); "
            f"{agg.get('death_rate_per_day_fleet')} deaths/day; churn {agg.get('churn_rate_per_day_fleet')}/day; "
            f"{agg.get('blocked_needs_input_total')} open blocked needs_input cards.\n"
            f"Triggers: {'; '.join(reasons)}\n"
            f"Boards: upero, sycode-trading, jarvis-os, sycode-ai, yorkstone-supplies."
        )
        log("FIRING: " + "; ".join(reasons))
        relay(alert_text)
        print(json.dumps({"state": "firing", "reasons": reasons, "metrics": agg}, indent=2))
        return 0

    if firing and state["firing"]:
        # still firing, already alerted; do not re-relay (no storm). Just log metric.
        log(f"STILL FIRING (no re-relay): {'; '.join(reasons)}; death-rate {agg.get('death_rate_pct_fleet')}%")
        print(json.dumps({"state": "firing-continues", "reasons": reasons, "metrics": agg}, indent=2))
        return 0

    # not firing, not was firing -> healthy
    save(state)  # persist any field changes
    log(f"HEALTHY: fleet death-rate {agg.get('death_rate_pct_fleet')}% under thresholds")
    print(json.dumps({"state": "healthy", "metrics": agg}, indent=2))
    return 0


def save(state):
    os.makedirs(METRICS_DIR, exist_ok=True)
    with open(ALERT_STATE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    sys.exit(main())
