#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""Data Stream Integrity Sentinel — news cache freshness + Hyperliquid socket heartbeat.

Part of the Option-B PEM design (jarvis-os t_1bfb62fc): extends the shared
``~/.hermes/var/pem.json`` ledger with two probes that catch the
*data-stopped-but-process-alive* failure modes the quota prober (pem_probe.py)
cannot see:

  * news_sentiment_cache  — freshness of news_sentiment_catalyst.py's on-disk cache
  * hyperliquid_socket    — liveness of the live Hyperliquid allMids websocket stream

Both follow pem_probe.py's failure-safe contract:
  - a probe NEVER raises into the caller;
  - network / filesystem errors degrade to ok=False + explicit ``error`` +
    status "unknown", and the shared ledger is still written with the healthy
    sources, so active data paths are never affected;
  - every external dependency (clock, file loader, ws connector) is injectable,
    so the detection logic is unit-tested with mocks — no live network in tests.

Ledger integration: ``pem_probe.py`` imports ``probe_news_cache`` and
``probe_hyperliquid_socket`` and registers them in PROBERS. ``build_ledger()``
already aggregates arbitrary sources, so the two new keys land in ``sources{}``
with no ledger-schema change beyond adding entries.

Usage (standalone, merges into the shared ledger without clobbering other sources):
  python3 pem_sentinel.py --source news_sentiment_cache
  python3 pem_sentinel.py --source hyperliquid_socket
  python3 pem_sentinel.py --dry-run
  python3 pem_sentinel.py --ledger /tmp/pem.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

# --- Defaults ---------------------------------------------------------------
DEFAULT_NEWS_CACHE_DIR = Path.home() / ".hermes" / "data" / "news_sentiment"
DEFAULT_NEWS_STATE_FILE = DEFAULT_NEWS_CACHE_DIR / "state.json"

# news_sentiment_catalyst.py runs via cron every 60m; a 1.5x budget catches
# a silently-dead cron without flapping on a single slow run.
NEWS_CACHE_STALE_MINUTES = 90.0
NEWS_CACHE_PROBE_TYPE = "cache_freshness"

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
HL_SOCKET_FRAME_TIMEOUT = 15.0  # seconds to wait for an active frame
HL_SOCKET_PROBE_TYPE = "socket_heartbeat"
HL_SUBSCRIPTION = {"method": "subscribe", "subscription": {"type": "allMids"}}

DEFAULT_LEDGER_PATH = Path.home() / ".hermes" / "var" / "pem.json"


