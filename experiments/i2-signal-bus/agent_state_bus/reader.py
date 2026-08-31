"""Reader side of the I2 agent-state signal bus.

Consumer-group based (ack after processing), with a staleness override:
a per-agent live view marks a non-terminal state STALE once no event
(including heartbeats) has been seen for STALE_TTL_S seconds, regardless
of what the last real status was. Terminal states (done/failed) are never
marked stale.

Debuggable with one shell command: `python -m agent_state_bus.bus_debug`.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

from .publisher import STREAM_KEY, connect
from .schema import AgentStateEvent

GROUP = os.environ.get("I2_BUS_GROUP", "state-readers")
STALE_TTL_S = float(os.environ.get("I2_BUS_STALE_TTL_S", "6"))


def ensure_group(r, stream_key=STREAM_KEY, group=GROUP):
    try:
        r.xgroup_create(stream_key, group, id="0", mkstream=True)
    except Exception as e:  # noqa: BLE001 - redis raises a plain ResponseError
        if "BUSYGROUP" not in str(e):
            raise


class AgentView:
    """Latest-known-state-per-agent, with a staleness override."""

    def __init__(self):
        self.state: dict[str, dict] = {}

    def apply(self, ev: AgentStateEvent):
        row = self.state.setdefault(ev.agent_id, {})
        if ev.event_type in ("idle", "working", "done", "failed"):
            row["status"] = ev.event_type
            row["status_at"] = ev.occurred_at
        row["last_seen_at"] = ev.occurred_at
        row["session_id"] = ev.session_id
        row["task_id"] = ev.task_id
        row["last_stream_id"] = getattr(ev, "stream_id", None)

    def live_view(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        out = {}
        for agent_id, row in self.state.items():
            status = row.get("status", "unknown")
            terminal = status in ("done", "failed")
            age = now - row.get("last_seen_at", now)
            stale = (not terminal) and age > STALE_TTL_S
            display = f"STALE(last={status})" if stale else status
            out[agent_id] = {**row, "display_status": display, "stale": stale, "age_s": round(age, 2)}
        return out


def _fmt(ev: AgentStateEvent) -> str:
    ts = datetime.fromtimestamp(ev.occurred_at, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    return f"{ts} agent={ev.agent_id} type={ev.event_type} data={ev.data}"


def read_batch(consumer_name, stream_key=STREAM_KEY, group=GROUP, ack=True,
                from_cursor="0", block_ms=1000, count=50):
    """One-shot read: '0' replays this consumer's still-pending (unacked)
    entries; '>' reads new entries. Returns the list of AgentStateEvent."""
    r = connect()
    ensure_group(r, stream_key, group)
    resp = r.xreadgroup(group, consumer_name, {stream_key: from_cursor}, count=count, block=block_ms)
    events = []
    if resp:
        for _key, entries in resp:
            for stream_id, fields in entries:
                sid = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
                ev = AgentStateEvent.from_fields(sid, fields)
                events.append(ev)
                if ack:
                    r.xack(stream_key, group, stream_id)
    return events


def watch(consumer_name: str, no_ack: bool = False, poll_block_ms: int = 1000):
    """Long-running reader: prints every live transition plus a periodic
    live-view snapshot (so staleness becomes visible even with no new events)."""
    r = connect()
    ensure_group(r)
    view = AgentView()

    # Replay pass first: anything left pending (unacked) for this consumer name.
    for ev in read_batch(consumer_name, ack=not no_ack, from_cursor="0", block_ms=100, count=200):
        view.apply(ev)
        print(f"[REPLAY] {_fmt(ev)}", flush=True)

    last_snapshot = 0.0
    while True:
        r2 = connect()
        ensure_group(r2)
        resp = r2.xreadgroup(GROUP, consumer_name, {STREAM_KEY: ">"}, count=10, block=poll_block_ms)
        if resp:
            for _key, entries in resp:
                for stream_id, fields in entries:
                    sid = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
                    ev = AgentStateEvent.from_fields(sid, fields)
                    view.apply(ev)
                    if not no_ack:
                        r2.xack(STREAM_KEY, GROUP, stream_id)
                    print(f"[LIVE]   {_fmt(ev)}", flush=True)
        now = time.time()
        if now - last_snapshot > 2:
            last_snapshot = now
            for agent_id, row in view.live_view(now).items():
                print(f"  VIEW agent={agent_id} status={row['display_status']} age={row['age_s']}s", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--consumer", default="reader-1")
    ap.add_argument("--no-ack", action="store_true")
    args = ap.parse_args()
    watch(args.consumer, no_ack=args.no_ack)
