#!/usr/bin/env python3
"""kanban_idempotency_board_guard.py - fleet-wide guard for kanban idempotency/board pairing.

Invariant (RCA t_4f419b25, hardening task t_c7040708):

  R1. Any `hermes kanban create` (or Python kanban_create tool call) that passes
      --idempotency-key MUST also pass --board. Without an explicit board, the
      dedup lookup resolves to whatever board the ambient HERMES_KANBAN_BOARD
      env / current-board pointer names, so the dedup can miss and duplicate
      cards flood the board (the DIAGNOSTIC-card QUEUE_BACKLOG flood).

  R2. Any dedup lookup (query_last_task / query_active_diag_cards style) MUST
      resolve the kanban.db from the idempotency key's embedded board slug
      (diag:<board>:<type>:<metric>), never from ambient HERMES_KANBAN_DB /
      HERMES_KANBAN_BOARD env.

No-agent watchdog contract (fleet cron convention): default scan prints nothing
when clean and exits 0; any stdout is the alert payload. `--check` exits 1 when
any ERROR-level violation is found. `--selfcheck` runs built-in unit tests.

Usage:
  kanban_idempotency_board_guard.py                 # scan all fleet roots, report
  kanban_idempotency_board_guard.py --check         # exit 1 on any ERROR
  kanban_idempotency_board_guard.py --json          # machine-readable findings
  kanban_idempotency_board_guard.py --target FILE   # scan one file (pre-commit)
  kanban_idempotency_board_guard.py --selfcheck     # run unit tests
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

def _resolve_hermes_root():
    """Return the real fleet HERMES root, ignoring profile-sandbox env drift.

    Ambient HERMES_HOME / HERMES_REAL_HOME can point at a profile sandbox or
    a bare home dir; the guard must always scan the canonical fleet root
    (~/.hermes) unless the override genuinely contains profiles/scripts.
    """
    cand = Path(os.environ.get("HERMES_REAL_HOME", "")).expanduser()
    if cand.name and (cand / "profiles").exists() and (cand / "scripts").exists():
        return cand
    default = Path.home() / ".hermes"
    if (default / "profiles").exists():
        return default
    return cand if cand.name else default


HERMES_ROOT = _resolve_hermes_root()
SCRIPTS_DIR = HERMES_ROOT / "scripts"
PROFILES_DIR = HERMES_ROOT / "profiles"
GLOBAL_CRON_STORE = HERMES_ROOT / "cron" / "jobs.json"

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------
CLI_CREATE_RE = re.compile(r"hermes\s+kanban\s+create\b")
CLI_CREATE_LIST_RE = re.compile(
    r"['\"]hermes['\"]\s*,\s*['\"]kanban['\"]\s*,\s*['\"]create['\"]"
)
PY_CREATE_RE = re.compile(r"\bkanban_create\s*\(")
IDEM_CLI_RE = re.compile(r"--idempotency-key")
IDEM_KW_RE = re.compile(r"\bidempotency_key\s*=")
BOARD_CLI_RE = re.compile(r"--board\b")
BOARD_KW_RE = re.compile(r"\bboard\s*=")
ENV_BOARD_PIN_RE = re.compile(r"HERMES_KANBAN_BOARD\s*=")
ENV_DB_AMBIENT_RE = re.compile(
    r"(?:os\.environ|os\.getenv|os\.environ\.get|environ\.get|os\.getenv|env\.get)"
    r"\(\s*[\"']HERMES_KANBAN_(?:DB|BOARD)[\"']"
)
ENV_DB_RAW_RE = re.compile(r"HERMES_KANBAN_(?:DB|BOARD)")
DEDUP_FN_RE = re.compile(
    r"\bdef\s+(query_last_task|query_active_diag_cards|query_[a-z_]*diag[a-z_]*|"
    r"[a-z_]*dedup[a-z_]*|[a-z_]*idempoten[a-z_]*)\s*\("
)
KEY_DECODE_RE = re.compile(r"\.split\([\"']:[\"']\)|parts\[1\]|parts\[0\]|key\.split")
IDEM_HINT_RE = re.compile(r"\bidempotency\b|\bdiag:")
DEDUP_FN_NAME_RE = re.compile(
    r"(query_last_task|query_active_diag_cards|query_[a-z_]*diag[a-z_]*|dedup|idempoten)"
)

SKIP_DIR_PARTS = {"backups", "archive", "__pycache__", "state", "staging", "logs",
                  ".git", ".pytest_cache", ".ruff_cache", "alerts", "output",
                  "cron", "notepad", "sessions", "memories", ".claude"}

# The guard never lints itself: its --selfcheck embeds fixture command strings
# in regular string literals which would otherwise be flagged as violations.
SELF_EXCLUDE = {"kanban_idempotency_board_guard.py"}
SKIP_SUFFIXES = {".bak", ".pyc", ".json", ".md", ".tsv", ".txt", ".db"}

# ---------------------------------------------------------------------------
# Scanning helpers
# ---------------------------------------------------------------------------

def iter_script_files(roots):
    """Yield (path, text) for every script under roots, skipping junk dirs/files."""
    seen = set()
    for root in roots:
        if not root or not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIR_PARTS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in SKIP_SUFFIXES:
                    continue
                if p.name.startswith(".") or ".bak" in p.name:
                    continue
                if p.name in SELF_EXCLUDE:
                    continue
                try:
                    key = str(p.resolve())
                except OSError:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not text.strip():
                    continue
                yield p, text


def iter_cron_prompts():
    """Yield (jobs_json_path, job_name, prompt) for every cron job prompt."""
    stores = []
    if GLOBAL_CRON_STORE.exists():
        stores.append(GLOBAL_CRON_STORE)
    if PROFILES_DIR.exists():
        seen: set[str] = set()
        for p in sorted(PROFILES_DIR.glob("*/cron/jobs.json")):
            real = str(p.resolve())
            if real in seen:
                continue
            seen.add(real)
            stores.append(p)
    for store in stores:
        try:
            data = json.loads(store.read_text(encoding="utf-8"))
        except Exception:
            continue
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
        for job in jobs:
            if not isinstance(job, dict):
                continue
            prompt = job.get("prompt") or ""
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            yield store, str(job.get("name", "?")), prompt


def _invocation_window(text, start, length=700):
    """Return a window of text starting at `start` (CLI args often span lines)."""
    return text[start:start + length]


def comment_string_mask(text):
    """Set of char offsets inside line comments or triple-quoted strings.

    Lets the guard ignore prose/docstring references to command shapes
    (e.g. "the key passed to `hermes kanban create --idempotency-key`") while
    still scanning real command construction sites.
    """
    masked = set()
    i, n = 0, len(text)
    in_triple = None
    triple_start = -1
    while i < n:
        c = text[i]
        if in_triple:
            if text[i:i + 3] == in_triple * 3:
                masked.update(range(triple_start, i + 3))
                in_triple = None
                i += 3
                continue
            i += 1
            continue
        if text[i:i + 3] in ('"""', "'''"):
            in_triple = text[i]
            triple_start = i
            i += 3
            continue
        if c == "#":
            end = text.find("\n", i)
            if end == -1:
                end = n
            masked.update(range(i, end))
            i = end
            continue
        i += 1
    return masked


