#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""local_inference_liveness_probe.py — DGX local-inference liveness probe.

Filed for kanban t_b0c418cd (P1 HOST: local inference DEAD).

Checks, in order:
  1. llama.cpp GPU path: llama-server --list-devices must exit 0 AND report a CUDA
     device. A CUDA-init OOM makes this abort (SIGABRT) — that is the exact failure
     mode this probe exists to catch.
  2. ollama daemon HTTP: GET /api/version must answer.
  3. ollama end-to-end inference: POST /api/generate on a small model must return a
     non-empty response within OLLAMA_TIMEOUT seconds. This is the check that catches
     "daemon answers but no model can actually be served".
  4. GB10 unified-memory headroom: on this platform CUDA free memory tracks host
     MemAvailable, so low host memory == CUDA init OOM. Warn below a threshold.

No-agent cron semantics: QUIET on full success (no stdout). Prints a report and
exits non-zero on any failure, so the cron delivers only when local inference is red.

Env overrides:
  LLAMA_SERVER_BIN   default /home/frank/llama.cpp/build/bin/llama-server
  OLLAMA_HOST_URL    default http://127.0.0.1:11434
  OLLAMA_PROBE_MODEL default llama3.2:3b
  OLLAMA_TIMEOUT     default 180 (seconds; cold model load on GB10 takes ~10-60s)
  MEM_WARN_GIB       default 8  (host MemAvailable floor)
  SWAP_WARN_GIB      default 2  (SwapFree floor — leading indicator, t_9b49cd19)
  STATE_FILE         default /home/frank/.hermes/state/local_inference_liveness.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LLAMA_BIN = os.environ.get("LLAMA_SERVER_BIN", "/home/frank/llama.cpp/build/bin/llama-server")
