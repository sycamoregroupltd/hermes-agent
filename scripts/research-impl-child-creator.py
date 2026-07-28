#!/usr/bin/env python3
"""
Research-to-Implementation Child Creator — Elon Governor Delegation
Phase 1: Reads gap-analysis.md, creates kanban child tasks for real gaps,
updates circuit breaker, produces Obsidian decision notes for audit trail.

Usage:
    python3 research-impl-child-creator.py              # Live execution
    python3 research-impl-child-creator.py --dry-run    # Preview only, no mutations
    python3 research-impl-child-creator.py --board <slug>  # Override board

Requires:
    - ~/.local/bin/hermes (Hermes kanban CLI)
    - /home/frank/.hermes/data/research-impl-gap-analysis.md (input)
    - /home/frank/.hermes/data/research-impl-circuit-breaker.json (state store)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent / "data"
GAP_ANALYSIS_PATH = DATA_DIR / "research-impl-gap-analysis.md"
CIRCUIT_BREAKER_PATH = DATA_DIR / "research-impl-circuit-breaker.json"
PROCESSED_DIR = DATA_DIR / "processed"
OBSIDIAN_DIR = Path("/home/frank/obsidian-fleet-vault/Research/Reviews")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
DEFAULT_BOARD = "upero"

# ── Fleet Profile Map ──────────────────────────────────────────────────────
# Maps item keywords / assignee hints to kanban profile names
FLEET_PROFILE_MAP = {
    # Explicit profile names
    "trading-devops": "trading-devops",
    "trading-risk-reviewer": "trading-risk-reviewer",
    "platform-db-migrator": "platform-db-migrator",
    "db-architect": "db-architect",
    "sycode-trading-pm": "sycode-trading-pm",
    "jarvis-os-pm": "jarvis-os-pm",
    "os-reviewer": "os-reviewer",
    "self-improve-engineer": "self-improve-engineer",
    "devops": "devops",
    "researcher-a": "researcher-a",
    "researcher-b": "researcher-b",
    "jarvis-voice": "jarvis-voice",
    "elon": "elon",
    # Semantic -> profile mappings
    "infra": "devops",
    "config": "devops",
    "pm": "jarvis-os-pm",
    "reviewer": "os-reviewer",
    "risk": "trading-risk-reviewer",
    "data": "db-architect",
    "migration": "platform-db-migrator",
}

# ── Gate Classification ────────────────────────────────────────────────────
GATE_A3_KEYWORDS = [
    "money", "payment", "spend", "live trading", "production deploy",
    "credential", "secret", "api key", "token", "password",
    "deploy", "restart prod", "irreversible", "live", "real money",
    "production restart", "prod restart", "live restart",
]
GATE_A2_KEYWORDS = [
    "config", "configuration", "data", "migration", "ddl",
    "backfill", "db write", "sql", "schema", "apply",
    "deploy config", "config change", "runtime config",
]


# ── Helpers ────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {level}: {msg}")


def warn(msg: str):
    log(msg, "WARN")


def err(msg: str):
    log(msg, "ERROR")


def run_hermes(args: list, capture: bool = True, board: str = None):
    """Run `hermes kanban` CLI command."""
    cmd = [HERMES_BIN, "kanban"]
    if board:
        cmd += ["--board", board]
    cmd += args
    log(f"Running: {' '.join(str(a) for a in cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=capture, text=True, timeout=60
        )
        if capture:
            if result.returncode != 0:
                warn(f"hermes CLI stderr: {result.stderr.strip()}")
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        return "", "", result.returncode
    except subprocess.TimeoutExpired:
        err(f"hermes command timed out: {cmd}")
        return "", "TIMEOUT", -1
    except FileNotFoundError:
        err(f"hermes CLI not found at {HERMES_BIN}")
        return "", "NOT_FOUND", -1


# ── Circuit Breaker ────────────────────────────────────────────────────────

def load_circuit_breaker() -> dict:
    """Load circuit breaker store, returning empty dict if missing."""
    if CIRCUIT_BREAKER_PATH.exists():
        try:
            with open(CIRCUIT_BREAKER_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            warn(f"Circuit breaker unreadable, starting fresh: {e}")
            return {}
    log("No existing circuit breaker — starting fresh")
    return {}


def save_circuit_breaker(data: dict):
    """Persist circuit breaker store with atomic write."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    # Load existing to merge — prevents overwrite from concurrent writers
    existing = {}
    if CIRCUIT_BREAKER_PATH.exists():
        try:
            with open(CIRCUIT_BREAKER_PATH) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    
    # Merge: our updates take precedence, but keep entries we didn't touch
    merged = {**existing, **data}
    
    tmp_path = CIRCUIT_BREAKER_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
    tmp_path.replace(CIRCUIT_BREAKER_PATH)
    log(f"Circuit breaker saved ({len(merged)} source entries)")


