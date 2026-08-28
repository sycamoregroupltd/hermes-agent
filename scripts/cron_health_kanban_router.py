#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""cron_health_kanban_router.py

Companion to dgx_cron_health_canary.py (t_634a8026). Invoked by the cron-health
canary wrapper ONLY when the canary emitted an UNHEALTHY alert block (or on a
healthy tick to resolve lingering cards). Converts cron-health alerts into
DEDUPED low-priority kanban remediation cards so the board owns remediation
instead of the alert channel alone.

Pattern (mirrors system-crontab-watchdog-kanban-router.py, t_dc22ee7e):
  - key    = stable failure signature over the SORTED alert lines
             (``cronhealth_<md5-12>``). Same key = same underlying issue.
  - ledger = ~/.hermes/state/cron-health-kanban-router-ledger.json (atomic
             save/load, same discipline as anomaly_ledger.py).
  - 14-day window (Option C dedup rule, narrow router-scope): only ONE card is
    created per failure signature within a 14-day window. While an open card
    exists -> append occurrence comment, no second card. If the card is closed
    but the window has NOT expired -> append a recurrence comment to the closed
    card, NO new card. Only when the window has expired (or no card exists) is
    a fresh card created.
  - created_by = "cron-health-canary"
  - board  = jarvis-os (control-plane board; canary is a fleet meta-monitor)
  - assignee = jarvis-os-pm (or CRON_HEALTH_KANBAN_ASSIGNEE override)
  - parent = source canary task (CRON_HEALTH_KANBAN_PARENT) when that task is
             NOT archived (archived parents would strand the child in todo).

IMPORTANT — Option C boundary (governance decision 2026-08-11, t_e2380003):
The board-wide dedupe guard (kanban_dedupe_guard.py) was NOT approved for
production enforcement (guardian KEEP-DRY-RUN-ONLY, t_71d3e221). This router
does NOT enforce board-wide dedupe. It only dedups ITS OWN card creation via a
per-key ledger + idempotency-key — the narrow, already-approved pattern from
t_dc22ee7e. No other tasks on the board are touched.

Re-pin partition (requirement 4 of t_634a8026):
  - classify_profile() marks a profile LOCKED (financial / trading / critical
    governance / control-plane) or ELIGIBLE (safe for a free/local fallback
    model).
  - FAIL-CLOSED: if a profile is not clearly non-critical it is LOCKED. The
    deny list always wins (even if an allowlist entry is added later).
  - Default mode is DRY-RUN: plan_repin() reports the exact config.yaml change
    that WOULD be made and writes the audit line; it NEVER mutates config.
  - apply_repin() requires --apply-repin AND an ELIGIBLE classification AND
    backs up config.yaml first (config.yaml.repin-bak-<ts>), validates YAML,
    writes atomically, and appends a detailed audit line + kanban comment.
  - Trading/financial/critical profiles can NEVER be repinned, even with
    --apply-repin (test-proven).

Auditability: every action (card create / dedupe comment / resolve / repin
plan / repin apply) writes a JSONL line to
~/.hermes/state/cron-health-kanban-router-audit.jsonl with a timestamp, action,
key, task id, profile, and detail. Card bodies embed the full source alert
lines (trace-ready).

Exit 0 = routing handled cleanly. Exit 2 = operational error (router must
never be silent when it fails).

CLI:
  (default)   read canary output on stdin; CRON_HEALTH_HEALTHY=1 means a clean
              tick (resolve lingering cards), 0/absent means UNHEALTHY block.
  --dry-run   report what WOULD happen; no board mutations, no ledger writes.
  --selftest  offline deterministic test (FakeHarness), no hermes CLI.
  --check-repin  classify every profile and print LOCKED/ELIGIBLE + the
              would-repin plan for eligible ones (read-only).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERMES = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
REAL_HERMES_HOME = Path(os.environ.get("HERMES_REAL_HOME", "/home/frank/.hermes")).expanduser()
if not (REAL_HERMES_HOME / "profiles").exists() and (REAL_HERMES_HOME / ".hermes" / "profiles").exists():
    REAL_HERMES_HOME = REAL_HERMES_HOME / ".hermes"
BOARD = os.environ.get("CRON_HEALTH_KANBAN_BOARD", "jarvis-os")
ASSIGNEE = os.environ.get("CRON_HEALTH_KANBAN_ASSIGNEE", "jarvis-os-pm")
PARENT_TASK = os.environ.get("CRON_HEALTH_KANBAN_PARENT", "t_d43a2e82")  # source canary implementation task
CREATED_BY = "cron-health-canary"
LEDGER_PATH = Path(os.environ.get(
    "CRON_HEALTH_KANBAN_LEDGER",
    "/home/frank/.hermes/state/cron-health-kanban-router-ledger.json",
))
AUDIT_PATH = Path(os.environ.get(
    "CRON_HEALTH_KANBAN_AUDIT",
    "/home/frank/.hermes/state/cron-health-kanban-router-audit.jsonl",
))
PROFILES_DIR = REAL_HERMES_HOME / "profiles"
DEDUP_WINDOW_DAYS = int(os.environ.get("CRON_HEALTH_DEDUP_WINDOW_DAYS", "14"))
DEDUP_WINDOW_SECONDS = DEDUP_WINDOW_DAYS * 24 * 3600
PRIORITY = int(os.environ.get("CRON_HEALTH_KANBAN_PRIORITY", "20"))  # low priority

