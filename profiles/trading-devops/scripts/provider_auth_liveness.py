#!/usr/bin/env python3
"""No-agent provider auth-liveness rail (t_0f9d94f9).

WHY THIS EXISTS
   All 3 provider auth rails (nous / openai-codex / xai-oauth) broke
   simultaneously; detection died WITH the outage because cron-health monitors are
   agent-tier (they run inside a gateway that itself needs a working provider).
   Re-auth is seat ritual. This is a DETERMINISTIC no-agent probe that reads auth
   state on disk (never runs an LLM, never calls a provider) and pages on the
   exact failure signatures that take the fleet down.

WHAT IT CHECKS (per provider)
   * token presence   - a usable credential exists (shared store / root / pool)
   * token validity   - JWT not expired / not relogin_required / not invalid_grant
   * 429 probe        - rate-limit / exhaustion markers (last_status, 429 codes)
   * xai refresh dry-run - refresh_token present AND no terminal refresh failure
                           (invalid_grant / xai_refresh_failed). Presence alone is
                           NOT validity: we decode the access-token JWT `exp` and
                           require at least one unexpired usable token.

   Liveness semantics (deterministic, self-healing):
     - nous    : shared store has a valid (unexpired) access_token, OR a usable
                 root/profile pool entry with an unexpired token. Sticky
                 last_auth_error alone does NOT trip (it is never cleared on
                 success and would false-positive forever).
     - codex   : at least one pool entry is present and not in an active
                 exhausted/429 state (or its reset has passed).
     - xai-oauth: at least one unexpired access token exists (provider-state
                 `providers.xai-oauth.tokens.access_token` OR any pool entry JWT).
                 A DEGRADED (non-fatal) state is emitted when a valid token exists
                 but refresh is failing / most copies are expired, so the fleet
                 sees the risk without a false-positive frank-gate card.

ON FAILURE (any provider with NO usable token)
   ONE frank_gate card (idempotency-keyed `auth-liveness`) on jarvis-os with the
   exact re-auth command(s) in the body, marked blocked/needs_input so it lands on
   Frank's decision surface, + spool a critical alert to #critical-alerts (via
   spool_alert_write.py -> jarvis alertmanager drain) + flip the selective-dispatch
   state flag (codex-selective-dispatch.json) as backpressure so fleet-dispatch
   gates on the broken provider. exit 1, stdout = the alert.

ON RECOVERY (all providers have a usable token)
   Auto-clear: complete the frank_gate card if open, reset the selective flag to
   normal (only if we own it), spool nothing. Empty stdout + exit 0 (silent, per
   --no-agent contract).

NEVER prints token values. Reads auth files only for metadata/JWT-exp.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = os.environ.get("AUTH_RAIL_HERMES", "/home/frank/.local/bin/hermes")
BOARD = os.environ.get("AUTH_RAIL_BOARD", "jarvis-os")
# A fresh idempotency key per 6h window so a recovered+re-broken episode opens a
# NEW card rather than re-pointing at the just-completed one (Hermes idempotency
# dedupes against non-archived tasks; a completed card would block re-use).
REALERT_SECONDS = 6 * 3600
def _idem_key() -> str:
    return f"auth-liveness-{int(time.time() // REALERT_SECONDS)}"

STATE_FILE = Path(os.environ.get(
    "AUTH_RAIL_STATE", "/home/frank/.hermes/state/provider-auth-liveness.json"))
ROOT_AUTH = Path(os.environ.get(
    "AUTH_RAIL_ROOT_AUTH", "/home/frank/.hermes/auth.json"))
SHARED_NOUS = Path(os.environ.get(
    "AUTH_RAIL_SHARED_NOUS", "/home/frank/.hermes/shared/nous_auth.json"))
PROFILES = Path(os.environ.get(
    "AUTH_RAIL_PROFILES", "/home/frank/.hermes/profiles"))
SELECTIVE_STATE = Path(os.environ.get(
    "CODEX_SELECTIVE_STATE", "/home/frank/.hermes/state/codex-selective-dispatch.json"))
SELECTIVE_ALLOWLIST = Path(os.environ.get(
    "CODEX_SELECTIVE_ALLOWLIST",
    "/home/frank/.hermes/state/codex-selective-dispatch-allowlist.json"))
SPOOL_WRITER = Path("/home/frank/.hermes/scripts/spool_alert_write.py")
SPOOL_DIR = Path("/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/incoming")

ALERTNAME = "ProviderAuthLiveness"

# Re-auth commands per provider (exact, from fleet runbooks + xai/nous fixes).
REAUTH_CMD = {
    "nous": "hermes model   # device-code relogin for Nous; then smoke: "
            "hermes -p jarvis chat -q 'Return exactly OK.' --toolsets \"\"",
    "openai-codex": "hermes auth add openai-codex   # or re-auth via `hermes model`; "
                    "codex exhaustion/429 clears on a valid credential",
    "xai-oauth": "hermes model   # interactive xAI OAuth re-auth (writes fresh "
                 "access+refresh); verify hermes auth status xai-oauth -> logged in",
}

PROVIDERS = ["nous", "openai-codex", "xai-oauth"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return time.time()


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _parse_ts(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _pool_entries(auth: dict, provider: str) -> list:
    pool = auth.get("credential_pool") or {}
    items = pool.get(provider)
    return items if isinstance(items, list) else []


def _provider_meta(auth: dict, provider: str) -> dict:
    provs = auth.get("providers") or {}
    meta = provs.get(provider)
    return meta if isinstance(meta, dict) else {}


def _jwt_exp(token) -> float | None:
    """Decode JWT `exp` from an access token. Returns epoch or None if unparseable."""
    if not isinstance(token, str) or "." not in token:
        return None
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        b = parts[1]
        b += "=" * (-len(b) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b.encode("ascii")).decode("utf-8"))
        exp = payload.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def _token_unexpired(token, now: float | None = None) -> bool:
    now = now_ts() if now is None else now
    exp = _jwt_exp(token)
    return exp is not None and exp > now


def check_nous() -> tuple[bool, str]:
    """Nous: shared store unexpired token (primary) OR a usable pool entry."""
    reasons = []
    shared_ok = False
    if SHARED_NOUS.exists():
        d = _load(SHARED_NOUS)
        tok = d.get("access_token")
        if tok and _token_unexpired(tok):
            shared_ok = True
        elif tok:
            reasons.append("shared-jwt-expired")
        else:
            reasons.append("shared-no-access-token")
    else:
        reasons.append("shared-missing")

    root_ok = False
    root = _load(ROOT_AUTH)
    for e in _pool_entries(root, "nous"):
        if not isinstance(e, dict):
            continue
        tok = e.get("access_token")
        if tok and _token_unexpired(tok):
            root_ok = True
            break
    err = _provider_meta(root, "nous").get("last_auth_error") or {}
    if err.get("relogin_required") or err.get("code") in ("invalid_grant", "nous_relogin"):
        reasons.append(f"relogin_required={err.get('code')}")

    ok = shared_ok or root_ok
    if not ok and not reasons:
        reasons.append("no-credential")
    return ok, "; ".join(reasons) or "ok"


def check_codex() -> tuple[bool, str]:
    """openai-codex: pool presence + not exhausted/rate-limited (429 class)."""
    reasons = []
    any_entry = False
    saw_ok = False
    now = now_ts()
    for auth_path in [ROOT_AUTH, *sorted(PROFILES.glob("*/auth.json"))]:
        auth = _load(auth_path)
        for e in _pool_entries(auth, "openai-codex"):
            if not isinstance(e, dict):
                continue
            any_entry = True
            st = str(e.get("last_status") or "").strip().lower()
            reset = _parse_ts(e.get("last_error_reset_at"))
            if st in ("exhausted", "rate_limited", "429", "usage_limit_reached"):
                if reset is not None and reset > now:
                    reasons.append(f"exhausted(reset={reset})")
                else:
                    saw_ok = True
            elif st in ("", "ok", "unknown-ok"):
                saw_ok = True
            elif st in ("dead", "unauthorized"):
                reasons.append(st)
    if not any_entry:
        return False, "no-credential"
    if reasons and not saw_ok:
        return False, "; ".join(reasons)
    return True, "ok"


def check_xai() -> tuple[bool, str, str]:
    """xai-oauth: at least one unexpired access token (provider-state OR pool JWT).

    Returns (healthy, reason, degraded_note). Degraded = a valid token exists but
    refresh is failing / most copies are expired (informational, non-fatal).
    """
    any_token = False
    unexpired = False
    degraded = []
    reasons = []
    now = now_ts()
    for auth_path in [ROOT_AUTH, *sorted(PROFILES.glob("*/auth.json"))]:
        auth = _load(auth_path)
        # provider-state tokens (authoritative)
        meta = _provider_meta(auth, "xai-oauth")
        toks = meta.get("tokens") or {}
        at = toks.get("access_token")
        if at:
            any_token = True
            if _token_unexpired(at, now):
                unexpired = True
        # pool entries
        for e in _pool_entries(auth, "xai-oauth"):
            if not isinstance(e, dict):
                continue
            tok = e.get("access_token")
            if tok:
                any_token = True
                if _token_unexpired(tok, now):
                    unexpired = True
            if not e.get("refresh_token"):
                reasons.append("no-refresh-token")
        err = meta.get("last_auth_error") or {}
        code = err.get("code") or ""
        if code in ("xai_refresh_failed", "invalid_grant") or err.get("relogin_required"):
            errat = _parse_ts(err.get("at"))
            if errat is not None and (now - errat) < REALERT_SECONDS:
                degraded.append(f"refresh-failed={code}")
    if not any_token:
        return False, "no-credential", ""
    if not unexpired:
        return False, "no-unexpired-access-token", "; ".join(degraded)
    # healthy but degraded
    note = "; ".join(sorted(set(degraded))) if degraded else ""
    return True, "ok", note


def run(cmd: list, timeout: int = 90) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def atomic_write(path: Path, payload: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def selective_backpressure(active: bool, why: str) -> None:
    """Flip the codex-selective-dispatch state flag (read by fleet-dispatch.sh).

    Only writes when the file is ours (source_task t_0f9d94f9) or currently inert,
    to avoid clobbering an active codex_exhaustion_circuit_breaker trip. On recovery
    we reset to normal ONLY if we own the file.
    """
    now = time.time()
    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")

    def owns(p: Path) -> bool:
        d = _load(p)
        return bool(d.get("source_task") == "t_0f9d94f9") or not d.get("tripped")

    if active:
        state = {
            "mode": "selective",
            "reason": why,
            "selective_active": True,
            "source_task": "t_0f9d94f9",
            "tripped": True,
            "updated_at": iso(now),
        }
        allow = {
            "mode": "selective",
            "reason": why,
            "provider": "nous",
            "profiles": [],
            "excluded_profiles": {},
            "expires_at": iso(now + 600),
            "generated_at": iso(now),
            "source_task": "t_0f9d94f9",
        }
        atomic_write(SELECTIVE_STATE, state)
        atomic_write(SELECTIVE_ALLOWLIST, allow)
    else:
        for p in (SELECTIVE_STATE, SELECTIVE_ALLOWLIST):
            if p.exists() and owns(p):
                try:
                    d = _load(p)
                    d["mode"] = "normal"
                    d["tripped"] = False
                    d["selective_active"] = False
                    d["reason"] = "provider-auth-recovered"
                    d["updated_at"] = iso(now)
                    atomic_write(p, d)
                except Exception:  # noqa: BLE001
                    pass


def spool_critical(subject: str, body: str) -> None:
    if not SPOOL_WRITER.exists():
        return
    run([sys.executable, str(SPOOL_WRITER), "--spool", str(SPOOL_DIR),
         "--alertname", ALERTNAME, "--severity", "critical",
         "--summary", f"{subject}\n{body[:1200]}"])


def _card_is_open(card_id: str) -> bool:
    rc, out = run([HERMES, "kanban", "--board", BOARD, "show", card_id])
    if rc != 0:
        return False
    # A card is open if it is not completed / archived.
    closed = re.search(r"\bstatus:\s*(done|completed|archived)\b", out, re.IGNORECASE)
    return closed is None


def frank_gate_card(report: str) -> str | None:
    """Ensure exactly ONE frank_gate card exists (idempotency-keyed). Returns card id."""
    st = _load(STATE_FILE)
    card_id = st.get("card_id")
    if card_id:
        # only reuse if the stored card is actually still OPEN
        if _card_is_open(card_id):
            return card_id
        # else fall through to create a fresh card for this new episode
        card_id = None
    title = "FRANK-GATE (AUTH): provider auth rail broken — re-auth required"
    body = (
        f"P0 no-agent auth-liveness probe (t_0f9d94f9) fired {utcnow()}.\n\n"
        f"{report}\n\n"
        "RE-AUTH COMMAND(S):\n"
        + "\n".join(f"- {REAUTH_CMD[p]}" for p in PROVIDERS)
        + "\n\nThis card auto-closes when the probe reports all providers healthy."
        " One card per episode."
    )
    rc, out = run([HERMES, "kanban", "--board", BOARD, "create", title,
                   "--body", body, "--idempotency-key", _idem_key(),
                   "--initial-status", "blocked", "--created-by", "trading-devops-auth-rail"])
    m = re.search(r"\b(t_[0-9a-f]{8})\b", out)
    if rc == 0 and m:
        cid = m.group(1)
        atomic_write(STATE_FILE, {"card_id": cid, "at": utcnow()})
        return cid
    return card_id


def clear_frank_gate() -> None:
    st = _load(STATE_FILE)
    card_id = st.get("card_id")
    if not card_id:
        return
    if _card_is_open(card_id):
        run([HERMES, "kanban", "--board", BOARD, "complete", card_id,
             "--summary", f"Auto-closed {utcnow()}: provider auth rail recovered (t_0f9d94f9)."])
    if STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="read-only: print per-provider status, no card/alert/flag changes")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON summary (check mode)")
    args = ap.parse_args()

    n_ok, n_note = check_nous()
    c_ok, c_note = check_codex()
    x_ok, x_note, x_degraded = check_xai()

    checks = [
        ("nous", n_ok, n_note, ""),
        ("openai-codex", c_ok, c_note, ""),
        ("xai-oauth", x_ok, x_note, x_degraded),
    ]

    lines = []
    broken = []
    for p, ok, note, degraded in checks:
        tag = "OK  " if ok else "BROKEN"
        if ok and degraded:
            tag = "DEGRADED"
        lines.append(f"{tag} {p}: {note}" + (f" [{degraded}]" if degraded else ""))
        if not ok:
            broken.append(p)

    report = "\n".join(lines)

    if args.check:
        print(report)
        if args.json:
            print(json.dumps({
                "healthy": not broken,
                "broken": broken,
                "per_provider": {p: (ok, note) for p, ok, note, _ in checks},
            }, indent=2))
        print("check_only=True (no card/alert/flag written)")
        return 0 if not broken else 1

    if not broken:
        # recovery path: auto-clear
        clear_frank_gate()
        selective_backpressure(active=False, why="provider-auth-recovered")
        # Observable degradation (stderr, not delivered per no-agent contract):
        for p, ok, note, degraded in checks:
            if ok and degraded:
                print(f"DEGRADED {p}: {note} [{degraded}]", file=sys.stderr)
        return 0  # silent

    # failure path
    alert = (f"PROVIDER AUTH RAIL BROKEN: {', '.join(broken)}\n" + report
             + "\n\nRE-AUTH:\n" + "\n".join(f"  {p}: {REAUTH_CMD[p]}" for p in broken))
    card_id = frank_gate_card(report)
    if card_id:
        alert += f"\nFrank-gate card: {card_id} on {BOARD}"
    st = _load(STATE_FILE)
    last = _parse_ts(st.get("last_alert_at"))
    if last is None or (now_ts() - last) > REALERT_SECONDS:
        spool_critical("Provider auth rail broken", alert)
        st["last_alert_at"] = utcnow()
        atomic_write(STATE_FILE, st)
    selective_backpressure(active=True, why=f"provider-auth: {', '.join(broken)}")
    print(alert)
    return 1


if __name__ == "__main__":
    sys.exit(main())
