#!/usr/bin/env python3
"""
route_models.py — usage-aware model router for the Hermes/Jarvis fleet.

Reads LIVE provider capacity (not config claims, not `hermes auth status`) and
picks the best working model for a task tier, honouring reservations.

Usage:
    route_models.py                 # capacity report for every seat
    route_models.py --tier frontier # print best model+provider for a tier
    route_models.py --json          # machine-readable full state
    route_models.py --probe         # add a real one-shot inference probe per seat
    route_models.py --card t_abc123 --board jarvis-os --tier bulk --apply
                                    # pin a kanban card to the chosen seat

Tiers: orchestrator | frontier | mid | bulk

Why this exists: on 2026-08-30 the jarvis gateway had a 3-rung fallback chain in
which all 3 rungs were dead (Nous depleted, local :8098 down, rung 3 == rung 1).
Every Discord message failed with a generic "use /reset" error. A router that
reads real remaining capacity prevents that class of failure.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

HERMES = "/home/frank/.hermes/hermes-agent"
PY = f"{HERMES}/venv/bin/python"
sys.path.insert(0, HERMES)

# --- Seat catalogue -------------------------------------------------------
# VERIFIED by live one-shot inference probe 2026-08-30. Do not add a seat here
# without probing it; the disk model cache is routinely stale (it was missing
# gpt-5.6-luna/sol, grok-4.6 and claude-opus-5 while all four worked).

@dataclass
class Seat:
    model: str
    provider: str
    tiers: tuple                 # tiers this seat may serve
    rank: int                    # lower = preferred within a tier
    reserved_for: Optional[str] = None   # tier that has exclusive claim
    # runtime-filled
    used_percent: Optional[float] = None
    headroom: Optional[float] = None
    reset_at: Optional[str] = None
    plan: Optional[str] = None
    status: str = "unknown"
    note: str = ""
    depleted: Optional[bool] = None
    stale_age_hours: Optional[float] = None


# Rank is PER TIER (lower = preferred). Frank's stated intent 2026-08-30:
#   orchestrator/PM -> opus-5 or gpt-5.6-sol   (fable-5 RESERVED for Jarvis)
#   mid             -> grok-4.6 or sonnet-5
#   bulk            -> gpt-5.6-luna
#
# Anthropic seats deliberately rank LOW on mid/bulk: fable-5, opus-5 and
# sonnet-5 all draw the SAME Anthropic weekly pool, so routing routine work
# there starves the orchestration seat Frank reserved. Spend Codex/xAI
# capacity first and keep the Anthropic window for work that needs it.
SEATS: list[Seat] = [
    # Frank's standing reservation: fable-5 is for Jarvis orchestration only.
    Seat("claude-fable-5",  "anthropic",    ("orchestrator",), 0, reserved_for="orchestrator"),
    Seat("claude-opus-5",   "anthropic",    ("orchestrator", "frontier"), 1),
    Seat("gpt-5.6-sol",     "openai-codex", ("orchestrator", "frontier"), 2),
    Seat("claude-sonnet-5", "anthropic",    ("frontier", "mid"), 3),
    Seat("grok-4.6",        "xai-oauth",    ("mid", "frontier", "orchestrator"), 3),
    Seat("grok-4.3",        "xai-oauth",    ("mid", "bulk"), 4),
    Seat("gpt-5.6-luna",    "openai-codex", ("bulk", "mid"), 5),
]

# Per-tier rank overrides, so one seat can be first choice for one tier and
# last resort for another without needing duplicate seat entries.
TIER_RANK: dict[tuple[str, str], int] = {
    ("mid", "grok-4.6"): 0,          # Frank: mid = grok 4.6 or sonnet 5
    ("mid", "claude-sonnet-5"): 1,
    ("mid", "grok-4.3"): 2,
    ("mid", "gpt-5.6-luna"): 3,
    ("bulk", "gpt-5.6-luna"): 0,     # Frank: lower tier = gpt-5.6-luna
    ("bulk", "grok-4.3"): 1,
    ("frontier", "claude-opus-5"): 0,
    ("frontier", "gpt-5.6-sol"): 1,
    ("frontier", "claude-sonnet-5"): 2,
    ("frontier", "grok-4.6"): 9,       # last resort only
    ("orchestrator", "grok-4.6"): 9,   # last resort only
}


def tier_rank(seat: dict, tier: str) -> int:
    return TIER_RANK.get((tier, seat["model"]), seat["rank"])

# Provider-level capacity is shared by all seats on that provider.
PROVIDERS = ["anthropic", "openai-codex", "xai-oauth", "nous"]

# Refuse to route to a provider with less than this % capacity left.
HEADROOM_FLOOR = 5.0

# ``build_credits_view`` deliberately degrades to an empty view when the portal
# is unauthenticated, unavailable, or returns an unparseable payload.  Those
# cases are not evidence of a healthy Nous account.  Keep this parser narrow:
# only the numeric balance fields rendered by account_usage.py count as proof.
_NOUS_BALANCE_LINE = re.compile(
    r"^\s*(?:Subscription credits|Top-up credits|Total usable):\s*"
    r"\$(-?(?:\d[\d,]*)(?:\.\d+)?)\s*$"
)


def _nous_balance_values(lines) -> list[float]:
    """Extract numeric values from known Nous balance display lines only."""
    if not isinstance(lines, (list, tuple)):
        return []
    values: list[float] = []
    for line in lines:
        if not isinstance(line, str):
            continue
        match = _NOUS_BALANCE_LINE.match(line)
        if match:
            try:
                values.append(float(match.group(1).replace(",", "")))
            except ValueError:
                continue
    return values


def classify_nous_capacity(view: object) -> dict:
    """Turn a CreditsView-shaped payload into fail-closed provider capacity.

    ``build_credits_view`` cannot distinguish auth, HTTP, and parse failures;
    all are represented by ``logged_in=False`` or an empty balance block.  Do
    not convert either form into ``ok/depleted:false``.  A healthy result needs
    both a logged-in account and at least one positive, recognized balance.
    """
    if not isinstance(view, dict):
        return {
            "status": "unavailable",
            "depleted": None,
            "lines": [],
            "note": "Nous capacity response was not a mapping",
        }

    raw_lines = view.get("lines", [])
    lines = (
        [line for line in raw_lines if isinstance(line, str)]
        if isinstance(raw_lines, (list, tuple)) else []
    )
    depleted = view.get("depleted")
    if depleted is True:
        return {
            "status": "depleted",
            "depleted": True,
            "lines": lines[:8],
            "note": "Nous credits exhausted",
        }
    if view.get("logged_in") is not True:
        return {
            "status": "unavailable",
            "depleted": None,
            "lines": lines[:8],
            "note": "Nous account unavailable or unauthenticated",
        }

    values = _nous_balance_values(lines)
    if not values:
        return {
            "status": "unknown",
            "depleted": None,
            "lines": lines[:8],
            "note": "no usable Nous balance evidence",
        }
    if not any(value > 0 for value in values):
        return {
            "status": "unavailable",
            "depleted": None,
            "lines": lines[:8],
            "note": "Nous balance evidence is non-positive",
        }

    return {
        "status": "ok",
        "depleted": False,
        "lines": lines[:8],
    }


def provider_capacity() -> dict:
    """Live usage per provider, straight from each vendor's usage API.

    IMPORTANT: Anthropic's own usage endpoint (/api/oauth/usage) rate-limits.
    Hermes' fetch_account_usage() swallows that 429 and returns None, which is
    INDISTINGUISHABLE from "this provider has no usage API". Routing on that
    would silently treat a 75%-consumed pool as unknown/healthy. So we cache the
    last good reading on disk and fall back to it, clearly marked as stale.
    """
    code = r'''
import sys, json
sys.path.insert(0, "/home/frank/.hermes/hermes-agent")
from agent import account_usage as au

out = {}
for prov in ["anthropic", "openai-codex", "xai-oauth"]:
    try:
        s = au.fetch_account_usage(prov)
        if s is None:
            out[prov] = {"status": "no_usage_api"}
            continue
        wins = [{"label": w.label,
                 "used_percent": w.used_percent,
                 "reset_at": str(w.reset_at)} for w in (s.windows or ())]
        out[prov] = {"status": "ok", "plan": s.plan,
                     "unavailable": s.unavailable_reason, "windows": wins}
    except Exception as e:
        out[prov] = {"status": "error", "error": f"{type(e).__name__}: {e}"[:200]}

# Nous bills by credit balance, not a usage window.
try:
    cv = au.build_credits_view()
    out["nous"] = {
        "logged_in": getattr(cv, "logged_in", False),
        "depleted": getattr(cv, "depleted", None),
        "lines": list(getattr(cv, "balance_lines", ()) or ()),
    }
except Exception as e:
    out["nous"] = {"error": f"{type(e).__name__}: {e}"[:200]}

print(json.dumps(out))
'''
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       timeout=180, cwd=HERMES)
    try:
        live = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        live = {"_error": (r.stdout + r.stderr)[-500:]}
    if isinstance(live, dict) and "nous" in live:
        live["nous"] = classify_nous_capacity(live["nous"])

    # --- stale-cache fallback for rate-limited usage endpoints --------------
    import os, time
    cache_path = "/home/frank/.hermes/cache/provider_capacity_cache.json"
    try:
        with open(cache_path) as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}

    now = time.time()
    for prov, data in list(live.items()):
        if prov.startswith("_"):
            continue
        if data.get("status") == "ok":
            cache[prov] = {"at": now, "data": data}
        elif data.get("status") == "no_usage_api":
            # Could be genuine, or a swallowed 429. If we have ever seen real
            # windows for this provider, the endpoint EXISTS — so a None now
            # means throttled, and the last good reading is the better guide.
            prev = cache.get(prov)
            if prev and (prev.get("data", {}).get("windows")):
                age_h = (now - prev["at"]) / 3600.0
                stale = dict(prev["data"])
                stale["status"] = "ok_stale"
                stale["stale_age_hours"] = round(age_h, 1)
                live[prov] = stale

    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as fh:
            json.dump(cache, fh)
    except Exception:
        pass

    return live


def probe_seat(seat: Seat, timeout: int = 120) -> tuple[bool, str]:
    """Real one-shot inference. The ONLY trustworthy liveness signal."""
    cmd = (f"cd /home/frank && timeout {timeout} env -u HERMES_DELEGATED_CHILD_CONTEXT "
           f"hermes -m {seat.model} --provider {seat.provider} -t '' "
           f"-z 'Reply with exactly: SEATOK'")
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True,
                           text=True, timeout=timeout + 20)
        out = (r.stdout or "").strip()
        if "SEATOK" in out:
            return True, ""
        return False, (out.replace("\n", " ")[:160] or "empty/timeout")
    except subprocess.TimeoutExpired:
        return False, "probe timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:160]


def worst_headroom(pdata: dict) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Tightest window governs: a 5%-used session with a 95%-used week is nearly out."""
    if pdata.get("status") not in {"ok", "ok_stale"}:
        return None, None, None
    wins = pdata.get("windows") or []
    if not wins:
        return None, None, pdata.get("plan")
    worst = max(wins, key=lambda w: (w.get("used_percent") or 0))
    used = worst.get("used_percent")
    return (100.0 - used if used is not None else None), worst.get("reset_at"), pdata.get("plan")


