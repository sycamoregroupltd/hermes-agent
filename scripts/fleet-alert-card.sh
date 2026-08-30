#!/usr/bin/env bash
# Write a host-cron alert to the BOARD — the only channel Frank actually meets.
#
# WHY: the host crontab alerters bypass hermes cron delivery entirely and call
# `hermes send` themselves. Their targets default to whatsapp:Frank (NOT PAIRED —
# dead since 2026-07-26) or discord:#critical-alerts (Frank: "i dont read discord
# either"). Every alert they have raised since then was written to nobody.
#
# This is ADDITIVE: it is called alongside the existing send, never instead of it,
# so if a channel is ever repaired both paths work. It must never fail the caller —
# a monitor that dies because its card write failed is worse than a missed card.
# Everything below is therefore fail-open: the python block is `2>/dev/null || true`
# and the script always `exit 0`.
#
# Usage: fleet-alert-card.sh <key> <subject> <body>          # raise / refresh
#        fleet-alert-card.sh --resolve <key|family-*> <evidence>   # clear when healthy
# One card per key. Repeat alerts with the same key update rather than accumulate.
#
# RESOLVE PATH (t_cef408bd, 2026-08-29). Before this, the ONLY way a card ever closed
# was the same alert re-firing. A condition that FIXED ITSELF left its card open for
# ever — 11 "vault autocommit STALE" cards were still open after both vaults had
# committed cleanly. A monitor that files cards nobody closes trains people to ignore
# the board, which is how a 6.5-day stack degradation stayed invisible in the first
# place. So every caller should now call `--resolve <its key>` on its CLEAN path.
# `--resolve` accepts a family glob (`degraded-*`) for callers whose key varies with
# the problem class, so a clean run closes whichever card is open. It only ever closes
# a card this script recorded that is still `ready` (unassigned, or still owned only by
# the routing PM from t_89678308 — a claimed card leaves `ready`); anything else is
# left alone and LOGGED to $GUARD_LOG (never silently dropped), and the state pointer
# is kept so the next clean run retries.
#
# KEY CONTRACT (t_cef408bd, 2026-08-29): <key> must be STABLE for the same class of
# problem. Derive it from problem IDENTITY only — which thing is broken, which named
# sub-check failed. NEVER hash free text that contains counters, timestamps,
# durations, byte counts or percentages: the key then changes every run, the
# supersede below can never fire, and every run mints a fresh card. That is exactly
# how stack-health-audit.sh produced 157 duplicate cards on jarvis-os.
# Volatile detail belongs in <body>, which is rebuilt on every re-fire.
#
# THREE HARDENINGS ADDED 2026-08-29 (t_cef408bd, fable-devops):
#  1. CREATE BEFORE SUPERSEDE. `--idempotency-key` returns the id of an existing
#     NON-ARCHIVED task, done or not. The old order (complete prev, then create)
#     meant the second run inside the same hour completed the live card and then got
#     that same completed card back from the idempotent create — leaving the
#     condition with NO live card until the next hour bucket. We now create first and
#     only supersede when the create actually returned a DIFFERENT card.
#  2. LOCKED, RE-READ, ATOMIC state writes. The state file is a read-modify-write
#     shared by 12 concurrent host monitors, and the old window spanned two
#     subprocess calls (up to 180s), so a slow writer clobbered every key another
#     monitor had written in the meantime. Proven case: state[stale_obsidian-fleet-vault]
#     still pointed at t_e56c3ea5 — a card superseded at 2026-08-28T19:30:02Z — so the
#     replacement card was never recorded and 10 vault-autocommit cards piled up
#     despite that caller using a perfectly stable key. We now take an flock, RE-READ
#     the state after the slow hermes calls, mutate only our own key, and os.replace().
#  3. PRODUCER-SIDE FAMILY CAP. Even if a future caller passes a bad key, at most
#     FLEET_ALERT_FAMILY_MAX (default 3) live cards can accumulate per key FAMILY
#     (the key with a trailing hex hash normalised away). The cap runs AFTER the
#     create, so a real new alert is never swallowed, and it only completes cards
#     this script itself recorded that are still unassigned-and-`ready` or still owned
#     only by the routing PM in `ready` (a card a worker claimed leaves `ready`).
set -uo pipefail
MODE="alert"
if [ "${1:-}" = "--resolve" ]; then MODE="resolve"; shift; fi
KEY="${1:-unknown}"; SUBJECT="${2:-alert}"; BODY="${3:-}"
BOARD="${FLEET_ALERT_BOARD:-jarvis-os}"
STATE="${FLEET_ALERT_STATE:-/home/frank/.hermes/state/host-cron-alert-cards.json}"
FAMILY_MAX="${FLEET_ALERT_FAMILY_MAX:-3}"
GUARD_LOG="${FLEET_ALERT_GUARD_LOG:-/home/frank/.hermes/logs/fleet-alert-card-guard.log}"
mkdir -p "$(dirname "$STATE")" "$(dirname "$GUARD_LOG")" 2>/dev/null || true