OPEN_STATUSES = ("ready", "todo", "running", "blocked", "review")
ACTIVE_STATUSES = ("ready", "todo", "blocked")  # eligible for auto-complete on resolve
# "blocked" included 2026-08-27: six CRON-HEALTH cards had been auto-blocked by the
# board and were therefore permanently ineligible for self-resolve -- immortal cards
# for problems that had long since cleared. A card the detector cannot close is the
# ratchet half of the same defect as the churning key above.
CLOSED_STATUSES = ("done", "archived")

# ---------------------------------------------------------------------------
# Repin partition (requirement 4 — strict security boundary)
# ---------------------------------------------------------------------------
# Financial / trading / critical governance / control-plane profiles. These can
# NEVER be auto-repinned. Prefix rules cover future trading-* additions so the
# boundary stays closed as the fleet grows.
REPIN_LOCKED_EXACT = {
    "jarvis", "jarvis-os-pm", "jarvis-coordinator", "jarvis-voice",
    "guardian", "os-reviewer", "os-architect", "tenant-guardian", "elon",
    "finance-ops", "paper-analyst", "paper-risk", "paper-trader",
    "sycode-trading", "sycode-trading-pm", "sycode-ai-pm",
    "research-trading", "trading-backtest-runner", "trading-breakout-trader",
    "trading-data-oracle", "trading-devops", "trading-market-analyst",
    "trading-mean-reversion", "trading-ml-ensemble", "trading-risk-reviewer",
    "trading-strategy-dev", "trading-trend-follower", "trading-volatility-arb",
    "workforce-scaler", "self-improve-engineer", "system-optimizer",
    "nervous-system-engineer", "eval-runner", "fleet-analyst",
}
REPIN_LOCKED_PREFIX = ("trading-", "paper-", "sycode-", "finance-", "nim-")
# Profiles that are safe for a free/local fallback re-pin (non-financial,
# non-trading, non-critical). The deny list above ALWAYS wins (fail-closed).
REPIN_ALLOW_PREFIX = (
    "builder", "platform-", "upero-", "yorkstone-", "frontend-", "db-",
    "devops", "test-", "research-", "prompt-", "comms-", "capability-",
)


def classify_profile(profile: str) -> tuple[str, str]:
    """Return (LOCKED|ELIGIBLE, reason).

    Fail-closed: anything that is not clearly safe is LOCKED. Exact deny list
    and deny prefixes always win over allow prefixes.
    """
    p = profile.strip().lower()
    if p in REPIN_LOCKED_EXACT:
        return "LOCKED", f"explicit deny list (financial/trading/critical/control-plane): {p}"
    if p.startswith(REPIN_LOCKED_PREFIX):
        return "LOCKED", f"deny prefix {p.split('-')[0]}-* (financial/trading/critical): {p}"
    if p.startswith(REPIN_ALLOW_PREFIX):
        return "ELIGIBLE", f"non-critical allow prefix: {p}"
    # Unknown / unclassified -> fail closed.
    return "LOCKED", f"unclassified profile (fail-closed): {p}"


REPIN_DEFAULT_PROVIDER = os.environ.get("CRON_HEALTH_REPIN_PROVIDER", "ollama-local")
REPIN_DEFAULT_MODEL = os.environ.get("CRON_HEALTH_REPIN_MODEL", "llama3.2:latest")
REPIN_DEFAULT_BASE_URL = os.environ.get("CRON_HEALTH_REPIN_BASE_URL", "http://localhost:11434/v1")


