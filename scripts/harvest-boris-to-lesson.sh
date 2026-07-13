#!/usr/bin/env bash
# harvest-boris-to-lesson.sh — tail step of dgx_boris_reflection.py (or its own daily cron).
#
# Reads each PM's BORIS-EVIDENCE-QUEUE.md. For every entry that has a Root cause
# + a Smallest preventive rule + a Verification (i.e. a verified, reusable lesson),
# emits a canonical L-<YYYYMMDD>-<slug>.md into ~/.hermes/shared-memory/lessons/
# and appends a corresponding line to INDEX.jsonl.
#
# Idempotent: re-running over the same queue skips entries whose dedup_key already
# exists (non-superseded). Genuine re-emission with changed content supersedes the
# prior lesson (sets supersedes:[old], flips old propagation_state->superseded).
#
# Reversible: only CREATES lesson files + appends/updates INDEX.jsonl. Never deletes
# a boris queue or rewrites it destructively.
#
# Usage:
#   harvest-boris-to-lesson.sh [queue_path ...]
#   BORIS_QUEUES="/path/a.md /path/b.md" harvest-boris-to-lesson.sh
set -uo pipefail

LESSONS_DIR="${LESSONS_DIR:-/home/frank/.hermes/shared-memory/lessons}"
INDEX="${INDEX:-${LESSONS_DIR}/INDEX.jsonl}"
mkdir -p "${LESSONS_DIR}"

# Default queue set: the jarvis profile-local queue discovered by dgx_boris_reflection.py.
# Extend by passing queue paths as args or setting BORIS_QUEUES.
if [ "$#" -gt 0 ]; then
  QUEUES=("$@")
else
  DEFAULT_QUEUE="/home/frank/.hermes/profiles/jarvis/BORIS-EVIDENCE-QUEUE.md"
  if [ -n "${BORIS_QUEUES:-}" ]; then
    read -r -a QUEUES <<< "${BORIS_QUEUES}"
  elif [ -f "${DEFAULT_QUEUE}" ]; then
    QUEUES=("${DEFAULT_QUEUE}")
  else
    QUEUES=()
  fi
fi

# Filter to existing files.
EXISTING=()
for q in "${QUEUES[@]:-}"; do
  [ -f "${q}" ] && EXISTING+=("${q}")
done

if [ "${#EXISTING[@]}" -eq 0 ]; then
  echo "harvest: no BORIS-EVIDENCE-QUEUE.md files found to process" >&2
  exit 0
fi

python3 - "${EXISTING[@]}" <<'PYEOF'
import sys, os, re, json, hashlib
from datetime import datetime, timezone

queues = sys.argv[1:]
LESSONS_DIR = os.environ.get("LESSONS_DIR", "/home/frank/.hermes/shared-memory/lessons")
INDEX = os.environ.get("INDEX", os.path.join(LESSONS_DIR, "INDEX.jsonl"))
os.makedirs(LESSONS_DIR, exist_ok=True)

# ---- owner PM per queue path ------------------------------------------------
def owner_pm_for(path):
    p = path.lower()
    if "sycode-trading" in p: return "sycode-trading-pm", "sycode-trading"
    if "sycode-ai" in p:      return "sycode-ai-pm", "sycode-ai"
    if "upero" in p:          return "upero-pm", "upero"
    return "jarvis-os-pm", "jarvis-os"   # default

BOARD_PM = {
    "jarvis-os": "jarvis-os-pm", "sycode-trading": "sycode-trading-pm",
    "upero": "upero-pm", "sycode-ai": "sycode-ai-pm",
}
KW_TAGS = {
    "429": "provider-rate-limit", "rate limit": "provider-rate-limit",
    "rate-limit": "provider-rate-limit", "free-tier": "free-tier",
    "free tier": "free-tier", "cron": "cron", "backoff": "backoff",
    "retry": "retry", "kanban": "kanban", "schema": "schema-mismatch",
    "corrupt": "corrupt-db", "credential": "credentials", "gateway": "gateway",
    "model": "model-routing",
}

def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()

def dedup_hash(rc, rule):
    return hashlib.sha256((norm(rc) + "|" + norm(rule)).encode()).hexdigest()[:16]

def slugify(title, maxlen=8):
    toks = re.findall(r"[a-z0-9]+", title.lower())
    slug = "-".join(toks[:maxlen])[:48]
    return slug or "lesson"

def parse_date(header):
    m = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})?Z?)?", header)
    if not m:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    d, t = m.group(1), m.group(2)
    return f"{d}T{(t or '00:00')}Z"

FIELDS = ["Root cause", "Failure impact", "Smallest preventive rule", "Action taken",
          "Verification of this entry", "Verification", "Sources checked",
          "Evidence preserved", "Next signal", "Picked error", "confidence"]

def parse_entry(block):
    """Return dict of field->text for a single entry block."""
    fields = {f: [] for f in FIELDS}
    cur = None
    for line in block.splitlines():
        hit = None
        for f in FIELDS:
            # marker like "**Root cause**:" or "- **Root cause**" (optional colon)
            if re.match(rf"^\s*(?:[-*]\s*)?\*\*{re.escape(f)}\*\*\s*:?", line, re.I):
                hit = f
                # capture inline text after the marker
                after = re.sub(rf"^\s*(?:[-*]\s*)?\*\*{re.escape(f)}\*\*\s*:?\s*", "", line, flags=re.I)
                if after.strip():
                    fields[f].append(after.strip())
                break
        if hit:
            cur = hit
            continue
        if cur and line.strip():
            fields[cur].append(line.strip())
    return {f: " ".join(v).strip() for f, v in fields.items()}

