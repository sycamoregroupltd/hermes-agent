#!/home/frank/.hermes/venvs/trading-ml/bin/python
"""
quant_researcher_6h_validator.py  --  Fail-closed post-run validator.

Companion no_agent cron job for the quant-researcher-6h edge loop
(job 13c1f9279025). The researcher LLM job reports last_status "ok" merely
because it delivered a response -- a verbatim 2023 canned template, a silent
stall, or a "let me know how you'd like to proceed?" question all still
"deliver" and thus report ok (see diagnostic t_fba0cda3 / t_abbfca07).

This validator is the fail-closed backstop. After each researcher run it
asserts that a REAL 2026 dated research artifact was produced with the
freshness-gate metrics, and that -- when the researcher claims
"validated/highlighted" -- the stated cohorts actually pass the gate
(stale-share <= 5%, fresh n >= 300, fresh WR > 53%). If the assertion fails,
it demotes the researcher job's last_status from "ok" to "error" by calling
the same mark_job_run() the scheduler uses, so the dashboard/fleet no longer
shows a green lie.

NOTE: as of t_78d2b4cf the researcher script no longer self-writes a dated
Obsidian note (durable persistence is the governor's job). Its in-process
self-check now calls selfcheck_from_body(body) against the rendered report
string. The file-based path (validate the most recent dated note on disk)
remains available for standalone/diagnostic invocations via main().

Deterministic, read-only against the DB. No trading, no writes to the DB,
no credential changes. Exits 0 (so this watchdog itself never goes red
spuriously) but mutates the researcher job's stored last_status when needed.

Usage:
    python quant_researcher_6h_validator.py            # standalone: check newest dated note on disk
    # in-process (from the researcher script):
    verdict = quant_researcher_6h_validator.selfcheck_from_body(body)
"""
import os
import re
import sys
import glob
import datetime as dt

# ---- Resolve the LIVE cron store (active profile), not the source tree ----
# The runtime's jobs.json lives at ~/.hermes/profiles/<profile>/cron/jobs.json.
# get_job()/mark_job_run() in the hermes-agent source tree read a DIFFERENT
# store, so calling them here produced "job_id ... not found, skipping save"
# and the demote never happened. Read the live jarvis jobs.json directly.
import json as _json
_LIVE_JOBS_PATH = os.path.expanduser("~/.hermes/profiles/jarvis/cron/jobs.json")

def mark_job_run(job_id: str, success: bool, error: str | None = None) -> None:
    """Mirror scheduler's mark_job_run against the LIVE jobs.json store.

    Only mutates last_status / last_error. Deliberately does NOT touch
    last_run_at: the scheduler is the authoritative writer of that field, and
    overwriting it here advances the validator's "since" window past the real
    last delivered output, causing a self-perpetuating false-fail loop
    (t_7364db59). A demotion must not corrupt the run timestamp.
    """
    try:
        with open(_LIVE_JOBS_PATH, "r", encoding="utf-8") as f:
            store = _json.load(f)
    except Exception:
        return
    jobs = store if isinstance(store, list) else store.get("jobs", store.get("data", []))
    for j in jobs:
        if str(j.get("id", "")) == job_id:
            j["last_status"] = "ok" if success else "error"
            j["last_error"] = (error or "")[:2000] if not success else None
            break
    else:
        return
    try:
        with open(_LIVE_JOBS_PATH, "w", encoding="utf-8") as f:
            _json.dump(store, f, indent=2)
    except Exception:
        return

def get_job(job_id: str):
    with open(_LIVE_JOBS_PATH, "r", encoding="utf-8") as f:
        store = _json.load(f)
    jobs = store if isinstance(store, list) else store.get("jobs", store.get("data", []))
    for j in jobs:
        if str(j.get("id", "")) == job_id:
            return j
    return None

RESEARCHER_JOB_ID = "13c1f9279025"
RESEARCH_DIR = os.path.expanduser("~/obsidian/quant-team/research")