def check_create_pairing(text, path, findings):
    """R1: any create with --idempotency-key must also pass --board."""
    env_pinned = bool(ENV_BOARD_PIN_RE.search(text))
    masked = comment_string_mask(text)

    def examine(m_start, win_end, kind):
        if m_start in masked:
            return
        win = text[m_start:win_end]
        if not IDEM_CLI_RE.search(win) and not IDEM_KW_RE.search(win):
            return
        if BOARD_CLI_RE.search(win) or BOARD_KW_RE.search(win):
            return
        severity = "WARN" if env_pinned else "ERROR"
        findings.append({
            "rule": "R1-create-pairing",
            "severity": severity,
            "path": str(path),
            "line": text.count("\n", 0, m_start) + 1,
            "detail": (
                f"{kind} with --idempotency-key has no --board in the same "
                "invocation"
                + (" (file pins HERMES_KANBAN_BOARD - accepted but fragile)"
                   if env_pinned else "")
            ),
        })

    for m in CLI_CREATE_RE.finditer(text):
        examine(m.start(), m.start() + 700, "hermes kanban create")
    for m in CLI_CREATE_LIST_RE.finditer(text):
        close = text.find("]", m.start())
        win_end = close + 1 if close != -1 else m.start() + 700
        examine(m.start(), win_end, "hermes kanban create (list form)")
    for m in PY_CREATE_RE.finditer(text):
        end = text.find(")", m.start())
        if end == -1:
            end = m.start() + 400
        examine(m.start(), end + 1, "kanban_create(...)")