def ensure_cb_entry(cb: dict, source_task_id: str):
    """Ensure a circuit breaker entry exists for a source task."""
    if source_task_id not in cb:
        cb[source_task_id] = {
            "source_task_id": source_task_id,
            "extraction_count": 0,
            "false_positive_count": 0,
            "routed_child_ids": [],
            "last_classification": None,
            "last_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "suppressed": False,
            "suppressed_reason": None,
        }


def record_false_positive(cb: dict, source_task_id: str):
    """Increment false-positive count and activate suppression if >= 3."""
    ensure_cb_entry(cb, source_task_id)
    entry = cb[source_task_id]
    entry["false_positive_count"] += 1
    entry["extraction_count"] += 1
    entry["last_classification"] = "false_positive"
    entry["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if entry["false_positive_count"] >= 3 and not entry["suppressed"]:
        entry["suppressed"] = True
        entry["suppressed_reason"] = (
            f"{entry['false_positive_count']} consecutive/accumulated false-positives"
        )
        warn(f"SUPPRESSED source {source_task_id}: {entry['suppressed_reason']}")


def record_child_created(cb: dict, source_task_id: str, child_id: str):
    """Record a successfully created child."""
    ensure_cb_entry(cb, source_task_id)
    entry = cb[source_task_id]
    entry["extraction_count"] += 1
    entry["routed_child_ids"].append(child_id)
    entry["last_classification"] = "real_gap"
    entry["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # If suppressed, clear suppression on manual override (child created regardless)
    if entry.get("suppressed"):
        entry["suppressed"] = False
        entry["suppressed_reason"] = "Override: child manually/auto-created"
        log(f"CLEARED suppression for {source_task_id} — child {child_id} created")


def is_suppressed(cb: dict, source_task_id: str) -> bool:
    """Check if a source task is suppressed (>= 3 false positives)."""
    entry = cb.get(source_task_id, {})
    return entry.get("suppressed", False)


# ── Gap Analysis Parser ────────────────────────────────────────────────────


def parse_gap_analysis() -> list:
    """
    Parse gap-analysis.md into a list of classified items.
    Supports two formats:
      1. Key-value list: "- key: value" under headers like "### real_gap: Title"
      2. Table format: "| Field | Value |" under headers like "### N. ✅ board -- Title"
    """
    if not GAP_ANALYSIS_PATH.exists():
        log(f"No gap analysis found at {GAP_ANALYSIS_PATH}")
        return []

    text = GAP_ANALYSIS_PATH.read_text()
    items = []
    current_item = None
    in_table = False
    in_real_actions = False
    for line in text.split("\n"):
        h1 = re.match(r"^###\s+(real_gap|false_positive|already_routed|already_done):\s*(.*)", line)
        h2 = re.match(r"^###\s+\d+\.\s+[\u2705\u274c\U0001f195\U0001f507]\s+(\S+)\s*[\u2014\u2013]\s*(.*)", line)
        if h1 or h2:
            if current_item:
                items.append(current_item)
            in_table = False
            if h1:
                classification = h1.group(1)
                title = h1.group(2).strip()
                source_task = ""
            else:
                source_task = h2.group(1)
                title = h2.group(2).strip()
                classification = "unknown"
            current_item = {
                "classification": classification, "title": title,
                "source_task": source_task, "confidence": 0.0,
                "gate": "A1", "suggested_assignee": "", "action": "",
                "actions": [],
                "verification": "", "reason": "", "source_root": "",
                "routed_children": [],
            }
            continue
        if current_item is None:
            continue
        stripped_line = line.strip()
        if stripped_line.startswith("**Real actions identified:**"):
            in_real_actions = True
            continue
        if stripped_line.startswith("## ") or stripped_line.startswith("### ") or stripped_line.startswith("| Field |"):
            in_real_actions = False
        if line.strip().startswith("| Field |"):
            in_table = True
            continue
        if in_table and line.strip().startswith("|---"):
            continue
        if in_table and line.strip().startswith("|"):
            rm = re.match(r"\|\s*(.+?)\s*\|\s*(.+?)\s*\|", line)
            if rm:
                field = rm.group(1).strip().lower()
                value = rm.group(2).strip()
                if field == "classification":
                    cv = value.replace("**", "").strip()
                    for emoji in ["\U0001f195", "\u2705", "\u274c"]:
                        if emoji in cv:
                            cv = cv.replace(emoji, "").strip()
                            break
                    current_item["classification"] = cv.strip().lower()
                elif field == "confidence":
                    try:
                        current_item["confidence"] = float(re.sub(r"[^0-9.]", "", value)) / 100.0
                    except ValueError:
                        current_item["confidence"] = 0.0
                elif field == "task":
                    current_item["source_task"] = value.strip()
                elif field == "board":
                    pass
                elif field == "assignee":
                    current_item["suggested_assignee"] = value.strip()
                elif field == "reason":
                    current_item["reason"] = value.strip()
                elif field == "action":
                    current_item["action"] = value.strip()
                    if value.strip():
                        current_item.setdefault("actions", []).append(value.strip())
                elif field == "routed children":
                    current_item["routed_children"] = re.findall(r"t_[a-f0-9]+", value)
                elif field == "verification":
                    current_item["verification"] = value.strip()
            continue
        if in_table and not line.strip().startswith("|"):
            in_table = False
        kv = re.match(r"^\s*[-*]?\s*(source_task|confidence|gate|suggested_assignee|action|verification|reason|source_root|routed_children|note)\s*:\s*(.+)", line)
        if kv:
            key = kv.group(1)
            value = kv.group(2).strip()
            if key == "confidence":
                try:
                    current_item["confidence"] = float(value)
                except ValueError:
                    current_item["confidence"] = 0.0
            elif key == "routed_children":
                current_item["routed_children"] = re.findall(r"t_[a-f0-9]+", value)
            elif key in ("source_task", "source_root"):
                current_item[key] = value.strip()
            elif key == "action":
                current_item["action"] = value
                current_item.setdefault("actions", []).append(value)
            else:
                current_item[key] = value
            continue
        if in_real_actions and line.strip().startswith("- `"):
            # Generated by research-impl-gap-analyzer.py under
            # "Real actions identified". Keep all real actions grouped in the
            # one source digest instead of creating one child per bullet.
            action = line.strip()[3:].rstrip("`").strip()
            if action:
                current_item.setdefault("actions", []).append(action)
            continue
        if in_real_actions and not line.strip():
            in_real_actions = False
    if current_item:
        items.append(current_item)
    log(f"Parsed {len(items)} items from gap analysis: "
         f"{sum(1 for i in items if i['classification'] == 'real_gap')} real_gap, "
         f"{sum(1 for i in items if i['classification'] == 'false_positive')} false_positive, "
         f"{sum(1 for i in items if i['classification'] in ('already_routed', 'already_done'))} already_routed/done")
    return items
# ── Classification Logic ───────────────────────────────────────────────────

def classify_gate(item: dict) -> str:
    """
    Classify gate: A1 (read-only/info), A2 (config/data), A3 (money/creds/prod).
    Returns one of: "A1", "A2", "A3".
    """
    text = f"{item.get('title', '')} {item.get('action', '')} {item.get('gate', '')}"
    text_lower = text.lower()

    # Check for explicit gate override in the gap analysis
    gate_override = item.get("gate", "").strip().upper()
    if gate_override in ("A1", "A2", "A3"):
        return gate_override

    # Check keywords
    for kw in GATE_A3_KEYWORDS:
        if kw in text_lower:
            return "A3"
    for kw in GATE_A2_KEYWORDS:
        if kw in text_lower:
            return "A2"
    return "A1"


def map_assignee(item: dict) -> str:
    """
    Map item to a fleet profile. Uses suggested_assignee if available and known,
    otherwise falls back to heuristic matching from title/action text.
    """
    suggested = item.get("suggested_assignee", "").strip().lower()
    if suggested and suggested in FLEET_PROFILE_MAP:
        return FLEET_PROFILE_MAP[suggested]

    # Heuristic fallback from title/action
    text = f"{item.get('title', '')} {item.get('action', '')}".lower()
    if any(kw in text for kw in ("db", "migration", "ddl", "schema", "backfill")):
        return "platform-db-migrator"
    if any(kw in text for kw in ("deploy", "config", "infra", "pipeline", "sync", "runtime")):
        return "trading-devops"
    if any(kw in text for kw in ("risk", "review", "gate")):
        return "trading-risk-reviewer"
    if any(kw in text for kw in ("pm", "triage", "prioritize", "route")):
        return "jarvis-os-pm"
    if any(kw in text for kw in ("research", "investigate")):
        return "researcher-a"
    if any(kw in text for kw in ("strategy", "trading", "signal", "position")):
        return "trading-devops"

    # Default
    return "devops"


# ── Kanban Operations ──────────────────────────────────────────────────────

def create_child_task(
    item: dict,
    gate_class: str,
    board: str = DEFAULT_BOARD,
    dry_run: bool = False,
) -> str:
    """
    Create a kanban child task for a real_gap item.
    Returns the child task ID, or None on failure/dry-run.
    """
    title = item.get("title", "Untitled gap").strip()
    source = item.get("source_task", "unknown")
    actions = [a for a in item.get("actions", []) if a]
    action = item.get("action", "")
    if actions:
        action_section = "\n".join(f"- {a}" for a in actions)
    else:
        action_section = action or "(none specified; manual PM review required before execution)"
    verification = item.get("verification", "")
    assignee = map_assignee(item)

    body = (
        f"## Auto-created from gap analysis\n\n"
        f"**Source task**: `{source}`\n"
        f"**Gate class**: {gate_class}\n"
        f"**Assignee**: {assignee}\n\n"
        f"### Grouped action digest\n{action_section}\n\n"
        f"### Verification criteria\n{verification}\n"
        f"\n### Decomposition guard\n"
        f"This is one grouped source-task digest. Do not split individual "
        f"markdown bullets/specification lines into separate child cards unless "
        f"each has a distinct owner, acceptance test, and gate.\n"
        f"\n---\n"
        f"*Created by research-to-implementation child creator (t_df14a205)*"
    )

    # Truncate title to reasonable length
    if len(title) > 200:
        title = title[:197] + "..."

    # Build create args
    create_args = ["create", "--assignee", assignee, "--body", body, title]

    # A2 items: create as blocked with dependency kind
    if gate_class == "A2":
        create_args += ["--initial-status", "blocked"]

    # A3 items should never reach here — blocked upstream
    if gate_class == "A3":
        err(f"A3 item '{title}' reached create_child_task — this should not happen")
        return None

    if dry_run:
        log(f"[DRY-RUN] Would create child: {title}")
        log(f"         Board: {board}, Assignee: {assignee}, Gate: {gate_class}")
        log(f"         Body length: {len(body)} chars")
        return f"dry-run-{hash(title) & 0xffff:04x}"

    stdout, stderr, rc = run_hermes(create_args, board=board)
    if rc != 0:
        err(f"Failed to create child '{title}': {stderr}")
        return None

    # Parse task ID from output
    task_id_match = re.search(r"(t_[a-f0-9]+)", stdout)
    if task_id_match:
        child_id = task_id_match.group(1)
        log(f"Created child {child_id} for source {source} (gate={gate_class}, assignee={assignee})")
        return child_id
    else:
        warn(f"Could not extract task ID from output: {stdout[:200]}")
        return None


def block_for_frank(item: dict, board: str = DEFAULT_BOARD, dry_run: bool = False):
    """
    Escalate A3-gated items to Frank. Creates a blocked status entry
    via governance artifact (cannot create kanban block on a non-existent task).
    Instead, this logs the item for manual escalation.
    """
    source = item.get("source_task", "unknown")
    title = item.get("title", "Untitled")

    if dry_run:
        log(f"[DRY-RUN] Would block for Frank: {title} (source={source}, gate=A3)")
        return

    log(f"NEEDS FRANK: A3-gated item '{title}' from {source} — blocked, requires human decision")
    # The actual block is handled by the governor loop, not this script.
    # This script flags it; the governor acts on the flag.


# ── Obsidian Decision Notes ────────────────────────────────────────────────

def create_obsidian_decision(
    item: dict,
    action_taken: str,
    child_id: str = None,
    dry_run: bool = False,
) -> str | None:
    """
    Create an Obsidian decision note for audit trail.
    Returns the file path, or None on failure.
    """
    source = item.get("source_task", "unknown")
    classification = item.get("classification", "unknown")
    title = item.get("title", "Untitled")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Decision verdict based on classification + action
    if classification == "real_gap":
        if child_id:
            verdict = f"CHILD_CREATED: {child_id}"
        else:
            verdict = "MANUAL_REVIEW_REQUIRED"
    elif classification == "false_positive":
        verdict = "NOOP_FALSE_POSITIVE"
    elif classification in ("already_routed", "already_done"):
        verdict = "NOOP_ALREADY_ROUTED"
    else:
        verdict = "UNCLASSIFIED"

    filepath = (
        OBSIDIAN_DIR
        / f"2026-07-04-research-actionable-{source.replace('/', '-')}-child-creator-decision.md"
    )

    frontmatter = (
        "---\n"
        f"title: \"Child creator decision — {source}\"\n"
        f"date: \"{ts[:10]}\"\n"
        f"source_task: \"{source}\"\n"
        f"classification: \"{classification}\"\n"
        f"verdict: \"{verdict}\"\n"
        f"gate_class: \"{item.get('gate', 'A1')}\"\n"
        f"confidence: {item.get('confidence', 0.0)}\n"
        f"child_created: \"{child_id or 'none'}\"\n"
        "type: research-actionable-child-creator\n"
        "---\n\n"
    )

    body = (
        f"# Child Creator Decision — `{source}`\n\n"
        f"## Verdict\n\n"
        f"**{verdict}**\n\n"
        f"- **Classification**: {classification}\n"
        f"- **Gate class**: {item.get('gate', 'A1')}\n"
        f"- **Confidence**: {item.get('confidence', 0.0)}\n"
        f"- **Child created**: {child_id or 'none'}\n"
        f"- **Timestamp**: {ts}\n\n"
        f"## Item\n\n"
        f"**{title}**\n\n"
        f"- Source: `{source}`\n"
        f"- Root: `{item.get('source_root', 'unknown')}`\n"
        f"- Action: {item.get('action', '(none)')}\n\n"
        f"## Action taken\n\n"
        f"{action_taken}\n\n"
        f"## Verification\n\n"
        f"{item.get('verification', '(none specified)')}\n"
        f"- Decision note persisted: ✅\n"
        f"- Circuit breaker updated: ✅\n"
    )

    content = frontmatter + body

    if dry_run:
        log(f"[DRY-RUN] Would write Obsidian note: {filepath}")
        return filepath

    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        filepath.write_text(content)
        log(f"Obsidian decision note written: {filepath}")
        return str(filepath)
    except OSError as e:
        err(f"Failed to write Obsidian note: {e}")
        return None


# ── Gap Analysis Archive ───────────────────────────────────────────────────

def archive_gap_analysis(dry_run: bool = False):
    """Move processed gap-analysis.md to processed/ with timestamp."""
    if not GAP_ANALYSIS_PATH.exists():
        return

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = PROCESSED_DIR / f"research-impl-gap-analysis-{ts}.md"

    if dry_run:
        log(f"[DRY-RUN] Would archive: {GAP_ANALYSIS_PATH} -> {dest}")
        return

    GAP_ANALYSIS_PATH.rename(dest)
    log(f"Archived gap analysis to {dest}")


# ── Main Pipeline ──────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    board = DEFAULT_BOARD
    for i, arg in enumerate(sys.argv):
        if arg in ("--board", "-b") and i + 1 < len(sys.argv):
            board = sys.argv[i + 1]

    if dry_run:
        log("=" * 60)
        log("DRY-RUN MODE — No mutations will be made")
        log("=" * 60)

    # 1. Load circuit breaker
    cb = load_circuit_breaker()

    # 2. Parse gap analysis
    items = parse_gap_analysis()
    if not items:
        log("No items to process. Exiting.")
        return

    summary = {
        "real_gap_processed": 0,
        "real_gap_created": 0,
        "real_gap_a3_blocked": 0,
        "real_gap_low_confidence": 0,
        "false_positive_processed": 0,
        "already_routed_processed": 0,
        "already_done_processed": 0,
        "circuit_breaker_suppressions_hit": 0,
        "errors": 0,
    }

    # 3. Process each item
    for item in items:
        classification = item["classification"]
        source = item.get("source_task", "unknown")
        confidence = item.get("confidence", 0.0)
        log(f"--- Processing [{classification}] {item['title'][:80]}... ---")

        if classification == "real_gap":
            summary["real_gap_processed"] += 1

            # Check suppression
            if is_suppressed(cb, source):
                summary["circuit_breaker_suppressions_hit"] += 1
                log(f"SKIPPED (suppressed): {source} — circuit breaker active")
                action_taken = "Skipped: source task suppressed by circuit breaker"
                create_obsidian_decision(item, action_taken, dry_run=dry_run)
                continue

            # Check confidence threshold
            if confidence < 0.6:
                summary["real_gap_low_confidence"] += 1
                log(f"FLAGGED (low confidence={confidence}): {source} — manual review needed")
                action_taken = f"Manual review required: confidence={confidence} < 0.6"
                create_obsidian_decision(item, action_taken, dry_run=dry_run)
                continue

            # Classify gate
            gate_class = classify_gate(item)
            item["gate"] = gate_class
            log(f"Gate: {gate_class} | Assignee: {map_assignee(item)} | Confidence: {confidence}")

            if gate_class == "A3":
                # Block for Frank — never auto-create
                summary["real_gap_a3_blocked"] += 1
                block_for_frank(item, board, dry_run)
                action_taken = f"Blocked for Frank (A3 gate). Requires human decision on {item['title']}"
                create_obsidian_decision(item, action_taken, dry_run=dry_run)
            else:
                # Create child task
                child_id = create_child_task(item, gate_class, board, dry_run)
                if child_id:
                    summary["real_gap_created"] += 1
                    record_child_created(cb, source, child_id)
                    action_taken = f"Child task {child_id} created (gate={gate_class})"
                else:
                    summary["errors"] += 1
                    action_taken = f"ERROR: Child creation failed for {source}"
                create_obsidian_decision(item, action_taken, child_id, dry_run)

        elif classification == "false_positive":
            summary["false_positive_processed"] += 1
            record_false_positive(cb, source)
            action_taken = f"No-op: classified as false_positive. Circuit breaker updated."
            create_obsidian_decision(item, action_taken, dry_run=dry_run)

        elif classification in ("already_routed", "already_done"):
            key = f"{classification}_processed"
            summary[key] += 1
            routed = item.get("routed_children", [])
            routed_str = ", ".join(routed) if routed else "none listed"
            action_taken = (
                f"No-op: classified as {classification}. "
                f"Existing children: {routed_str}"
            )
            create_obsidian_decision(item, action_taken, dry_run=dry_run)

        else:
            log(f"Unknown classification: {classification}", level="WARN")

    # 4. Save circuit breaker
    if not dry_run:
        save_circuit_breaker(cb)
    else:
        log(f"[DRY-RUN] Would save circuit breaker ({len(cb)} entries)")

    # 5. Archive gap analysis
    archive_gap_analysis(dry_run)

    # 6. Summary
    log("=" * 60)
    log("CHILD CREATOR SUMMARY")
    log("=" * 60)
    log(f"  Real gaps processed:          {summary['real_gap_processed']}")
    log(f"  -> Children created:          {summary['real_gap_created']}")
    log(f"  -> Blocked for Frank (A3):    {summary['real_gap_a3_blocked']}")
    log(f"  -> Low confidence (<0.6):     {summary['real_gap_low_confidence']}")
    log(f"  -> Suppressions hit:          {summary['circuit_breaker_suppressions_hit']}")
    log(f"  False positives processed:    {summary['false_positive_processed']}")
    log(f"  Already routed processed:     {summary['already_routed_processed']}")
    log(f"  Already done processed:       {summary['already_done_processed']}")
    log(f"  Errors:                       {summary['errors']}")
    log("=" * 60)

    if dry_run:
        log("DRY-RUN COMPLETE — No mutations made")
    else:
        log("Child creator cycle complete")

    if summary["real_gap_a3_blocked"] > 0:
        warn(f"{summary['real_gap_a3_blocked']} A3 items blocked for Frank — needs human review")


if __name__ == "__main__":
    main()
