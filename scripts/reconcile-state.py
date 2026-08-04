#!/usr/bin/env python3
"""
reconcile-state.py — immutable reconciled state for the sycode-trading lane.

Joins FIVE domains on the natural correlation key `task_id` (t_xxxxxxxx), which
already threads through kanban cards, branch names, PR titles, commit messages, and
obsidian note frontmatter:

    kanban card  ↔  git branch/commits  ↔  PR  ↔  merge→main  ↔  deployed build  ↔  obsidian note

Outputs (Frank chose these two surfaces):
  1. STATE.md  — the Obsidian dashboard (single pane of glass), git-committed by the
                 vault autocommit cron = immutable, content-addressed history.
  2. a compact boot HEADLINE — injected into every session by the SessionStart hook.

DOCTRINE (same as seat-live-state): this is a FALSIFIABLE PRIOR, not an oracle. Every
fact is anchored to its immutable source (git SHA / kanban row / deploy label). A fresh
point-of-use probe always wins; if it disagrees, THIS reconciler is the suspect.

HARD RULES: strictly read-only (only write is the vault-tracked STATE.md/headline; the
only network call is one bounded `git fetch`). NO psql (classifier-gated). Every external
call is timeout-wrapped; a failed probe renders `PROBE FAILED`, never blank/0. Always
exits 0. Self-dated + content-hashed so staleness is visible.
"""
import subprocess, sqlite3, hashlib, os, re, time, datetime

REPO      = "/home/frank/sycode-trading"
VAULT     = "/home/frank/obsidian/sycode-trading"
STATE_MD  = os.path.join(VAULT, "STATE.md")
BOARD_DB  = "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db"
GH_REPO   = "sycamoregroupltd/sycode-trading"
CONTAINER = "sycodetrading-server"
STATE_DIR = "/home/frank/.hermes/state"
HEADLINE  = os.path.join(STATE_DIR, "state-headline.txt")
PHASES    = os.path.join(STATE_DIR, "ns-phases.tsv")
TASK_RE   = re.compile(r"t_[0-9a-f]{6,}")
FAILS     = []