OLLAMA_URL = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434").rstrip("/")
PROBE_MODEL = os.environ.get("OLLAMA_PROBE_MODEL", "llama3.2:3b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "180"))
MEM_WARN_GIB = float(os.environ.get("MEM_WARN_GIB", "8"))
# Leading indicator: alert while swap is merely under pressure, not once it is
# exhausted. 15Gi/15Gi consumed is what took CUDA offline in t_b0c418cd.
SWAP_WARN_GIB = float(os.environ.get("SWAP_WARN_GIB", "2"))
STATE_FILE = Path(
    os.environ.get("STATE_FILE", "/home/frank/.hermes/state/local_inference_liveness.json")
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mem_available_gib() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return -1.0


def check_llama_cuda() -> dict:
    """llama.cpp must enumerate a CUDA device without aborting."""
    if not Path(LLAMA_BIN).exists():
        return {"ok": False, "detail": f"binary missing: {LLAMA_BIN}"}
    try:
        proc = subprocess.run(
            [LLAMA_BIN, "--list-devices"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "llama-server --list-devices timed out (120s)"}
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        # SIGABRT from ggml_cuda_init OOM shows as negative rc / 'CUDA error'.
        return {
            "ok": False,
            "detail": f"rc={proc.returncode}: {out.strip()[-400:]}",
        }
    if "CUDA" not in out:
        return {"ok": False, "detail": f"no CUDA device enumerated: {out.strip()[-300:]}"}
    line = next((l.strip() for l in out.splitlines() if "CUDA" in l), out.strip()[:200])
    return {"ok": True, "detail": line}


def http_json(url: str, payload: dict | None, timeout: int) -> tuple[bool, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        return False, str(exc)


def check_ollama_daemon() -> dict:
    ok, body = http_json(f"{OLLAMA_URL}/api/version", None, 15)
    if not ok:
        return {"ok": False, "detail": f"/api/version failed: {body}"}
    version = body.get("version", "?") if isinstance(body, dict) else "?"
    return {"ok": True, "detail": f"ollama {version}"}


def check_ollama_inference() -> dict:
    started = time.time()
    ok, body = http_json(
        f"{OLLAMA_URL}/api/generate",
        {"model": PROBE_MODEL, "prompt": "Reply with exactly: OK", "stream": False},
        OLLAMA_TIMEOUT,
    )
    elapsed = round(time.time() - started, 1)
    if not ok:
        return {
            "ok": False,
            "detail": f"/api/generate({PROBE_MODEL}) failed after {elapsed}s: {body}",
        }
    resp = (body.get("response") or "").strip() if isinstance(body, dict) else ""
    if not resp:
        return {
            "ok": False,
            "detail": f"/api/generate({PROBE_MODEL}) returned empty response after {elapsed}s",
        }
    return {"ok": True, "detail": f"{PROBE_MODEL} answered in {elapsed}s: {resp[:60]!r}"}


def check_memory_headroom() -> dict:
    avail = mem_available_gib()
    if avail < 0:
        return {"ok": False, "detail": "could not read /proc/meminfo"}
    # GB10: CUDA free memory tracks host MemAvailable — low host memory IS GPU pressure.
    if avail < MEM_WARN_GIB:
        return {
            "ok": False,
            "detail": f"host MemAvailable {avail:.1f} GiB < {MEM_WARN_GIB} GiB "
            "— GB10 unified memory: CUDA init will OOM",
        }
    return {"ok": True, "detail": f"host MemAvailable {avail:.1f} GiB (== CUDA free on GB10)"}


def swap_stats() -> tuple[float, float]:
    """(SwapFree GiB, SwapTotal GiB). (-1, -1) if unreadable."""
    free = total = -1.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("SwapFree:"):
                free = int(line.split()[1]) / (1024 * 1024)
            elif line.startswith("SwapTotal:"):
                total = int(line.split()[1]) / (1024 * 1024)
    except OSError:
        return -1.0, -1.0
    return free, total


def check_swap_pressure() -> dict:
    """LEADING indicator for the GB10 unified-memory OOM (kanban t_9b49cd19).

    MemAvailable stays healthy while swap silently fills, so the memory-headroom
    check only trips once the outage is already underway. Swap exhaustion is what
    actually denied cudaSetDeviceFlags its pinned allocation in t_b0c418cd, so
    SwapFree must be alerted on BEFORE it reaches zero, not after.
    """
    free, total = swap_stats()
    if free < 0 or total < 0:
        return {"ok": False, "detail": "could not read swap stats from /proc/meminfo"}
    if total == 0:
        return {"ok": True, "detail": "no swap configured"}
    used_pct = 100.0 * (total - free) / total
    if free < SWAP_WARN_GIB:
        return {
            "ok": False,
            "detail": f"SwapFree {free:.2f} GiB of {total:.1f} GiB "
            f"({used_pct:.0f}% used) < {SWAP_WARN_GIB} GiB floor — swap exhaustion "
            "precedes CUDA-init OOM on GB10. Check for orphan agent-browser "
            "Chromium roots first: ~/.hermes/scripts/orphan_agent_browser_reaper.py "
            "(DRY_RUN=1 to inspect). Runbook: kanban t_9b49cd19.",
        }
    return {
        "ok": True,
        "detail": f"SwapFree {free:.2f} GiB of {total:.1f} GiB ({used_pct:.0f}% used)",
    }


CHECKS = [
    ("llama_cpp_cuda", check_llama_cuda),
    ("ollama_daemon", check_ollama_daemon),
    ("ollama_inference", check_ollama_inference),
    ("unified_memory_headroom", check_memory_headroom),
    ("swap_pressure", check_swap_pressure),
]


def main() -> int:
    results = {}
    for name, fn in CHECKS:
        try:
            results[name] = fn()
        except Exception as exc:  # never let the probe itself vanish silently
            results[name] = {"ok": False, "detail": f"probe error: {exc!r}"}

    failed = [n for n, r in results.items() if not r["ok"]]
    state = {
        "checked_at": now_iso(),
        "status": "red" if failed else "green",
        "failed": failed,
        "results": results,
    }
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        print(f"WARN: could not write state file {STATE_FILE}: {exc}", file=sys.stderr)

    if not failed:
        return 0  # quiet on success

    print("LOCAL INFERENCE RED — DGX local model serving is degraded or dead")
    print(f"checked_at: {state['checked_at']}")
    for name, r in results.items():
        print(f"  [{'ok ' if r['ok'] else 'FAIL'}] {name}: {r['detail']}")
    print("")
    print("Runbook: kanban t_b0c418cd. On GB10 the GPU shares host memory — a CUDA")
    print("init OOM means host memory/swap pressure, not model size. Check top RSS")
    print("and swap consumers before touching the llama.cpp build (the build is fine).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