# As of t_78d2b4cf the researcher script NO LONGER self-writes a dated note;
# durable Obsidian persistence is the governor's job. The delivered artifact
# the fleet actually consumes is the scheduler-captured cron output file. The
# standalone validator MUST therefore validate THAT file, not a (no longer
# written) self-note -- otherwise it always false-fails with
# "no-dated-2026-research-file-written-since-last-run" and demotes a healthy
# job (see task t_7364db59).
CRON_OUTPUT_DIR = os.path.expanduser(
    "~/.hermes/profiles/jarvis/cron/output/13c1f9279025"
)

# Files the researcher is expected to emit (dated). Both the LLM loop and the
# deterministic no_agent script name their dated note this way.
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-quant-edge-sweep-6h\.md$")
GENERIC_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-.*\.md$")

# Metric markers that prove the run actually computed the freshness gate.
# The deterministic no_agent script emits `fresh_window_min`/`fresh_lag`;
# the LLM loop emits `freshness` windows + `fresh subset` + `stale-share`.
# Requiring ANY of these (not all) is enough to prove a real computation ran.
METRIC_MARKERS = [
    "fresh_window_min",
    "fresh_lag",
    "freshness",
    "stale-share",
    "stale_share",
    "fresh subset",
]

# Fraud markers: a run that parrots the old 2023 canned template must FAIL.
FORBIDDEN_MARKERS = [
    "2023-10-05",
    "Quant Research Report: Trading Edge Identification",
    "2023-10-05-quant-research.md",
]

# Contamination cross-check markers (governor task t_47fd45ce). The deterministic
# no_agent script (quant_researcher_6h.py) emits these; a genuine run MUST contain
# the cross-check section AND must never present a CONTAMINATED cohort as validated.
CONTAM_SECTION_MARKER = "Data-Epoch Contamination Cross-Check"
CONTAM_ROW_MARKER = "CONTAMINATED(pre-clean-epoch)"
CLEAN_EPOCH_MARKER = "clean-candidate-599f58e7e"

# Gate thresholds (mirror of quant_researcher_6h.py / freshness-gate spec).
WR_THRESH = 53.0
N_THRESH = 300
STALE_THRESH = 5.0


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load_researcher_job():
    try:
        return get_job(RESEARCHER_JOB_ID)
    except Exception as e:  # pragma: no cover - defensive
        return {"_load_error": str(e)}


def _most_recent_research_file(since_iso: str | None):
    """Return (path, mtime) of the most recent dated research file.

    If since_iso is provided (the researcher's last_run_at), only consider
    files written AFTER that timestamp -- proving THIS run produced output,
    not a stale prior artifact.
    """
    if not os.path.isdir(RESEARCH_DIR):
        return None, None
    best = None
    best_mtime = -1.0
    since_ts = None
    if since_iso:
        # Parse either ISO with tz or naive; normalise loosely.
        s = since_iso.replace("Z", "+00:00")
        try:
            since_ts = dt.datetime.fromisoformat(s).timestamp()
        except Exception:
            since_ts = None
    for name in os.listdir(RESEARCH_DIR):
        if not GENERIC_DATE_RE.match(name):
            continue
        p = os.path.join(RESEARCH_DIR, name)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        # Require the file to post-date the researcher's last run, when known.
        if since_ts is not None and mtime < since_ts:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best = p
    return best, (best_mtime if best else None)


