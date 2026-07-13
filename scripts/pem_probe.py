#!/usr/bin/env python3
"""Proactive External Monitor (PEM) — API Quota Prober & Rate-Limit Tracking Engine.

Part of the Option-B design from the Proactive External Data Source Health
Monitoring proposal (jarvis-os t_1bfb62fc): lightweight, decoupled background
probes poll external API quota/rate-limit surfaces and write a centralized
status ledger to ``~/.hermes/var/pem.json``.

This module is the **probing** half only. It NEVER mutates credentials,
restarts collectors, or raises alerts — those are the job of the downstream
Proactive Remediation Engine (PRE, t_1bfb62fc_remediation). The probes are
designed to fail safely: if a single source is unreachable the ledger is still
written with the healthy sources plus an explicit ``error`` field, so active
data paths are never affected.

Sources probed:
  * Firecrawl  — ``firecrawl credit-usage --json`` (remaining/plan credits)
  * GitHub     — ``gh api rate_limit`` (per-resource remaining limits)
  * Hyperliquid— POST /info {type:exchangeStatus} (HTTP health / specialStatuses)

Plus the Data Stream Integrity Sentinel probes (delegated to pem_sentinel.py,
same shared ledger, new sources ``news_sentiment_cache`` + ``hyperliquid_socket``):
  * news cache freshness  — last_run field + file mtime of news_sentiment_catalyst.py state
  * HL socket heartbeat     — live allMids frame arrival within a timeout window

Usage:
  python3 pem_probe.py            # probe all sources, write ledger
  python3 pem_probe.py --dry-run  # print ledger JSON, do not write
  python3 pem_probe.py --source github  # one source only
  python3 pem_probe.py --ledger /tmp/pem.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess  # noqa: S404 - intentional, local CLI tools only (firecrawl, gh)
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

# --- Prober thresholds (from proposal remediation matrix) --------------------
# GitHub: warn when remaining < 100 requests; Firecrawl/HL warn below 20%.
GITHUB_REMAINING_WARN = 100
CREDIT_PCT_WARN = 20.0

# Centralised ledger location (proposal §4).
DEFAULT_LEDGER_PATH = Path.home() / ".hermes" / "var" / "pem.json"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """UTC timestamp with timezone marker, e.g. 2026-07-11T01:43:00Z."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_float(value: Any) -> Optional[float]:
    """Best-effort numeric coercion; returns None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compute_pct(remaining: Optional[float], total: Optional[float]) -> Optional[float]:
    if remaining is None or total is None or total <= 0:
        return None
    return round(remaining / total * 100.0, 4)


def _status_for_quota(remaining: Optional[float], total: Optional[float],
                      warn_pct: float = CREDIT_PCT_WARN) -> str:
    """Map a remaining/total quota onto ok / warning / exhausted."""
    pct = _compute_pct(remaining, total)
    if remaining is not None and remaining <= 0:
        return "exhausted"
    if pct is not None and pct < warn_pct:
        return "warning"
    return "ok"


# ---------------------------------------------------------------------------
# Source probers (each returns a status dict; never raises on network issues)
# ---------------------------------------------------------------------------
def probe_firecrawl(runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Query Firecrawl credit balance via the CLI.

    Parses the JSON shape emitted by ``firecrawl credit-usage --json``:
        {"success": true, "data": {"remainingCredits": N, "planCredits": M,
         "billingPeriodStart": ISO, "billingPeriodEnd": ISO}}
    """
    base: dict[str, Any] = {
        "source": "firecrawl",
        "probe_type": "credit_balance",
        "ok": False,
        "error": None,
        "remaining_credits": None,
        "plan_credits": None,
        "used_credits": None,
        "credit_pct": None,
        "status": "unknown",
        "billing_period_start": None,
        "billing_period_end": None,
    }
    try:
        proc = runner(["firecrawl", "credit-usage", "--json"],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, timeout=30)
    except Exception as exc:  # pragma: no cover - defensive
        base["error"] = f"firecrawl invocation failed: {exc}"
        return base

    if proc.returncode != 0:
        base["error"] = (proc.stderr or proc.stdout or "unknown error").strip()[:500]
        return base

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        base["error"] = f"invalid JSON from firecrawl: {exc}"
        return base

    data = (payload or {}).get("data") or {}
    remaining = _to_float(data.get("remainingCredits"))
    plan = _to_float(data.get("planCredits"))
    used = None
    if remaining is not None and plan is not None:
        used = round(plan - remaining, 6)

    base.update(
        ok=True,
        remaining_credits=remaining,
        plan_credits=plan,
        used_credits=used,
        credit_pct=_compute_pct(remaining, plan),
        status=_status_for_quota(remaining, plan),
        billing_period_start=data.get("billingPeriodStart"),
        billing_period_end=data.get("billingPeriodEnd"),
    )
    return base