# ---------------------------------------------------------------------------
# Time helpers (shared shape with pem_probe.py)
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """UTC timestamp, e.g. 2026-07-11T01:43:00Z."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> Optional[float]:
    """Best-effort coercion of an ISO string or epoch number to epoch seconds.

    Returns None when the value cannot be interpreted.
    """
    if value is None:
        return None
    # Numeric epoch (seconds or milliseconds)
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # milliseconds
            v /= 1000.0
        return v
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Pure number-as-string
    try:
        f = float(s)
        if f > 1e12:
            f /= 1000.0
        return f
    except ValueError:
        pass
    # ISO 8601 (+/- offset). Normalise the trailing Z to +00:00.
    try:
        norm = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = _dt.datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _iso_from_epoch(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------
# Probe 1 — news_sentiment cache freshness
# ---------------------------------------------------------------------------
def probe_news_cache(
    cache_dir: Path = DEFAULT_NEWS_CACHE_DIR,
    state_file: Path = DEFAULT_NEWS_STATE_FILE,
    now: Optional[float] = None,
    state_loader: Optional[Callable[[Path], Optional[dict]]] = None,
    mtime_loader: Optional[Callable[[Path], Optional[float]]] = None,
    stale_minutes: float = NEWS_CACHE_STALE_MINUTES,
) -> dict[str, Any]:
    """Probe the freshness of news_sentiment_catalyst.py's on-disk cache.

    Two independent freshness signals are evaluated and the NEWEST wins (most
    recent successful write):
      - A) the ``last_run`` / ``generated_at`` field inside state.json
      - B) the filesystem mtime of state.json

    A cache is ``stale`` when the freshest signal is older than ``stale_minutes``.
    The probe is ok=False only when it cannot read the cache at all (missing /
    unreadable / corrupt) — a stale-but-readable cache is still a successful probe
    with status="stale".

    Injectables (for tests):
      - now:            current epoch seconds
      - state_loader:   callable(Path) -> dict | None  (parses state.json)
      - mtime_loader:   callable(Path) -> float | None (os.path.getmtime)
    """
    now = now if now is not None else time.time()
    base: dict[str, Any] = {
        "source": "news_sentiment_cache",
        "probe_type": NEWS_CACHE_PROBE_TYPE,
        "ok": False,
        "error": None,
        "cache_dir": str(cache_dir),
        "state_file": str(state_file),
        "last_run_field": None,
        "state_file_mtime": None,
        "freshest_age_s": None,
        "stale_minutes": stale_minutes,
        "status": "unknown",
    }

    def _default_loader(p: Path) -> Optional[dict]:
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return None

    def _default_mtime(p: Path) -> Optional[float]:
        try:
            return os.path.getmtime(p)
        except OSError:
            return None

    load = state_loader or _default_loader
    mtime = mtime_loader or _default_mtime

    # Signal A — field inside state.json
    last_run_ts: Optional[float] = None
    try:
        st = load(state_file)
        if isinstance(st, dict):
            raw = st.get("last_run") or st.get("generated_at") or st.get("last_run_iso")
            last_run_ts = _parse_ts(raw)
    except Exception as e:  # pragma: no cover - defensive
        base["error"] = f"state read: {e}"

    # Signal B — filesystem mtime
    mtime_ts = mtime(state_file)

    candidates = [t for t in (last_run_ts, mtime_ts) if t is not None]
    if not candidates:
        base["error"] = base["error"] or "no cache file / unreadable state"
        base["status"] = "unknown"
        return base

    freshest = max(candidates)  # newest successful write
    age_s = max(0.0, now - freshest)
    base["last_run_field"] = _iso_from_epoch(last_run_ts)
    base["state_file_mtime"] = _iso_from_epoch(mtime_ts)
    base["freshest_age_s"] = round(age_s, 1)

    stale = age_s > stale_minutes * 60.0
    base["ok"] = True
    base["status"] = "stale" if stale else "ok"
    return base


# ---------------------------------------------------------------------------
# Probe 2 — Hyperliquid allMids socket heartbeat
# ---------------------------------------------------------------------------
def probe_hyperliquid_socket(
    url: str = HL_WS_URL,
    connect: Optional[Callable[..., Any]] = None,
    subscription: dict = HL_SUBSCRIPTION,
    frame_timeout: float = HL_SOCKET_FRAME_TIMEOUT,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Probe the liveness of the live Hyperliquid allMids websocket stream.

    Opens the socket, subscribes to ``allMids``, then waits up to ``frame_timeout``
    seconds for ANY non-empty frame. A frame arriving proves the stream is alive
    (status="ok"); silence for the whole window means the stream is dead even
    though the process/connection may be "up" (status="stale").

    A probe crash (DNS, TLS, connection reset, send error) degrades to
    ok=False + status="unknown" and never raises.

    Injectables (for tests):
      - connect: callable(url) -> ws-like object with .send/.recv/.close/.settimeout
      - now / frame_timeout control timing
    """
    now = now if now is not None else time.time()
    base: dict[str, Any] = {
        "source": "hyperliquid_socket",
        "probe_type": HL_SOCKET_PROBE_TYPE,
        "ok": False,
        "error": None,
        "url": url,
        "frames_received": 0,
        "last_frame_age_s": None,
        "frame_timeout": frame_timeout,
        "status": "unknown",
    }

    def _real_connect(u: str) -> Any:
        import websocket  # websocket-client (sync, supports timeout)

        return websocket.create_connection(u, timeout=min(10.0, frame_timeout))

    do_connect = connect or _real_connect
    ws = None
    try:
        ws = do_connect(url)
        # Best-effort subscribe; the server may also push unprompted frames.
        try:
            ws.send(json.dumps(subscription))
        except Exception:
            pass
        deadline = time.time() + frame_timeout
        frames = 0
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                ws.settimeout(remaining)
                msg = ws.recv()
            except Exception:
                # recv timeout or socket error -> stop waiting
                break
            if msg is None:
                break
            frames += 1
            if msg:  # any non-empty frame proves the stream is alive
                break
        base["frames_received"] = frames
        if frames > 0:
            base["ok"] = True
            base["last_frame_age_s"] = 0.0
            base["status"] = "ok"
        else:
            # Probe succeeded; the stream simply produced no frame in time.
            base["ok"] = True
            base["status"] = "stale"
            base["error"] = f"no frame within {frame_timeout:g}s"
    except Exception as e:
        base["ok"] = False
        base["error"] = f"hyperliquid socket probe failed: {e}"
        base["status"] = "unknown"
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    return base


