#!/usr/bin/env python3
"""Hermes-native per-profile OAuth refresh/served-model liveness smoke.

Invoker: Hermes no-agent cron job ``oauth-refresh-liveness`` in the jarvis-voice
profile store.  This is intentionally a deterministic plumbing script: it uses
one headless Hermes one-shot per selected (profile, OAuth provider) pair, reads
that run's usage receipt, and never trusts credential_pool.last_status.

The script only proposes re-auth/routing commands in kanban card bodies.  It
never edits profile configuration, creates/rotates credentials, or copies
auth.json between profiles.

DEPLOYMENT NOTE (t_83905adb, 2026-09-11): this file exists in two locations
that MUST be kept byte-identical by hand on every edit:
  - /home/frank/.hermes/scripts/oauth_refresh_liveness.py            (canonical source / "oracle", per loop-registry.yaml)
  - /home/frank/.hermes/profiles/jarvis-voice/scripts/oauth-refresh-liveness.py  (ACTUALLY EXECUTED by cron job 8f2e1a6c4b90 —
      relative "script" names in jobs.json resolve against that profile's own
      HERMES_HOME, not the shared root)
There is no automated sync step. Any future edit to this script's logic must
be applied to BOTH paths and verified with `sha256sum` before closing the task,
or the fix will silently be dead on the live fleet (see os-reviewer's review on
t_83905adb round 1 for the exact failure mode this caused).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERMES = os.environ.get("OAUTH_LIVENESS_HERMES", "/home/frank/.local/bin/hermes")
BOARD = os.environ.get("OAUTH_LIVENESS_BOARD", "jarvis-os")
PROFILE_ROOT = Path(os.environ.get("OAUTH_LIVENESS_PROFILES", "/home/frank/.hermes/profiles"))
OUTPUT_DIR = Path(os.environ.get(
    "OAUTH_LIVENESS_OUTPUT",
    "/home/frank/.hermes/profiles/jarvis-voice/state/oauth-refresh-liveness",
))
MAX_WORKERS = 2
CPU_LIMIT = 65.0
PROBE_TIMEOUT_SECONDS = float(os.environ.get("OAUTH_LIVENESS_PROBE_TIMEOUT_SECONDS", "180"))
# A fleet-wide cap prevents 63 serialised one-shots from wedging cron for an
# hour when one provider call stalls. Runs that cannot finish fail closed; the
# next tick retries from the first unfinished pair.
RUN_DEADLINE_SECONDS = float(os.environ.get("OAUTH_LIVENESS_RUN_DEADLINE_SECONDS", "420"))
OAUTH_PROVIDERS = {"xai-oauth", "openai-codex", "nous"}
DEADLINE_REASONS = {
    "run_deadline_exceeded_before_probe",
    "run_deadline_exceeded_during_probe",
}
INVoker = "oauth-refresh-liveness (Hermes no-agent cron)"
SENSITIVE_ENV_RE = re.compile(
    r"(?:^|_)(?:API[_-]?KEY|ACCESS[_-]?KEY(?:[_-]?ID)?|CLIENT[_-]?(?:ID|SECRET)|TOKEN|SECRET|PASSWORD|PASSWD|AUTH(?:ORIZATION)?|"
    r"CREDENTIALS?|PRIVATE[_-]?KEY)(?:$|_)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def pair_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("profile") or ""), str(row.get("provider") or ""))


def is_deadline_error(row: dict[str, Any]) -> bool:
    return str(row.get("reason") or "") in DEADLINE_REASONS


def rotate_pairs(pairs: list[dict[str, Any]], cursor: int) -> tuple[list[dict[str, Any]], int]:
    if not pairs:
        return [], 0
    cursor = cursor % len(pairs)
    return pairs[cursor:] + pairs[:cursor], cursor


def next_scan_cursor(
    original: list[dict[str, Any]],
    rotated: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> int:
    by_key = {pair_key(r): r for r in results}
    for item in rotated:
        row = by_key.get(pair_key(item))
        if row and is_deadline_error(row):
            target = pair_key(item)
            for i, orig in enumerate(original):
                if pair_key(orig) == target:
                    return i
            return 0
    return 0


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def cpu_pressure_percent() -> float:
    """Return Linux CPU PSI avg300 as a percentage; conservative load fallback."""
    try:
        text = Path("/proc/pressure/cpu").read_text(encoding="ascii")
        match = re.search(r"^some\s+.*\bavg300=([0-9.]+)", text, re.MULTILINE)
        if match:
            return float(match.group(1))
    except (OSError, ValueError):
        pass
    try:
        load1 = os.getloadavg()[0]
        return (load1 / max(1, os.cpu_count() or 1)) * 100.0
    except (OSError, ValueError):
        return 100.0


def _parse_config(path: Path) -> dict[str, Any]:
    """Parse only the stable model/fallback fields needed by this probe."""
    try:
        import yaml  # PyYAML is part of the Hermes runtime.
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - receipt must expose bad config
        raise ValueError(f"config parse failed: {type(exc).__name__}: {exc}") from exc


def _profile_has_enabled_jobs(profile_dir: Path) -> bool:
    """Return True if the profile has at least one ENABLED cron job."""
    jobs_path = profile_dir / "cron" / "jobs.json"
    if not jobs_path.is_file():
        return False
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    for job in data.get("jobs", []) if isinstance(data, dict) else []:
        if isinstance(job, dict) and job.get("enabled"):
            return True
    return False


def discover_pairs() -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    if not PROFILE_ROOT.is_dir():
        return pairs
    for profile_dir in sorted(PROFILE_ROOT.iterdir(), key=lambda p: p.name):
        if not profile_dir.is_dir() or profile_dir.name.startswith("."):
            continue
        # Skip non-runtime profiles (e.g. backups) that have no enabled cron
        # jobs — they are backup stores, not agents, and don't need probes.
        if not _profile_has_enabled_jobs(profile_dir):
            continue
        config_path = profile_dir / "config.yaml"
        if not config_path.is_file():
            pairs.append({
                "profile": profile_dir.name,
                "provider": None,
                "model": None,
                "primary": True,
                "primary_provider": None,
                "config_error": "missing config.yaml",
                "config_error_kind": "missing_config",
            })
            continue
        try:
            config = _parse_config(config_path)
        except ValueError as exc:
            pairs.append({
                "profile": profile_dir.name,
                "provider": None,
                "model": None,
                "primary": True,
                "primary_provider": None,
                "config_error": str(exc),
                "config_error_kind": "malformed_config",
            })
            continue
        model_present = "model" in config
        model_value = config.get("model")
        model = model_value if isinstance(model_value, dict) else {}
        primary_provider = model.get("provider")
        default_model = model.get("default")
        order: list[dict[str, Any]] = []
        if not model_present:
            pairs.append({
                "profile": profile_dir.name,
                "provider": None,
                "model": None,
                "primary": True,
                "primary_provider": None,
                "config_error": "missing primary model configuration",
                "config_error_kind": "missing_primary_model",
            })
        elif not isinstance(model_value, dict):
            # A present but malformed model block makes the primary unknown;
            # emit a critical receipt instead of silently skipping the profile.
            pairs.append({
                "profile": profile_dir.name,
                "provider": None,
                "model": None,
                "primary": True,
                "primary_provider": None,
                "config_error": "primary model configuration is not a mapping",
                "config_error_kind": "malformed_primary_model",
            })
        elif not isinstance(primary_provider, str) or not primary_provider.strip():
            pairs.append({
                "profile": profile_dir.name,
                "provider": None,
                "model": None,
                "primary": True,
                "primary_provider": None,
                "config_error": "missing primary provider configuration",
                "config_error_kind": "missing_primary_provider",
            })
        elif primary_provider in OAUTH_PROVIDERS:
            order.append({"provider": primary_provider, "model": default_model, "primary": True})
        fallbacks = config.get("fallback_providers")
        if "fallback_providers" in config and not isinstance(fallbacks, list):
            pairs.append({
                "profile": profile_dir.name,
                "provider": None,
                "model": None,
                "primary": False,
                "primary_provider": primary_provider,
                "config_error": "fallback_providers configuration is not a list",
                "config_error_kind": "malformed_fallback_config",
            })
            fallbacks = []
        if not isinstance(fallbacks, list):
            fallbacks = []
        for index, rung in enumerate(fallbacks):
            if not isinstance(rung, dict):
                pairs.append({
                    "profile": profile_dir.name,
                    "provider": None,
                    "model": None,
                    "primary": False,
                    "primary_provider": primary_provider,
                    "config_error": f"fallback rung {index} is not a mapping",
                    "config_error_kind": "malformed_fallback_rung",
                })
                continue
            provider = rung.get("provider")
            if provider not in OAUTH_PROVIDERS:
                continue
            if any(x["provider"] == provider for x in order):
                continue
            order.append({"provider": provider, "model": rung.get("model"), "primary": False})
        for item in order:
            if not isinstance(item.get("model"), str) or not item["model"].strip():
                pairs.append({
                    "profile": profile_dir.name,
                    "provider": item["provider"],
                    "model": None,
                    "primary": item["primary"],
                    "primary_provider": primary_provider,
                    "config_error": "missing requested model",
                })
            else:
                pairs.append({
                    "profile": profile_dir.name,
                    "provider": item["provider"],
                    "model": item["model"].strip(),
                    "primary": item["primary"],
                    "primary_provider": primary_provider,
                    "order": [x["provider"] for x in order],
                })
    return pairs


def load_fixture(path: Path) -> list[dict[str, Any]]:
    """Load harmless fake check results for deterministic end-to-end tests.

    Accepted shape is {"checks": [{profile, provider, model, healthy, ...}]}.
    Each check may include ``primary`` and ``order``; absent values use the
    first provider for that profile as primary and input order as the fallback
    order.  Fixture mode still exercises receipt/card/close behavior.
    """
    data = load_json(path, {})
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError("fixture must contain a checks list")
    rows: list[dict[str, Any]] = []
    first_provider: dict[str, str] = {}
    for raw in checks:
        if not isinstance(raw, dict):
            raise ValueError("fixture checks must be objects")
        profile = str(raw.get("profile") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        if not profile or provider not in OAUTH_PROVIDERS:
            raise ValueError("fixture check requires profile and supported provider")
        first_provider.setdefault(profile, provider)
        rows.append(dict(raw, profile=profile, provider=provider))
    for row in rows:
        row.setdefault("model", "fixture-model")
        row.setdefault("primary", row["provider"] == first_provider[row["profile"]])
        row.setdefault("primary_provider", first_provider[row["profile"]])
        row.setdefault("order", [x["provider"] for x in rows if x["profile"] == row["profile"]])
    return rows


def safe_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if (
            key.startswith("HERMES_KANBAN_")
            or key in {"HERMES_SESSION_SOURCE", "HERMES_KANBAN_RUN_ID"}
            or SENSITIVE_ENV_RE.search(key)
        ):
            env.pop(key, None)
    # Cron launches this script with a profile-scoped HERMES_HOME. Nested
    # profile probes must resolve from the canonical root, then -p selects the
    # requested profile store; otherwise the child looks under a nonexistent
    # <jarvis-voice>/profiles/<profile> path.
    env["HERMES_HOME"] = "/home/frank/.hermes"
    env.pop("HERMES_PROFILE", None)
    return env


def parse_usage(
    path: Path,
    requested_provider: str,
    requested_model: str,
    returncode: int = 0,
) -> tuple[bool, str, dict[str, Any]]:
    data = load_json(path, {})
    completed = data.get("completed") is True
    failed = data.get("failed")
    served_provider = data.get("provider")
    served_model = data.get("model")
    healthy = (
        returncode == 0
        and completed
        and failed is False
        and served_provider == requested_provider
        and served_model == requested_model
    )
    reason = "ok" if healthy else (
        f"returncode={returncode}, completed={completed}, failed={failed}, served_provider={served_provider!r}, "
        f"served_model={served_model!r}, requested_provider={requested_provider!r}, "
        f"requested_model={requested_model!r}"
    )
    return healthy, reason, {
        "completed": data.get("completed"),
        "failed": data.get("failed"),
        "served_provider": served_provider,
        "served_model": served_model,
        "total_tokens": data.get("total_tokens"),
        "cost_status": data.get("cost_status"),
    }


def probe_pair(
    pair: dict[str, Any],
    fixture: bool = False,
    deadline: float | None = None,
) -> dict[str, Any]:
    base = {
        "profile": pair["profile"],
        "provider": pair.get("provider"),
        "model": pair.get("model"),
        "primary": bool(pair.get("primary")),
        "primary_provider": pair.get("primary_provider"),
        "order": pair.get("order"),
        "config_error": pair.get("config_error"),
        "config_error_kind": pair.get("config_error_kind"),
        "invoker": INVoker,
        "checked_at": utc_now(),
    }
    if pair.get("config_error"):
        primary = pair.get("primary") is True
        base.update(
            status="error",
            healthy=False,
            severity="critical" if primary or pair.get("primary") is None else "warning",
            reason=pair["config_error"],
        )
        return base
    if fixture:
        preset = pair.get("status")
        if preset in {"skipped", "error"}:
            base.update(
                status=str(preset),
                healthy=False,
                reason=str(pair.get("reason") or preset),
            )
            return base
        returncode = int(pair.get("returncode", 0))
        usage_fields = {"completed", "failed", "served_provider", "served_model"}
        if usage_fields.intersection(pair):
            completed = pair.get("completed") is True
            failed = pair.get("failed")
            served_provider = pair.get("served_provider")
            served_model = pair.get("served_model")
            healthy = (
                returncode == 0
                and completed
                and failed is False
                and served_provider == pair["provider"]
                and served_model == pair.get("model")
            )
            reason = "ok" if healthy else (
                f"returncode={returncode}, completed={completed}, failed={failed}, "
                f"served_provider={served_provider!r}, served_model={served_model!r}, "
                f"requested_provider={pair['provider']!r}, requested_model={pair.get('model')!r}"
            )
        else:
            healthy = bool(pair.get("healthy")) and returncode == 0
            reason = str(pair.get("reason") or ("ok" if healthy else "fixture-dead"))
        base.update(status="live" if healthy else "dead", healthy=healthy, reason=reason, returncode=returncode)
        for key in ("completed", "failed", "served_provider", "served_model"):
            if key in pair:
                base[key] = pair[key]
        return base
    usage_fd, usage_name = tempfile.mkstemp(prefix="oauth-liveness-", suffix=".json", dir=str(OUTPUT_DIR))
    os.close(usage_fd)
    usage_path = Path(usage_name)
    cmd = [
        HERMES, "-p", pair["profile"], "-z", "OK",
        "--provider", pair["provider"], "-m", pair["model"], "-t", "",
        "--usage-file", str(usage_path),
    ]
    try:
        if cpu_pressure_percent() > CPU_LIMIT:
            base.update(status="skipped", healthy=False, reason="cpu_pressure_above_65_percent")
            return base
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            base.update(
                status="error", healthy=False,
                reason="run_deadline_exceeded_before_probe",
            )
            return base
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=min(PROBE_TIMEOUT_SECONDS, remaining) if remaining is not None else PROBE_TIMEOUT_SECONDS,
            env=safe_env(),
            cwd=str(PROFILE_ROOT / pair["profile"]),
            start_new_session=True,
        )
        healthy, reason, usage = parse_usage(
            usage_path, pair["provider"], pair["model"], completed.returncode,
        )
        base.update(
            status="live" if healthy else "dead",
            healthy=healthy,
            reason=reason if completed.returncode == 0 else f"rc={completed.returncode}; {reason}",
            returncode=completed.returncode,
            **usage,
        )
        return base
    except subprocess.TimeoutExpired:
        if deadline is not None and time.monotonic() >= deadline:
            base.update(
                status="error", healthy=False,
                reason="run_deadline_exceeded_during_probe",
            )
        else:
            base.update(status="dead", healthy=False, reason="one-shot-timeout")
        return base
    except OSError as exc:
        base.update(status="error", healthy=False, reason=f"probe launch: {type(exc).__name__}: {exc}")
        return base
    finally:
        try:
            usage_path.unlink()
        except OSError:
            pass


def _live_probe_base(pair: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": pair["profile"],
        "provider": pair.get("provider"),
        "model": pair.get("model"),
        "primary": bool(pair.get("primary")),
        "primary_provider": pair.get("primary_provider"),
        "order": pair.get("order"),
        "config_error": pair.get("config_error"),
        "config_error_kind": pair.get("config_error_kind"),
        "invoker": INVoker,
        "checked_at": utc_now(),
    }


def _launch_live_probe(
    pair: dict[str, Any],
) -> tuple[subprocess.Popen[bytes], Path] | dict[str, Any]:
    base = _live_probe_base(pair)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    usage_fd, usage_name = tempfile.mkstemp(
        prefix="oauth-liveness-", suffix=".json", dir=str(OUTPUT_DIR),
    )
    os.close(usage_fd)
    usage_path = Path(usage_name)
    cmd = [
        HERMES, "-p", pair["profile"], "-z", "OK",
        "--provider", pair["provider"], "-m", pair["model"], "-t", "",
        "--usage-file", str(usage_path),
    ]
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=safe_env(),
            cwd=str(PROFILE_ROOT / pair["profile"]),
            start_new_session=True,
        )
    except OSError as exc:
        usage_path.unlink(missing_ok=True)
        base.update(
            status="error", healthy=False,
            reason=f"probe launch: {type(exc).__name__}: {exc}",
        )
        return base
    return process, usage_path


def _terminate_live_probe(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _finish_live_probe(
    pair: dict[str, Any],
    base: dict[str, Any],
    process: subprocess.Popen[bytes],
    usage_path: Path,
    reason_override: str | None = None,
) -> dict[str, Any]:
    returncode = process.returncode if process.returncode is not None else process.poll()
    returncode = int(returncode) if returncode is not None else -signal.SIGKILL
    healthy, reason, usage = parse_usage(
        usage_path, pair["provider"], pair["model"], returncode,
    )
    base.update(
        status=("error" if reason_override else ("live" if healthy else "dead")),
        healthy=healthy,
        reason=reason_override or (reason if returncode == 0 else f"rc={returncode}; {reason}"),
        returncode=returncode,
        **usage,
    )
    usage_path.unlink(missing_ok=True)
    return base


def _deadline_result(pair: dict[str, Any], reason: str) -> dict[str, Any]:
    base = _live_probe_base(pair)
    base.update(status="error", healthy=False, reason=reason)
    return base


def run_probes(pairs: list[dict[str, Any]], fixture: bool) -> list[dict[str, Any]]:
    if fixture:
        return [probe_pair(pair, fixture=True) for pair in pairs]
    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + RUN_DEADLINE_SECONDS
    next_index = 0
    # Each active entry is (pair, base, usage_path, per_probe_deadline).
    active: dict[subprocess.Popen[bytes], tuple[dict[str, Any], dict[str, Any], Path, float]] = {}
    try:
        while next_index < len(pairs) or active:
            while next_index < len(pairs) and len(active) < MAX_WORKERS:
                if cpu_pressure_percent() > CPU_LIMIT:
                    for pair in pairs[next_index:]:
                        if pair.get("config_error"):
                            results.append(probe_pair(pair, False))
                        else:
                            base = _live_probe_base(pair)
                            base.update(
                                status="skipped", healthy=False,
                                reason="cpu_pressure_above_65_percent",
                            )
                            results.append(base)
                    next_index = len(pairs)
                    break
                if time.monotonic() >= deadline:
                    for pair in pairs[next_index:]:
                        if pair.get("config_error"):
                            results.append(probe_pair(pair, False))
                        else:
                            results.append(_deadline_result(
                                pair, "run_deadline_exceeded_before_probe",
                            ))
                    next_index = len(pairs)
                    break
                pair = pairs[next_index]
                next_index += 1
                if pair.get("config_error"):
                    results.append(probe_pair(pair, False))
                    continue
                launched = _launch_live_probe(pair)
                if isinstance(launched, dict):
                    results.append(launched)
                    continue
                process, usage_path = launched
                active[process] = (
                    pair, _live_probe_base(pair), usage_path,
                    min(deadline, time.monotonic() + PROBE_TIMEOUT_SECONDS),
                )
            if not active:
                continue
            now = time.monotonic()
            for process, (pair, base, usage_path, probe_deadline) in list(active.items()):
                if process.poll() is not None:
                    results.append(_finish_live_probe(pair, base, process, usage_path))
                    active.pop(process)
                elif now >= deadline:
                    _terminate_live_probe(process)
                    results.append(_finish_live_probe(
                        pair, base, process, usage_path,
                        "run_deadline_exceeded_during_probe",
                    ))
                    active.pop(process)
                elif now >= probe_deadline:
                    _terminate_live_probe(process)
                    results.append(_finish_live_probe(
                        pair, base, process, usage_path, "one-shot-timeout",
                    ))
                    active.pop(process)
            if active:
                remaining = max(0.0, deadline - time.monotonic())
                time.sleep(min(0.05, remaining) if remaining else 0)
    finally:
        for process, (_, _, usage_path, _) in list(active.items()):
            _terminate_live_probe(process)
            usage_path.unlink(missing_ok=True)
    return sorted(results, key=lambda x: (str(x.get("profile") or ""), str(x.get("provider") or "")))


def read_credential_pool_entry(profile: str, provider: str) -> dict[str, Any] | None:
    """Best-effort, read-only lookup of a profile's credential_pool entry.

    Returns the entry with the most recent ``last_status_at`` for
    ``provider`` in ``<profile>/auth.json``, or None on any missing/malformed
    data. Callers must treat None as "insufficient signal, do not classify" —
    this never raises and never writes.
    """
    data = load_json(PROFILE_ROOT / profile / "auth.json", None)
    if not isinstance(data, dict):
        return None
    pool = data.get("credential_pool")
    if not isinstance(pool, dict):
        return None
    entries = pool.get(provider)
    if not isinstance(entries, list):
        return None
    candidates = [e for e in entries if isinstance(e, dict)]
    if not candidates:
        return None

    def sort_key(entry: dict[str, Any]) -> float:
        try:
            return float(entry.get("last_status_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    return max(candidates, key=sort_key)


def classify_quota_error(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return quota metadata if ``entry`` shows a live-token usage-cap 429.

    Distinguishes a plan-level ``usage_limit_reached`` 429 on an otherwise
    live token (shared account quota, resets on its own, re-auth is a no-op)
    from a genuinely dead credential (invalid_grant / relogin_required /
    other 401s), which falls through unchanged to the existing oauth-dead
    path. Returns None when the entry doesn't match this specific pattern.
    """
    if not entry or entry.get("last_error_code") != 429:
        return None
    message = str(entry.get("last_error_message") or "")
    if "usage_limit_reached" not in message:
        return None
    resets_at: int | None = None
    match = re.search(r"resets_at['\"]?\s*:\s*(\d+)", message)
    if match:
        try:
            resets_at = int(match.group(1))
        except ValueError:
            resets_at = None
    resets_at_iso = (
        datetime.fromtimestamp(resets_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if resets_at is not None else None
    )
    return {
        "error_code": 429,
        "error_message": message,
        "resets_at": resets_at,
        "resets_at_iso": resets_at_iso,
    }


def card_key(result: dict[str, Any], quota: dict[str, Any] | None) -> str:
    kind = "oauth-quota" if quota else "oauth-dead"
    return f"{kind}:{result['profile']}:{result['provider']}"


def card_body(
    result: dict[str, Any],
    results: list[dict[str, Any]],
    quota: dict[str, Any] | None = None,
) -> str:
    profile = result["profile"]
    provider = result["provider"]
    if quota:
        resets = quota.get("resets_at_iso") or "unknown"
        return (
            f"oauth-refresh-liveness detected a live-token usage quota exhaustion for "
            f"profile={profile}, provider={provider}.\n"
            f"Observed error: {quota.get('error_message') or 'usage_limit_reached (429)'}\n"
            f"Requested model: {result.get('model')}\n\n"
            f"Quota exhausted, resets at {resets}. No action needed unless fallback is "
            "also failing (check the profile's served-model receipt / recent runs).\n\n"
            "This is a shared plan-level usage cap on an otherwise live OAuth token, not "
            "a revoked/expired credential. Re-auth has zero effect on this class of error "
            "and will not clear the cap early; do not run the re-auth command for this card."
        )
    primary_provider = result.get("primary_provider")
    live = {r["provider"] for r in results if r.get("profile") == profile and r.get("healthy")}
    order = result.get("order") or [r["provider"] for r in results if r.get("profile") == profile]
    fallback = (
        None if primary_provider in live
        else next((p for p in order if p != primary_provider and p in live), None)
    )
    reauth = f"hermes -p {profile} auth add {provider} --type oauth --no-browser"
    if fallback:
        fallback_row = next(r for r in results if r.get("profile") == profile and r.get("provider") == fallback)
        model = fallback_row.get("model") or "<fallback-model>"
        flip = (
            f"hermes -p {profile} config set model.provider {fallback} && "
            f"hermes -p {profile} config set model.default {model}"
        )
    else:
        flip = (
            "Primary is already live; no interim flip required."
            if primary_provider in live
            else "No live fallback rung observed; do not flip primary automatically."
        )
    return (
        f"oauth-refresh-liveness detected a dead OAuth provider for profile={profile}, provider={provider}.\n"
        f"Observed reason: {result.get('reason', 'unknown')}\n"
        f"Requested model: {result.get('model')}\n\n"
        f"FRANK RE-AUTH (exact):\n{reauth}\n\n"
        f"INTERIM FLIP (primary -> first LIVE fallback rung; proposal only):\n{flip}\n\n"
        "The script does not edit config or credentials. Re-run the cron after re-auth; the card closes on a live served-model receipt."
    )


def board_call(args: list[str]) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            [HERMES, "kanban", "--board", BOARD, *args],
            capture_output=True, text=True, timeout=45, env=safe_env(),
        )
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def extract_card_id(text: str) -> str | None:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("id", "task_id"):
                if isinstance(data.get(key), str) and re.fullmatch(r"t_[0-9a-f]{8}", data[key]):
                    return data[key]
    except (ValueError, TypeError):
        pass
    match = re.search(r"\bt_[0-9a-f]{8}\b", text)
    return match.group(0) if match else None