def probe_github(runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Query GitHub API rate limits via ``gh api rate_limit``.

    Parses the shape: ``{"resources": {"core": {limit, used, remaining, reset},
    "search": {...}, ...}, "rate": {...}}``. The ``rate`` key mirrors ``core``
    and is treated as the primary resource.
    """
    base: dict[str, Any] = {
        "source": "github",
        "probe_type": "rate_limit",
        "ok": False,
        "error": None,
        "primary_resource": "core",
        "remaining": None,
        "limit": None,
        "used": None,
        "reset_epoch": None,
        "status": "unknown",
        "resources": {},
    }
    try:
        proc = runner(["gh", "api", "rate_limit"],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, timeout=30)
    except Exception as exc:  # pragma: no cover - defensive
        base["error"] = f"gh invocation failed: {exc}"
        return base

    if proc.returncode != 0:
        base["error"] = (proc.stderr or proc.stdout or "unknown error").strip()[:500]
        return base

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        base["error"] = f"invalid JSON from gh: {exc}"
        return base

    resources = (payload or {}).get("resources") or {}
    rate = (payload or {}).get("rate") or {}

    # Per-resource summary (used by the remediation engine to throttle per bucket).
    resource_summary = {}
    for name, bucket in resources.items():
        if not isinstance(bucket, dict):
            continue
        bucket_remaining = _to_float(bucket.get("remaining"))
        bucket_limit = _to_float(bucket.get("limit"))
        resource_summary[name] = {
            "remaining": bucket_remaining,
            "limit": bucket_limit,
            "used": _to_float(bucket.get("used")),
            "reset_epoch": _to_float(bucket.get("reset")),
            "status": ("warning" if bucket_remaining is not None
                       and bucket_remaining < GITHUB_REMAINING_WARN else "ok"),
        }

    primary = resources.get("core", rate) or {}
    remaining = _to_float(primary.get("remaining"))
    limit = _to_float(primary.get("limit"))

    status = "ok"
    if remaining is not None and remaining < GITHUB_REMAINING_WARN:
        status = "warning"

    base.update(
        ok=True,
        remaining=remaining,
        limit=limit,
        used=_to_float(primary.get("used")),
        reset_epoch=_to_float(primary.get("reset")),
        status=status,
        resources=resource_summary,
    )
    return base


def probe_hyperliquid(post_json: Optional[Callable[[str, dict], dict]] = None) -> dict[str, Any]:
    """Probe Hyperliquid exchange health via the /info endpoint.

    Sends ``{"type": "exchangeStatus"}`` to ``https://api.hyperliquid.xyz/info``
    and treats HTTP 200 with a parseable body as healthy. ``specialStatuses`` is
    normally ``null``; a non-null value signals a degraded/migration state.

    ``post_json`` is injectable for tests (defaults to a real HTTP POST). It must
    accept ``(url, payload)`` and return the parsed JSON body, raising on error.
    """
    url = "https://api.hyperliquid.xyz/info"
    base: dict[str, Any] = {
        "source": "hyperliquid",
        "probe_type": "exchange_status",
        "ok": False,
        "error": None,
        "http_status": None,
        "special_statuses": None,
        "server_time_epoch_ms": None,
        "status": "unknown",
    }

    def _real_post(_url: str, _payload: dict) -> dict:
        import urllib.request
        req = urllib.request.Request(
            _url,
            data=json.dumps(_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed host
            raw = resp.read().decode("utf-8")
            base["http_status"] = getattr(resp, "status", None)
        return json.loads(raw)

    do_post = post_json or _real_post

    try:
        body = do_post(url, {"type": "exchangeStatus"})
    except Exception as exc:  # network / HTTP / JSON errors all land here
        base["error"] = f"hyperliquid probe failed: {exc}"
        return base

    if not isinstance(body, dict):
        base["error"] = f"unexpected hyperliquid response type: {type(body).__name__}"
        return base

    special = body.get("specialStatuses")
    base.update(
        ok=True,
        special_statuses=special,
        server_time_epoch_ms=_to_float(body.get("time")),
        status="degraded" if special else "ok",
    )
    return base


# ---------------------------------------------------------------------------
# Engine + ledger
# ---------------------------------------------------------------------------
# Data Stream Integrity Sentinel probes (pem_sentinel.py) register here so the
# main PEM sweep writes them into the same shared ledger. Imported lazily inside
# run_probes() so pem_probe.py stays usable even if the sentinel module is
# absent (no hard import coupling).
SENTINEL_SOURCE_KEYS = ("news_sentiment_cache", "hyperliquid_socket")

# Quota probers (the original PEM probing set).
PROBERS: dict[str, Callable[..., dict[str, Any]]] = {
    "firecrawl": probe_firecrawl,
    "github": probe_github,
    "hyperliquid": probe_hyperliquid,
}


def run_probes(sources: Optional[list[str]] = None,
               runner: Callable[..., Any] = subprocess.run,
               post_json: Optional[Callable[[str, dict], dict]] = None) -> list[dict[str, Any]]:
    """Run the requested probers (all by default). Returns a list of status dicts.

    Includes the quota probers above AND the sentinel probes
    (news_sentiment_cache, hyperliquid_socket) which live in pem_sentinel.py.
    """
    # Build the full source registry, joining the quota probers and the sentinel
    # probers (if the module is importable).
    registry = dict(PROBERS)
    sentinel_probers = _load_sentinel_probers()
    for key, fn in sentinel_probers.items():
        registry.setdefault(key, fn)

    selected = sources or list(registry.keys())
    results: list[dict[str, Any]] = []
    for name in selected:
        prober = registry.get(name)
        if prober is None:
            results.append({"source": name, "ok": False, "error": f"unknown source '{name}'"})
            continue
        try:
            if name == "hyperliquid":
                results.append(prober(post_json=post_json))
            elif name == "news_sentiment_cache":
                results.append(prober())
            elif name == "hyperliquid_socket":
                results.append(prober())
            else:
                # Firecrawl/GitHub probers accept a `runner` for injection.
                results.append(prober(runner=runner))
        except Exception as exc:  # pragma: no cover - defensive
            results.append({"source": name, "ok": False,
                           "error": f"prober crashed: {exc}", "status": "unknown"})
    return results


def _load_sentinel_probers() -> dict[str, Callable[..., dict[str, Any]]]:
    """Lazily import pem_sentinel.py's probers (best-effort)."""
    try:
        import importlib
        mod = importlib.import_module("pem_sentinel")
        return {
            "news_sentiment_cache": mod.probe_news_cache,
            "hyperliquid_socket": mod.probe_hyperliquid_socket,
        }
    except Exception:
        return {}


# Canonical ledger schema version. Bump on breaking ledger-shape changes.
LEDGER_SCHEMA_VERSION = "1.0.0"


def build_ledger(results: list[dict[str, Any]], generated_at: Optional[str] = None) -> dict[str, Any]:
    """Assemble the on-disk ledger envelope from probe results."""
    generated_at = generated_at or _now_iso()
    by_source: dict[str, dict[str, Any]] = {}
    errored = 0
    for res in results:
        src = res.get("source", "unknown")
        by_source[src] = res
        if not res.get("ok"):
            errored += 1

    overall = "degraded" if errored else "ok"
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "generated_at": generated_at,
        "engine": "pem-probe",
        "overall_status": overall,
        "sources": by_source,
    }


def write_ledger(ledger: dict[str, Any], path: Path = DEFAULT_LEDGER_PATH) -> Path:
    """Atomically write the ledger as pretty JSON, creating parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PEM API quota prober & rate-limit tracker")
    _all_keys = list(PROBERS.keys()) + list(SENTINEL_SOURCE_KEYS)
    parser.add_argument("--source", action="append",
                        choices=_all_keys,
                        help="Limit to specific source(s); repeatable. Default: all.")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH),
                        help="Ledger output path (default: ~/.hermes/var/pem.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the ledger JSON instead of writing it.")
    args = parser.parse_args(argv)

    sources = args.source if args.source else None
    results = run_probes(sources)
    ledger = build_ledger(results)

    if args.dry_run:
        print(json.dumps(ledger, indent=2, sort_keys=True))
    else:
        written = write_ledger(ledger, Path(args.ledger))
        print(f"PEM ledger written: {written} (overall_status={ledger['overall_status']})")

    # Non-zero exit only if every probed source failed (useful for cron alerting).
    return 0 if any(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
