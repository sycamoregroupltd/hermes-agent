#!/usr/bin/env python3
"""Governed fleet knowledge-catalog regeneration cron (canonical implementation).

Closes the structural gap that the canonical fleet generator
``generate_knowledge_catalogs.py`` had no recurring mechanism: it only
regenerated when an analyst ran it by hand, so ``--check`` drifted stale.

This script is the sole implementation behind the jarvis no_agent cron
``fleet-knowledge-catalog-regen``. It:
  1. Runs the canonical generator, which writes through the governed atomic
     ``second_brain_writer.write_text_atomic`` into the fleet vault.
  2. Re-runs the generator with ``--check`` to confirm the live catalog tree
     is byte-exact against the current profiles/skills/quarantine state.
  3. Stays silent when clean (watchdog pattern).
  4. On any failure, prints a concise failure/debt alert to stdout (delivered
     verbatim to the gateway-connected sink ``discord:#fleet-reports``) and
     appends a durable run record to a log the second-brain health loop tails.

Edit this canonical file, not the profile-local shim.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

GENERATOR = Path("/home/frank/obsidian-fleet-vault/System/Scripts/generate_knowledge_catalogs.py")
PROFILES_DIR = Path("/home/frank/.hermes/profiles")
SKILLS_DIR = Path("/home/frank/.hermes/skills")
QUARANTINED_PROFILES = Path("/home/frank/.hermes/quarantined-profiles")
QUARANTINED_SKILLS = Path("/home/frank/.hermes/quarantined-skills")
OUTPUT = Path("/home/frank/obsidian-fleet-vault")
REGISTRY = Path("/home/frank/obsidian-fleet-vault/Projects/Portfolio/registry.yaml")
LOG = Path("/home/frank/.hermes/var/fleet-knowledge-catalog-regen.log")
PY = sys.executable or "python3"
GENERATOR_ID = "control-spine/scripts/generate_knowledge_catalogs.py"


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, *map(str, args)], capture_output=True, text=True, timeout=300)


def _assert_real_catalog_roots(output: Path, roots: tuple[Path, ...]) -> None:
    """Reject symlinked roots or ancestors before any generator filesystem walk."""
    if output.is_symlink():
        raise RuntimeError(f"refusing catalog regeneration; output is a symlink: {output}")
    output_real = output.resolve(strict=False)
    for root in roots:
        current = root
        while current != output:
            if current.is_symlink():
                raise RuntimeError(
                    f"refusing catalog regeneration; catalog root or ancestor is a symlink: {current}"
                )
            current = current.parent
        root_real = root.resolve(strict=False)
        try:
            root_real.relative_to(output_real)
        except ValueError as exc:
            raise RuntimeError(
                f"refusing catalog regeneration; catalog root resolves outside output: {root}"
            ) from exc


def assert_owned_catalog_tree(output: Path = OUTPUT) -> None:
    """Fail closed before the generator can delete or overwrite curated pages."""
    roots = (output / "Agents" / "Catalog", output / "Skills" / "Catalog")
    _assert_real_catalog_roots(output, roots)
    paths = [path for root in roots if root.is_dir() for path in root.glob("*.md")]
    paths.extend(
        path
        for path in (
            output / "Agents" / "Agents-Home.md",
            output / "Agents" / "Inactive-Agents.md",
            output / "Skills" / "Skills-Home.md",
            output / "Skills" / "Inactive-Skills.md",
        )
        if path.exists()
    )
    manifest = output / "System" / "Catalogs" / "catalog-manifest.yaml"
    if manifest.exists():
        paths.append(manifest)
    unowned: list[str] = []
    for path in sorted(paths):
        if path.is_symlink():
            unowned.append(f"{path} (symlink)")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        generated = re.search(r"(?m)^(?:generated|generated_at):\s*['\"]?true['\"]?\s*$", text)
        generator = re.search(r"(?m)^generator:\s*['\"]?([^'\"\n]+)", text)
        owned = bool(generator and generator.group(1).strip() == GENERATOR_ID)
        if path == manifest:
            owned = owned and bool(re.search(r"(?m)^generated_at:\s*\d{4}-\d{2}-\d{2}", text))
        else:
            owned = owned and bool(generated)
        if not owned:
            unowned.append(str(path))
    if unowned:
        listed = ", ".join(unowned)
        raise RuntimeError(
            "refusing catalog regeneration; unowned or symlinked catalog paths: " + listed
        )


def parse_counts(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(("generated", "catalogs current")):
            return line.strip()
    return ""


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    base = [
        str(GENERATOR),
        "--profiles-dir", str(PROFILES_DIR),
        "--skills-dir", str(SKILLS_DIR),
        "--quarantined-profiles-dir", str(QUARANTINED_PROFILES),
        "--quarantined-skills-dir", str(QUARANTINED_SKILLS),
        "--output", str(OUTPUT),
        "--registry", str(REGISTRY),
    ]
    try:
        assert_owned_catalog_tree()
    except RuntimeError as exc:
        print(f"[fleet-knowledge-catalog-regen] FAIL @ {ts} | {exc}")
        return 1
    regen = run(base)
    check = run([*base, "--check"])
    counts = parse_counts(regen.stdout) or parse_counts(check.stdout)
    ok = regen.returncode == 0 and check.returncode == 0
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{ts} ok={ok} regen_rc={regen.returncode} check_rc={check.returncode} {counts}\n"
        )
    if ok:
        return 0
    bits: list[str] = []
    if regen.returncode != 0:
        last = regen.stderr.strip().splitlines()[-1] if regen.stderr.strip() else "no stderr"
        bits.append(f"regen FAILED rc={regen.returncode}: {last}")
    if check.returncode != 0:
        bits.append("catalog --check DRIFT: generated tree out of sync with live profiles/skills/quarantine")
    print(
        f"[fleet-knowledge-catalog-regen] FAIL @ {ts} | {' | '.join(bits)} "
        f"| counts={counts or 'n/a'} | log={LOG}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