def check_dedup_db_resolution(text, path, findings):
    """R2: dedup lookups must resolve DB from the key's embedded board slug."""
    if not path.suffix == ".py":
        # Shell: flag only named dedup functions/blocks resolving from ambient env.
        for m in re.finditer(r"(?:query_last_task|query_active_diag_cards|dedup)",
                             text):
            win = text[max(0, m.start() - 60):m.start() + 160]
            if ENV_DB_RAW_RE.search(win) and IDEM_HINT_RE.search(win):
                findings.append({
                    "rule": "R2-dedup-db-resolution",
                    "severity": "ERROR",
                    "path": str(path),
                    "line": text.count("\n", 0, m.start()) + 1,
                    "detail": "dedup lookup resolves kanban DB from ambient "
                              "HERMES_KANBAN_DB/BOARD env instead of the key",
                })
        return

    for m in DEDUP_FN_RE.finditer(text):
        body_start = m.end()
        # Find the end of the function: next top-level 'def ' or EOF.
        body_end = text.find("\ndef ", body_start)
        if body_end == -1:
            body_end = len(text)
        body = text[body_start:body_end]
        if not IDEM_HINT_RE.search(body):
            continue
        if KEY_DECODE_RE.search(body):
            continue  # resolves from the key - correct
        env_hits = [ln for ln in body.split("\n")
                    if ENV_DB_RAW_RE.search(ln)
                    and ("environ" in ln or "getenv" in ln or "HERMES_KANBAN_DB" in ln)]
        if env_hits:
            hit_line = env_hits[0]
            hit_offset = body.find(hit_line)
            if hit_offset == -1:
                hit_offset = 0
            abs_offset = body_start + hit_offset
            findings.append({
                "rule": "R2-dedup-db-resolution",
                "severity": "ERROR",
                "path": str(path),
                "line": text.count("\n", 0, abs_offset) + 1,
                "detail": f"dedup lookup {m.group(1)}() resolves DB from ambient "
                          "env; must decode board from the idempotency key",
            })


def scan_text(text, path, findings):
    check_create_pairing(text, path, findings)
    check_dedup_db_resolution(text, path, findings)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def scan_roots(target=None):
    findings = []
    if target:
        p = Path(target).expanduser()
        if p.is_dir():
            roots = [p]
        else:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"guard: cannot read {p}: {exc}", file=sys.stderr)
                sys.exit(2)
            scan_text(text, p, findings)
            return findings
    else:
        roots = [SCRIPTS_DIR]
        _seen_roots: set[str] = {str(SCRIPTS_DIR.resolve())}
        for _sd in sorted(PROFILES_DIR.glob("*/scripts")):
            _rr = str(_sd.resolve())
            if _rr in _seen_roots:
                continue  # symlink alias (e.g. sycode-trading -> sycode-trading-pm) — dedupe
            _seen_roots.add(_rr)
            roots.append(_sd)
    for path, text in iter_script_files(roots):
        scan_text(text, path, findings)
    for store, job_name, prompt in iter_cron_prompts():
        # Reuse R1 on the prompt text, reporting the jobs.json location.
        before = len(findings)
        check_create_pairing(prompt, Path(f"{store}#job:{job_name}"), findings)
        for f in findings[before:]:
            f["detail"] += f" (cron job '{job_name}')"
    return findings