def read_profile_model(profile: str) -> dict | None:
    """Read the profile's current top-level model pin (model.default/provider/base_url)."""
    cfg = PROFILES_DIR / profile / "config.yaml"
    if not cfg.exists():
        return None
    try:
        import yaml  # local import: only needed for repin paths
        with open(cfg, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        m = data.get("model") or {}
        return {
            "profile": profile,
            "config": str(cfg),
            "default": str(m.get("default") or ""),
            "provider": str(m.get("provider") or ""),
            "base_url": str(m.get("base_url") or ""),
        }
    except Exception as exc:
        return {"profile": profile, "config": str(cfg), "error": str(exc)}


def plan_repin(profile: str) -> dict:
    """Compute the WOULD-repin plan for one profile (never mutates config)."""
    cls, reason = classify_profile(profile)
    if cls == "LOCKED":
        return {"profile": profile, "class": cls, "reason": reason, "repin": False}
    cur = read_profile_model(profile)
    if cur is None or cur.get("error"):
        return {"profile": profile, "class": cls, "reason": reason,
                "repin": False, "error": cur.get("error") if cur else "no config.yaml"}
    target = {
        "default": REPIN_DEFAULT_MODEL,
        "provider": REPIN_DEFAULT_PROVIDER,
        "base_url": REPIN_DEFAULT_BASE_URL,
    }
    return {
        "profile": profile,
        "class": cls,
        "reason": reason,
        "repin": True,
        "current": {"default": cur["default"], "provider": cur["provider"], "base_url": cur["base_url"]},
        "target": target,
        "config": cur["config"],
    }


def apply_repin(profile: str, audit: "Audit") -> dict:
    """Apply the re-pin to an ELIGIBLE profile's config.yaml.

    REQUIRES explicit --apply-repin (caller passes apply=True only when the
    flag was given). Backs up config.yaml first, validates YAML, writes
    atomically, and records an audit line. Trading/financial/critical profiles
    are refused here regardless of flags (fail-closed defense in depth).
    """
    plan = plan_repin(profile)
    if not plan.get("repin"):
        audit.write("repin_refused", key=f"repin:{profile}",
                    detail=f"class={plan.get('class')} reason={plan.get('reason')}")
        return {"profile": profile, "applied": False,
                "reason": plan.get("reason", "not eligible")}
    cfg = Path(plan["config"])
    import yaml
    backup = cfg.with_name(cfg.name + f".repin-bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    try:
        with open(cfg, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("model", {})["default"] = plan["target"]["default"]
        data["model"]["provider"] = plan["target"]["provider"]
        data["model"]["base_url"] = plan["target"]["base_url"]
        # Validate we can re-serialize before touching the live file.
        rendered = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        yaml.safe_load(rendered)
    except Exception as exc:
        audit.write("repin_failed", key=f"repin:{profile}", detail=f"yaml error: {exc}")
        return {"profile": profile, "applied": False, "reason": f"yaml error: {exc}"}
    try:
        backup.write_bytes(cfg.read_bytes())
        fd, tmp = tempfile.mkstemp(prefix=cfg.name + ".", dir=str(cfg.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.replace(tmp, cfg)
    except Exception as exc:
        audit.write("repin_failed", key=f"repin:{profile}", detail=f"write error: {exc}")
        return {"profile": profile, "applied": False, "reason": f"write error: {exc}"}
    audit.write("repin_applied", key=f"repin:{profile}",
                detail=f"{plan['current']} -> {plan['target']} backup={backup}")
    return {"profile": profile, "applied": True, "backup": str(backup),
            "current": plan["current"], "target": plan["target"]}


# ---------------------------------------------------------------------------
# Audit log (trace-ready)
# ---------------------------------------------------------------------------
class Audit:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or AUDIT_PATH)

    def write(self, action: str, key: str, task_id: str | None = None,
              profile: str = "", detail: str = "") -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": utc_now_iso(),
                "action": action,
                "key": key,
                "task_id": task_id,
                "profile": profile,
                "detail": detail,
                "board": BOARD,
                "assignee": ASSIGNEE,
            }
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        except Exception:
            # Audit must never crash the router; write to stderr as last resort.
            print(f"AUDIT_FAIL {action} {key}", file=sys.stderr)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Ledger (atomic save/load)
# ---------------------------------------------------------------------------
def load_ledger(path: Path | None = None) -> dict:
    path = Path(path or LEDGER_PATH)
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            data = {"version": 1, "entries": {}}
        data.setdefault("entries", {})
        data.setdefault("version", 1)
        return data
    except Exception:
        try:
            path.rename(path.with_suffix(path.suffix + ".corrupt"))
        except Exception:
            pass
        return {"version": 1, "entries": {}}


def save_ledger(ledger: dict, path: Path | None = None) -> None:
    path = Path(path or LEDGER_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Hermes CLI wrapper (bounded retry, same discipline as security_audit router)
# ---------------------------------------------------------------------------
def _run_hermes(args: list[str], timeout: int = 30, attempts: int = 2,
                base_delay: float = 2.0) -> subprocess.CompletedProcess | None:
    import time
    env = os.environ.copy()
    env["HERMES_HOME"] = str(REAL_HERMES_HOME)
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                [HERMES, *args], capture_output=True, text=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return None
        except Exception:
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return None
    return None


def _extract_task_id(stdout: str) -> str | None:
    try:
        data = json.loads(stdout.strip())
        if isinstance(data, dict):
            return data.get("id") or data.get("task_id")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("id") or data[0].get("task_id")
    except Exception:
        pass
    m = re.search(r"\b(t_[0-9a-f]{8,})\b", stdout)
    return m.group(1) if m else None


def _card_status(task_id: str) -> str | None:
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _task_created_at(task_id: str) -> float | None:
    """Epoch seconds when the task was created (for the 14-day window)."""
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT created_at FROM tasks WHERE id=?", (task_id,)).fetchone()
        con.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _parent_is_usable(parent: str | None) -> bool:
    """Only link a parent that exists and is NOT archived (archived would strand the child)."""
    if not parent:
        return False
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT status FROM tasks WHERE id=?", (parent,)).fetchone()
        con.close()
        return bool(row) and row[0] not in ("archived",)
    except Exception:
        return False


def existing_open_card(key: str) -> str | None:
    """Return the id of an open card for this router key, or None.

    Direct read-only sqlite lookup on idempotency_key + created_by (same as
    system-crontab-watchdog-kanban-router.py — CLI list does not expose
    idempotency_key).
    """
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        row = con.execute(
            "SELECT id FROM tasks WHERE idempotency_key=? AND created_by=? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
            (key, CREATED_BY, *OPEN_STATUSES),
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def existing_any_card(key: str) -> tuple[str | None, str | None]:
    """Return (task_id, status) for ANY non-archived card with this key (open or closed)."""
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return None, None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key=? AND created_by=? "
            "AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
            (key, CREATED_BY),
        ).fetchone()
        con.close()
        return (row[0], row[1]) if row else (None, None)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Alert parsing / key derivation
# ---------------------------------------------------------------------------
_ALERT_LINE_RE = re.compile(r"^\s*(?:•\s*)?(?P<kind>[A-Z][A-Z-]+)\s+(?P<body>.+)$")


def parse_alerts(alert_text: str) -> list[dict]:
    """Parse the canary alert block into structured issues.

    Accepts lines like:
        • ERROR jarvis/job-name: reason
        • DELIVERY jarvis/job-name: reason
        • OVERDUE jarvis/job-name: ...
        • PATH-MISMATCH jarvis/job-name: ...
        • UNPINNED [snapshot-recorded] <profile>/<job> [hash]: ...
    or any "KIND rest" line. The header line (🔴 CRON HEALTH: ...) is ignored.

    Returns a list of {kind, body, profile, job}. Unparsed lines keep empty
    profile/job so they still contribute to the block signature (via body) but
    never crash the parser.
    """
    issues: list[dict] = []
    for ln in alert_text.splitlines():
        s = ln.strip()
        if not s or s.startswith("🔴") or s.startswith("CRON HEALTH"):
            continue
        m = _ALERT_LINE_RE.match(s)
        if not m:
            issues.append({"kind": "RAW", "body": s, "profile": "", "job": ""})
            continue
        kind = m.group("kind")
        body = m.group("body").strip()
        profile, job = "", ""
        # UNPINNED lines carry an optional "snapshot-recorded" prefix before the
        # profile/job token: "UNPINNED snapshot-recorded fleet-analyst/job [h]: ..."
        search_body = body
        if kind == "UNPINNED":
            search_body = re.sub(r"^snapshot-recorded\s+", "", body)
        pm = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.:() -]+?)\s*(?:\[[^\]]*\]|:)", search_body)
        if not pm:
            pm = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.:-]+)\b", search_body)
        if pm:
            profile, job = pm.group(1), pm.group(2).strip()
        issues.append({"kind": kind, "body": body, "profile": profile, "job": job})
    return issues


