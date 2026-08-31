"""Wrap a REAL `hermes -z` oneshot agent run, publishing idle -> working ->
done/failed to the I2 signal bus. With --simulate-crash-after, the wrapper
hard-kills its own process mid-"working" (no done event, no clean shutdown)
to prove the reader's staleness handling is a genuine writer-death case,
not a scripted graceful exit.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import threading
import time
import uuid

from .publisher import AgentStateBus
from .schema import AgentStateEvent

HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")

DEFAULT_PROMPT = (
    "You are a disposable test agent for an internal signal-bus proof. "
    "Do not call any tools. Do not read, write, or modify any files. "
    "Do not run any commands. Output exactly the single word: done"
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--task-id", default="t_dc046875-i2-probe")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--model", default="gpt-5.6-luna")
    ap.add_argument("--provider", default="openai-codex")
    ap.add_argument("--heartbeat-interval", type=float, default=1.5)
    ap.add_argument(
        "--simulate-crash-after",
        type=float,
        default=None,
        help="seconds after publishing 'working' to hard-kill this process (staleness test)",
    )
    args = ap.parse_args()

    session_id = f"i2demo-{uuid.uuid4().hex[:8]}"
    bus = AgentStateBus()

    bus.publish(
        AgentStateEvent(
            event_type="idle",
            agent_id=args.agent_id,
            session_id=session_id,
            task_id=args.task_id,
            data={"phase": "pre-spawn"},
        )
    )

    usage_file = f"/tmp/i2-usage-{session_id}.json"
    cmd = [
        HERMES_BIN,
        "-z",
        args.prompt,
        "-m",
        args.model,
        "--provider",
        args.provider,
        "--usage-file",
        usage_file,
        "--safe-mode",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    bus.publish(
        AgentStateEvent(
            event_type="working",
            agent_id=args.agent_id,
            session_id=session_id,
            task_id=args.task_id,
            data={"pid": proc.pid, "model": args.model, "provider": args.provider},
        )
    )

    stop = threading.Event()

    def heartbeat_loop():
        while not stop.wait(args.heartbeat_interval):
            bus.publish(
                AgentStateEvent(
                    event_type="heartbeat",
                    agent_id=args.agent_id,
                    session_id=session_id,
                    task_id=args.task_id,
                    data={"pid": proc.pid},
                )
            )

    hb = threading.Thread(target=heartbeat_loop, daemon=True)
    hb.start()

    if args.simulate_crash_after is not None:
        time.sleep(args.simulate_crash_after)
        print(f"[run_real_agent] simulating hard writer death for agent={args.agent_id} now", flush=True)
        os.kill(os.getpid(), 9)  # no cleanup, no done event: genuine writer death

    out, _ = proc.communicate()
    stop.set()
    hb.join(timeout=2)

    status = "done" if proc.returncode == 0 else "failed"
    bus.publish(
        AgentStateEvent(
            event_type=status,
            agent_id=args.agent_id,
            session_id=session_id,
            task_id=args.task_id,
            data={"returncode": proc.returncode, "output_tail": (out or "")[-300:]},
        )
    )
    print(f"[run_real_agent] agent={args.agent_id} session={session_id} status={status} rc={proc.returncode}")
    print((out or "")[-500:])


if __name__ == "__main__":
    main()