def build_state(do_probe: bool = False) -> dict:
    cap = provider_capacity()
    seats: list[Seat] = []

    for s in SEATS:
        seat = Seat(**{k: v for k, v in asdict(s).items()
                       if k in {"model", "provider", "tiers", "rank", "reserved_for"}})
        pdata = cap.get(seat.provider, {})

        if seat.provider == "nous":
            seat.depleted = pdata.get("depleted")
            if pdata.get("status") == "depleted":
                seat.status, seat.note = "depleted", "Nous credits exhausted"
            elif pdata.get("status") == "ok" and pdata.get("depleted") is False:
                seat.status = "available"
            else:
                seat.status = pdata.get("status", "unknown")
                seat.note = str(
                    pdata.get("note", "Nous capacity evidence unavailable")
                )[:120]
        else:
            head, reset, plan = worst_headroom(pdata)
            seat.headroom, seat.reset_at, seat.plan = head, reset, plan
            if pdata.get("status") in {"ok", "ok_stale"}:
                w = (pdata.get("windows") or [])
                if w:
                    seat.used_percent = max((x.get("used_percent") or 0) for x in w)
                if pdata.get("status") == "ok_stale":
                    seat.note = f"STALE {pdata.get('stale_age_hours')}h (usage API throttled)"
                    seat.stale_age_hours = pdata.get("stale_age_hours")
                if head is not None and head < HEADROOM_FLOOR:
                    seat.status = "exhausted"
                    seat.note = (seat.note + " | " if seat.note else "") + f"only {head:.0f}% headroom"
                else:
                    seat.status = "available"
            elif pdata.get("status") == "no_usage_api":
                seat.status, seat.note = "available", "no usage API — capacity unknown"
            else:
                seat.status, seat.note = "unknown", str(pdata.get("error", ""))[:120]

        if do_probe and seat.status in {"available", "unknown"}:
            ok, err = probe_seat(seat)
            if not ok:
                # 429 is transient throttling, NOT a dead seat.
                seat.status = "throttled" if ("429" in err or "rate" in err.lower()) else "failing"
                seat.note = (seat.note + " | " if seat.note else "") + f"probe: {err}"
            else:
                seat.note = (seat.note + " | " if seat.note else "") + "probe: SEATOK"

        seats.append(seat)

    return {"capacity": cap, "seats": [asdict(x) for x in seats]}