def _content_checks(path: str | None = None, body: str | None = None):
    """Return (ok, reasons[]). ok=False means fail-closed.

    Accepts EITHER a file path (legacy) OR an in-memory body string. The
    researcher script now renders the report to stdout (no self-written note),
    so the validator's self-check runs against the in-memory body. The
    disk-file path remains supported for standalone/diagnostic runs.
    """
    if body is not None:
        text = body
    elif path is not None:
        try:
            text = open(path, "r", encoding="utf-8", errors="replace").read()
        except Exception as e:
            return False, [f"cannot-read-file: {e}"]
    else:
        return False, ["no-path-or-body-provided"]

    reasons = []

    # 1. Fraud / stale-template guard. Only the specific 2023-10-05 canned
    #    template and ANY pre-2026 run-date stamp are forbidden -- a genuine
    #    2026 report may legitimately cite 2025/2024 academic papers.
    for fm in FORBIDDEN_MARKERS:
        if fm in text:
            reasons.append(f"forbidden-marker-present: {fm!r}")
    # A research run must be stamped with the CURRENT (2026) year. A prior-year
    # run-date stamp (e.g. "2023-10-05", "2024-..", "2025-..") means a stale
    # template was parroted. Current-year occurrences and academic citations in
    # prose are explicitly allowed.
    if re.search(r"\b202[0-5]-\d{2}-\d{2}\b", text):
        reasons.append("prior-year-run-date-stamp-detected")

    # 2. Required freshness-gate metrics must be present (quorum of >=3).
    #    The deterministic no_agent script emits fresh_window_min/fresh_lag/
    #    stale_share; the LLM loop emits freshness/fresh subset/stale-share.
    #    Requiring a quorum (not all) proves a real computation ran while
    #    tolerating either authoring style -- and still fails the degenerate
    #    template, which emits none of these.
    low = text.lower()
    present = [m for m in METRIC_MARKERS if m.lower() in low]
    if len(present) < 3:
        reasons.append("missing-metric-markers: " + ", ".join(present) + " (need >=3)")

    # 4. (t_47fd45ce ACC #1) Contamination-epoch cross-check must be present.
    #    A genuine run cross-references the data_epoch_registry + kill-list
    #    K1-K7 and tags pre-clean-epoch signals CONTAMINATED/UNVALIDATED.
    if CONTAM_SECTION_MARKER not in text:
        reasons.append("missing-contamination-cross-check-section")
    if CLEAN_EPOCH_MARKER not in text:
        reasons.append("missing-clean-candidate-epoch-reference")

    # 5. (t_47fd45ce ACC #1/#2) A CONTAMINATED cohort must NEVER be presented
    #    as validated. The cross-check section legitimately lists contaminated
    #    rows as FAIL-closed; only an affirmative "Validated Cohorts" table row
    #    carrying a contamination flag would be a hard violation. Detect via the
    #    table's Contam? column (YES) or the CONTAMINATED marker, whichever form
    #    the report author uses.
    validated_block = _validated_cohorts_block(text)
    if validated_block:
        contam_col = None
        for line in validated_block.splitlines():
            if CONTAM_ROW_MARKER in line:
                reasons.append("contaminated-cohort-presented-as-validated")
                break
            # Locate the Contam? column from the header row, then test each
            # data row's value (YES == contaminated, must not be validated).
            if "|" in line:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                low = [c.lower() for c in cells]
                if "contam?" in low:
                    contam_col = low.index("contam?")
                    continue  # header row itself is not a data row
                if contam_col is not None and 0 <= contam_col < len(cells):
                    if cells[contam_col].upper() == "YES":
                        reasons.append("contaminated-cohort-presented-as-validated")
                        break

    # 6. (t_47fd45ce ACC #3) Citation-staleness gate. A run that cites the
    #    obsolete 2023-10-05 canned research doc -- which does not exist on
    #    disk -- fails closed to EVIDENCE STALE. Any other obsidian path
    #    citation must exist on disk (missing path = stale evidence).
    if "2023-10-05-quant-research.md" in text:
        reasons.append("obsolete-citation-2023-10-05-quant-research.md-present")
    for m in re.finditer(r"(?:~/obsidian/|obsidian/)([^\s`'\"\)>]+?\.md)", text):
        raw = m.group(0)
        full = os.path.expanduser(raw) if raw.startswith("~") else os.path.expanduser("~/" + raw)
        if not os.path.isfile(full):
            reasons.append(f"cited-doc-missing: {raw}")

    # 3. Affirmative pass-claim consistency check (defence-in-depth).
    #    A CORRECT fail-closed report ("nothing is validated", "no cohort
    #    validated") MUST pass. We therefore only treat the run as an
    #    AFFIRMATIVE claim when it carries an explicit validated-cohort
    #    section header (the deterministic script's "## Validated Cohorts"
    #    pass block). Fuzzy word-matching on "validated" trips on the
    #    correct fail-closed prose and was a false positive.
    has_validated_section = bool(re.search(
        r"^##\s*.*validated cohorts.*$", text, re.IGNORECASE | re.MULTILINE))
    if has_validated_section:
        if not _any_cohort_passes_gate(text):
            reasons.append("claims-validated-but-no-cohort-passes-gate")

    return (len(reasons) == 0), reasons