def derive_key(alert_text: str) -> str | None:
    """Stable failure signature over the parsed (kind, profile, job) triples.

    Volatile detail (error text, "last_run_age=149.1h" numbers, the "N more"
    counter) does NOT enter the key: the same set of failing profiles/jobs maps
    to the same key across ticks, so the 14-day dedup actually holds. A NEW
    failing profile/job (or a new issue KIND) changes the key and opens a fresh
    card — the system-crontab targets_<md5> semantics.
    """
    issues = parse_alerts(alert_text)
    if not issues:
        return None
    # 2026-08-27: keyed on the issue KIND SET only, not (kind, profile, job).
    #
    # The original triple-keying was deliberate ("a NEW failing profile/job opens
    # a fresh card") and it is what produced 232 cards against 1 resolve. In a
    # 74-profile fleet the failing profile/job set drifts on almost every 30m
    # tick, so a content-derived key is a new key nearly every tick: the audit
    # log shows dedup hits stopping entirely on 2026-08-22 while creates ran on
    # to 08-27. A detector that opens cards faster than it can close them is a
    # ratchet, and the board is the delivery pipe -- flooding it breaks the thing
    # this feeds.
    #
    # The KIND set is a small closed vocabulary, so the same class of problem now
    # maps to one long-lived owner packet that updates in place and resolves when
    # clean. Which profiles/jobs are currently failing is volatile detail and
    # belongs in the body and the recurrence comments, where it already is --
    # nothing is lost, it just stops minting cards.
    # CONSTANT key. One current-owner packet, exactly as card t_e9ae306e asked.
    #
    # This is the third keying scheme and the first that actually holds. The
    # (kind, profile, job) triple minted 232 cards against 1 resolve. Narrowing it
    # to the KIND SET on 2026-08-27 looked right in unit tests -- same kinds gave
    # the same key -- but LIVE it produced 2 creates and 0 dedupes in 90 minutes,
    # because in a 74-profile fleet the set of failing KINDS moves per tick too.
    #
    # The lesson generalises: ANY content-derived key churns when the content is a
    # live fault set. Identity must come from the JOB, not from what the job found.
    # What is currently failing is volatile detail and belongs in the body and the
    # recurrence comments -- where append_comment() already puts it -- not in the
    # key. Nothing is lost; it just stops minting cards.
    return "cronhealth_current"