def run_selfcheck():
    """Unit tests over temp fixtures. Exit 0 on pass, 1 on fail."""
    checks = []

    def fixture(name, content, suffix=".py"):
        d = Path(tempfile.mkdtemp(prefix="idem-guard-"))
        p = d / (name + suffix)
        p.write_text(content, encoding="utf-8")
        return p

    # t1: create with --idempotency-key AND --board -> pass
    p = fixture("ok_board",
                "cmd = ['hermes','kanban','create','--board','jarvis-os',\n"
                "       'title','--assignee','devops','--idempotency-key','diag:x']\n")
    scan_text(p.read_text(), p, (f := []))
    checks.append(("t1 create key+board passes", len(f) == 0))

    # t2: create with --idempotency-key, no --board, no pin -> ERROR
    p = fixture("no_board",
                "cmd = ['hermes','kanban','create','title','--assignee','x',\n"
                "       '--idempotency-key','diag:jarvis-os:Q:B']\n")
    f = []
    scan_text(p.read_text(), p, f)
    checks.append(("t2 create key w/o board errors", any(x["severity"] == "ERROR" for x in f)))

    # t3: create with key, no --board, but file pins HERMES_KANBAN_BOARD -> WARN
    p = fixture("env_pin",
                "export HERMES_KANBAN_BOARD=jarvis-os\n"
                "hermes kanban create --assignee x --idempotency-key diag:x:a:b title\n")
    f = []
    scan_text(p.read_text(), p, f)
    checks.append(("t3 env-pinned create is WARN not ERROR",
                   any(x["severity"] == "WARN" for x in f)
                   and not any(x["severity"] == "ERROR" for x in f)))

    # t4: query_last_task decodes board from key -> pass
    p = fixture("key_decode",
                "def query_last_task(idempotency_key):\n"
                "    parts = idempotency_key.split(':')\n"
                "    board_slug = parts[1]\n"
                "    db_path = resolve_board_db(board_slug)\n"
                "    conn = kb.connect(db_path=db_path)\n")
    f = []
    scan_text(p.read_text(), p, f)
    checks.append(("t4 key-decoded dedup passes", len(f) == 0))

    # t5: query_active_diag_cards resolves from ambient env -> ERROR
    p = fixture("ambient_env",
                "def query_active_diag_cards(priority=None):\n"
                "    board_name = os.environ.get('HERMES_KANBAN_BOARD') or 'sycode-trading'\n"
                "    db_path = resolve_board_db(board_name)\n"
                "    cursor.execute(\"SELECT id FROM tasks WHERE idempotency_key LIKE 'diag:%'\")\n")
    f = []
    scan_text(p.read_text(), p, f)
    checks.append(("t5 ambient-env dedup errors",
                   any(x["severity"] == "ERROR" for x in f)))

    # t6: python kanban_create tool call with idempotency_key, no board -> ERROR
    p = fixture("py_tool",
                "kanban_create(title='x', assignee='devops', idempotency_key='diag:y:a:b')\n",
                suffix=".py")
    f = []
    scan_text(p.read_text(), p, f)
    checks.append(("t6 py kanban_create key w/o board errors",
                   any(x["severity"] == "ERROR" for x in f)))

    # t7: unrelated code with HERMES_KANBAN_DB but no dedup function -> pass
    p = fixture("unrelated",
                "db_path = os.environ.get('HERMES_KANBAN_DB')\n"
                "print('hello')\n")
    f = []
    scan_text(p.read_text(), p, f)
    checks.append(("t7 unrelated ambient read passes", len(f) == 0))

    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 1 if failures else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Kanban idempotency/board pairing guard")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when any ERROR-level violation is found")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--target", default=None,
                    help="scan a single file or directory instead of fleet roots")
    ap.add_argument("--selfcheck", action="store_true", help="run unit tests and exit")
    ap.add_argument("--verbose", action="store_true", help="include pass summary")
    args = ap.parse_args(argv)

    if args.selfcheck:
        return run_selfcheck()

    findings = scan_roots(target=args.target)
    errors = [f for f in findings if f["severity"] == "ERROR"]
    warns = [f for f in findings if f["severity"] == "WARN"]

    if args.json:
        print(json.dumps({"errors": errors, "warns": warns}, indent=2))
    else:
        for f in findings:
            print(f"[{f['severity']}] {f['rule']} {f['path']}:{f['line']} - {f['detail']}")
        if args.verbose:
            print(f"guard: scanned; {len(errors)} errors, {len(warns)} warns")

    if args.check and errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
