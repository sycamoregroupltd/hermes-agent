#!/usr/bin/env python3
"""
DLQ growth monitor for the BullMQ `database-writes` dead-letter queue.

Built for kanban task t_b06b8ce8 (RCA t_9f23ec8f): 326 dead signal_journeys
upserts accumulated in bull:database-writes:failed 01:00-02:29 UTC with ZERO
alerting; discovered ~13h later during an unrelated review chain.

This monitor is PAPER-ONLY OBSERVABILITY. It performs strictly read-only Redis
ops (ZCARD / ZRANGE / ZRANGEBYSCORE / ZSCORE / HGET). It NEVER mutates the queue
(no DEL / POP / LREM / retry / requeue). Alerting only.

Alert fires when ANY of:
  - GROWTH:   >= GROWTH_THRESHOLD (default 5) failures whose fail-score lands in
              the trailing ~5m window (ZRANGEBYSCORE over window_start_ms).
              This is a RATE signal — the only signal that remains meaningful
              against an intentionally unbounded retention buffer.
              Design choice (AC2 rework, kanban t_09e3fcfa): we use the true
              windowed rate via new_failures_since(), NOT a last-run size delta.
              A delta baseline was structurally broken: clear_state() on the
              healthy path deleted last_size, so the next delta defaulted to 0
              and a +6 burst from a healthy baseline produced NO alert. The
              windowed signal is stateless (no last_size dependency), so the
              healthy path may safely clear stale alert state.
  - SATURATION: size >= SATURATION_LEVEL (default 450)
                The queue's retained set is deliberately unbounded
                (removeOnFail=false, failedRetainedCap=-1 — see BullMQWriteQueue.ts
                :656). A large depth signals that the forensic buffer is growing
                and approaching operational manageability limits, but there is NO
                implicit discard cap anymore since t_19da81f1 removed it.

Removed: LEVEL rule (absolute threshold) was retired. An unbounded queue is
monotonically non-decreasing; any finite LEVEL threshold latches permanently
the moment the first failure lands and never clears without manual drain. See
kanban t_09e3fcfa for the full justification.

Design note (AC3, kanban t_09e3fcfa): DLQ depth is a RETENTION signal, not a
LOSS signal. The preferred loss signal is the /metrics Prometheus counter
`sycodetrading_bullmq_queue_fk_terminal_total` — process-local, so it resets
on every deploy; consume it as a 15m `increase(...)` exactly like the existing
`DatabaseWriteQueueTerminalFkLoss` alert. This monitor stays metric-scrape-free
(read-only Redis only), but terminal-loss alerting SHOULD be driven from that
counter rather than from DLQ growth alone.

On alert, prints a block to stdout and (best-effort):
  - sends a Telegram alert to Frank (trading-devops SOUL alert channel)
  - creates an idempotency-keyed kanban card (breach-fingerprint key) on the
    sycode-trading board, assigned to the PM for triage

Failure-class sample: the newest N failed jobs are read (failedReason +
data.table/operation) and bucketed so the alert shows WHICH class of writer
failure is killing writes — not just a count.

State file dedup: only alerts on a changed breach fingerprint, then once per
REMIND_SECONDS while the same fingerprint persists. Silent (empty stdout) on
healthy / unchanged state so the no-agent cron stays quiet between breaches.

Run: python3 dlq_growth_monitor.py            # live (alerts)
     python3 dlq_growth_monitor.py --dry-run  # compute + print, no notify
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---- config (env-overridable) ------------------------------------------------
REDIS_CONTAINER = os.getenv("DLQ_REDIS_CONTAINER", "sycodetrading-redis")
QUEUE_NAME = os.getenv("DLQ_QUEUE_NAME", "database-writes")
DLQ_KEY = f"bull:{QUEUE_NAME}:failed"

# LEVEL is retired — see docstring and kanban t_09e3fcfa. Kept as env-constant
# only so the env var does not silently enter another script; but it is NOT
# consulted by any branching logic anymore.
# ALERT_LEVEL = int(os.getenv("DLQ_ALERT_LEVEL", "10"))
GROWTH_THRESHOLD = int(os.getenv("DLQ_GROWTH_THRESHOLD", "5"))  # new/tick
SATURATION_LEVEL = int(os.getenv("DLQ_SATURATION_LEVEL", "450"))  # operational capacity
SAMPLE_N = int(os.getenv("DLQ_SAMPLE_N", "12"))                # failure-class sample size
REMIND_SECONDS = int(os.getenv("DLQ_REMIND_SECONDS", str(24 * 3600)))

STATE_DIR = Path(os.getenv("DLQ_STATE_DIR",
    "/home/frank/.hermes/profiles/trading-devops/cron/state"))
STATE_FILE = STATE_DIR / "dlq_growth_monitor.seen.json"

KANBAN_BOARD = os.getenv("DLQ_KANBAN_BOARD", "sycode-trading")
KANBAN_ASSIGNEE = os.getenv("DLQ_KANBAN_ASSIGNEE", "sycode-trading-pm")
TELEGRAM_TARGET = os.getenv("DLQ_TELEGRAM_TARGET", "telegram")

# t_fde79a2e durability (AC-4/AC-6): the deterministic classifier test must stay
# runnable at the installed path so an independent reviewer can execute it between
# agent sessions. The canonical copy lives OUTSIDE ~/.hermes (which is gitignored
# and can be reset between sessions); on every monitor tick we restore it if the
# installed copy has gone missing. Pure file copy — never touches Redis/DB/
# runtime/deploy/cron state.
SELF_DIR = Path(__file__).resolve().parent
TEST_FIXTURE_INSTALLED = SELF_DIR / "dlq_growth_monitor.test.py"
TEST_FIXTURE_TWIN = Path(
    os.getenv("DLQ_TEST_FIXTURE_TWIN",
              "/home/frank/.local/share/dlq-guard/dlq_growth_monitor.test.py"))


# ---- failure-class classification -------------------------------------------
# Ordered (most specific first). Each (label, compiled-regex on lowercased reason).
CLASS_RULES = [
    # Test-transformed BullMQ worker colliding with the production Redis store
    # (t_9efa2462 / t_fde79a2e): vitest runtime injected into the live
    # database-writes queue. Signature is precise — `[vitest]` + the logger
    # mock missing `runWithContext`. MUST be classified explicitly, never
    # swallowed by `other/unknown`.
    ("test-runtime-prod-redis-collision",
     re.compile(r"\[vitest\]|runwithcontext|vitest worker", re.I)),
    ("db-connection/unreachable",
     re.compile(r"unable to connect|econnrefused|etimedout|fetch failed|timed out|network|dns|enotfound", re.I)),
    ("supabase-layer/down (5xx)",
     re.compile(r"supabase fetch 5\d\d", re.I)),
    ("schema-cache (supabase transient)",
     re.compile(r"could not query the database for the schema cache", re.I)),
    ("immutable-trigger-violation",
     re.compile(r"immutable|trigger-time|cannot (update|modify)|protected column", re.I)),
    ("duplicate-key/pk-conflict",
     re.compile(r"23505|duplicate key|unique constraint", re.I)),
    ("fk-constraint-violation",
     re.compile(r"foreign key|violates .*constraint|23503", re.I)),
    ("table-missing",
     re.compile(r"relation .* does not exist|42p01|table .* missing|does not exist", re.I)),
    ("serialization/lock",
     re.compile(r"serialization failure|40p01|deadlock|could not serialize", re.I)),
    ("payload/too-large",
     re.compile(r"too large|size limit|413|request entity", re.I)),
    ("rate-limit/quota",
     re.compile(r"rate limit|429|quota|too many requests", re.I)),
]
CLASS_OTHER = "other/unknown"
CLASS_UNREADABLE = "unreadable/empty-reason"

# Exported sentinel so callers/tests can assert the Vitest/production-Redis
# collision signature is recognised rather than absorbed by the catch-all.
KNOWN_TEST_RUNTIME = "test-runtime-prod-redis-collision"


def _ensure_test_fixture_present():
    """Restore the deterministic classifier test at the installed path if it has
    gone missing (the recurring vanish that blocked t_fde79a2e's review gate).
    Authoritative bytes live in TEST_FIXTURE_TWIN, outside the gitignored
    ~/.hermes tree. Best-effort; never fatal, never mutates queue/DB."""
    if not TEST_FIXTURE_TWIN.exists() or TEST_FIXTURE_INSTALLED.exists():
        return
    try:
        shutil.copy(TEST_FIXTURE_TWIN, TEST_FIXTURE_INSTALLED)
        print(f"(self-heal) restored {TEST_FIXTURE_INSTALLED.name} "
              f"from {TEST_FIXTURE_TWIN}", file=sys.stderr)
    except Exception as e:
        print(f"(self-heal failed: {e})", file=sys.stderr)


def classify(reason: str) -> str:
    if not reason or not reason.strip():
        # Empty/unreadable reason is a read failure, NOT an unknown signature.
        # Never silently fold it into `other/unknown` (no silent zero — AC-5).
        return CLASS_UNREADABLE
    r = reason.lower()
    for label, rx in CLASS_RULES:
        if rx.search(r):
            return label
    return CLASS_OTHER


# ---- read-only redis helpers ------------------------------------------------
def redis(args, timeout=30):
    """Run a single redis-cli command via `docker exec`. READ-ONLY GUARANTEED:
    only ZCARD / ZRANGE / ZRANGEBYSCORE / HGET are ever passed here."""
    cmd = ["docker", "exec", REDIS_CONTAINER, "redis-cli", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"redis-cli timeout on {args[0]}")
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError((err[-1] if err else f"rc={r.returncode}")[:160])
    return r.stdout


def dlq_size() -> int:
    out = redis(["ZCARD", DLQ_KEY]).strip()
    return int(out) if out.isdigit() else 0


def newest_job_ids(n: int) -> list[str]:
    """Newest n job ids (highest fail-score last). ZRANGE index -n..-1."""
    out = redis(["ZRANGE", DLQ_KEY, str(-n), "-1"]).strip().splitlines()
    return [j for j in out if j]


def fail_time(job_id: str) -> int:
    """Fail timestamp (zscore) in ms → epoch s."""
    out = redis(["ZSCORE", DLQ_KEY, job_id]).strip()
    try:
        return int(int(out) / 1000)
    except (ValueError, TypeError):
        return 0


def job_failed_reason(job_id: str) -> str:
    return redis(["HGET", f"bull:{QUEUE_NAME}:{job_id}", "failedReason"]).strip()


def job_table(job_id: str) -> str:
    raw = redis(["HGET", f"bull:{QUEUE_NAME}:{job_id}", "data"]).strip()
    try:
        d = json.loads(raw)
        return d.get("table", "?")
    except Exception:
        return "?"


def new_failures_since(window_start_ms: int) -> list[str]:
    """Job ids with fail-score >= window_start_ms (i.e. failed within window)."""
    out = redis(["ZRANGEBYSCORE", DLQ_KEY, str(window_start_ms), "+inf"]).strip().splitlines()
    return [j for j in out if j]


# ---- state dedup ------------------------------------------------------------
def read_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def write_state(payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE_FILE)


def clear_state():
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def normalize_fingerprint(text: str) -> str:
    """Drop volatile counts/timestamps so the same breach signature dedups.

    Must handle: plain size=N, "+N new", "level N >= M", timestamps,
    duration hints (~5m), SATURATED at N, "last run size N", AND
    sampled job IDs that differ per tick (the flood-root cause from
    kanban t_09e3fcfa — 62 duplicate cards/tick when same breach
    emitted fresh because sample lines contained different newest-job ids).
    """
    s = re.sub(r"\d+ dead", "<N> dead", text)
    s = re.sub(r"size=\d+", "size=<N>", s)
    s = re.sub(r"\+?\d+ new", "+<N> new", s)
    s = re.sub(r"\blevel\s+\d+\s*(>=|<)?\s*\d+\b", "level <N>", s, flags=re.I)
    # Strip ISO-ish timestamps: 2026-08-04T20:00, 2026-08-04 20:00
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", "<TS>", s)
    # Strip durations: "5m", "10m", "5 min", "10 minutes" etc.
    s = re.sub(r"\b\d+\s*(?:m(?:in(?:utes)?)?|mins)\b", "<D>", s, flags=re.I)
    # Strip trailing numeric sizes: "SATURATED at 143", "last run size 138"
    s = re.sub(r"SATURATED at \d+", "SATURATED at <N>", s, flags=re.I)
    s = re.sub(r"(last run size )\d+", r"\1<N>", s, flags=re.I)
    # Strip sampled job IDs from sample lines: "| abcdef1234567890…" or
    # "| abc123…".  Job IDs are 6+ hex chars after " | " on a sample line.
    s = re.sub(r"\| [0-9a-f]{6,}\s*[…\.]", r"| <JID>", s)
    return s


def should_emit(alert_text: str) -> bool:
    if not alert_text:
        clear_state()
        return False
    now = int(time.time())
    fp = normalize_fingerprint(alert_text)
    state = read_state()
    if state.get("fingerprint") != fp:
        # New or changed stale set — emit immediately. Preserve existing
        # state fields so alert-bookkeeping (first_seen/last_alert/last_seen)
        # survives the fingerprint change.
        write_state({**state, "fingerprint": fp, "first_seen": now, "last_alert": now})
        return True
    last = int(state.get("last_alert", 0))
    if now - last >= REMIND_SECONDS:
        write_state({**state, "last_alert": now})
        return True
    write_state({**state, "last_seen": now})
    return False


# ---- notifiers (best-effort, never fatal) -----------------------------------
def send_telegram(msg: str):
    try:
        subprocess.run(["hermes", "send", "-t", TELEGRAM_TARGET, "-m", msg],
                       timeout=30, capture_output=True, text=True)
        return True
    except Exception as e:
        print(f"(telegram alert failed: {e})", file=sys.stderr)
        return False


STANDING_CARD_IDEM_KEY = f"dlq-{QUEUE_NAME}-standing"

def create_kanban_card(title: str, body: str, idem_key: str):
    try:
        subprocess.run(
            ["hermes", "kanban", "--board", KANBAN_BOARD, "create", title,
             "--assignee", KANBAN_ASSIGNEE, "--priority", "2",
             "--idempotency-key", idem_key, "--body", body],
            timeout=30, capture_output=True, text=True)
        return True
    except Exception as e:
        print(f"(kanban card creation failed: {e})", file=sys.stderr)
        return False

def upsert_standing_card(title: str, body: str) -> bool:
    """Upsert the ONE standing DLQ alert card.

    Uses the fixed standing idempotency key so the kanban service
    returns the existing card id instead of creating a new one on
    every tick.  On the second and subsequent ticks the existing
    card is updated via a comment with the latest evidence.
    Returns True if the card was created or commented successfully.
    """
    # First attempt: create with the standing idempotency key.
    # The kanban service returns the existing task id when the key
    # matches a non-archived card (see kanban_db.py idempotency check).
    created = create_kanban_card(title, body, STANDING_CARD_IDEM_KEY)
    if created:
        return True
    # If create failed (e.g. CLI error), fall back to commenting on the
    # most recent non-archived card with the standing key so the alert
    # is not silently dropped.
    print(f"(standing card create failed, attempting comment fallback)", file=sys.stderr)
    return append_standing_comment(body)

def append_standing_comment(body: str) -> bool:
    """Append a comment to the standing DLQ card as a fallback path."""
    try:
        cmd = ["hermes", "kanban", "--board", KANBAN_BOARD, "comment",
               STANDING_CARD_IDEM_KEY, body[:4000], "--author", "dlq-growth-monitor"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True
        print(f"(standing card comment failed: {result.stderr.strip()[:120]})", file=sys.stderr)
        return False
    except Exception as e:
        print(f"(standing card comment exception: {e})", file=sys.stderr)
        return False


# ---- main -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + print alert, but do NOT notify (kanban/telegram)")
    args = ap.parse_args()
    dry_run = args.dry_run

    # Durability guard: keep the reviewer-executable test fixture present
    # (t_fde79a2e AC-4/AC-6). Runs before any Redis/DB access.
    _ensure_test_fixture_present()

    now = int(time.time())
    window_start_ms = (now - 300) * 1000  # last ~5 min (one tick)

    try:
        size = dlq_size()
        # GROWTH = windowed RATE signal (AC2 rework, t_09e3fcfa). Count
        # failures whose fail-score falls inside the trailing ~5m window.
        # Stateless — does NOT depend on last_size, so a healthy-path
        # clear_state() can no longer blind it (the old delta baseline
        # defaulted to 0 whenever the previous tick was healthy).
        window_new = new_failures_since(window_start_ms)
    except Exception as e:
        # probe failure — surface it as an alert so silent infra death is visible
        err = str(e)[:120]
        alert = (
            f"🔴 DLQ MONITOR PROBE FAILURE — cannot read bull:{QUEUE_NAME}:failed\n"
            f"   error: {err}\n"
            f"   Redis container '{REDIS_CONTAINER}' may be down → writer failures are INVISIBLE. Escalate."
        )
        if should_emit(alert) and not dry_run:
            print(alert)
            send_telegram(alert)
            create_kanban_card(
                f"P2 DLQ monitor probe failure ({QUEUE_NAME})",
                f"Auto-raised by dlq_growth_monitor.py. Cannot read {DLQ_KEY}; "
                f"Redis/container may be down. Writer failures would be silent. "
                f"Source: t_b06b8ce8 (RCA t_9f23ec8f). Error: {err}",
                f"dlq-probe-fail-{QUEUE_NAME}")
        elif dry_run:
            print(alert)
        return

    window_new_count = len(window_new)

    saturated = size >= SATURATION_LEVEL
    growing = window_new_count >= GROWTH_THRESHOLD

    if not (saturated or growing):
        # healthy-ish: clear any stale alert state and stay silent.
        # Safe for GROWTH: the windowed signal is stateless (no last_size
        # baseline to preserve — see AC2 rework in the module docstring).
        clear_state()
        return

    # build failure-class sample from newest N
    ids = newest_job_ids(SAMPLE_N)
    buckets = {}
    table_buckets = {}
    sample_lines = []
    for jid in ids[-SAMPLE_N:]:
        reason = job_failed_reason(jid)
        table = job_table(jid)
        cls = classify(reason)
        buckets[cls] = buckets.get(cls, 0) + 1
        table_buckets[table] = table_buckets.get(table, 0) + 1
        ts = fail_time(jid)
        tstr = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts)) if ts else "?"
        sample_lines.append(f"   · {cls} | {table} | {tstr} | {jid[:32]}…")
    n_sampled = len(ids[-SAMPLE_N:])
    class_summary = ", ".join(f"{k}={v}" for k, v in sorted(buckets.items(), key=lambda x: -x[1]))
    table_summary = ", ".join(f"{k}={v}" for k, v in sorted(table_buckets.items(), key=lambda x: -x[1]))

    causes = []
    if saturated:
        causes.append(f"SATURATED at {size} (unbounded retention — no discard cap since t_19da81f1)")
    if growing:
        causes.append(f"+{window_new_count} new in last ~5m (windowed rate)")

    alert = (
        f"🔴 P2 DLQ GROWTH ALERT — bull:{QUEUE_NAME}:failed\n"
        f"   size={size}  ({'; '.join(causes)})\n"
        f"   failure-class mix (newest {n_sampled} sampled): {class_summary}\n"
        f"   table mix (newest {n_sampled} sampled): {table_summary}\n"
        f"   sample:\n" + "\n".join(sample_lines[:8]) + "\n"
        f"   RCA ref t_9f23ec8f — 326 dead writes sat silent ~13h. This alert closes that gap.\n"
        f"   Read-only monitor; queue NOT mutated. Triage the underlying writer failure."
    )

    if dry_run:
        print(alert)
        return

    if should_emit(alert):
        print(alert)
        send_telegram(alert)
        # Upsert ONE standing card (idempotency key = queue name).
        # The kanban service returns the existing task id when the key
        # matches a non-archived card, so repeated ticks update the
        # same standing card instead of creating a new one.
        standing_title = (
            f"P2 DLQ GROWTH: bull:{QUEUE_NAME}:failed "
            f"size={size} ({class_summary})"
        )
        standing_body = (
            f"Auto-raised by dlq_growth_monitor.py "
            f"(kanban t_b06b8ce8, RCA t_9f23ec8f).\n\n"
            f"DLQ size={size}. Causes: {'; '.join(causes)}.\n"
            f"Failure-class mix (newest {n_sampled} sampled): {class_summary}\n"
            f"Table mix (newest {n_sampled} sampled): {table_summary}\n\n"
            f"Sample failures:\n" + "\n".join(sample_lines[:8]) + "\n\n"
            f"PAPER-ONLY OBSERVABILITY — the monitor performs NO queue mutation. "
            f"Investigate the database-writes BullMQ worker / Supabase connectivity. "
            f"If {size}>=450 the DLQ is approaching operational manageability limits "
            f"(unbounded retention — no discard cap since t_19da81f1). "
            f"Consider draining old forensic entries to keep the buffer within a manageable size.\n\n"
            f"[STANDING CARD] This card is updated in-place on each alert tick. "
            f"Older per-fingerprint cards have been superseded."
        )
        upsert_standing_card(standing_title, standing_body)
    # else: same fingerprint already alerted within remind window → stay quiet


if __name__ == "__main__":
    main()