def load_index():
    entries = []
    if os.path.exists(INDEX):
        with open(INDEX) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try: entries.append(json.loads(line))
                    except json.JSONDecodeError: pass
    return entries

def save_index(entries):
    with open(INDEX, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

def harvestable(f):
    verif = f["Verification"] or f["Verification of this entry"]
    return f["Root cause"] and f["Smallest preventive rule"] and verif

emitted = 0
for q in queues:
    owner_pm, owner_board = owner_pm_for(q)
    text = open(q, encoding="utf-8", errors="replace").read()
    # split into entries on level 2-4 markdown headers
    parts = re.split(r"^#{2,4}\s+", text, flags=re.M)
    entries = load_index()
    by_dedup = {e.get("dedup_key"): e for e in entries if e.get("dedup_key") and e.get("propagation_state") != "superseded"}
    for part in parts:
        header = part.splitlines()[0] if part.splitlines() else ""
        f = parse_entry(part)
        if not harvestable(f):
            continue
        rc, rule, verif = f["Root cause"], f["Smallest preventive rule"], f["Verification"]
        conf = (f.get("confidence") or "").strip().lower()
        confidence = conf if conf in ("verified", "proposed", "report-only") else "verified"
        created_at = parse_date(header)
        d = dedup_hash(rc, rule)
        verif = f["Verification"] or f["Verification of this entry"]

        # relevant_to: owner + any referenced boards
        low = part.lower()
        refs = []
        for b in BOARD_PM:
            if re.search(rf"\b{re.escape(b)}\b", low):
                refs.append(BOARD_PM[b])
        relevant_to = []
        for pm in [owner_pm] + refs:
            if pm not in relevant_to:
                relevant_to.append(pm)

        # tags: referenced boards + keyword buckets + any #hashtags
        tags = []
        for b in BOARD_PM:
            if re.search(rf"\b{re.escape(b)}\b", low):
                tags.append(b)
        for kw, tag in KW_TAGS.items():
            if kw in low and tag not in tags:
                tags.append(tag)
        tags += re.findall(r"#([a-z0-9\-_]+)", low)
        tags = list(dict.fromkeys(tags))

        title = re.sub(r"^[\d\sZ:\-]+", "", header).strip(" —-")
        title = title or f["Picked error"][:80] or "untitled lesson"
        sdate = created_at[:10].replace("-", "")
        lid = f"L-{sdate}-{slugify(title)}"

        # ---- dedup / supersede / conflict ---------------------------------
        if d in by_dedup:
            existing = by_dedup[d]
            # True idempotency: same lesson_id (same day+slug) and still 'new'/'landed'
            # means an unchanged re-emission -> skip (no churn).
            if existing.get("lesson_id") == lid and existing.get("propagation_state") in ("new", "landed", "fanned-out"):
                continue
            # Otherwise same dedup_key but different id -> genuine supersede of the prior.
            # (fall through to supersede handling below)
        # conflict: same root-cause hash but clearly different rule?
        rc_hash = hashlib.sha256(norm(rc).encode()).hexdigest()[:16]
        conflicts_with = None
        for e in entries:
            if e.get("rc_hash") == rc_hash and e.get("dedup_key") != d and e.get("propagation_state") != "superseded":
                conflicts_with = e.get("lesson_id")
                break

        # supersede handling
        supersedes = []
        for i, e in enumerate(entries):
            if e.get("dedup_key") == d and e.get("propagation_state") != "superseded":
                supersedes.append(e.get("lesson_id"))
                entries[i]["propagation_state"] = "superseded"
                entries[i]["superseded_by"] = lid

        lid_path = os.path.join(LESSONS_DIR, lid + ".md")
        rec = {
            "lesson_id": lid, "title": title,
            "type": "failure-prevention", "harness_primitive": "verification_gate",
            "source_pm": owner_pm, "source_board": owner_board,
            "source_evidence": [f"{q}#{slugify(header)}"],
            "tags": tags, "relevant_to": relevant_to, "confidence": confidence,
            "propagation_state": "new", "supersedes": supersedes,
            "created_at": created_at, "landed_in": {}, "path": lid_path,
            "root_cause": rc, "rule": rule, "dedup_key": d, "rc_hash": rc_hash,
        }
        if conflicts_with:
            rec["confidence"] = "proposed"
            rec["conflicts_with"] = conflicts_with

        # write the markdown lesson file
        body = f"""---
lesson_id: {lid}
title: "{title}"
type: failure-prevention
harness_primitive: verification_gate
source_pm: {owner_pm}
source_board: {owner_board}
source_evidence:
  - {q}#{slugify(header)}
tags: [{", ".join(tags)}]
relevant_to: [{", ".join(relevant_to)}]
confidence: {rec['confidence']}
propagation_state: new
supersedes: [{", ".join(supersedes)}]
created_at: {created_at}
landed_in: {{}}
---

## Root cause
{rc}

## Smallest preventive rule
{rule}

## Sources checked
{f['Sources checked'] or '(see source evidence)'}

## Evidence preserved
{f['Evidence preserved'] or '(see source evidence)'}

## Verification
{verif}

## Next signal
{f['Next signal'] or '(none recorded)'}
"""
        with open(lid_path, "w") as fh:
            fh.write(body)
        # remove any prior record with same id, then append
        entries = [e for e in entries if e.get("lesson_id") != lid]
        entries.append(rec)
        save_index(entries)
        by_dedup[d] = rec
        emitted += 1
        print(f"harvested: {lid} (pm={owner_pm}, relevant_to={relevant_to}, tags={tags})")

print(f"\nharvest complete: {emitted} lesson(s) emitted/updated across {len(queues)} queue(s)")
PYEOF