# ---------------------------------------------------------------------------
# Card operations
# ---------------------------------------------------------------------------
def create_card(key: str, alert_text: str, audit: Audit,
                occurrence: int = 1, force_new: bool = False) -> str | None:
    """Create one low-priority remediation card for a cron-health anomaly."""
    issues = parse_alerts(alert_text)
    lines = [ln.strip() for ln in alert_text.splitlines() if ln.strip()]
    report_lines = "\n".join(f"- {ln}" for ln in lines) if lines else alert_text.strip()
    profiles = sorted({i["profile"] for i in issues if i["profile"]})

    title = f"CRON-HEALTH→ACTION: {len(issues)} issue(s) in fleet cron health"
    if occurrence > 1:
        title = f"CRON-HEALTH→ACTION (recurrence #{occurrence}): {len(issues)} issue(s) in fleet cron health"
    if len(profiles) == 1:
        title = f"CRON-HEALTH→ACTION: {profiles[0]} — {len(issues)} cron health issue(s)"

    body_parts = [
        "Auto-routed by `cron-health-canary` (task t_634a8026). The cron-health "
        "meta-canary found persistent failures / overdue jobs / path mismatches. "
        "Alerts remain as notification; the board now owns remediation.",
        "",
        f"Dedupe key: `{key}`",
        f"Host: `{os.uname().nodename if hasattr(os, 'uname') else 'dgx'}`",
        f"Detected: {utc_now_iso()}",
        f"Assignees: `{ASSIGNEE}` (profile owner for profile-scoped items)",
        "",
        "Source alert lines:",
        report_lines,
        "",
        "Suggested remediation: inspect the affected profile's cron store "
        "(`~/.hermes/profiles/<profile>/cron/jobs.json`), fix the failing "
        "job/script, restore any missing profile-local script copy, or re-pin "
        "an unpinned model/provider per the fleet baseline. Confirm the next "
        "canary tick is silent.",
        "",
        "Acceptance: `dgx_cron_health_canary.py` returns SILENT (no output) for "
        "these profiles/jobs on the next tick and no open CRON-HEALTH card "
        "remains for this key.",
    ]
    body = "\n".join(body_parts)

    args = [
        "kanban", "--board", BOARD, "create", title,
        "--assignee", ASSIGNEE,
        "--priority", str(PRIORITY),
        "--created-by", CREATED_BY,
        "--body", body,
        "--json",
    ]
    if _parent_is_usable(PARENT_TASK):
        args += ["--parent", PARENT_TASK]
    if not force_new:
        args += ["--idempotency-key", key]
    proc = _run_hermes(args)
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"CRON_HEALTH_KANBAN_CREATE_FAIL key={key} err={err[:300]}", file=sys.stderr)
        return None
    tid = _extract_task_id(proc.stdout)
    if not tid:
        print(f"CRON_HEALTH_KANBAN_CREATE_FAIL key={key} no_id_in_output={proc.stdout[:300]}", file=sys.stderr)
        return None
    audit.write("card_created", key=key, task_id=tid,
                profile=",".join(profiles), detail=f"occurrence={occurrence}")
    return tid


def append_comment(task_id: str, key: str, alert_text: str, occurrence: int) -> bool:
    """Append a fresh occurrence comment to an existing card."""
    lines = [ln.strip() for ln in alert_text.splitlines() if ln.strip()]
    report_lines = "\n".join(f"- {ln}" for ln in lines) if lines else alert_text.strip()
    body = "\n".join([
        f"[cron-health-canary occurrence #{occurrence} @ {utc_now_iso()} — still UNHEALTHY]",
        "",
        "Current issue set (dedup: same key, no new card within 14-day window):",
        report_lines,
    ])
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"CRON_HEALTH_KANBAN_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    return True


def comment_recurrence_closed(task_id: str, key: str, alert_text: str, occurrence: int) -> bool:
    """Append a recurrence note to a CLOSED card within the 14-day window (no new card)."""
    body = "\n".join([
        f"[cron-health-canary recurrence #{occurrence} @ {utc_now_iso()} — card closed, "
        f"14-day dedup window NOT expired; no new card per Option C narrow rule]",
        "",
        "Current issue set:",
        "\n".join(f"- {ln.strip()}" for ln in alert_text.splitlines() if ln.strip()),
    ])
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"CRON_HEALTH_KANBAN_RECUR_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    return True