def _validated_cohorts_block(text: str):
    """Return the text of the 'Validated Cohorts' table (if any), else ''.

    Used by the contamination gate (#5): an affirmative validated table must
    never contain a CONTAMINATED row."""
    start = text.find("## Validated Cohorts")
    if start < 0:
        return ""
    # Stop at the next '## ' heading.
    nxt = text.find("\n## ", start + 1)
    return text[start: nxt] if nxt > 0 else text[start:]


def _any_cohort_passes_gate(text: str) -> bool:
    """Scan markdown tables for a row that satisfies all three gates.

    Recognises the deterministic script's clean-epoch table header
    (CleanN / CleanWR / Clean_stale — the t_47fd45ce ACC #2 gate columns),
    the script's freshness header (FreshN / FreshWR / stale_share), and the
    LLM loop's bolded header (**FreshN** / **FreshWR** / **stale_share**),
    plus the LLM loop's prose form ("Win Rate on the fresh subset
    (N = 2,017) with 0.00% stale-share")."""
    def _clean(cells):
        # Keep underscores (column names like Clean_stale / stale_share) but
        # strip markdown/backtick/whitespace noise and lowercase for matching.
        return [re.sub(r"[-*`\s]", "", c).lower() for c in cells]

    # Locate the header row once, then test every subsequent data row against
    # the header's column positions (data rows don't repeat the column names).
    header_idx = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        low = _clean(cells)
        # Prefer the clean-epoch gate columns (authoritative for t_47fd45ce).
        if "cleann" in low and "cleanwr" in low and "clean_stale" in low:
            header_idx = (
                low.index("cleann"),
                low.index("cleanwr"),
                low.index("clean_stale"),
            )
            break
        if "freshn" in low and "freshwr" in low and "stale_share" in low:
            try:
                header_idx = (
                    low.index("freshn"),
                    low.index("freshwr"),
                    low.index("stale_share"),
                )
            except ValueError:
                continue
            break
    if header_idx is not None:
        i_n, i_wr, i_st = header_idx
        for line in text.splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            low = _clean(cells)
            # skip the header itself and the markdown separator (---) row
            if "cleann" in low or "freshn" in low or set(cells) <= {"---", ""}:
                continue
            if len(cells) <= max(i_n, i_wr, i_st):
                continue
            try:
                n = float(cells[i_n].replace(",", "").replace("%", ""))
                wr = float(cells[i_wr].replace("%", ""))
                st = float(cells[i_st].replace("%", ""))
            except ValueError:
                continue
            if n >= N_THRESH and wr > WR_THRESH and st <= STALE_THRESH:
                return True
    # Prose form (LLM loop): "Win Rate on the fresh subset (N = 2,017) with 0.00% stale-share"
    for m in re.finditer(
        r"fresh[ -]?(?:subset )?(?:win rate|wr)[^\n(]*?\(?\$?N\s*=\s*([\d,]+)\)?"
        r".{0,80}?stale[ -]?share[^\d]{0,6}?([\d.]+)%",
        text,
        re.IGNORECASE,
    ):
        try:
            n = float(m.group(1).replace(",", ""))
            st = float(m.group(2))
        except ValueError:
            continue
        # extract a WR percentage near the match
        wr_m = re.search(r"([\d.]+)%\s*(?:win rate|wr)", m.group(0), re.IGNORECASE)
        wr = float(wr_m.group(1)) if wr_m else 0.0
        if n >= N_THRESH and wr > WR_THRESH and st <= STALE_THRESH:
            return True
    return False


