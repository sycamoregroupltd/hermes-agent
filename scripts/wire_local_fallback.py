#!/usr/bin/env python3
"""Fleet sweep: add the local llama.cpp gpt-oss-120b rung to every profile's
fallback chain (Frank-approved 2026-07-05). Text surgery — preserves comments
and formatting; yaml.safe_load used only to VALIDATE after edit.

- Insert local rung after the nous/deepseek-v4-flash rung (else at list head).
- Remove known-dead rungs (groq 401 key; nous gemini-3-pro-preview 404).
- Ensure custom_providers entry llamacpp-local exists (holds base_url/api_key).
- Backup <file>.bak-localfallback-20260705; restore on validation failure.
DRY_RUN=1 prints planned changes without writing.
"""
import os, glob, shutil, yaml

BASE_URL = "http://127.0.0.1:8098/v1"
DRY = os.environ.get("DRY_RUN") == "1"

def split_items(lines):
    """Split list-item lines into item groups (each starts at '- ')."""
    items, cur = [], []
    for ln in lines:
        if ln.lstrip().startswith("- "):
            if cur:
                items.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        items.append(cur)
    return items

def wire(path):
    src = open(path).read()
    if BASE_URL in src.split("custom_providers")[0] and "fallback_providers" in src:
        pass  # cheap pre-check not reliable; do full check below
    lines = src.split("\n")
    # locate fallback_providers block
    try:
        fb_i = next(i for i, l in enumerate(lines) if l.rstrip() == "fallback_providers:")
    except StopIteration:
        return f"SKIP {path}: no fallback_providers block"
    j = fb_i + 1
    while j < len(lines) and (lines[j].startswith(" ") or lines[j].startswith("-")) and lines[j].strip():
        # stop when a new top-level key starts
        if not lines[j].startswith(" ") and not lines[j].lstrip().startswith("- "):
            break
        j += 1
    block = lines[fb_i + 1 : j]
    if not block:
        return f"SKIP {path}: empty fallback block"
    items = split_items(block)
    if any(BASE_URL in "\n".join(it) for it in items):
        return f"OK   {path}: already wired"
    indent = items[0][0][: len(items[0][0]) - len(items[0][0].lstrip())]
    sub = indent + "  "
    removed = 0
    kept = []
    for it in items:
        t = "\n".join(it)
        if "api.groq.com/openai/v1" in t or "gemini-3-pro-preview" in t:
            removed += 1
        else:
            kept.append(it)
    rung = [indent + "- provider: custom",
            sub + "model: gpt-oss-120b",
            sub + f"base_url: {BASE_URL}"]
    pos = 0
    for k, it in enumerate(kept):
        if "deepseek-v4-flash" in "\n".join(it):
            pos = k + 1
            break
    kept.insert(pos, rung)
    new_block = [l for it in kept for l in it]
    lines[fb_i + 1 : j] = new_block
    out = "\n".join(lines)
    # custom_providers entry
    if BASE_URL not in out.split("fallback_providers")[0] or True:
        cp_lines = None
        if any(l.rstrip() == "custom_providers:" for l in out.split("\n")):
            ol = out.split("\n")
            ci = next(i for i, l in enumerate(ol) if l.rstrip() == "custom_providers:")
            # find existing item indent
            k = ci + 1
            ind = "  - " if ol[k].lstrip().startswith("- ") and ol[k].startswith("  ") else "- "
            base = "    " if ind == "  - " else "  "
            entry = [ind.replace("- ", "- name: llamacpp-local"),
                     base + f"base_url: {BASE_URL}",
                     base + "api_key: sk-local-no-auth",
                     base + "model: gpt-oss-120b"]
            # only add if not present anywhere
            if BASE_URL not in "\n".join(ol[ci:ci + 40]):
                ol[ci + 1 : ci + 1] = entry
                out = "\n".join(ol)
        else:
            out = out.rstrip("\n") + ("\ncustom_providers:\n"
                 "  - name: llamacpp-local\n"
                 f"    base_url: {BASE_URL}\n"
                 "    api_key: sk-local-no-auth\n"
                 "    model: gpt-oss-120b\n")
    msg = f"WIRE {path}: rung@{pos}" + (f", removed {removed} dead" if removed else "")
    if DRY:
        return "DRY " + msg
    bak = path + ".bak-localfallback-20260705"
    shutil.copy2(path, bak)
    open(path, "w").write(out)
    try:
        cfg = yaml.safe_load(open(path))
        fbs = cfg.get("fallback_providers") or []
        assert any(r.get("base_url") == BASE_URL for r in fbs if isinstance(r, dict)), "rung missing after edit"
        cps = cfg.get("custom_providers") or []
        assert any(c.get("base_url") == BASE_URL for c in cps if isinstance(c, dict)), "custom entry missing"
    except Exception as e:
        shutil.copy2(bak, path)
        return f"FAIL {path}: {e} (restored)"
    return msg

_seen_real = set()
paths = ["/home/frank/.hermes/config.yaml"] + [
    rp for _p in sorted(glob.glob("/home/frank/.hermes/profiles/*/config.yaml"))
    for rp in [os.path.realpath(_p)] if not (rp in _seen_real or _seen_real.add(rp))
]
for p in paths:
    try:
        print(wire(p))
    except Exception as e:
        print(f"ERR  {p}: {e}")