def card_status(card_id: str) -> str | None:
    rc, text = board_call(["show", card_id, "--json"])
    if rc != 0:
        return None
    try:
        data = json.loads(text)
        task = data.get("task", data) if isinstance(data, dict) else {}
        status = task.get("status") if isinstance(task, dict) else None
        return str(status) if status else None
    except (ValueError, TypeError):
        match = re.search(r"\bstatus:\s*(\w+)", text, re.IGNORECASE)
        return match.group(1).lower() if match else None


def create_or_reuse_card(result: dict[str, Any], results: list[dict[str, Any]], state: dict[str, Any]) -> str | None:
    # Additive disambiguation only (t_83905adb): a "dead" probe result may be
    # a live token hitting a shared plan-level usage cap rather than a
    # genuinely revoked/expired credential. Inspect the profile's own
    # credential_pool entry (read-only) and route to a distinct card
    # kind/title/body when it matches; the underlying liveness/health
    # determination in run_probes/parse_usage is unchanged.
    quota = None
    if result.get("status") == "dead":
        entry = read_credential_pool_entry(result["profile"], result["provider"])
        quota = classify_quota_error(entry)
    key = card_key(result, quota)
    existing = state.get("cards", {}).get(key)
    if existing and card_status(existing) not in {"done", "completed", "archived"}:
        return existing
    title = key
    rc, text = board_call([
        "create", title, "--assignee", "fleet-engineer", "--priority", "170",
        "--idempotency-key", key, "--body", card_body(result, results, quota), "--json",
    ])
    card_id = extract_card_id(text) if rc == 0 else None
    if card_id:
        state.setdefault("cards", {})[key] = card_id
    return card_id