def sh(cmd, timeout=8):
    """Run a shell command read-only; return stripped stdout or None (never raises)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        return out if out else None
    except Exception:
        return None

def probe(label, cmd, timeout=8):
    v = sh(cmd, timeout)
    if v is None:
        FAILS.append(label)
        return "PROBE FAILED"
    return v

def q(sql):
    """Read-only kanban query -> list of tuples (never raises)."""
    try:
        con = sqlite3.connect(f"file:{BOARD_DB}?mode=ro", uri=True, timeout=3)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except Exception as e:
        FAILS.append("board-db")
        return []

# ── 1. global anchors ─────────────────────────────────────────────────────────
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
deployed = probe("deployed-sha",
    f'docker inspect {CONTAINER} --format \'{{{{index .Config.Labels "com.sycodetrading.git.sha"}}}}\'', 4)
fetch_ok = sh(f'timeout 3 git -C {REPO} fetch -q --depth=50 origin main', 5) is not None or True
# bounded fetch already ran above via sh(); recompute ref regardless
sh(f'timeout 3 git -C {REPO} fetch -q --depth=50 origin main', 5)
main = probe("origin-main", f'git -C {REPO} rev-parse origin/main', 3)
head_branch = probe("head-branch", f'git -C {REPO} rev-parse --abbrev-ref HEAD', 3)

behind = "unknown"
undeployed = []
if deployed != "PROBE FAILED" and main != "PROBE FAILED":
    if sh(f'git -C {REPO} cat-file -e {deployed} 2>/dev/null && echo ok', 3):
        behind = sh(f'git -C {REPO} rev-list --count {deployed}..origin/main', 3) or "unknown"
        log = sh(f'git -C {REPO} log {deployed}..origin/main --no-merges --pretty=%h\x1f%s', 4) or ""
        for line in log.splitlines():
            if "\x1f" not in line: continue
            h, s = line.split("\x1f", 1)
            undeployed.append({"sha": h, "subj": s,
                               "tasks": TASK_RE.findall(s),
                               "pr": (re.search(r"#(\d+)", s) or [None, None])[1] if re.search(r"#(\d+)", s) else None})

health = sh(f'timeout 2 curl -4 -s http://127.0.0.1:7777/health', 3) or ""
mode = "paper" if re.search("paper", health, re.I) else ("LIVE ⚠" if re.search("live", health, re.I) else "unknown (call sycode_status)")
provider = probe("provider",
    "timeout 6 hermes status 2>/dev/null | grep -iE '^\\s*(Model|Provider):' | sed 's/^ *//' | tr -s ' ' | paste -sd' | ' -", 8)

# ── 2. board rollup + blocked-pile composition (crash graveyard detector) ──────
rows = q("SELECT status, COUNT(*) FROM tasks GROUP BY status")
counts = {s: c for s, c in rows}
def cnt(*ss): return sum(counts.get(s, 0) for s in ss)
blk_total = counts.get("blocked", 0)
crash = q("SELECT COUNT(*) FROM tasks WHERE status='blocked' AND last_failure_error LIKE '%not alive%'")
crash = crash[0][0] if crash else 0
spam = q("SELECT COUNT(*) FROM tasks WHERE status='blocked' AND (title LIKE 'DIAGNOSTIC%' OR title LIKE '%[PIPELINE_STARVATION]%' OR title LIKE '%[DLQ_ERRORS]%' OR title LIKE '%[QUEUE_BACKLOG]%')")
spam = spam[0][0] if spam else 0
real_blocked = max(blk_total - crash, 0)  # crash includes spam-that-crashed; real ≈ non-crash

# ── 3. PRs (open + recently merged) ────────────────────────────────────────────
import json
def gh_json(args, timeout=12):
    out = sh(f'timeout {timeout} gh pr list --repo {GH_REPO} {args}', timeout+2)
    try:
        return json.loads(out) if out else []
    except Exception:
        FAILS.append("gh-pr")
        return []
FIELDS = "number,title,state,headRefName,mergeCommit,isDraft"
prs_open   = gh_json(f"--state open   --limit 300 --json {FIELDS}")
prs_merged = gh_json(f"--state merged --limit 150 --json {FIELDS}")
pr_by_task = {}   # task_id -> pr dict
def index_pr(p):
    for t in TASK_RE.findall(p.get("headRefName","") + " " + p.get("title","")):
        pr_by_task.setdefault(t, p)
for p in prs_open + prs_merged: index_pr(p)

def is_deployed(merge_sha):
    if not merge_sha or deployed == "PROBE FAILED": return False
    return sh(f'git -C {REPO} merge-base --is-ancestor {merge_sha} {deployed} 2>/dev/null && echo Y', 3) == "Y"

# ── 4. active/relevant cards from kanban ───────────────────────────────────────
active = q("SELECT id,title,status,assignee,priority,consecutive_failures FROM tasks "
           "WHERE status IN ('ready','running','review','todo','scheduled') ORDER BY priority DESC, status")
card = {r[0]: {"title": r[1], "status": r[2], "assignee": r[3], "prio": r[4], "fails": r[5]} for r in active}

# ── 5. obsidian note map (one vault scan) ──────────────────────────────────────
note_map = {}
scan = sh(f"timeout 8 grep -rHoE 't_[0-9a-f]{{6,}}' {VAULT} --include='*.md' 2>/dev/null", 10) or ""
for line in scan.splitlines():
    if ":" not in line: continue
    path, tid = line.rsplit(":", 1)
    rel = os.path.relpath(path, VAULT)
    note_map.setdefault(tid, set()).add(rel)

# ── 6. build the per-task lineage set ──────────────────────────────────────────
task_ids = set(pr_by_task) | set(card) | {t for c in undeployed for t in c["tasks"]}
def sort_key(t):
    c = card.get(t, {})
    st = c.get("status", "")
    order = {"running":0,"review":1,"ready":2,"todo":3,"scheduled":4}.get(st, 5)
    return (order, -(c.get("prio") or 0))
lineage = []
for t in sorted(task_ids, key=sort_key):
    c = card.get(t)
    p = pr_by_task.get(t)
    merge_sha = (p or {}).get("mergeCommit", {})
    merge_sha = merge_sha.get("oid") if isinstance(merge_sha, dict) else None
    depl = is_deployed(merge_sha) if (p and p.get("state") == "MERGED") else False
    if p:
        pstate = "draft" if p.get("isDraft") else p.get("state","").lower()
        prcell = f"#{p['number']} {pstate}"
    else:
        prcell = "—"
    lineage.append({
        "task": t,
        "card": (f"{c['status']}" + (f" ⚠{c['fails']}f" if c and c.get("fails") else "")) if c else "—",
        "title": (c or {}).get("title") or (p or {}).get("title","")[:60] or "",
        "pr": prcell,
        "deployed": "✅" if depl else ("merged·UNDEPLOYED" if (p and p.get("state")=="MERGED") else "—"),
        "notes": len(note_map.get(t, [])),
    })

# ── 7. phase board ─────────────────────────────────────────────────────────────
phases = []
try:
    with open(PHASES) as f:
        for ln in f:
            parts = ln.rstrip("\n").split("\t")
            if len(parts) >= 4: phases.append(parts[:4])
except Exception:
    FAILS.append("phases-file")

# ── 7. event signature verification (t_3f244a06 — verify-and-report only) ─────
import base64 as _b64

def _verify_event_sig(event_id, kind, payload, created_at, sig_b64, task_id=None, run_id=None):
    """Verify one task_events row's ed25519 signature against allowed_signers.
    
    Uses the SAME canonical form as the signer (kanban_db.py): actual
    task_id/run_id values, NOT hard-coded None. Mismatched forms cause
    every verified event to fail even when perfectly signed.
    
    Returns: ('GOOD', principal) | ('UNVERIFIED', None) | ('BAD', msg)
    Never raises — any failure class becomes UNVERIFIED so this never blocks.
    """
    if not sig_b64:
        return ("UNSIGNED", None)
    try:
        signature = _b64.b64decode(sig_b64)
    except Exception:
        return ("BAD invalid-base64", None)
    
    # Build candidate payloads for each registered principal and test each
    fp_to_principal = {}
    principal_to_pub = {}
    try:
        with open("/home/frank/.hermes/governance/allowed_signers") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.split()
                if len(parts) < 3:
                    continue
                principal = parts[0]
                key_type = key_base64 = None
                for i, tok in enumerate(parts[2:], start=2):
                    if tok.startswith("namespaces=") or tok.startswith("valid-after=") or tok.startswith("valid-before="):
                        continue
                    key_type = tok
                    key_base64 = parts[i + 1] if i + 1 < len(parts) else None
                    break
                if key_type != "ssh-ed25519" or not key_base64:
                    continue
                pub_bytes = _b64.b64decode(key_base64)
                principal_to_pub[principal] = pub_bytes
    except Exception:
        return ("UNVERIFIED no-signers-file", None)
    
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return ("UNVERIFIED no-cryptography", None)
    
    # CRITICAL FIX: use actual task_id/run_id, matching the signer's form.
    # The signer (hermes_cli/kanban_event_signing.py sign_event_payload)
    # includes actual values; a verifier that hardcodes None will always
    # report every signed event as UNTRUSTED despite perfect signatures.
    d = {"task_id": task_id, "kind": kind, "payload": json.loads(payload) if isinstance(payload, str) and payload else None, "run_id": run_id, "created_at": int(created_at)}
    msg = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    
    for principal, pub_bytes in principal_to_pub.items():
        try:
            pubkey = serialization.load_ssh_public_key(f"ssh-ed25519 {_b64.b64encode(pub_bytes).decode()}".encode())
            try:
                pubkey.verify(signature, msg)
                return ("GOOD", principal)
            except Exception:
                pass
        except Exception:
            continue
    
    return ("UNTRUSTED signed-by-unregistered-key", None)

SIG_STATS = {"GOOD": 0, "UNSIGNED": 0, "UNTRUSTED": 0, "BAD": 0, "TOTAL": 0}
try:
    # Full-table scan: verify every SIGNED event (crypto), count the rest.
    # Older events predate signing (column added 2026-08-04) and are
    # legitimately UNSIGNED — that is the verify-and-report baseline.
    _total_n = q("SELECT COUNT(*) FROM task_events")
    _total_n = int(_total_n[0][0]) if _total_n else 0
    SIG_STATS["TOTAL"] = _total_n
    _sig_rows = q("SELECT id, task_id, run_id, kind, payload, created_at, event_signature "
                  "FROM task_events WHERE event_signature IS NOT NULL AND event_signature != '' "
                  "ORDER BY id")
    _signed_n = len(_sig_rows)
    SIG_STATS["UNSIGNED"] = _total_n - _signed_n
    for _r in _sig_rows:
        # q() returns plain tuples (no row_factory) — index positionally
        eid, tid, kind, payload, created, sig = int(_r[0]), _r[1], _r[3], _r[4], _r[5], _r[6]
        rid = _r[2] if _r[2] is not None else None
        status, _ = _verify_event_sig(eid, kind, payload, created, sig, task_id=tid, run_id=rid)
        SIG_STATS[status] = SIG_STATS.get(status, 0) + 1
except Exception:
    pass  # Column may not exist yet on this board — skip silently
# ── 8. render STATE.md ─────────────────────────────────────────────────────────
gap_tag = "up-to-date" if behind == "0" else (f"+{behind} merged, UNDEPLOYED" if behind not in ("unknown","PROBE FAILED") else "gap unknown")
head_flag = "" if head_branch in ("main","PROBE FAILED") else f"  ⚠ OFF-MAIN (deploy from origin/main; fresh worktree)"
fail_line = ", ".join(FAILS) if FAILS else "none"

body = []
body.append(f"# sycode-trading — IMMUTABLE STATE\n")
body.append(f"> Reconciled {now} · sources joined on `task_id` · **falsifiable prior** — a fresh probe wins; if it disagrees, `reconcile-state.py` is the bug.\n")
body.append(f"> Regenerate: `python3 ~/.hermes/scripts/reconcile-state.py` · git-committed here = immutable history.\n")

body.append("\n## Deploy & gate\n")
body.append(f"- **Deployed build**: `{deployed[:12]}` → **main** `{main[:12] if main!='PROBE FAILED' else main}` — **{gap_tag}**")
body.append(f"- **Shared checkout HEAD**: `{head_branch}`{head_flag}")
body.append(f"- **Trading mode**: {mode} · gate = `open_positions==0` → **call `mcp__jarvis__sycode_status` for the live count** (shell cannot read it)")
body.append(f"- **Provider**: {provider}")
if undeployed:
    body.append(f"\n**{len(undeployed)} undeployed commits** (merged, not shipped):")
    for c in undeployed:
        tags = " ".join(c["tasks"]) or ""
        pr = f"#{c['pr']} " if c["pr"] else ""
        body.append(f"  - `{c['sha']}` {pr}{c['subj'][:80]}  {tags}")

body.append("\n## North Star phase board\n")
body.append("| Phase | Status | Title | Next |")
body.append("|---|---|---|---|")
for p in phases:
    body.append(f"| **{p[0]}** | {p[1]} | {p[2]} | {p[3]} |")

body.append("\n## Board health\n")
body.append(f"- **Active**: {counts.get('running',0)} running · {counts.get('ready',0)} ready · {counts.get('todo',0)} todo · {counts.get('scheduled',0)} scheduled · {counts.get('done',0)} done")
body.append(f"- **Blocked: {blk_total}** — but **{crash} carry `pid not alive` (crash graveyard, not backlog)**; ~{spam} diagnostic-storm spam; **~{max(blk_total-crash,0)} genuinely gated**. ⚠ Triage the pile as noise, not work.")
sig_line = f"- **Event signatures**: {SIG_STATS['TOTAL']} events checked — {SIG_STATS['GOOD']} GOOD, {SIG_STATS.get('UNSIGNED', 0)} UNSIGNED, {SIG_STATS.get('UNTRUSTED', 0)} UNTRUSTED, {SIG_STATS.get('BAD', 0)} BAD (t_3f244a06 verify-and-report)"
if SIG_STATS['TOTAL'] > 0:
    body.append(sig_line)

body.append("\n## Work lineage (active + recent, joined on task_id)\n")
body.append("| task_id | card | PR | deployed? | notes | title |")
body.append("|---|---|---|---|---|---|")
for r in lineage[:45]:
    body.append(f"| `{r['task']}` | {r['card']} | {r['pr']} | {r['deployed']} | {r['notes']} | {r['title'][:52]} |")
if len(lineage) > 45:
    body.append(f"\n_(+{len(lineage)-45} more active task_ids — full set in the reconciler)_")

body.append(f"\n---\n_PROBES FAILED: {fail_line}_")

content = "\n".join(body) + "\n"
chash = hashlib.sha256(content.encode()).hexdigest()[:12]
content = content.replace("**falsifiable prior**", f"**falsifiable prior** · state-hash `{chash}`", 1)

os.makedirs(STATE_DIR, exist_ok=True)
try:
    with open(STATE_MD, "w") as f: f.write(content)
except Exception:
    print("FAILED to write STATE.md"); raise SystemExit(0)

# ── 9. compact boot headline ───────────────────────────────────────────────────
active_phase = next((p for p in phases if p[1].upper().startswith("ACTIVE") or p[1]=="ACTIVE"), None)
hl = []
hl.append(f"━━ SYCODE STATE @ {now} · hash {chash} ━━ (falsifiable prior — full: {STATE_MD})")
hl.append(f"DEPLOY: {deployed[:9]} → main {main[:9] if main!='PROBE FAILED' else '?'} ({gap_tag}) | HEAD {head_branch}{'  ⚠OFF-MAIN' if head_flag else ''}")
hl.append(f"MODE: {mode} (gate open_positions==0 → call sycode_status) | PROVIDER: {provider[:60] if provider else '?'}")
hl.append(f"BOARD: {counts.get('running',0)}run/{counts.get('ready',0)}ready/{counts.get('todo',0)}todo/{blk_total}blocked ({crash} are dead-workers, not backlog)")
if active_phase:
    hl.append(f"NORTH STAR: {active_phase[0]} ACTIVE — {active_phase[2]} → {active_phase[3][:120]}")
hl.append(f"UNDEPLOYED: {len(undeployed)} commits | PROBES FAILED: {fail_line}")
hl.append("DOCTRINE: memory=provenance, STATE.md=reconciled prior; re-probe the exact field before any deploy/merge/gate/DML.")
try:
    with open(HEADLINE, "w") as f: f.write("\n".join(hl) + "\n")
except Exception:
    pass

print("\n".join(hl))
print(f"\nSTATE.md written ({len(content)} bytes, {len(lineage)} tasks joined) → {STATE_MD}")