ROUTABLE = {"available", "unknown"}


def choose(state: dict, tier: str) -> Optional[dict]:
    """Best seat for a tier: honour reservations, prefer rank, then headroom."""
    cands = []
    for s in state["seats"]:
        if tier not in s["tiers"]:
            continue
        if s["status"] not in ROUTABLE:
            continue
        # Never route to Nous unless its status is explicitly healthy.  Unknown
        # is routable for legacy providers with no usage API, but not for a
        # balance-backed provider whose evidence is missing.
        if s["provider"] == "nous" and (
            s["status"] != "available" or s.get("depleted") is not False
        ):
            continue
        if s["reserved_for"] and s["reserved_for"] != tier:
            continue
        cands.append(s)
    if not cands:
        return None
    # rank first (Frank's stated preference order), headroom breaks ties
    cands.sort(key=lambda s: (tier_rank(s, tier),
                              -(s["headroom"] if s["headroom"] is not None else 50)))
    return cands[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Usage-aware model router")
    ap.add_argument("--tier", choices=["orchestrator", "frontier", "mid", "bulk"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--probe", action="store_true",
                    help="add a live inference probe per seat (slow, ~2min)")
    ap.add_argument("--card"), ap.add_argument("--board")
    ap.add_argument("--apply", action="store_true", help="actually pin the card")
    a = ap.parse_args()

    state = build_state(do_probe=a.probe)

    if a.json:
        print(json.dumps(state, indent=1))
        return 0

    if a.tier:
        pick = choose(state, a.tier)
        if not pick:
            print(f"NO SEAT AVAILABLE for tier '{a.tier}'", file=sys.stderr)
            return 2
        if a.card and a.board:
            cmd = (f"env -u HERMES_DELEGATED_CHILD_CONTEXT hermes kanban --board {a.board} "
                   f"set-model {a.card} {pick['model']} --provider {pick['provider']}")
            if a.apply:
                r = subprocess.run(["bash", "-c", f"cd /home/frank && {cmd}"],
                                   capture_output=True, text=True, timeout=120)
                print(r.stdout.strip() or r.stderr.strip())
            else:
                print(f"DRY-RUN: {cmd}")
            return 0
        print(f"{pick['model']} --provider {pick['provider']}")
        return 0

    # Default: human-readable capacity report
    print(f"{'SEAT':22} {'PROVIDER':14} {'STATUS':10} {'USED':>6} {'LEFT':>6}  NOTE")
    print("-" * 96)
    for s in state["seats"]:
        used = f"{s['used_percent']:.0f}%" if s["used_percent"] is not None else "-"
        left = f"{s['headroom']:.0f}%" if s["headroom"] is not None else "-"
        tag = " [RESERVED:" + s["reserved_for"] + "]" if s["reserved_for"] else ""
        print(f"{s['model']:22} {s['provider']:14} {s['status']:10} {used:>6} {left:>6}  {s['note']}{tag}")

    print("\nROUTING DECISION")
    for t in ["orchestrator", "frontier", "mid", "bulk"]:
        p = choose(state, t)
        print(f"  {t:13} -> " + (f"{p['model']} --provider {p['provider']}" if p else "!! NO SEAT AVAILABLE"))

    nous = state["capacity"].get("nous", {})
    if nous.get("status") != "ok":
        print(
            f"\n  WARNING: Nous capacity is {nous.get('status', 'unknown')} — "
            "do not route cards/profiles to Nous."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
