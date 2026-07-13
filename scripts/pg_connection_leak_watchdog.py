#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. Keep in sync with the
# profile scripts dir copy used by the no_agent cron (symlinks are blocked by
# the cron security check; copy, do not symlink).
"""
Postgres connection-leak watchdog + stuck-script reaper (FLAG-ONLY).

Follow-up to kanban t_76ced2ed / incident 2026-07-12-sycode-db-readonly-outage.

Root cause of that incident: a GLOBAL Postgres max_connections exhaustion
(100/100 backends, 91 in SELECT/PARSE waiting) pinned by two ad-hoc host
research scripts (/tmp/fusion_calib_v2.*.py) holding lock contention that
propagated into the sycodetrading-server query path. The sycode-db MCP is
embedded in sycodetrading-server, so "restart the MCP" = full live-server
bounce (too large a blast radius). A proactive read-only guard is the fix.

WHAT THIS SCRIPT DOES
  - Read-only probes of pg_stat_activity (NO DB writes, NO docker restart).
  - Alert (print to stdout -> cron delivers to Frank) when EITHER:
      (A) free non-superuser slots < 10% of (max_connections - reserved), OR
      (B) > 15 client backends wedged in active lock/resource waiting > 30 min.
  - Optional stuck-script reaper: FLAG (never auto-kill) ad-hoc /tmp/*.py host
    research scripts whose Postgres backends have been waiting > 60 min.
    Paper-mode safety: a kill-list allowlist + operator confirmation would be
    required before any kill — out of scope here.
  - Report harness: on alert, capture a backend breakdown (count by state,
    top waiting sources by client IP and by host script) and post it to
    Obsidian (monitoring/) + print to stdout for cron delivery to Frank.

SAFETY
  Read-only. It never writes to the DB, never runs `docker restart`, never
  edits credentials, never deploys, never spends. SILENT when healthy
  (empty stdout = no delivery), matching the dgx_data_freshness_probe pattern.

Usage:
  pg_connection_leak_watchdog.py                 # live read-only probe, alert if breached
  pg_connection_leak_watchdog.py --dry-run       # probe live, always print metrics, never alert-exit
  pg_connection_leak_watchdog.py --simulate-incident
        [--no-write]                             # exercise eval+report on a synthetic 07-12 replay
  pg_connection_leak_watchdog.py --self-test     # assert the incident replay trips both conditions

Env overrides:
  PG_CONTAINER            docker container name holding Postgres (default sycodetrading-supabase-db)
  FREE_SLOT_PCT_THRESHOLD free non-su slot pct floor (default 0.10 -> 10%)
  WAITING_COUNT_THRESHOLD wedged-backend count ceiling (default 15)
  WAITING_AGE_MIN         min age of a wedged active query to count (default 30)
  REAP_WAIT_MIN           min age of a waiting backend to flag a host script (default 60)
  DB_PORTS               host ports to scan for client sockets in reaper (default 5432,64322)
  OBSIDIAN_MONITOR_DIR    where to write the alert note (default .../quant-team/monitoring)
  WATCHDOG_STATE          fingerprint state file (default cron state dir)
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from second_brain_writer import write_markdown_atomic

# ---- config (env-overridable) -------------------------------------------------
PG_CONTAINER = os.getenv("PG_CONTAINER", "sycodetrading-supabase-db")
FREE_SLOT_PCT_THRESHOLD = float(os.getenv("FREE_SLOT_PCT_THRESHOLD", "0.10"))
WAITING_COUNT_THRESHOLD = int(os.getenv("WAITING_COUNT_THRESHOLD", "15"))
WAITING_AGE_MIN = int(os.getenv("WAITING_AGE_MIN", "30"))
REAP_WAIT_MIN = int(os.getenv("REAP_WAIT_MIN", "60"))
DB_PORTS = [p.strip() for p in os.getenv("DB_PORTS", "5432,64322").split(",") if p.strip()]

OBSIDIAN_MONITOR_DIR = Path(os.getenv(
    "OBSIDIAN_MONITOR_DIR",
    "/home/frank/obsidian/quant-team/monitoring"))
HEALTH_JSONL = Path(os.getenv(
    "PG_LEAK_HEALTH_LOG",
    "/home/frank/.hermes/profiles/devops/cron/output/pg_connection_leak_health.jsonl"))
STATE = Path(os.getenv(
    "WATCHDOG_STATE",
    "/home/frank/.hermes/profiles/devops/cron/state/pg_connection_leak_watchdog.first_seen.json"))

REMIND_SECONDS = int(os.getenv("PG_LEAK_REMIND_SECONDS", str(24 * 3600)))

# Wedged = an ACTIVE backend blocked on something (wait_event set) that has been
# running > WAITING_AGE_MIN. idle ClientRead (waiting for the client to send the
# next query) is NORMAL and must NOT be counted as a leak.
WAITING_EVENT_TYPES = {"Lock", "LWLock", "bufferpin"}


# ---- low-level psql (read-only) ----------------------------------------------
def _psql(query, db="postgres", timeout=60):
    r = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "postgres", "-d", db,
         "-Atc", query],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise RuntimeError(msg.splitlines()[-1][:120] if msg else "rc=%d" % r.returncode)
    return r.stdout.strip()


def psql_scalar(query):
    return _psql(query)


def psql_json(query):
    out = _psql(query)
    if not out:
        return None
    return json.loads(out)


# ---- state fetch --------------------------------------------------------------
def fetch_state():
    """Read-only: pull max_connections, reserved, superuser roles, and activity."""
    max_conn = int(psql_scalar("SHOW max_connections;"))
    reserved = int(psql_scalar("SHOW superuser_reserved_connections;"))
    superusers = set(psql_scalar(
        "SELECT string_agg(rolname, ',') FROM pg_roles WHERE rolsuper;").split(","))
    rows = psql_json(
        "SELECT json_agg(row_to_json(t)) FROM ("
        "  SELECT pid, usename, state, wait_event_type, wait_event, client_addr,"
        "         client_port, application_name, backend_type,"
        "         EXTRACT(EPOCH FROM (now() - query_start)) AS query_age_s"
        "  FROM pg_stat_activity WHERE pid <> pg_backend_pid()"
        ") t;")
    if rows is None:
        rows = []
    return {
        "max_conn": max_conn,
        "reserved": reserved,
        "superusers": superusers,
        "rows": rows,
    }


# ---- evaluation ---------------------------------------------------------------
def evaluate(state):
    """Return (findings dict, alerts list). Pure function of `state`."""
    max_conn = state["max_conn"]
    reserved = state["reserved"]
    superusers = state["superusers"]
    rows = state["rows"]

    total = len(rows)
    non_su = sum(1 for r in rows if r.get("usename") not in superusers)
    su = total - non_su
    slots_for_non_su = max(0, max_conn - reserved)
    # Postgres admits a non-superuser (e.g. the mcp_readonly role) connection
    # only while TOTAL backends < (max_connections - superuser_reserved_connections).
    # A superuser backend (the `postgres` role sycodetrading-server uses) STILL
    # counts against that total — which is exactly why the 2026-07-12 incident
    # locked out the read path even though the wedged backends were superuser.
    # So free non-su slots are measured against TOTAL backends, not just non-su.
    free_non_su = slots_for_non_su - total
    free_pct = max(0.0, free_non_su) / slots_for_non_su if slots_for_non_su else 0.0

    # wedged active backends (the leak signature)
    waiting = [r for r in rows
               if r.get("state") == "active"
               and r.get("wait_event") is not None
               and (r.get("query_age_s") or 0) > WAITING_AGE_MIN * 60]
    # resource-contention subset (lock/lwlock/bufferpin) regardless of state
    lock_contention = [r for r in rows
                       if r.get("wait_event_type") in WAITING_EVENT_TYPES
                       and (r.get("query_age_s") or 0) > WAITING_AGE_MIN * 60]
    # idle-in-transaction holding a backend open (informational; can hold locks)
    idle_in_xact = [r for r in rows
                    if (r.get("state") or "").startswith("idle in transaction")
                    and (r.get("query_age_s") or 0) > WAITING_AGE_MIN * 60]

    findings = {
        "max_connections": max_conn,
        "superuser_reserved": reserved,
        "total_backends": total,
        "superuser_backends": su,
        "nonsuperuser_backends": non_su,
        "slots_for_nonsuperuser": slots_for_non_su,
        "free_nonsuperuser_slots": free_non_su,
        "free_nonsuperuser_pct": round(free_pct, 4),
        "wedged_active_waiting": len(waiting),
        "lock_contention": len(lock_contention),
        "idle_in_transaction": len(idle_in_xact),
    }

    alerts = []
    if free_pct < FREE_SLOT_PCT_THRESHOLD:
        alerts.append(
            "  RED free non-superuser slots %.1f%% (< %.0f%% floor): %d free of %d "
            "(max_connections=%d, superuser_reserved=%d, non-su backends=%d)"
            % (free_pct * 100, FREE_SLOT_PCT_THRESHOLD * 100, free_non_su,
               slots_for_non_su, max_conn, reserved, non_su))
    if len(waiting) > WAITING_COUNT_THRESHOLD:
        alerts.append(
            "  RED %d client backends wedged in active waiting > %d min (> %d ceiling)"
            % (len(waiting), WAITING_AGE_MIN, WAITING_COUNT_THRESHOLD))
    return findings, alerts, waiting, lock_contention, idle_in_xact


# ---- stuck-script reaper (FLAG-ONLY) -----------------------------------------
def reap_candidates(state, min_age_min=REAP_WAIT_MIN):
    """Best-effort: flag ad-hoc /tmp/*.py host scripts with waiting DB backends.

    Never kills. Maps DB client_port (from pg_stat_activity) back to a host PID
    via `ss -tnp`, then reads the process command line. Degrades gracefully if
    ss is unavailable or the mapping is ambiguous.
    """
    candidates = []
    try:
        ss_out = subprocess.run(
            ["ss", "-tnp", "-H"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return [{"note": "reaper skipped: `ss` unavailable on this host"}]

    # map: source (local) port -> (pid, cmdline) for connections to the DB ports.
    # `ss -tnp -H` line layout (whitespace-separated):
    #   State  Recv-Q  Send-Q  Local:Addr:Port  Peer:Addr:Port  Process
    # where Process looks like  users:(("python3",pid=12345,fd=3))
    # The local/peer ports are NOT adjacent to `users:`, so we parse them from
    # their own fields (f[3]=local, f[4]=peer) and pull pid from anywhere.
    port_to_proc = {}
    for line in ss_out.splitlines():
        f = line.split()
        if len(f) < 5:
            continue
        try:
            lport = f[3].rsplit(":", 1)[-1]
            rport = f[4].rsplit(":", 1)[-1]
        except Exception:
            continue
        if rport not in DB_PORTS:
            continue
        m = re.search(r"pid=(\d+)", line)
        if not m:
            continue
        pid = int(m.group(1))
        cmd = _read_cmdline(pid)
        port_to_proc[lport] = {"pid": pid, "cmd": cmd}

    for r in state["rows"]:
        if (r.get("wait_event") is None) or (r.get("query_age_s") or 0) <= min_age_min * 60:
            continue
        cport = r.get("client_port")
        if cport is None:
            continue
        proc = port_to_proc.get(str(cport))
        if not proc:
            continue
        if re.search(r"/tmp/[^ ]+\.py", proc["cmd"]):
            candidates.append({
                "pid": proc["pid"],
                "cmd": proc["cmd"],
                "db_pid": r.get("pid"),
                "client_addr": r.get("client_addr"),
                "client_port": cport,
                "wait_event_type": r.get("wait_event_type"),
                "wait_event": r.get("wait_event"),
                "age_min": round((r.get("query_age_s") or 0) / 60.0, 1),
                "state": r.get("state"),
            })
    # de-dup by pid
    seen = set()
    uniq = []
    for c in candidates:
        if c["pid"] in seen:
            continue
        seen.add(c["pid"])
        uniq.append(c)
    return uniq


def _read_cmdline(pid):
    """Resolve a process command line. Prefer /proc/<pid>/cmdline; fall back to
    `ps -p <pid> -o args=` which is more reliable than /proc/cmdline in some
    sandboxed/container-adjacent environments where /proc cmdline reads empty."""
    try:
        txt = Path("/proc/%d/cmdline" % pid).read_text().replace("\0", " ").strip()
        if txt:
            return txt
    except Exception:
        pass
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return "?"


# ---- report + persistence -----------------------------------------------------
def build_report(findings, alerts, waiting, lock_contention, idle_in_xact, reaps):
    now = datetime.datetime.now(datetime.timezone.utc)
    lines = []
    lines.append("PG CONNECTION-LEAK ALERT  (%s UTC)" % now.isoformat())
    lines.append("=" * 64)
    lines.append("Triggered conditions:")
    for a in alerts:
        lines.append(a)
    lines.append("")
    lines.append("Backend metrics:")
    for k, v in findings.items():
        lines.append("  %-32s %s" % (k, v))
    lines.append("")
    lines.append("Top waiting sources by client_addr:")
    by_addr = {}
    for r in waiting + lock_contention:
        addr = r.get("client_addr") or "<local/unix>"
        by_addr[addr] = by_addr.get(addr, 0) + 1
    for addr, n in sorted(by_addr.items(), key=lambda x: -x[1])[:10]:
        lines.append("  %-20s %d" % (str(addr), n))
    lines.append("")
    lines.append("Wedged backends (state=active, wait_event set, age>%dmin):" % WAITING_AGE_MIN)
    for r in sorted(waiting, key=lambda x: -(x.get("query_age_s") or 0))[:15]:
        lines.append("  pid=%-8s %s age=%.0fm wait=%s/%s app=%s client=%s" % (
            r.get("pid"), r.get("state"), (r.get("query_age_s") or 0) / 60.0,
            r.get("wait_event_type"), r.get("wait_event"), r.get("application_name"),
            r.get("client_addr")))
    lines.append("")
    lines.append("FLAG-ONLY reap candidates (ad-hoc /tmp/*.py, NO auto-kill):")
    if reaps:
        for c in reaps:
            lines.append("  pid=%-8d age=%.1fm wait=%s/%s client=%s:%s cmd=%s" % (
                c["pid"], c["age_min"], c["wait_event_type"], c["wait_event"],
                c["client_addr"], c["client_port"], c["cmd"]))
        lines.append("  -> operator must confirm kill via an allowlist; no action taken here.")
    else:
        lines.append("  none (or reaper could not map; see note).")
    lines.append("")
    lines.append("Remediation (paper-mode, no restart, no writes):")
    lines.append("  - Kill the flagged ad-hoc host scripts after operator confirmation.")
    lines.append("  - Do NOT `docker restart sycodetrading-server` (embedded sycode-db MCP;")
    lines.append("    full live-server bounce = large blast radius).")
    lines.append("  - See incidents/2026-07-12-sycode-db-readonly-outage.md")
    return "\n".join(lines)


def write_obsidian(report, ts_iso):
    stamp = ts_iso.replace(":", "").replace("-", "").replace(".", "")[:15]
    path = OBSIDIAN_MONITOR_DIR / ("pg-connection-leak-%s.md" % stamp)
    report_date = ts_iso[:10]
    write_markdown_atomic(
        path,
        report + "\n",
        title="PG connection-leak alert %s" % stamp,
        type="incident",
        status="active",
        created=report_date,
        updated=report_date,
        confidence="high",
        tags=["postgres", "connection-exhaustion", "watchdog", "alert"],
        sources=["sycodetrading-supabase-db:pg_stat_activity"],
        project="sycode-trading",
        owners=["devops"],
        knowledge_tier="evidence",
        generated=True,
        generator="pg_connection_leak_watchdog.py",
        operational_status="alert",
        generated_at=ts_iso,
        kanban_task="t_76ced2ed",
    )
    return path


def write_health_jsonl(findings, alerting):
    try:
        HEALTH_JSONL.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "source": "pg-connection-leak-watchdog",
               "alerting": alerting, **findings}
        with open(HEALTH_JSONL, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


# ---- alert dedup (silent-when-healthy, slow re-remind) ------------------------
def read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def write_state(payload):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(".%s.tmp-%d" % (STATE.name, os.getpid()))
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE)


def should_emit(alerts):
    if not alerts:
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass
        return False
    now = int(time.time())
    fp = "\n".join(sorted(alerts))
    st = read_state()
    if st.get("fingerprint") != fp:
        write_state({"fingerprint": fp, "first_seen": now, "last_alert": now})
        return True
    if now - int(st.get("last_alert") or 0) >= REMIND_SECONDS:
        write_state({**st, "last_alert": now, "last_seen": now})
        return True
    write_state({**st, "last_seen": now})
    return False


# ---- simulation / self-test ---------------------------------------------------
def synthetic_incident_state():
    """Replay of 2026-07-12: 100 backends, 91 wedged in lock waiting, 0 free."""
    superusers = {"postgres"}
    rows = []
    # 3 superuser backends (healthy idle)
    for pid in (9001, 9002, 9003):
        rows.append({"pid": pid, "usename": "postgres", "state": "idle",
                     "wait_event_type": "Client", "wait_event": "ClientRead",
                     "client_addr": "127.0.0.1", "client_port": 40000 + pid,
                     "application_name": "psql", "backend_type": "client backend",
                     "query_age_s": 10.0})
    # 91 wedged non-superuser backends (the leak) — active, locked, 3h old
    for i in range(91):
        rows.append({"pid": 10000 + i, "usename": "postgres", "state": "active",
                     "wait_event_type": "Lock", "wait_event": "transactionid",
                     "client_addr": "172.18.0.27", "client_port": 50000 + i,
                     "application_name": "postgres.js",
                     "backend_type": "client backend",
                     "query_age_s": 3 * 3600.0})
    # 6 idle non-superuser backends
    for i in range(6):
        rows.append({"pid": 20000 + i, "usename": "postgres", "state": "idle",
                     "wait_event_type": "Client", "wait_event": "ClientRead",
                     "client_addr": "172.18.0.6", "client_port": 60000 + i,
                     "application_name": "PostgREST", "backend_type": "client backend",
                     "query_age_s": 5.0})
    return {"max_conn": 100, "reserved": 3, "superusers": superusers, "rows": rows}


# ---- main ---------------------------------------------------------------------
def main(argv):
    dry_run = "--dry-run" in argv
    simulate = "--simulate-incident" in argv
    no_write = "--no-write" in argv
    self_test = "--self-test" in argv

    if simulate or self_test:
        state = synthetic_incident_state()
        note = "SIMULATED 2026-07-12 incident replay"
    else:
        state = fetch_state()
        note = "LIVE read-only probe of %s" % PG_CONTAINER

    findings, alerts, waiting, lock_contention, idle_in_xact = evaluate(state)
    reaps = reap_candidates(state) if not (simulate or self_test) else []

    if self_test:
        ok_a = findings["free_nonsuperuser_pct"] < FREE_SLOT_PCT_THRESHOLD
        ok_b = findings["wedged_active_waiting"] > WAITING_COUNT_THRESHOLD
        print("SELF-TEST %s: conditionA(free_pct)=%s free=%.4f  conditionB(waiting)=%s n=%d"
              % ("PASS" if (ok_a and ok_b) else "FAIL", ok_a,
                 findings["free_nonsuperuser_pct"], ok_b,
                 findings["wedged_active_waiting"]))
        return 0 if (ok_a and ok_b) else 1

    if simulate:
        report = build_report(findings, alerts, waiting, lock_contention,
                              idle_in_xact, reaps)
        print("[%s]" % note)
        print(report)
        if not no_write:
            p = write_obsidian(report, datetime.datetime.now(datetime.timezone.utc).isoformat())
            print("\n(simulated alert note written to %s)" % p)
        # simulate always "emits"
        return 0

    # live path
    write_health_jsonl(findings, bool(alerts))
    if dry_run:
        report = build_report(findings, alerts, waiting, lock_contention,
                              idle_in_xact, reaps)
        print("[%s] DRY-RUN (metrics only, no alert emitted):" % note)
        print(report)
        return 0

    if should_emit(alerts):
        report = build_report(findings, alerts, waiting, lock_contention,
                              idle_in_xact, reaps)
        # stdout is delivered to Frank by the cron wrapper
        print(report)
        if not no_write:
            p = write_obsidian(report, datetime.datetime.now(datetime.timezone.utc).isoformat())
            print("\n(alert note written to %s)" % p)
        return 0
    # healthy: silent (empty stdout = no delivery)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