def close_card(result: dict[str, Any], state: dict[str, Any]) -> bool:
    # A prior fire for this (profile, provider) pair may have filed either an
    # oauth-dead or an oauth-quota card (or, across script versions, one of
    # each in sequence); close whichever key(s) are tracked in state.
    profile, provider = result["profile"], result["provider"]
    any_found = False
    all_closed = True
    for key in (f"oauth-dead:{profile}:{provider}", f"oauth-quota:{profile}:{provider}"):
        card_id = state.get("cards", {}).get(key)
        if not card_id:
            continue
        any_found = True
        status = card_status(card_id)
        if status in {"done", "completed", "archived"}:
            continue
        rc, _ = board_call([
            "complete", card_id, "--summary",
            f"oauth-refresh-liveness recovered {profile}:{provider} at {utc_now()} (served provider/model verified).",
        ])
        if rc != 0 or card_status(card_id) not in {"done", "completed", "archived"}:
            all_closed = False
    return any_found and all_closed


def write_receipts(results: list[dict[str, Any]], state: dict[str, Any]) -> None:
    by_profile: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_profile.setdefault(result["profile"], []).append(result)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for profile, rows in by_profile.items():
        payload = {
            "invoker": INVoker,
            "checked_at": utc_now(),
            "profile": profile,
            "providers": sorted(rows, key=lambda x: str(x.get("provider") or "")),
            "dead_primary": any(r.get("primary") and r.get("status") == "dead" for r in rows),
            "errors": [r for r in rows if r.get("status") == "error"],
            "skipped": [r for r in rows if r.get("status") == "skipped"],
            "config_errors": [r for r in rows if r.get("config_error")],
        }
        atomic_json(OUTPUT_DIR / f"{profile}.json", payload)
    atomic_json(OUTPUT_DIR / "state.json", state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, help="Use fake checks from a JSON fixture")
    args = parser.parse_args()
    try:
        pairs = load_fixture(args.fixture) if args.fixture else discover_pairs()
    except (OSError, ValueError) as exc:
        print(json.dumps({"invoker": INVoker, "status": "error", "error": str(exc)}))
        return 2
    state_path = OUTPUT_DIR / "state.json"
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("cards", {})
    original = list(pairs)
    try:
        cursor = int(state.get("scan_cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    if args.fixture:
        rotated, cursor = original, 0
    else:
        rotated, cursor = rotate_pairs(original, cursor)
    results = run_probes(rotated, fixture=bool(args.fixture))
    if not args.fixture:
        state["scan_cursor"] = next_scan_cursor(original, rotated, results)
    for result in results:
        if result.get("healthy"):
            close_card(result, state)
        elif result.get("status") == "dead":
            create_or_reuse_card(result, results, state)
    write_receipts(results, state)
    dead_primary = [
        f"{r['profile']}:{r['provider']}" for r in results
        if r.get("primary") and not r.get("healthy") and r.get("status") == "dead"
    ]
    dead_any = [
        f"{r['profile']}:{r['provider']}" for r in results
        if not r.get("healthy") and r.get("status") == "dead"
    ]
    errors = [r for r in results if r.get("status") == "error"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    deadline_errors = [r for r in results if is_deadline_error(r)]
    summary = {
        "invoker": INVoker,
        "checked_pairs": len(results),
        "dead_any": dead_any,
        "dead_primary": dead_primary,
        "errors": errors,
        "skipped": [f"{r.get('profile')}:{r.get('provider')}" for r in skipped],
        "deadline_errors": len(deadline_errors),
        "scan_cursor": state.get("scan_cursor", 0),
        "config_errors": [
            {
                "profile": r.get("profile"),
                "provider": r.get("provider"),
                "primary": r.get("primary"),
                "severity": r.get("severity", "critical"),
                "reason": r.get("reason"),
            }
            for r in errors
            if r.get("config_error")
        ],
        "receipts_dir": str(OUTPUT_DIR),
    }
    if errors:
        print("OAUTH_REFRESH_LIVENESS_ERROR " + json.dumps(summary, sort_keys=True))
        return 1
    if dead_primary:
        print("OAUTH_REFRESH_LIVENESS_PRIMARY_DEAD " + json.dumps(summary, sort_keys=True))
        return 1
    if dead_any:
        print("OAUTH_REFRESH_LIVENESS_FALLBACK_DEAD " + json.dumps(summary, sort_keys=True))
        return 0
    if skipped:
        print("OAUTH_REFRESH_LIVENESS_INCOMPLETE " + json.dumps(summary, sort_keys=True))
        return 0
    print("OAUTH_REFRESH_LIVENESS_OK " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
