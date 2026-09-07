#!/usr/bin/env python3
"""Nous + OpenRouter daily free/cloaked catalog scan (Hermes-native, no-agent).

Frank policy 2026-09-07:
  - Nous = :free/$0 OR deepseek-v4-flash price band only
  - OpenRouter = :free ONLY (never pin/route paid OR)
  - Do not flip jarvis profile primary off Max/Codex
  - No hermes update / money / A3

Empty stdout when quiet (no material delta). Prints MATERIAL lines only when
Frank should be paged (new cloaked/free, paid-OR stripped, auth/smoke broken).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HOME = Path(os.environ.get("HOME", "/home/frank"))
HERMES = HOME / ".hermes"
CACHE = HERMES / "cache" / "nous_daily_catalog.json"
RECEIPT_DIR = HERMES / "deploy-state" / "ops-notes"
ROOT_CFG = HERMES / "config.yaml"
JARVIS_CFG = HERMES / "profiles" / "jarvis" / "config.yaml"
NOUS_URL = "https://inference-api.nousresearch.com/v1/models"
OR_URL = "https://openrouter.ai/api/v1/models"
LONDON = ZoneInfo("Europe/London")
FLASH_RE = re.compile(r"deepseek.*v4.*flash|~deepseek/deepseek-v4-flash", re.I)


def now_london() -> datetime:
    return datetime.now(LONDON)


def fetch_models(url: str, timeout: float = 45.0) -> tuple[int, list[dict[str, Any]], str]:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-nous-daily-catalog/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            code = getattr(resp, "status", 200) or 200
            data = json.loads(body.decode("utf-8", errors="replace"))
            models = data.get("data") if isinstance(data, dict) else data
            if not isinstance(models, list):
                return int(code), [], "unexpected_payload"
            return int(code), models, "ok"
    except urllib.error.HTTPError as e:
        return int(e.code), [], f"http_error:{e}"
    except Exception as e:  # noqa: BLE001 — cron-safe classify
        return 0, [], f"error:{type(e).__name__}:{e}"


def model_id(m: dict[str, Any]) -> str:
    return str(m.get("id") or m.get("name") or "").strip()


def ctx_len(m: dict[str, Any]) -> int:
    for k in ("context_length", "context_window", "max_context_length"):
        v = m.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    meta = m.get("top_provider") or m.get("architecture") or {}
    if isinstance(meta, dict):
        for k in ("context_length", "max_completion_tokens"):
            v = meta.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
    return 0


def is_free_zero_price(m: dict[str, Any]) -> bool:
    pricing = m.get("pricing") or {}
    if not isinstance(pricing, dict):
        return False
    try:
        prompt = float(pricing.get("prompt") or pricing.get("input") or 1)
        completion = float(pricing.get("completion") or pricing.get("output") or 1)
        return prompt == 0 and completion == 0
    except (TypeError, ValueError):
        return False


def flash_band(mid: str) -> bool:
    return bool(FLASH_RE.search(mid or ""))


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_yaml_fallbacks(path: Path, fallbacks: list[Any]) -> bool:
    """Rewrite only fallback_providers; prefer ruamel to preserve formatting."""
    stamp = now_london().strftime("%Y%m%dT%H%M%S")
    try:
        from ruamel.yaml import YAML  # type: ignore

        y = YAML()
        y.preserve_quotes = True
        with path.open() as f:
            data = y.load(f)
        if data is None:
            return False
        data["fallback_providers"] = fallbacks
        bak = Path(str(path) + f".bak-nous-catalog-{stamp}")
        bak.write_text(path.read_text())
        with path.open("w") as f:
            y.dump(data, f)
        return True
    except Exception:
        try:
            import yaml  # type: ignore

            cfg = yaml.safe_load(path.read_text()) or {}
            if not isinstance(cfg, dict):
                return False
            bak = Path(str(path) + f".bak-nous-catalog-{stamp}")
            bak.write_text(path.read_text())
            cfg["fallback_providers"] = fallbacks
            path.write_text(yaml.safe_dump(cfg, sort_keys=False))
            return True
        except Exception:
            return False


def strip_paid_openrouter(fallbacks: list[Any]) -> tuple[list[Any], list[dict[str, str]]]:
    kept: list[Any] = []
    removed: list[dict[str, str]] = []
    for fb in fallbacks:
        if not isinstance(fb, dict):
            kept.append(fb)
            continue
        prov = str(fb.get("provider") or "").lower()
        model = str(fb.get("model") or "")
        if prov == "openrouter" and model and not model.endswith(":free"):
            removed.append({"provider": "openrouter", "model": model})
            continue
        kept.append(fb)
    return kept, removed


def promote_free(fallbacks: list[Any], free_ids: list[str], top_n: int = 2) -> tuple[list[Any], bool]:
    """Ensure top free Nous models appear as nous fallbacks."""
    existing = {
        str(x.get("model"))
        for x in fallbacks
        if isinstance(x, dict) and str(x.get("provider", "")).lower() == "nous"
    }
    to_add = [mid for mid in free_ids[:top_n] if mid not in existing]
    if not to_add:
        return fallbacks, False
    inserts = [
        {
            "provider": "nous",
            "model": mid,
            "base_url": "https://inference-api.nousresearch.com/v1",
        }
        for mid in to_add
    ]
    out: list[Any] = []
    inserted = False
    flash_seen = False
    for fb in fallbacks:
        if isinstance(fb, dict) and str(fb.get("provider", "")).lower() == "nous" and flash_band(
            str(fb.get("model") or "")
        ):
            out.append(fb)
            flash_seen = True
            continue
        if not inserted and flash_seen and (
            not isinstance(fb, dict) or str(fb.get("provider", "")).lower() != "nous"
        ):
            out.extend(inserts)
            inserted = True
        out.append(fb)
    if not inserted:
        idx = 0
        for i, fb in enumerate(out):
            if isinstance(fb, dict) and str(fb.get("provider", "")).lower() == "nous":
                idx = i + 1
        out = out[:idx] + inserts + out[idx:]
    return out, True


def stealth_tags(m: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "label", "labels"):
        v = m.get(key)
        if isinstance(v, list):
            tags.extend(str(x).lower() for x in v)
        elif isinstance(v, str):
            tags.append(v.lower())
    blob = " ".join(tags + [model_id(m).lower(), str(m.get("name") or "").lower()])
    out = []
    if "stealth" in blob:
        out.append("stealth")
    if "cloak" in blob:
        out.append("cloaked")
    return out


def main() -> int:
    ts = now_london()
    scan_date = ts.strftime("%Y-%m-%d")
    material: list[str] = []

    nous_code, nous_models, nous_err = fetch_models(NOUS_URL)
    or_code, or_models, or_err = fetch_models(OR_URL)

    if nous_code != 200:
        material.append(f"MATERIAL auth/smoke: Nous /v1/models http={nous_code} err={nous_err}")
    if or_code != 200:
        material.append(f"MATERIAL auth/smoke: OpenRouter /v1/models http={or_code} err={or_err}")

    free_nous: list[dict[str, Any]] = []
    flash_refs: list[str] = []
    expensive_examples: list[str] = []
    cloaked_nous: list[dict[str, Any]] = []
    for m in nous_models:
        if not isinstance(m, dict):
            continue
        mid = model_id(m)
        if not mid:
            continue
        if mid.endswith(":free") or is_free_zero_price(m):
            free_nous.append({"id": mid, "context": ctx_len(m)})
        if flash_band(mid):
            flash_refs.append(mid)
        tags = stealth_tags(m)
        if tags:
            cloaked_nous.append({"id": mid, "tags": tags, "context": ctx_len(m)})
        if (
            not mid.endswith(":free")
            and not flash_band(mid)
            and not is_free_zero_price(m)
            and len(expensive_examples) < 10
        ):
            expensive_examples.append(mid)

    free_nous.sort(key=lambda x: (-int(x.get("context") or 0), x["id"]))
    free_ids = [x["id"] for x in free_nous]

    free_or: list[dict[str, Any]] = []
    cloaked_or: list[str] = []
    stealth_or: list[str] = []
    for m in or_models:
        if not isinstance(m, dict):
            continue
        mid = model_id(m)
        if not mid:
            continue
        if mid.endswith(":free") or is_free_zero_price(m):
            free_or.append({"id": mid, "context": ctx_len(m)})
        tags = stealth_tags(m)
        if "cloaked" in tags:
            cloaked_or.append(mid)
        if "stealth" in tags:
            stealth_or.append(mid)
    free_or.sort(key=lambda x: (-int(x.get("context") or 0), x["id"]))

    prev: dict[str, Any] = {}
    if CACHE.exists():
        try:
            prev = json.loads(CACHE.read_text())
        except Exception:
            prev = {}

    prev_free = {x.get("id") for x in (prev.get("free_list") or []) if isinstance(x, dict)}
    new_free = [x for x in free_ids if x not in prev_free]
    gone_free = sorted(prev_free - set(free_ids)) if prev_free else []

    root = load_yaml(ROOT_CFG)
    jarvis = load_yaml(JARVIS_CFG)
    jarvis_primary = jarvis.get("model") if isinstance(jarvis.get("model"), dict) else {}
    root_fbs = list(root.get("fallback_providers") or [])
    cleaned, removed_or = strip_paid_openrouter(root_fbs)
    promoted, promote_changed = promote_free(cleaned, free_ids, top_n=2)
    config_changed = False
    if removed_or or promote_changed:
        if ROOT_CFG.exists() and write_yaml_fallbacks(ROOT_CFG, promoted):
            config_changed = True
            if removed_or:
                material.append(
                    "MATERIAL paid-OR stripped from root fallbacks: "
                    + ", ".join(r["model"] for r in removed_or)
                )
        else:
            material.append("MATERIAL config write failed for root fallbacks")

    if new_free:
        material.append("MATERIAL new Nous free: " + ", ".join(new_free[:8]))

    prev_cloaked = {
        x.get("id")
        for x in ((prev.get("cloaked_stealth") or {}).get("nous") or [])
        if isinstance(x, dict)
    }
    brand_new_cloaked = [x["id"] for x in cloaked_nous if x["id"] not in prev_cloaked]
    if brand_new_cloaked:
        material.append("MATERIAL new cloaked/stealth Nous: " + ", ".join(brand_new_cloaked[:8]))

    cache_obj = {
        "scan_date": scan_date,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamp_london": ts.strftime("%Y-%m-%d %H:%M %Z"),
        "policy": (
            "Frank 2026-09-07: Nous = :free/$0 OR deepseek-v4-flash price band only. "
            "OpenRouter = :free only. Max plans for expensive quality. No hermes update/money/A3."
        ),
        "sources": {
            "nous_models_url": NOUS_URL,
            "nous_http": nous_code,
            "nous_model_count": len(nous_models),
            "nous_err": nous_err,
            "openrouter_models_url": OR_URL,
            "openrouter_http": or_code,
            "openrouter_model_count": len(or_models),
            "openrouter_err": or_err,
        },
        "free_list": free_nous,
        "free_count": len(free_nous),
        "flash_refs": flash_refs[:20],
        "top_picks": {
            "best_free_large_ctx": free_ids[0] if free_ids else None,
            "top5_free": free_ids[:5],
            "free_fallbacks_recommended": free_ids[:2],
            "flash_band_primary_refs": flash_refs[:6],
            "new_free_vs_prev": new_free,
            "gone_free_vs_prev": gone_free,
        },
        "expensive_blocked_examples_seen": expensive_examples,
        "cloaked_stealth": {
            "nous": cloaked_nous[:30],
            "openrouter_stealth": stealth_or[:20],
            "openrouter_cloaked": cloaked_or[:20],
        },
        "openrouter_crosscheck": {
            "free_count": len(free_or),
            "free_list_top": [x["id"] for x in free_or[:10]],
        },
        "config_action": {
            "root_fallbacks_after": [
                {"provider": x.get("provider"), "model": x.get("model")}
                for x in promoted
                if isinstance(x, dict)
            ][:20],
            "paid_or_removed": removed_or,
            "promote_changed": promote_changed,
            "config_changed": config_changed,
            "jarvis_primary_untouched": {
                "provider": jarvis_primary.get("provider"),
                "default": jarvis_primary.get("default") or jarvis_primary.get("model"),
            },
        },
        "hermes_native": True,
        "script": "nous_daily_catalog_scan.py",
    }

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache_obj, indent=2) + "\n")

    receipt = RECEIPT_DIR / f"NOUS-DAILY-CATALOG-{scan_date}.md"
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# NOUS daily catalog scan — {scan_date}",
        "",
        "**Status:** Isolation-safe ops receipt · no secrets · no hermes update · no money/A3",
        "**Policy:** Frank 2026-09-07 — Nous = `:free`/$0 **OR** `deepseek-v4-flash` price band only; OpenRouter = `:free` only.",
        "**Host:** spark-4be3 (`dgx` as frank)",
        f"**Captured:** {ts.strftime('%Y-%m-%d %H:%M %Z')} ({cache_obj['timestamp_utc']})",
        "**Cache:** `~/.hermes/cache/nous_daily_catalog.json`",
        "**Runner:** Hermes-native no-agent `nous_daily_catalog_scan.py`",
        "",
        "## Sources",
        "",
        "| Source | HTTP | Models |",
        "|---|---:|---:|",
        f"| Nous `{NOUS_URL}` | {nous_code} | {len(nous_models)} |",
        f"| OpenRouter `{OR_URL}` | {or_code} | {len(or_models)} |",
        "",
        f"## Free on Nous today — **{len(free_nous)}**",
        "",
        "| Model id | Context |",
        "|---|---:|",
    ]
    for x in free_nous[:30]:
        lines.append(f"| `{x['id']}` | {x.get('context') or ''} |")
    lines += ["", "### Top 5 free", ""]
    for i, mid in enumerate(free_ids[:5], 1):
        ctx = next((x.get("context") for x in free_nous if x["id"] == mid), "")
        lines.append(f"{i}. `{mid}` — ctx **{ctx}**")
    lines += [
        "",
        "## Config action",
        "",
        f"- paid OpenRouter stripped: `{json.dumps(removed_or)}`",
        f"- promote_changed: `{promote_changed}` config_changed: `{config_changed}`",
        f"- jarvis primary untouched: `{json.dumps(cache_obj['config_action']['jarvis_primary_untouched'])}`",
        "",
        "## Material",
        "",
    ]
    if material:
        for mline in material:
            lines.append(f"- {mline}")
    else:
        lines.append("- (quiet — no material delta)")
    lines.append("")
    receipt.write_text("\n".join(lines) + "\n")

    for mline in material:
        print(mline)
    return 0 if nous_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