def selfcheck_from_body(body: str) -> str:
    """In-process fail-closed self-check for the researcher script.

    Called from quant_researcher_6h.py with the rendered report string (so the
    researcher never has to self-write a dated note). Reuses the same content
    checks as the disk-based main(). On failure it demotes the researcher
    job's last_status to "error" and returns a short verdict string for the
    researcher to write to stderr. Returns "" (empty) on PASS so the delivered
    report stays clean.

    Never raises: any internal error is swallowed and surfaced as a
    'self-validator-error' verdict so the researcher's delivery is never
    blocked by the validator.
    """
    run_label = _now_utc().strftime("%Y-%m-%d %H:%M UTC")
    try:
        ok, reasons = _content_checks(body=body)
        if ok:
            return ""
        detail = "; ".join(reasons)
        try:
            mark_job_run(
                RESEARCHER_JOB_ID,
                False,
                f"FAIL-CLOSED validator (in-process {run_label}): {detail}",
            )
            action = f"demoted researcher job {RESEARCHER_JOB_ID} last_status -> error"
        except Exception as e:  # pragma: no cover - defensive
            action = f"validator detected failure but could not demote job: {e}"
        return (
            f"FAIL-CLOSED validator ({run_label}): {detail} | action: {action}"
        )
    except Exception as e:  # pragma: no cover - defensive
        return f"self-validator-error: {e}"


def _most_recent_cron_output(since_iso: str | None):
    """Return (path, mtime) of the most recent delivered cron output file.

    The researcher script (post t_78d2b4cf) emits its report to stdout and the
    scheduler captures it under CRON_OUTPUT_DIR. That is the artifact the fleet
    consumes, so the standalone validator checks IT (not a self-written note,
    which the script no longer produces). Restricts to files matching the
    scheduler's timestamped naming so foreign files never pollute the check.
    """
    if not os.path.isdir(CRON_OUTPUT_DIR):
        return None, None
    best = None
    best_mtime = -1.0
    since_ts = None
    if since_iso:
        s = since_iso.replace("Z", "+00:00")
        try:
            since_ts = dt.datetime.fromisoformat(s).timestamp()
        except Exception:
            since_ts = None
    for name in os.listdir(CRON_OUTPUT_DIR):
        # scheduler writes files like 2026-07-11_00-06-10.md
        if not re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.md$", name):
            continue
        p = os.path.join(CRON_OUTPUT_DIR, name)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        if since_ts is not None and mtime < since_ts:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best = p
    return best, (best_mtime if best else None)


def main() -> int:
    run_label = _now_utc().strftime("%Y-%m-%d %H:%M UTC")
    job = _load_researcher_job()
    last_run_at = (job or {}).get("last_run_at")

    # Post t_78d2b4cf: the researcher script validates its own in-memory body
    # in-process (selfcheck_from_body) and emits the report to stdout, captured
    # by the scheduler to CRON_OUTPUT_DIR. Validate THAT delivered file -- the
    # self-written dated note no longer exists, so checking RESEARCH_DIR would
    # always false-fail (t_7364db59).
    #
    # We validate the newest delivered output UNCONDITIONALLY (no "since"
    # gating). The standalone validator is only ever invoked by the scheduler
    # immediately after a real run, so a timestamp gate is redundant -- and it
    # was actively harmful: a prior false-fail had advanced last_run_at past
    # the real newest output, permanently hiding it (t_7364db59).
    path, mtime = _most_recent_cron_output(None)

    problems = []
    if path is None:
        problems.append("no-cron-output-file-delivered-since-last-run")
    else:
        ok, reasons = _content_checks(path)
        if not ok:
            problems.extend(reasons)

    if problems:
        detail = "; ".join(problems)
        # Fail-closed: demote the researcher job's status from ok to error.
        try:
            mark_job_run(
                RESEARCHER_JOB_ID,
                False,
                f"FAIL-CLOSED validator ({run_label}): {detail}",
            )
            action = f"demoted researcher job {RESEARCHER_JOB_ID} last_status -> error"
        except Exception as e:
            action = f"validator detected failure but could not demote job: {e}"
        print(
            "# quant-researcher-6h FAIL-CLOSED validator\n"
            f"**Run time:** {run_label}\n"
            f"**Researcher last_run_at:** {last_run_at}\n"
            f"**Checked file:** {path or '(none)'}\n"
            f"**Failure reasons:** {detail}\n"
            f"**Action:** {action}\n"
        )
        # Exit 0: this watchdog itself succeeded; the researcher job now shows red.
        return 0

    print(
        "# quant-researcher-6h validator PASS\n"
        f"**Run time:** {run_label}\n"
        f"**Researcher last_run_at:** {last_run_at}\n"
        f"**Verified file:** {path}\n"
        "**Result:** genuine 2026 research artifact with freshness-gate metrics present.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