python3 - "$KEY" "$SUBJECT" "$BODY" "$BOARD" "$STATE" "$FAMILY_MAX" "$GUARD_LOG" "$MODE" <<'PY' 2>/dev/null || true
import fcntl, json, os, re, subprocess, sys, tempfile, time

key, subject, body, board, state_path, family_max_s, guard_log, mode = sys.argv[1:9]
try:
    family_max = max(1, int(family_max_s))
except Exception:
    family_max = 3
lock_path = state_path + ".lock"
stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def glog(msg):
    """Visible trail for every guard decision. A guard that acts silently is a new
    silent-failure mode, which is the class of bug this whole script exists to fix."""
    try:
        with open(guard_log, "a") as f:
            f.write("[%s] key=%s %s\n" % (stamp, key, msg))
    except Exception:
        pass


class StateLock:
    """Bounded exclusive flock. On timeout we do NOT proceed with any state WRITE
    (t_5240b5f2 fail-clean: never clobber the shared file). `held` tells the caller
    whether the lock was actually acquired; write paths check it and skip+log rather
    than mutate unlocked. The alert card itself is still created (that happens
    outside the lock), so a contended lock can never lose an alert — it only defers
    the state pointer to the next clean run."""

    def __enter__(self):
        self.held = False
        self.fh = None
        try:
            self.fh = open(lock_path, "a+")
            deadline = time.time() + 20
            while True:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.held = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        glog("LOCK-TIMEOUT after 20s — proceeding unlocked")
                        break
                    time.sleep(0.25)
        except Exception as e:
            glog("LOCK-ERROR %s — proceeding unlocked" % e)
        return self

    def __exit__(self, *a):
        try:
            if self.held:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            if self.fh:
                self.fh.close()
        except Exception:
            pass
        return False


