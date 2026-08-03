#!/usr/bin/env python3
"""
cmux_seat_watch.py — read-only watcher for the live Claude Code seats on DGX.
Tails the 4 most-recently-active Claude transcripts under ~/.claude/projects/-home-frank,
prints ONLY a compact delta when new assistant activity appears. Designed for no_agent
cron use: empty stdout = silence (watchdog pattern), non-empty = delivered verbatim.
No mutation of any shared target.
"""
import json, glob, os

TRANSCRIPT_DIR = "/home/frank/.claude/projects/-home-frank"
STATE = os.path.expanduser("~/.hermes/profiles/jarvis/state/cmux_seat_watch.json")
os.makedirs(os.path.dirname(STATE), exist_ok=True)

def summarize_new(new_lines, cap=6):
    out = []
    for l in new_lines[-60:]:
        l = l.strip()
        if not l:
            continue
        try:
            r = json.loads(l)
        except Exception:
            continue
        msg = r.get("message", {})
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            c = msg.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict):
                        if b.get("type") == "text" and b.get("text"):
                            out.append("◆ " + b["text"].strip().replace("\n", " ")[:200])
                        elif b.get("type") == "tool_use":
                            out.append("⚙ " + str(b.get("name", "")) + " " + str(b.get("input", {}))[:120])
            elif isinstance(c, str) and c.strip():
                out.append("◆ " + c.strip()[:200])
    return out[-cap:]

def main():
    files = sorted(glob.glob(os.path.join(TRANSCRIPT_DIR, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:4]
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
    out = []
    for fp in files:
        name = os.path.basename(fp).replace(".jsonl", "")
        try:
            with open(fp) as f:
                lines = [l for l in f if l.strip()]
        except Exception:
            continue
        n = len(lines)
        prev = state.get(name, 0)
        if n <= prev:
            state[name] = n
            continue
        new = lines[min(prev, n):]
        summary = summarize_new(new)
        if summary:
            mtime = os.path.getmtime(fp)
            import datetime
            ts = datetime.datetime.utcfromtimestamp(mtime).strftime("%H:%M UTC")
            out.append(f"SESSION {name}  (+{n - prev} lines, {ts})")
            out.extend(summary)
        state[name] = n
    json.dump(state, open(STATE, "w"))
    if out:
        print("\n".join(out))

if __name__ == "__main__":
    main()
