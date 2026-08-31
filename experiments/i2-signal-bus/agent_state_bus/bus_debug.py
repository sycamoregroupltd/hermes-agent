"""Single-shell-command debug view for the I2 agent-state signal bus.

Usage:  python -m agent_state_bus.bus_debug
"""
from __future__ import annotations

import time

from .publisher import STREAM_KEY, connect
from .reader import GROUP, STALE_TTL_S, AgentView, ensure_group
from .schema import AgentStateEvent


def _b2s(v):
    return v.decode() if isinstance(v, bytes) else v


def main():
    r = connect()
    ensure_group(r)

    info = r.xinfo_stream(STREAM_KEY)
    length = info.get(b"length", info.get("length"))
    print(f"=== i2-signal-bus debug === stream={STREAM_KEY} length={length}")

    for g in r.xinfo_groups(STREAM_KEY):
        gname = _b2s(g.get(b"name", g.get("name")))
        consumers = g.get(b"consumers", g.get("consumers"))
        pending_summary = r.xpending(STREAM_KEY, gname)
        print(f"group={gname} consumers={consumers} pending={pending_summary}")

    view = AgentView()
    entries = r.xrevrange(STREAM_KEY, count=500)
    for stream_id, fields in reversed(entries):
        sid = _b2s(stream_id)
        ev = AgentStateEvent.from_fields(sid, fields)
        view.apply(ev)

    now = time.time()
    print(f"--- live view (stale_ttl={STALE_TTL_S}s) ---")
    for agent_id, row in sorted(view.live_view(now).items()):
        print(
            f"  agent={agent_id:28s} status={row['display_status']:20s} "
            f"age={row['age_s']:>6.2f}s task={row.get('task_id')} session={row.get('session_id')}"
        )


if __name__ == "__main__":
    main()