def load_state():
    try:
        with open(state_path) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def save_state(state):
    d = os.path.dirname(state_path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".host-cron-alert-cards.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, state_path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def hermes(*a):
    try:
        p = subprocess.run(["hermes", *a], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=90)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception:
        return 1, ""


def assignee_for(brd):
    """Map a board to the PM who should triage its host-alert cards.

    Host-alert cards default to the jarvis-os lane. Before t_89678308 they were
    created WITHOUT --assignee, so they landed unassigned and invisible to
    dispatch — the ghost-card class swept in the 2026-08-29 refactor. Every
    alert must now land OWNED by the board PM so it is triaged and dispatched.
    Unknown boards fall back to the jarvis-os PM (the default lane).
    """
    return {
        "jarvis-os": "jarvis-os-pm",
        "sycode-trading": "sycode-trading-pm",
        "upero": "upero-pm",
        "yorkstone-supplies": "yorkstone-supplies-pm",
        "ecohome": "ecohome-pm",
    }.get(brd, "jarvis-os-pm")


def family_of(k):
    """Collapse a trailing hex hash so a rotating-key producer maps to ONE family.
    'degraded-91eb38f0c7b2' -> 'degraded-*';  'stale_obsidian-fleet-vault' -> itself."""
    f = re.sub(r"([-_])[0-9a-f]{8,}$", r"\1*", k)
    if f == k and re.fullmatch(r"[0-9a-f]{8,}", k or ""):
        f = "*"
    return f


def card_status(card_id, brd=None):
    """(status, assignee) for a card, or None if it cannot be read.
    None means UNKNOWN — callers must treat it as 'do not touch', never as 'gone'."""
    rc, out = hermes("kanban", "--board", brd or board, "show", card_id)
    if rc != 0 or not out:
        return None
    st = re.search(r"^\s*status:\s*(\S+)", out, re.M)
    if not st:
        return None
    asg = re.search(r"^\s*assignee:\s*(\S*)", out, re.M)
    return (st.group(1), (asg.group(1) if asg else ""))


def is_reapable(card_id, brd=None):
    """Only ever auto-complete a card that is still an untouched alert card:
    status ready and either unassigned OR still owned only by the board PM this
    script routes to. A card a worker has claimed leaves `ready` (goes running),
    so a ready card owned solely by the routing PM is still safe to auto-close.
    Never stomp a card a worker has picked up."""
    s = card_status(card_id, brd)
    if not s or s[0] != "ready":
        return False
    return s[1] in ("-", "") or s[1] == assignee_for(brd or board)


# ---- RESOLVE MODE: the condition is healthy, close its card with evidence ----
if mode == "resolve":
    evidence = subject
    with StateLock():
        state = load_state()
        if key.endswith("*"):
            pref = key[:-1]
            targets = [k for k in state
                       if not k.startswith("__") and isinstance(state.get(k), dict)
                       and (k.startswith(pref) or family_of(k) == key)]
        else:
            targets = [key] if isinstance(state.get(key), dict) else []
        entries = [(k, state[k]) for k in targets if state[k].get("card_id")]
    cleared = []
    for k, ent in entries:
        cid = ent.get("card_id")
        brd = ent.get("board", board)
        st = card_status(cid, brd)
        if st is None:
            glog("RESOLVE-UNKNOWN target=%s card=%s — status unreadable, pointer KEPT" % (k, cid))
            continue
        if st[0] == "done":
            cleared.append(k)
            glog("RESOLVE-ALREADY-DONE target=%s card=%s — pointer cleared" % (k, cid))
            continue
        if st[0] != "ready" or (st[1] not in ("-", "") and st[1] != assignee_for(brd)):
            glog("RESOLVE-SKIPPED target=%s card=%s status=%s assignee=%s — a worker owns it, "
                 "pointer KEPT" % (k, cid, st[0], st[1]))
            continue
        rc2, out2 = hermes("kanban", "--board", brd, "complete", cid, "--summary",
                           ("RESOLVED %s: %s" % (stamp, evidence))[:1500])
        if rc2 == 0:
            cleared.append(k)
            glog("RESOLVE-COMPLETE target=%s card=%s" % (k, cid))
        else:
            glog("RESOLVE-FAILED target=%s card=%s rc=%s %s — pointer KEPT, next clean run retries"
                 % (k, cid, rc2, (out2 or "").strip().replace("\n", " ")[:200]))
    if cleared:
        with StateLock() as lock:
            if not lock.held:
                glog("RESOLVE-WRITE-SKIPPED lock contended — state left untouched, "
                     "next clean run retries")
            else:
                state = load_state()               # RE-READ, same discipline as the alert path
                fams = state.get("__families__", {})
                for k in cleared:
                    state.pop(k, None)
                    for fam in list(fams):
                        fams[fam] = [e for e in fams[fam] if e.get("key") != k]
                        if not fams[fam]:
                            del fams[fam]
                try:
                    save_state(state)
                except Exception as e:
                    glog("STATE-WRITE-FAILED(resolve) %s" % e)
    raise SystemExit(0)



# ---- read the previous card for this key (short critical section) -------------
with StateLock():
    prev = (load_state().get(key) or {})

# ---- CREATE FIRST (see hardening 1 in the header) -----------------------------
rc, out = hermes(
    "kanban", "--board", board, "create", f"[host-alert] {subject}"[:120],
    "--assignee", assignee_for(board),
    "--body",
    f"{body[:5000]}\n\n---\nRaised {stamp} by host-crontab monitor key='{key}'.\n"
    f"Delivered to the BOARD because the script's own target "
    f"(whatsapp/discord) is a channel Frank does not read.\n"
    f"The voice line reads this card on every call.",
    "--idempotency-key", f"hostalert-{key}-{int(time.time()//3600)}")
m = re.search(r"\b(t_[0-9a-f]{8})\b", out)
new_id = m.group(1) if (rc == 0 and m) else None

if new_id is None:
    glog("CREATE-FAILED rc=%s — state left untouched, caller unaffected" % rc)
    raise SystemExit(0)

# ---- supersede the previous card for this key, only if it is a DIFFERENT card --
prev_id = prev.get("card_id")
if prev_id and prev_id != new_id:
    hermes("kanban", "--board", prev.get("board", board), "complete", prev_id,
           "--summary", f"Superseded {stamp} by a newer '{key}' alert ({new_id}).")

# ---- record + producer-side family cap (short critical section) ---------------
family = family_of(key)
excess = []
with StateLock() as lock:
    if not lock.held:
        glog("ALERT-WRITE-SKIPPED lock contended key=%s card=%s — state left untouched, "
             "card already created, next run reconciles the pointer" % (key, new_id))
    else:
        state = load_state()                      # RE-READ: never write back a pre-I/O snapshot
        state[key] = {"card_id": new_id, "board": board, "at": stamp}
        fams = state.setdefault("__families__", {})
        entries = [e for e in fams.get(family, []) if e.get("card_id") != new_id]
        entries.append({"card_id": new_id, "key": key, "at": stamp})
        if len(entries) > family_max:
            excess = entries[:-family_max]
            entries = entries[-family_max:]
        fams[family] = entries
        try:
            save_state(state)
        except Exception as e:
            glog("STATE-WRITE-FAILED %s" % e)
            excess = []                            # do not reap on the back of an unsaved cap

for e in excess:
    cid = e.get("card_id")
    if not cid or not is_reapable(cid):
        glog("CAP-SKIPPED card=%s family=%s (not an untouched ready card)" % (cid, family))
        continue
    crc, _ = hermes("kanban", "--board", board, "complete", cid, "--summary",
                    f"Auto-capped {stamp}: more than {family_max} live cards in alert "
                    f"family '{family}'. Superseded by {new_id}. The producer's alert key "
                    f"is not stable for one problem class — see t_cef408bd.")
    glog("CAP-COMPLETE card=%s family=%s rc=%s newest=%s" % (cid, family, crc, new_id))
PY
exit 0