def resolve_card(task_id: str, key: str) -> bool:
    """Append a RESOLVED comment + auto-complete if the card is still in an active state."""
    ts = utc_now_iso()
    body = (f"RESOLVED: cron health returned clean as of {ts}. All previously "
            f"alerting profiles/jobs for key `{key}` are now healthy and silent.")
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"CRON_HEALTH_KANBAN_RESOLVE_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    status = _card_status(task_id)
    if status in ACTIVE_STATUSES:
        proc2 = _run_hermes([
            "kanban", "--board", BOARD, "complete", task_id,
            "--summary", f"cron-health-canary self-heal: key {key} returned healthy @ {ts}.",
        ])
        if proc2 is None or proc2.returncode != 0:
            err = (proc2.stderr or proc2.stdout or "") if proc2 else "timeout"
            print(f"CRON_HEALTH_KANBAN_RESOLVE_COMPLETE_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
            return False
    elif status is not None:
        note = (f"cron-health-canary: key {key} returned healthy @ {ts}, but task already "
                f"in '{status}'; left open for owner.")
        _run_hermes(["kanban", "--board", BOARD, "comment", task_id, note])
    return True


# ---------------------------------------------------------------------------
# Public: process one canary tick's outcome
# ---------------------------------------------------------------------------
def process_tick(*, healthy: bool, alert_text: str, audit: Audit | None = None) -> dict:
    """Drive one router tick given the canary's health outcome.

    ``healthy``: True = canary was silent (resolve lingering cards)
    ``alert_text``: the canary's UNHEALTHY output block ('' when healthy)
    """
    audit = audit or Audit()
    ledger = load_ledger()
    entries = ledger.setdefault("entries", {})
    now_ts = datetime.now(timezone.utc).timestamp()

    if not healthy:
        key = derive_key(alert_text)
        if not key:
            # Alert block present but no parseable alert line -> report, no card.
            audit.write("unparsable_alert", key="none", detail=alert_text[:300])
            return {"action": "unparsable", "key": None}
        entry = entries.get(key)
        # Recurrence: ledger points at a card. Decide create / dedupe / suppress.
        if entry is not None:
            tid = entry.get("task_id")
            status = _card_status(tid)
            created_at = _task_created_at(tid)
            window_age = (now_ts - (created_at or entry.get("first_seen_ts") or now_ts))
            in_window = window_age <= DEDUP_WINDOW_SECONDS
            if status in OPEN_STATUSES:
                # Same signature, card still open -> dedupe (comment bump).
                entry["occurrences"] = int(entry.get("occurrences", 1)) + 1
                entry["last_seen"] = utc_now_iso()
                save_ledger(ledger)
                ok = append_comment(tid, key, alert_text, entry["occurrences"])
                audit.write("card_deduped", key=key, task_id=tid,
                            detail=f"occurrence={entry['occurrences']} commented={ok}")
                return {"action": "deduped", "key": key, "task_id": tid,
                        "occurrences": entry["occurrences"], "commented": ok}
            if status in CLOSED_STATUSES and in_window:
                # Closed but within 14-day window -> no new card (Option C).
                entry["occurrences"] = int(entry.get("occurrences", 1)) + 1
                entry["last_seen"] = utc_now_iso()
                save_ledger(ledger)
                ok = comment_recurrence_closed(tid, key, alert_text, entry["occurrences"])
                audit.write("recurrence_suppressed", key=key, task_id=tid,
                            detail=f"closed within {DEDUP_WINDOW_DAYS}d window; occurrence={entry['occurrences']} commented={ok}")
                return {"action": "recurrence_suppressed", "key": key, "task_id": tid,
                        "occurrences": entry["occurrences"], "commented": ok}
            if status in CLOSED_STATUSES:
                # Closed AND window expired -> open a fresh card.
                del entries[key]
                save_ledger(ledger)
                entry = None
            if status is None:
                # Ledger points at a vanished card -> drop and re-create.
                del entries[key]
                save_ledger(ledger)
                entry = None
        if entry is None:
            # Ledger miss: probe the board directly (ledger-loss reconciliation).
            existing, estatus = existing_any_card(key)
            if existing and estatus in OPEN_STATUSES:
                entries[key] = {"task_id": existing, "key": key,
                                "first_seen": utc_now_iso(), "last_seen": utc_now_iso(),
                                "occurrences": 2}
                save_ledger(ledger)
                ok = append_comment(existing, key, alert_text, 2)
                audit.write("card_rehydrated", key=key, task_id=existing, detail=f"commented={ok}")
                return {"action": "deduped", "key": key, "task_id": existing,
                        "occurrences": 2, "commented": ok}
            if existing and estatus in CLOSED_STATUSES:
                created_at = _task_created_at(existing)
                if (now_ts - (created_at or now_ts)) <= DEDUP_WINDOW_SECONDS:
                    ok = comment_recurrence_closed(existing, key, alert_text, 1)
                    audit.write("recurrence_suppressed", key=key, task_id=existing,
                                detail=f"board-reconciled closed within window; commented={ok}")
                    return {"action": "recurrence_suppressed", "key": key, "task_id": existing,
                            "occurrences": 1, "commented": ok}
            # First detection (or window expired) -> create.
            tid = create_card(key, alert_text, audit, occurrence=1)
            if tid is None:
                return {"action": "create_failed", "key": key}
            entries[key] = {"task_id": tid, "key": key,
                            "first_seen": utc_now_iso(), "last_seen": utc_now_iso(),
                            "occurrences": 1}
            save_ledger(ledger)
            return {"action": "created", "key": key, "task_id": tid, "occurrences": 1}

    # Healthy: resolve every lingering ledger entry + open card for this router.
    resolved = []
    resolved_tids = set()
    for key in list(entries.keys()):
        entry = entries[key]
        tid = entry["task_id"]
        ok = resolve_card(tid, key)
        if ok:
            resolved_tids.add(tid)
            resolved.append(tid)
            audit.write("card_resolved", key=key, task_id=tid)
        del entries[key]
    if resolved or entries:
        save_ledger(ledger)
    return {"action": "resolved", "keys": resolved, "task_ids": resolved} if resolved \
        else {"action": "no_entry"}


def _fingerprint(alert_text: str) -> str:
    return hashlib.md5(alert_text.strip().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    selftest = "--selftest" in argv
    check_repin = "--check-repin" in argv
    apply_repin = "--apply-repin" in argv
    audit = Audit()

    if selftest:
        return _selftest()

    if check_repin:
        print("=== CRON-HEALTH REPIN PARTITION (read-only) ===")
        profiles = sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir())
        locked = eligible = 0
        for profile in profiles:
            cls, reason = classify_profile(profile)
            if cls == "LOCKED":
                locked += 1
            else:
                eligible += 1
            plan = plan_repin(profile)
            if plan.get("repin"):
                print(f"  ELIGIBLE {profile}: {plan['current']['provider']}/{plan['current']['default']} "
                      f"-> {plan['target']['provider']}/{plan['target']['default']}")
            else:
                print(f"  {cls:8s} {profile}: {plan.get('reason', reason)}")
        print(f"  summary: {locked} LOCKED, {eligible} ELIGIBLE (fail-closed default)")
        return 0

    alert_text = sys.stdin.read() if not sys.stdin.isatty() else ""
    healthy = os.environ.get("CRON_HEALTH_HEALTHY", "0") == "1"

    if dry:
        key = derive_key(alert_text) if alert_text else None
        if healthy:
            print("CRON_HEALTH_DRY_RUN action=resolve healthy=True")
        else:
            if not key:
                print("CRON_HEALTH_DRY_RUN action=create alert_text_empty_or_unparsable")
                return 0
            existing, estatus = existing_any_card(key)
            action = "append" if existing else "create"
            print(f"CRON_HEALTH_DRY_RUN action={action} key={key} task_id={existing or 'NEW'} status={estatus or '-'}")
        return 0

    try:
        result = process_tick(healthy=healthy, alert_text=alert_text, audit=audit)
        print(f"CRON_HEALTH_KANBAN_ROUTER {json.dumps(result, sort_keys=True)}")
        return 0
    except Exception as exc:
        print(f"CRON_HEALTH_KANBAN_ROUTER_FAILURE {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# Deterministic offline selftest (FakeHarness, no hermes CLI)
# ---------------------------------------------------------------------------
def _selftest() -> int:
    failures: list[str] = []

    # 1. Key derivation deterministic + order-invariant.
    report = "  • ERROR jarvis/job-x: boom\n  • OVERDUE devops/job-y: old"
    k1 = derive_key(report)
    k2 = derive_key(report)
    if not k1 or not k2:
        failures.append(f"key derivation returned None: {k1} / {k2}")
        return 1
    if k1 != k2:
        failures.append(f"key not deterministic: {k1} != {k2}")
    if not k1.startswith("cronhealth_"):
        failures.append(f"key prefix wrong: {k1}")
    k3 = derive_key("  • OVERDUE devops/job-y: old\n  • ERROR jarvis/job-x: boom")
    if k1 != k3:
        failures.append(f"key not order-invariant: {k1} != {k3}")

    # 2. Alert parsing.
    issues = parse_alerts("🔴 CRON HEALTH: 2 issue(s)...\n  • ERROR jarvis/job-x: boom\n  • PATH-MISMATCH devops/job-y: script missing")
    if len(issues) != 2:
        failures.append(f"parse_alerts expected 2, got {len(issues)}: {issues}")
    if issues[0]["profile"] != "jarvis" or issues[0]["job"] != "job-x":
        failures.append(f"parse_alerts profile/job wrong: {issues[0]}")

    # 3. Repin partition boundary (security critical).
    for locked in ("trading-breakout-trader", "sycode-trading", "finance-ops",
                   "paper-risk", "jarvis", "guardian", "os-reviewer", "trading-strategy-dev"):
        cls, _ = classify_profile(locked)
        if cls != "LOCKED":
            failures.append(f"repin partition LOCKED broken for {locked}: {cls}")
    for eligible in ("frontend-builder", "platform-builder", "upero-ui-builder",
                     "devops", "test-engineer", "research-upero"):
        cls, _ = classify_profile(eligible)
        if cls != "ELIGIBLE":
            failures.append(f"repin partition ELIGIBLE broken for {eligible}: {cls}")
    # Fail-closed: unclassified profile is LOCKED.
    cls, _ = classify_profile("random-unknown-profile")
    if cls != "LOCKED":
        failures.append(f"fail-closed broken: {cls}")

    # 4. process_tick create -> dedupe -> recurrence-suppress -> resolve flows.
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    global LEDGER_PATH, AUDIT_PATH
    LEDGER_PATH = tmp / "ledger.json"
    AUDIT_PATH = tmp / "audit.jsonl"
    audit = Audit(AUDIT_PATH)

    created = []
    commented = []
    completed = []
    statuses = {}

    def fake_run(args, timeout=30, attempts=2, base_delay=2.0):
        if "create" in args:
            tid = f"t_selftest{len(created) + 1:04d}"
            created.append((args, tid))
            statuses.setdefault(tid, "ready")
            out = json.dumps({"id": tid})
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")
        if "comment" in args:
            tid = args[4]
            commented.append((tid, args[-1]))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if "complete" in args:
            idx = args.index("complete")
            tid = next((a for a in args[idx + 1:] if not a.startswith("--")), None)
            if tid:
                completed.append(tid)
                statuses[tid] = "done"  # kanban board status after completion
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    g = globals()
    real_run = g.get("_run_hermes")
    real_status = g.get("_card_status")
    real_created_at = g.get("_task_created_at")
    real_existing_any = g.get("existing_any_card")
    real_parent = g.get("_parent_is_usable")
    g["_run_hermes"] = fake_run
    g["_card_status"] = lambda tid: statuses.get(tid, "ready")
    g["_task_created_at"] = lambda tid: datetime.now(timezone.utc).timestamp() - 100
    # Board reconcile: the closed card stays on the board after the healthy
    # tick (ledger cleared). existing_any_card must reflect that reality so the
    # closed-within-window path can suppress instead of re-create.
    board_cards: dict[str, tuple[str, str]] = {}

    def fake_existing_any(key: str):
        return board_cards.get(key, (None, None))

    g["existing_any_card"] = fake_existing_any
    g["_parent_is_usable"] = lambda parent: False  # avoid --parent in selftest args

    try:
        # First detection -> create.
        res1 = process_tick(healthy=False, alert_text=report, audit=audit)
        if res1["action"] != "created":
            failures.append(f"first tick should create, got {res1}")
        if len(created) != 1:
            failures.append(f"expected 1 create, got {len(created)}")
        tid1 = res1["task_id"]
        board_cards[res1["key"]] = (tid1, "ready")

        # Second detection (same key, card open) -> dedupe.
        res2 = process_tick(healthy=False, alert_text=report, audit=audit)
        if res2["action"] != "deduped":
            failures.append(f"second tick should dedupe, got {res2}")
        if len(created) != 1:
            failures.append(f"no second card should be created, got {len(created)}")
        if len(commented) != 1:
            failures.append(f"expected 1 comment on dedupe, got {len(commented)}")

        # Healthy -> resolve + auto-complete.
        res3 = process_tick(healthy=True, alert_text="", audit=audit)
        if res3["action"] != "resolved":
            failures.append(f"healthy tick should resolve, got {res3}")
        if tid1 not in completed:
            failures.append(f"expected auto-complete of {tid1}, got completed={completed}")

        # Closed within window -> recurrence suppressed (no new card).
        statuses[tid1] = "done"
        board_cards[res1["key"]] = (tid1, "done")
        g["_task_created_at"] = lambda tid: datetime.now(timezone.utc).timestamp() - 100  # just created
        res4 = process_tick(healthy=False, alert_text=report, audit=audit)
        if res4["action"] != "recurrence_suppressed":
            failures.append(f"closed-within-window should suppress, got {res4}")
        if len(created) != 1:
            failures.append(f"no new card on recurrence within window, got {len(created)}")

        # Window expired -> fresh card.
        g["_task_created_at"] = lambda tid: datetime.now(timezone.utc).timestamp() - (DEDUP_WINDOW_SECONDS + 3600)
        res5 = process_tick(healthy=False, alert_text=report, audit=audit)
        if res5["action"] != "created":
            failures.append(f"window-expired should create fresh, got {res5}")
        if len(created) != 2:
            failures.append(f"expected 2nd create after window expiry, got {len(created)}")

        # Audit lines present for every major action.
        audit_lines = AUDIT_PATH.read_text().strip().splitlines()
        actions = {json.loads(a)["action"] for a in audit_lines}
        for want in ("card_created", "card_deduped", "card_resolved", "recurrence_suppressed"):
            if want not in actions:
                failures.append(f"audit missing {want}: {sorted(actions)}")

        # Repin apply must refuse a LOCKED profile even with apply semantics.
        g["_run_hermes"] = real_run  # not needed; apply_repin writes config only for ELIGIBLE
        res_repin = apply_repin("trading-breakout-trader", audit)
        if res_repin.get("applied"):
            failures.append("apply_repin must refuse LOCKED trading profile")
    finally:
        g["_run_hermes"] = real_run
        g["_card_status"] = real_status
        g["_task_created_at"] = real_created_at
        g["existing_any_card"] = real_existing_any
        g["_parent_is_usable"] = real_parent

    if failures:
        print("SELFTEST_FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELFTEST_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
