#!/home/frank/ml-venv/bin/python
"""Governed on-demand JupyterLab controller for Sycode analysis.

Security posture:
- binds only 127.0.0.1
- uses a per-start random token kept only in a 0600 runtime state file
- avoids the stale ~/.jupyter all-interface/static-token config by setting isolated
  JUPYTER_CONFIG_DIR/JUPYTER_RUNTIME_DIR/JUPYTER_DATA_DIR
- scrubs credential/trading/database environment variables before launching kernels
- uses /home/frank/sycode-trading/tools/notebooks as the canonical notebook root
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/frank/sycode-trading/tools/notebooks")
VENV = Path("/home/frank/ml-venv")
JUPYTER = VENV / "bin" / "jupyter"
RUNTIME_BASE = Path(os.environ.get("SYCODE_JUPYTER_RUNTIME", f"/tmp/sycode-jupyter-workbench-{os.getuid()}"))
STATE = RUNTIME_BASE / "state.json"
HOST = "127.0.0.1"
DEFAULT_PORT = 8888
SENSITIVE_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "API_KEY",
    "PRIVATE_KEY",
    "DATABASE_URL",
    "SUPABASE",
    "HYPERLIQUID",
    "BINANCE",
    "COINALYZE",
    "ANTHROPIC",
    "OPENAI",
    "GITHUB",
    "TWILIO",
    "ELEVENLABS",
    "XAI",
)


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    if not ROOT.exists():
        raise SystemExit(f"canonical notebook root missing: {ROOT}")
    RUNTIME_BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ["config", "runtime", "data", "home", "logs"]:
        (RUNTIME_BASE / name).mkdir(mode=0o700, exist_ok=True)


def read_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def write_state(data: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.25)
        return s.connect_ex((HOST, port)) == 0


def wait_health(port: int, token: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if api_request(port, token, "GET", "/api/status", tolerate=True) is not None:
            return True
        time.sleep(0.5)
    return False


def scrubbed_env() -> dict:
    keep = {}
    for k, v in os.environ.items():
        upper = k.upper()
        if any(marker in upper for marker in SENSITIVE_MARKERS):
            continue
        if upper in {"PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGPORT"}:
            continue
        keep[k] = v
    keep.update(
        {
            "HOME": str(RUNTIME_BASE / "home"),
            "JUPYTER_CONFIG_DIR": str(RUNTIME_BASE / "config"),
            "JUPYTER_RUNTIME_DIR": str(RUNTIME_BASE / "runtime"),
            "JUPYTER_DATA_DIR": str(RUNTIME_BASE / "data"),
            "SYCODE_JUPYTER_GOVERNED": "1",
            "PYTHONNOUSERSITE": "1",
            "PATH": f"{VENV / 'bin'}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
    )
    return keep


def start(args: argparse.Namespace) -> int:
    ensure_dirs()
    state = read_state()
    if state.get("pid") and pid_alive(int(state["pid"])):
        print(json.dumps({"status": "already-running", "pid": state["pid"], "url": public_url(state)}, indent=2))
        return 0
    port = args.port
    if port_open(port):
        raise SystemExit(f"refusing to start: {HOST}:{port} already has a listener")
    token = secrets.token_urlsafe(32)
    log_path = RUNTIME_BASE / "logs" / f"jupyter-{int(time.time())}.log"
    # Keep the token out of argv/proc listings. This transient config lives under
    # a 0700 runtime dir and is deleted by stop(); it is not the stale static
    # ~/.jupyter token config this controller intentionally bypasses.
    config_path = RUNTIME_BASE / "config" / "jupyter_server_config.py"
    config_path.write_text(
        "\n".join(
            [
                f"c.ServerApp.ip = {HOST!r}",
                f"c.ServerApp.port = {port!r}",
                f"c.ServerApp.root_dir = {str(ROOT)!r}",
                "c.ServerApp.allow_remote_access = False",
                "c.ServerApp.open_browser = False",
                "c.ServerApp.terminals_enabled = False",
                "c.ServerApp.allow_origin = ''",
                "c.ServerApp.password = ''",
                "c.ServerApp.jpserver_extensions = {'jupyter_server_terminals': False}",
                f"c.IdentityProvider.token = {token!r}",
                "",
            ]
        )
    )
    os.chmod(config_path, 0o600)
    cmd = [str(JUPYTER), "lab", "--no-browser", f"--config={config_path}"]
    with log_path.open("ab", buffering=0) as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    data = {
        "pid": proc.pid,
        "port": port,
        "host": HOST,
        "root_dir": str(ROOT),
        "runtime_base": str(RUNTIME_BASE),
        "token": token,
        "config_path": str(config_path),
        "log_path": str(log_path),
        "started_at": utc(),
        "env_scrubbed_markers": list(SENSITIVE_MARKERS),
    }
    write_state(data)
    if not wait_health(port, token, timeout=args.timeout):
        print(json.dumps({"status": "start-failed-health", "pid": proc.pid, "log_path": str(log_path)}, indent=2))
        return 2
    print(json.dumps({"status": "started", "pid": proc.pid, "url": public_url(data), "root_dir": str(ROOT), "log_path": str(log_path)}, indent=2))
    return 0


def public_url(state: dict) -> str:
    return f"http://{state.get('host', HOST)}:{state.get('port', DEFAULT_PORT)}/lab"


def api_request(port: int, token: str, method: str, path: str, payload: dict | None = None, tolerate: bool = False):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{HOST}:{port}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode())
    except Exception:
        if tolerate:
            return None
        raise


def health(args: argparse.Namespace) -> int:
    state = read_state()
    pid = int(state.get("pid") or 0)
    port = int(state.get("port") or DEFAULT_PORT)
    token = state.get("token")
    status = {"pid_alive": bool(pid and pid_alive(pid)), "port": port, "listener_open": port_open(port), "root_dir": state.get("root_dir")}
    if token and status["listener_open"]:
        status["api_status"] = api_request(port, token, "GET", "/api/status", tolerate=True)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status.get("pid_alive") and status.get("api_status") is not None else 1


def smoke(args: argparse.Namespace) -> int:
    state = read_state()
    if not state:
        raise SystemExit("not running")
    pid = int(state.get("pid") or 0)
    if not pid_alive(pid):
        raise SystemExit("state pid is not alive")
    port = int(state["port"])
    token = state["token"]
    notebook_rel = f"_governed_smoke/smoke-{int(time.time())}.ipynb"
    session = api_request(
        port,
        token,
        "POST",
        "/api/sessions",
        {"path": notebook_rel, "type": "notebook", "name": Path(notebook_rel).name, "kernel": {"name": "python3"}},
    )
    kernel_id = session["kernel"]["id"]
    conn = RUNTIME_BASE / "runtime" / f"kernel-{kernel_id}.json"
    deadline = time.time() + 15
    while time.time() < deadline and not conn.exists():
        time.sleep(0.2)
    if not conn.exists():
        raise SystemExit(f"kernel connection file not found for {kernel_id}")
    from jupyter_client import BlockingKernelClient

    kc = BlockingKernelClient(connection_file=str(conn))
    kc.load_connection_file()
    kc.start_channels()
    code = r'''
import json, os, platform, subprocess, sys
from pathlib import Path
import pandas as pd
cpu_total = sum(i*i for i in range(1000))
df = pd.DataFrame({'a':[1,2,3], 'b':[10,20,30]})
gpu = subprocess.run(['nvidia-smi','--query-gpu=name,memory.total','--format=csv,noheader'], capture_output=True, text=True, timeout=10)
artifact = {
    'status': 'ok',
    'python': sys.version.split()[0],
    'platform': platform.platform(),
    'cpu_total': cpu_total,
    'df_rows': int(len(df)),
    'df_b_sum': int(df['b'].sum()),
    'gpu_rc': gpu.returncode,
    'gpu_lines': [line.strip() for line in gpu.stdout.splitlines()[:4]],
    'danger_env_present': sorted([k for k in os.environ if any(m in k.upper() for m in ['TOKEN','SECRET','PASSWORD','DATABASE_URL','API_KEY','PRIVATE_KEY','HYPERLIQUID','BINANCE','SUPABASE'])])[:20],
}
print(json.dumps(artifact, sort_keys=True))
'''
    msg_id = kc.execute(code, store_history=False)
    outputs = []
    ok = False
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        msg = kc.get_iopub_msg(timeout=5)
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        typ = msg["msg_type"]
        content = msg["content"]
        if typ == "stream":
            outputs.append(content.get("text", ""))
        elif typ == "error":
            outputs.append("ERROR:" + "\n".join(content.get("traceback", [])))
            break
        elif typ == "status" and content.get("execution_state") == "idle":
            ok = True
            break
    kc.stop_channels()
    api_request(port, token, "DELETE", f"/api/sessions/{session['id']}", tolerate=True)
    text = "".join(outputs).strip()
    parsed = json.loads(text.splitlines()[-1]) if text else {"status": "no-output"}
    report = {
        "status": "ok" if ok and parsed.get("status") == "ok" else "failed",
        "created_session": True,
        "kernel_id": kernel_id,
        "notebook_path": notebook_rel,
        "root_dir": str(ROOT),
        "artifact": parsed,
        "checked_at": utc(),
    }
    out_path = Path(args.output).expanduser() if args.output else RUNTIME_BASE / "smoke-output.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    os.chmod(out_path, 0o600)
    print(json.dumps({"status": report["status"], "artifact_path": str(out_path), "df_rows": parsed.get("df_rows"), "gpu_rc": parsed.get("gpu_rc"), "danger_env_present_count": len(parsed.get("danger_env_present", []))}, indent=2))
    return 0 if report["status"] == "ok" and len(parsed.get("danger_env_present", [])) == 0 else 2


def stop(args: argparse.Namespace) -> int:
    state = read_state()
    pid = int(state.get("pid") or 0)
    stopped = False
    if pid and pid_alive(pid):
        os.killpg(pid, signal.SIGTERM)
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not pid_alive(pid):
                stopped = True
                break
            time.sleep(0.25)
        if pid_alive(pid):
            os.killpg(pid, signal.SIGKILL)
        stopped = True
    token = state.pop("token", None)
    state["stopped_at"] = utc()
    state["token_removed_from_state"] = bool(token)
    write_state(state)
    # Remove transient token-bearing state/config after recording stop evidence.
    config_path = state.get("config_path")
    if config_path:
        try:
            Path(config_path).unlink()
        except FileNotFoundError:
            pass
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass
    print(json.dumps({"status": "stopped" if stopped else "not-running", "pid": pid, "listener_open_after_stop": port_open(int(state.get("port") or DEFAULT_PORT))}, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    ps = sub.add_parser("start")
    ps.add_argument("--port", type=int, default=DEFAULT_PORT)
    ps.add_argument("--timeout", type=float, default=30)
    sub.add_parser("health")
    sm = sub.add_parser("smoke")
    sm.add_argument("--output")
    sm.add_argument("--timeout", type=float, default=60)
    st = sub.add_parser("stop")
    st.add_argument("--timeout", type=float, default=10)
    args = p.parse_args()
    return globals()[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
