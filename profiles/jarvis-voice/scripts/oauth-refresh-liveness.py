#!/usr/bin/env python3
"""Hermes-native per-profile OAuth refresh/served-model liveness smoke.

Invoker: Hermes no-agent cron job ``oauth-refresh-liveness`` in the jarvis-voice
profile store.  This is intentionally a deterministic plumbing script: it uses
one headless Hermes one-shot per selected (profile, OAuth provider) pair, reads
that run's usage receipt, and never trusts credential_pool.last_status.

The script only proposes re-auth/routing commands in kanban card bodies.  It
never edits profile configuration, creates/rotates credentials, or copies
auth.json between profiles.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
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
PROBE_TIMEOUT_SECONDS = 180.0
# A fleet-wide cap prevents 63 serialised one-shots from wedging cron for an
# hour when one provider call stalls. Runs that cannot finish fail closed.
RUN_DEADLINE_SECONDS = 420.0
OAUTH_PROVIDERS = {"xai-oauth", "openai-codex", "nous"}
INVoker = "oauth-refresh-liveness (Hermes no-agent cron)"
SENSITIVE_ENV_RE = re.compile(
    r"(?:^|_)(?:API[_-]?KEY|ACCESS[_-]?KEY(?:[_-]?ID)?|CLIENT[_-]?(?:ID|SECRET)|TOKEN|SECRET|PASSWORD|PASSWD|AUTH(?:ORIZATION)?|"
    r"CREDENTIALS?|PRIVATE[_-]?KEY)(?:$|_)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def discover_pairs() -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    if not PROFILE_ROOT.is_dir():
        return pairs
    for profile_dir in sorted(PROFILE_ROOT.iterdir(), key=lambda p: p.name):
        if not profile_dir.is_dir() or profile_dir.name.startswith("."):
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


def run_probes(pairs: list[dict[str, Any]], fixture: bool) -> list[dict[str, Any]]:
    if fixture:
        return [probe_pair(pair, fixture=True) for pair in pairs]
    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + RUN_DEADLINE_SECONDS
    next_index = 0
    active: dict[concurrent.futures.Future[dict[str, Any]], dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        while next_index < len(pairs) or active:
            while next_index < len(pairs) and len(active) < MAX_WORKERS:
                if cpu_pressure_percent() > CPU_LIMIT:
                    for pair in pairs[next_index:]:
                        if pair.get("config_error"):
                            # Configuration errors are safety findings, not
                            # workload skips; retain their receipt/alert data.
                            results.append(probe_pair(pair, False))
                        else:
                            results.append({
                                "profile": pair["profile"], "provider": pair.get("provider"),
                                "model": pair.get("model"), "primary": bool(pair.get("primary")),
                                "primary_provider": pair.get("primary_provider"),
                                "order": pair.get("order"),
                                "config_error": pair.get("config_error"),
                                "config_error_kind": pair.get("config_error_kind"),
                                "invoker": INVoker, "checked_at": utc_now(),
                                "status": "skipped", "healthy": False,
                                "reason": "cpu_pressure_above_65_percent",
                            })
                    next_index = len(pairs)
                    break
                if time.monotonic() >= deadline:
                    for pair in pairs[next_index:]:
                        results.append(probe_pair(pair, False, deadline=deadline))
                    next_index = len(pairs)
                    break
                pair = pairs[next_index]
                next_index += 1
                active[pool.submit(probe_pair, pair, False, deadline)] = pair
            if not active:
                continue
            done, _ = concurrent.futures.wait(
                active, return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                pair = active.pop(future)
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - one pair must not hide others
                    results.append({
                        "profile": pair["profile"], "provider": pair.get("provider"),
                        "model": pair.get("model"), "primary": bool(pair.get("primary")),
                        "primary_provider": pair.get("primary_provider"),
                        "invoker": INVoker, "checked_at": utc_now(),
                        "status": "error", "healthy": False,
                        "reason": f"worker error: {type(exc).__name__}: {exc}",
                    })
    return sorted(results, key=lambda x: (str(x.get("profile") or ""), str(x.get("provider") or "")))


def card_body(result: dict[str, Any], results: list[dict[str, Any]]) -> str:
    profile = result["profile"]
    provider = result["provider"]
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
    key = f"oauth-dead:{result['profile']}:{result['provider']}"
    existing = state.get("cards", {}).get(key)
    if existing and card_status(existing) not in {"done", "completed", "archived"}:
        return existing
    title = key
    rc, text = board_call([
        "create", title, "--assignee", "fleet-engineer", "--priority", "170",
        "--idempotency-key", key, "--body", card_body(result, results), "--json",
    ])
    card_id = extract_card_id(text) if rc == 0 else None
    if card_id:
        state.setdefault("cards", {})[key] = card_id
    return card_id


def close_card(result: dict[str, Any], state: dict[str, Any]) -> bool:
    key = f"oauth-dead:{result['profile']}:{result['provider']}"
    card_id = state.get("cards", {}).get(key)
    if not card_id:
        return False
    status = card_status(card_id)
    if status in {"done", "completed", "archived"}:
        return True
    rc, _ = board_call([
        "complete", card_id, "--summary",
        f"oauth-refresh-liveness recovered {result['profile']}:{result['provider']} at {utc_now()} (served provider/model verified).",
    ])
    if rc != 0:
        return False
    return card_status(card_id) in {"done", "completed", "archived"}


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
    results = run_probes(pairs, fixture=bool(args.fixture))
    state_path = OUTPUT_DIR / "state.json"
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("cards", {})
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
    summary = {
        "invoker": INVoker,
        "checked_pairs": len(results),
        "dead_any": dead_any,
        "dead_primary": dead_primary,
        "errors": errors,
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


if __name__ == "__main__":
    raise SystemExit(main())