# ---------------------------------------------------------------------------
# Engine: run + merge into the shared ledger
# ---------------------------------------------------------------------------
SENTINEL_PROBERS: dict[str, Callable[..., dict[str, Any]]] = {
    "news_sentiment_cache": probe_news_cache,
    "hyperliquid_socket": probe_hyperliquid_socket,
}


def run_sentinel_probes(
    sources: Optional[list[str]] = None,
    news_now: Optional[float] = None,
    news_state_loader: Optional[Callable[[Path], Optional[dict]]] = None,
    news_mtime_loader: Optional[Callable[[Path], Optional[float]]] = None,
    hl_connect: Optional[Callable[..., Any]] = None,
    hl_frame_timeout: float = HL_SOCKET_FRAME_TIMEOUT,
) -> list[dict[str, Any]]:
    """Run the requested sentinel probes (both by default)."""
    selected = sources or list(SENTINEL_PROBERS.keys())
    results: list[dict[str, Any]] = []
    for name in selected:
        prober = SENTINEL_PROBERS.get(name)
        if prober is None:
            results.append(
                {"source": name, "ok": False, "error": f"unknown source '{name}'",
                 "status": "unknown"}
            )
            continue
        if name == "news_sentiment_cache":
            results.append(prober(
                now=news_now,
                state_loader=news_state_loader,
                mtime_loader=news_mtime_loader,
            ))
        elif name == "hyperliquid_socket":
            results.append(prober(connect=hl_connect, frame_timeout=hl_frame_timeout))
    return results


def merge_sentinel_into_ledger(
    results: list[dict[str, Any]],
    path: Path = DEFAULT_LEDGER_PATH,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Merge sentinel results into the shared pem.json WITHOUT clobbering other sources.

    Reads the existing ledger (if any), and:
      - adds/updates the two sentinel source keys in ``sources{}``;
      - recomputes ``overall_status`` so it stays "degraded" if ANY source
        (quota or sentinel) failed to probe;
      - stamps ``generated_at`` (and ``engine`` if missing).
    Atomically writes. Returns the merged ledger dict.
    """
    generated_at = generated_at or _now_iso()
    path = Path(path)
    # Load prior ledger (tolerant of missing/corrupt file).
    ledger: dict[str, Any] = {}
    if path.exists():
        try:
            ledger = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            ledger = {}
    sources = ledger.get("sources") or {}
    errored = 0
    for res in results:
        src = res.get("source", "unknown")
        sources[src] = res
        if not res.get("ok"):
            errored += 1
    # Existing quota sources that didn't run this pass still count toward health.
    for src, prev in (ledger.get("sources") or {}).items():
        if not prev.get("ok"):
            errored += 1

    ledger["schema_version"] = ledger.get("schema_version", "1.0.0")
    ledger["generated_at"] = generated_at
    ledger["engine"] = ledger.get("engine", "pem-probe")
    ledger["overall_status"] = "degraded" if errored else "ok"
    ledger["sources"] = sources

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return ledger


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PEM Data Stream Integrity Sentinel")
    parser.add_argument(
        "--source", action="append", choices=list(SENTINEL_PROBERS.keys()),
        help="Limit to specific sentinel source(s); repeatable. Default: both.",
    )
    parser.add_argument(
        "--ledger", default=str(DEFAULT_LEDGER_PATH),
        help="Shared pem.json path (default: ~/.hermes/var/pem.json)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print merged ledger JSON instead of writing it.")
    parser.add_argument(
        "--frame-timeout", type=float, default=HL_SOCKET_FRAME_TIMEOUT,
        help=f"Hyperliquid socket wait-for-frame timeout (default {HL_SOCKET_FRAME_TIMEOUT:g}s)",
    )
    args = parser.parse_args(argv)

    results = run_sentinel_probes(
        sources=args.source, hl_frame_timeout=args.frame_timeout
    )

    if args.dry_run:
        # For dry-run, show only the sentinel slice for clarity.
        print(json.dumps(
            {r["source"]: r for r in results}, indent=2, sort_keys=True
        ))
        return 0 if any(r.get("ok") for r in results) else 1

    ledger = merge_sentinel_into_ledger(results, Path(args.ledger))
    flags = ", ".join(
        f"{k}={v['status']}" for k, v in ledger["sources"].items()
        if k in SENTINEL_PROBERS
    )
    print(f"PEM sentinel merged into {args.ledger} (overall_status={ledger['overall_status']}; {flags})")
    return 0 if any(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
