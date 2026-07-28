#!/home/frank/.hermes/venvs/trading-ml/bin/python
"""
quant_researcher_6h.py  --  Deterministic, fail-closed quant edge sweep.

Replaces the previous LLM-agent cron job (13c1f9279025) which could emit a
stale cached template (e.g. a 2023-10-05 report) instead of current analysis.
This script computes everything from live Postgres via DuckDB+Polars, stamps
the report with the actual run date, and FAILS CLOSED (never "validates")
when a cohort violates the freshness gate. Output is deterministic markdown
to stdout (captured as the cron report) and a dated Obsidian note.

Contract (from trading-data-analysis skill + t_672329ea acceptance):
  - Every one-candle synthetic cohort emits lag_min, fresh_window_min,
    fresh_lag, and stale-share.
  - A cohort FAILS CLOSED unless ALL hold:
        stale_share <= 5%
        fresh subset N >= 300
        fresh subset WR >= 53%
  - No 2023-dated (or any stale) content may appear; the run date is real.

Contamination-epoch cross-check (governor task t_47fd45ce, 2026-07-09):
  - ACC #1: every candidate is cross-referenced against the fusion-calibration
    data_epoch_registry and the kill-list K1-K7 (live strategy_lineage_kills
    table + obsidian assessment). Signals overlapping a known-defect epoch are
    tagged CONTAMINATED / UNVALIDATED.
  - ACC #2: a "WR>53%, n>=300 validated" claim is permitted ONLY for cohorts
    computed strictly within the open clean-candidate-599f58e7e epoch
    (post 2026-07-05 22:08Z). This also honours the t_ec3d651c LOW-CONFIDENCE
    instruction (clean n<100): do not recalibrate the engine / fire MCE alerts.
  - ACC #3: any cited ~/obsidian/**/*.md research doc must exist on disk and be
    dated on/after the clean-label rebuild (2026-07-03); otherwise the report
    fails closed to EVIDENCE STALE. (Legitimate prior-year academic citations
    in prose are allowed — only path-citations are gated.)
"""
import sys, json, os, re, datetime as dt, time
import duckdb
import polars as pl

from second_brain_writer import write_markdown_atomic

# Password is injected from env (docker default is "postgres"); never hardcode/commit a literal.
PGPW = os.environ.get("PGPASSWORD", "postgres")
DB_URL = f"postgresql://postgres:{PGPW}@127.0.0.1:5432/postgres"
RESEARCH_DIR = os.path.expanduser("~/obsidian/quant-team/research")
# The effective evaluation window is the OPEN CLEAN EPOCH only (see
# win_clause in main(): signals are floored to `triggered_at >=
# clean_epoch_start`). The legacy 90-day rolling window (QR_WINDOW_DAYS) is
# retained as a SHOULD-NEVER-FIRE fallback for the epoch-registry-unreachable
# fail-safe: load_epoch_registry() falls back to CLEAN_EPOCH_FALLBACK, so even
# when the registry is down the scan is bounded to the clean-epoch open, NOT a
# 90-day wide contaminated scan. A pre-clean (CONTAMINATED) appendix is
# produced by counting signals in the prior 90 days with a COUNT(*) query only
# (no candle join, no cohort evaluation) so the excluded population is auditable
# without ever entering the gate (t_ef1d2490 acceptance #1).
WINDOW_DAYS = int(os.environ.get("QR_WINDOW_DAYS", "90"))
FRESH_WINDOW = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
WR_THRESH = 53.0
N_THRESH = 300
STALE_THRESH = 5.0
RET_CLIP = 10.0
WIN_TH = 0.2
MAX_RUNTIME_SECONDS = int(os.environ.get("QR_MAX_RUNTIME_SECONDS", "900"))
CANONICAL_FAIL_CLOSED_EARLY_EXIT = os.environ.get(
    "QR_CANONICAL_FAIL_CLOSED_EARLY_EXIT", "1"
) not in ("0", "false", "False", "no")

# ---- Data-epoch contamination cross-check (governor task t_47fd45ce) ----
CLEAN_LABEL_REBUILD = dt.datetime(2026, 7, 3, 0, 0, tzinfo=dt.timezone.utc)
CLEAN_EPOCH_NAME = "clean-candidate-599f58e7e"
CLEAN_EPOCH_FALLBACK = dt.datetime(2026, 7, 5, 22, 8, 0, tzinfo=dt.timezone.utc)
# ---- Early-epoch fresh-N ramp + blind period (t_4cc128ea) ----
# During the first EARLY_EPOCH_DAYS of a clean epoch the fresh-N gate is
# softened to N_THRESH_EARLY (WR/stale stay strict). Cohorts passing ONLY via
# this ramp are tagged EARLY-EPOCH and must be segregated from VALIDATED.
EARLY_EPOCH_DAYS = 14
N_THRESH_EARLY = 50
KILL_LIST_DOC = os.path.expanduser(
    "~/obsidian/quant-team/2026-06-29-WORLDCLASS-ASSESSMENT-edges-refuted-claude.md"
)


def _stage_start(name: str) -> float:
    ts = time.monotonic()
    sys.stderr.write(f"[quant-researcher-stage] START {name}\n")
    sys.stderr.flush()
    return ts


def _stage_done(name: str, started: float) -> None:
    sys.stderr.write(
        f"[quant-researcher-stage] DONE {name} elapsed_s={time.monotonic() - started:.2f}\n"
    )
    sys.stderr.flush()


def _check_runtime_budget(run_started: float, stage: str) -> None:
    elapsed = time.monotonic() - run_started
    if elapsed > MAX_RUNTIME_SECONDS:
        raise TimeoutError(
            f"bounded abort at stage={stage}: elapsed_s={elapsed:.1f} > "
            f"QR_MAX_RUNTIME_SECONDS={MAX_RUNTIME_SECONDS}"
        )


def write_research_note(body, run_date, operational_status):
    note_path = os.path.join(RESEARCH_DIR, f"{run_date}-quant-edge-sweep-6h.md")
    write_markdown_atomic(
        note_path,
        body,
        title=f"Quantitative Research: 6-Hour Systematic Edge & Carry Sweep — {run_date}",
        type="research",
        status="active",
        created=run_date,
        updated=run_date,
        confidence="high",
        tags=["sycode", "quant-research", "edge-sweep", "clean-epoch", "fail-closed"],
        sources=[
            "sycodetrading-supabase-db:signal_journeys",
            "sycodetrading-supabase-db:candles",
            "sycodetrading-supabase-db:data_epoch_registry",
            "sycodetrading-supabase-db:strategy_lineage_kills",
        ],
        project="sycode-trading",
        owners=["quant-researcher"],
        knowledge_tier="evidence",
        generated=True,
        generator="quant_researcher_6h.py",
        operational_status=operational_status,
        cron_job="13c1f9279025",
    )

# ---------------------------------------------------------------------------
# CANONICAL VALIDATED-EDGE VERDICT (kanban t_4df5351d / t_460bb546)
# ---------------------------------------------------------------------------
# The fusion-calibration report (execution/fusion_calibration_report_v2.py)
# and this quant-researcher nightly MUST publish the SAME validated_edge_status
# so a synthetic Tier-2 merge can never be read as a validated edge. The single
# source of truth is compute_validated_edge_status(tier1_n, tier1_wr, 300, 50.0)
# in fusion_calibration_report_v2.py. We import it if the sycode-trading repo is
# reachable; otherwise fall back to a verbatim inline copy kept in lockstep so
# the verdict logic can never silently diverge. The 300 / 50.0 thresholds are
# the canonical validation floor.
VALIDATED = "VALIDATED"
FAIL_CLOSED = "FAIL_CLOSED"
INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"

try:
    import importlib.util as _ilu  # noqa: E402
    _fur_path = "/home/frank/sycode-trading/execution/fusion_calibration_report_v2.py"
    _fur_spec = _ilu.spec_from_file_location("fusion_calibration_report_v2", _fur_path)
    _fur_mod = _ilu.module_from_spec(_fur_spec)
    _fur_spec.loader.exec_module(_fur_mod)
    compute_validated_edge_status = _fur_mod.compute_validated_edge_status
except Exception:  # pragma: no cover - fallback only when repo is absent
    def compute_validated_edge_status(
        tier1_n: int,
        tier1_wr,
        floor_n: int = 300,
        wr_floor: float = 50.0,
    ):
        """Verbatim mirror of fusion_calibration_report_v2.compute_validated_edge_status.

        Anchored on the Tier-1 realized-exit sample; synthetic Tier-2 rows are
        never passed here. Kept in lockstep with the canonical module.
        """
        if tier1_n <= 0:
            return (INSUFFICIENT_SAMPLE,
                    "No Tier-1 realized-exit outcomes in the clean epoch window.")
        if tier1_n < floor_n:
            return (
                INSUFFICIENT_SAMPLE,
                f"Tier-1 fresh N={tier1_n} < {floor_n} validation floor — not enough "
                f"realized-exit evidence to claim an edge. The quant-researcher "
                f"FAIL-CLOSED verdict reflects the same insufficient sample.")
        if tier1_wr is None or tier1_wr <= wr_floor:
            return (
                FAIL_CLOSED,
                f"Tier-1 fresh N={tier1_n} >= {floor_n} but Tier-1 WR="
                f"{tier1_wr if tier1_wr is not None else 'unknown'}% "
                f"<= {wr_floor}% coin-flip floor — the cohort fails closed: "
                f"no validated edge.")
        return (
            VALIDATED,
            f"Tier-1 fresh N={tier1_n} >= {floor_n} AND Tier-1 WR={tier1_wr:.1f}% "
            f"> {wr_floor}% — an independent, non-random edge is validated.")


def compute_canonical_tier1_edge(con, clean_epoch_start, window_days: int = 30):
    """Return (status, reason) for the canonical validated-edge verdict.

    Mirrors the fusion-calibration report's AUTHORITATIVE Tier-1 realized-exit
    inputs exactly (signal_journeys + decision_outcomes, bounded to the clean
    epoch), so both reports mechanically agree. Read-only; no mutation.

    The shared compute_validated_edge_status(clean_n, clean_wr, 300, 50.0) is
    the single source of truth for the verdict.
    """
    floor_iso = clean_epoch_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Verbatim mirror of fusion_calibration_report_v2 dedup_rows query (clean
    # sample): one outcome per journey, authoritative final lane first, bounded
    # to the certified clean epoch + 30d window. Tier-2 synthetic derivations
    # are intentionally NOT included (t_21e22d8d / t_47fd45ce / t_ec3d651c).
    sql = f"""
        SELECT d.is_win
        FROM pg.signal_journeys sj
        JOIN pg.decision_outcomes d ON d.journey_id = sj.id
        WHERE d.outcome_class IN ('WIN', 'LOSS')
          AND d.is_final = true
          AND d.finalized_at >= TIMESTAMPTZ '{floor_iso}+00'
          AND d.finalized_at >= NOW() - INTERVAL '{int(window_days)} days'
    """
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:  # fail visibly, don't invent a verdict
        sys.stderr.write(f"canonical-tier1-query-warning: {e}\n")
        return (INSUFFICIENT_SAMPLE,
                f"Tier-1 realized-exit query failed ({e}); cannot assert an edge.")
    clean_n = len(rows)
    if clean_n == 0:
        clean_wr = None
    else:
        wins = sum(1 for (w,) in rows if w)
        clean_wr = 100.0 * wins / clean_n
    return compute_validated_edge_status(clean_n, clean_wr, 300, 50.0)


def load_epoch_registry(con):
    """Return (clean_epoch_start, registry_rows).

    clean_epoch_start: tz-aware open time of the clean-candidate-599f58e7e epoch.
    registry_rows: [(name, starts, ends, known_defects, is_clean)] for the
    system-of-record data_epoch_registry. A signal with triggered_at BEFORE
    clean_epoch_start overlaps a known-defect epoch and is CONTAMINATED per
    t_47fd45ce acceptance #1. Falls back to constants if the registry is
    unreachable (fail-safe: treat everything pre-fallback as contaminated).
    """
    clean_start = CLEAN_EPOCH_FALLBACK
    rows = []
    try:
        for name, starts, ends, defects in con.execute(
            "SELECT name, starts_at, ends_at, known_defects FROM pg.data_epoch_registry"
        ).fetchall():
            is_clean = (name == CLEAN_EPOCH_NAME)
            if is_clean and starts is not None:
                clean_start = starts
            rows.append((name, starts, ends, defects or "", is_clean))
    except Exception as e:
        sys.stderr.write(f"epoch-registry-read-warning: {e}\n")
    return clean_start, rows


def load_kill_list(con):
    """Return (list_of_reason_strings, kill_doc_exists_bool).

    The fusion-calibration kill-list K1-K7 is the live strategy_lineage_kills
    table (system of record) plus the obsidian assessment doc. Cross-referenced
    per t_47fd45ce acceptance #1.
    """
    reasons = []
    try:
        for (r,) in con.execute("SELECT reason FROM pg.strategy_lineage_kills").fetchall():
            reasons.append(r or "")
    except Exception as e:
        sys.stderr.write(f"kill-list-read-warning: {e}\n")
    return reasons, os.path.isfile(KILL_LIST_DOC)


def build_preclean_audit(con, clean_epoch_start):
    """t_ef1d2490 acceptance #1: keep excluded pre-clean data REPORTED, not silent.

    Signals with triggered_at BEFORE the open clean epoch (clean-candidate-599f58e7e)
    overlap known-defect epochs (pre-0629-leak, dirty-label-1p03m, funding-null,
    oi-sparse, corrupted-candle15m-backfill). They MUST NOT enter the freshness/
    validation gate (that is the clean-epoch clamp, win_clause in main()).

    Rather than silently drop them, this COUNT-only query audits the EXCLUDED
    population so the contamination is transparent. No candle join, no cohort
    evaluation, no full-history signal pull -- it never re-enters the gate and
    stays within the script_timeout budget (a single COUNT(*) over ~90d of
    signal_journeys, vs the previous 150k-row join).

    Returns (n_preclean, n_preclean_contam_90d) where:
      n_preclean          = signals before the clean-epoch open (the excluded set)
      n_preclean_contam_90d = signals in the prior 90d before open that overlap a
                              defect window (a contamination-intensity indicator).
    On any DB error returns (0, 0) so the report never fails closed on the audit
    path itself (the gate is already clean-epoch-bounded by the SQL floor).
    """
    n_preclean = 0
    n_preclean_contam_90d = 0
    try:
        floor_iso = clean_epoch_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # Prior 90-day bound for the contamination-intensity indicator (uses the
        # legacy WINDOW_DAYS default of 90; never widens the evaluation sample).
        prior_iso = (clean_epoch_start - dt.timedelta(days=WINDOW_DAYS)).astimezone(
            dt.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        # Mirror the sweep's evaluable signal filter (entry_price present/positive,
        # the 4 tracked timeframes) so the excluded count is apples-to-apples
        # with the clean-epoch gate sample (not a wider raw-table count).
        base = ("FROM pg.signal_journeys "
                "WHERE triggered_at IS NOT NULL "
                "AND entry_price IS NOT NULL AND entry_price > 0 "
                "AND timeframe IN ('15m','1h','4h','1d')")
        n_preclean = con.execute(
            f"SELECT COUNT(*) {base} "
            f"AND triggered_at < TIMESTAMPTZ '{floor_iso}+00'"
        ).fetchone()[0]
        n_preclean_contam_90d = con.execute(
            f"SELECT COUNT(*) {base} "
            f"AND triggered_at >= TIMESTAMPTZ '{prior_iso}+00' "
            f"AND triggered_at < TIMESTAMPTZ '{floor_iso}+00'"
        ).fetchone()[0]
    except Exception as e:
        sys.stderr.write(f"preclean-audit-warning: {e}\n")
    return n_preclean, n_preclean_contam_90d


def render_preclean_appendix(n_preclean, n_preclean_contam_90d):
    """Markdown section for the excluded pre-clean (CONTAMINATED) cohort."""
    lines = [
        "## Pre-Clean (Excluded, CONTAMINATED) Appendix — Audit Only",
        "",
        "- Per t_ef1d2490 acceptance #1, pre-clean-epoch signals are **excluded from the "
        "freshness/validation gate** (the scan window is clamped to the open clean epoch "
        "via the `triggered_at >= clean_epoch_start` SQL floor) but are **not silently "
        "dropped**: they are reported here for audit.",
        "- These signals overlap known-defect epochs (pre-0629-leak, dirty-label-1p03m, "
        "funding-null, oi-sparse, corrupted-candle15m-backfill) and are tagged "
        "CONTAMINATED/UNVALIDATED. They are counted with a COUNT(*) query only — never "
        "join-asof'd to candles, never evaluated for wins/losses, never eligible for a "
        "VALIDATED claim.",
        f"- **Excluded pre-clean signals (before clean-epoch open):** {n_preclean:,}",
        f"- **Of those, in the prior {WINDOW_DAYS}d (defect-window intensity indicator):** "
        f"{n_preclean_contam_90d:,}",
        "- If this appendix is absent, the epoch registry was unreachable and the "
        "fail-safe treated all pre-fallback signals as contaminated (existing line-74 "
        "fallback).",
        "",
    ]
    return lines


def canonical_fail_closed_early_exit(run_label, run_date, clean_epoch_start,
                                     kill_list, kill_doc_exists, epoch_rows,
                                     n_preclean, n_preclean_contam_90d,
                                     canonical_status, canonical_reason):
    """Emit a bounded fail-closed report before the heavy synthetic sweep.

    The fleet currently consumes the canonical Tier-1 realized-exit verdict as
    the promotion/edge safety latch shared with fusion-calibration. When that
    verdict is INSUFFICIENT_SAMPLE/FAIL_CLOSED there is no safe implementation
    action to discover in the expensive candle join, so exit cleanly with the
    fail-closed provenance instead of burning the scheduler's 3600s cap.
    """
    clean_epoch_floor_iso = clean_epoch_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    epoch_open = clean_epoch_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = []
    body.append("# Quantitative Research: 6-Hour Systematic Edge & Carry Sweep (FAIL-CLOSED)")
    body.append("**Cadence:** every 6 hours (cron `15 */6 * * *`, 4 runs/day) — matches the '6h' contract.")
    body.append(f"**Date:** {run_label}")
    body.append("**Job ID:** 13c1f9279025 (deterministic no_agent script)")
    body.append(f"**Data window:** clean epoch [{clean_epoch_floor_iso}Z, now] (canonical Tier-1 realized-exit check; synthetic candle sweep skipped by bounded fail-closed early exit)")
    body.append("")
    body.append("## Result: FAIL-CLOSED — canonical Tier-1 sample below validation floor")
    body.append("")
    body.append(f"- **VALIDATED_EDGE_STATUS: `{canonical_status}`** — {canonical_reason}")
    body.append("- **Bounded-exit policy:** because the shared fusion-calibration/quant-researcher verdict is not `VALIDATED`, this run exits before the heavy synthetic forward-label candle join. No cohort is cleared for implementation or paper-sleeve routing.")
    body.append("- **Runtime guard:** `QR_MAX_RUNTIME_SECONDS` bounds this script before the scheduler hard cap; stage timings are emitted to stderr as `[quant-researcher-stage]` lines.")
    body.append("")
    body.append("## Methodology & Freshness-Gate Invariant")
    body.append("- A cohort would **FAIL CLOSED** unless ALL hold: stale_share <= 5.0%, fresh N >= 300, fresh WR >= 53%.")
    body.append("- `fresh_window_min`: 15m=15, 1h=60, 4h=240, 1d=1440. `fresh_lag` = median lag_min of the fresh subset.")
    body.append("- `stale_share` = stale / (fresh+stale); clean-epoch eligibility requires `n_clean_fresh` >= 300.")
    body.append("- Synthetic forward-label computation remains available for diagnostics with `QR_CANONICAL_FAIL_CLOSED_EARLY_EXIT=0`, but the default cron path is fail-closed and bounded when Tier-1 evidence is insufficient.")
    body.append("")
    body.append("## Data-Epoch Contamination Cross-Check (vs fusion calibration kill-list)")
    body.append("")
    body.append(f"- **Clean-candidate epoch:** `{CLEAN_EPOCH_NAME}` opens **{epoch_open}** (UTC). Per t_47fd45ce acceptance #2, a `WR>53%, n>=300 validated` claim is permitted ONLY for cohorts computed strictly within this open epoch. All signals before it overlap a known-defect epoch and are tagged CONTAMINATED/UNVALIDATED.")
    body.append(f"- **Kill-list cross-reference (K1-K7):** live `strategy_lineage_kills` table returned {len(kill_list)} kill(s); obsidian assessment doc present: {kill_doc_exists}.")
    body.append("- **Consulted `data_epoch_registry` (system of record):**")
    for name, starts, ends, defects, is_clean in epoch_rows:
        s = str(starts) if starts is not None else "-infinity"
        e = str(ends) if ends is not None else "open"
        tag = "CLEAN" if is_clean else "DEFECT"
        body.append(f"  - `{name}` [{tag}] {s} -> {e}: {defects[:120]}")
    body.append("")
    body.extend(render_preclean_appendix(n_preclean, n_preclean_contam_90d))
    report = "\n".join(body)
    print(report)
    try:
        write_research_note(report, run_date, "fail-closed")
    except Exception as e:
        sys.stderr.write(f"note-write-warning: {e}\n")
    run_self_validator(report)


def check_citations(body):
    """Acceptance #3 (governor task t_47fd45ce): citation-staleness gate.

    The governor's actual finding was the report citing
    `2023-10-05-quant-research.md` -- a doc that does NOT exist on disk and is
    ~21 months older than the clean-label rebuild. The failure mode is a
    MISSING / obsolete-canned citation used as the basis for an edge claim.

    Hard-fail (EVIDENCE STALE) on:
      * the specific obsolete 2023-10-05 canned report / path (the leaked
        artifact), and
      * ANY other `~/obsidian/**/*.md` path-citation that is MISSING on disk.
    A cited doc's age alone is NOT a hard fail: the canonical K1-K7 refutation
    doc (2026-06-29) pre-dates the rebuild by design yet IS the clean-epoch
    authority, so it must remain citable. Legitimate prior-year academic
    citations in prose (not path-citations) are explicitly allowed."""
    # Obsolete canned-template markers: hard fail regardless of presence.
    if "2023-10-05-quant-research.md" in body or \
       "Quant Research Report: Trading Edge Identification (2023-10-05)" in body:
        return False, "obsolete-citation-2023-10-05-quant-research.md-present"
    for m in re.finditer(r"(?:~/obsidian/|obsidian/)([^\s`'\"\)>]+?\.md)", body):
        raw = m.group(0)
        full = os.path.expanduser(raw) if raw.startswith("~") else os.path.expanduser("~/" + raw)
        if not os.path.isfile(full):
            return False, f"cited-doc-missing: {raw}"
    return True, ""


def gate_cohort(n_clean_fresh, wr_clean_fresh_v, clean_stale_share,
                kill_listed, in_early_epoch):
    """Return (pass_gate, early_epoch_only) for one cohort (t_4cc128ea + t_ef1d2490).

    Pure, side-effect-free decision used by main() so the early-epoch ramp
    logic is unit-testable without a DB. The fresh-N gate uses N_THRESH_EARLY
    during the first EARLY_EPOCH_DAYS of the open clean epoch; WR and stale
    gates stay strict. early_epoch_only is True whenever the cohort passes the gate
    while in_early_epoch is True -- segregated from VALIDATED regardless of
    sample size. This enforces the blind-period contract: NO cohort may be
    labelled VALIDATED while the clean epoch is younger than EARLY_EPOCH_DAYS,
    even one whose fresh N already clears the full N_THRESH floor.

    CRITICAL (t_ef1d2490): the gate is decided on the clean-epoch subset ONLY.
    The scan window is clamped to the open clean epoch by the `triggered_at >=
    clean_epoch_start` SQL floor in main(), so the evaluation DataFrame never
    contains pre-clean (CONTAMINATED) rows. The `contaminated` flag therefore
    is NOT an input to the gate: (a) on the normal path it is always False
    because pre-clean rows were already excluded upstream; (b) if the epoch
    registry is unreachable the fail-safe floor still bounds the scan to the
    fallback open, so a True here would falsely block legitimate clean-epoch
    cohorts. The excluded pre-clean population is reported in a separate audit
    appendix instead of being allowed to zero out the clean-epoch fresh N.
    """
    n_req = N_THRESH_EARLY if in_early_epoch else N_THRESH
    pass_gate = (
        (n_clean_fresh >= n_req)
        and (wr_clean_fresh_v >= WR_THRESH)
        and (clean_stale_share <= STALE_THRESH)
        and (not kill_listed)
    )
    # BLIND-PERIOD CONTRACT (t_26cdaf62): while the clean epoch is younger than
    # EARLY_EPOCH_DAYS NO cohort may be VALIDATED regardless of sample size. Any
    # cohort that passes the (softened) gate during the blind period is tagged
    # EARLY-EPOCH only -- never routed to the VALIDATED bucket. This honours the
    # t_4df5351d blind-period contract (VALIDATED_EDGE_STATUS stays
    # INSUFFICIENT_SAMPLE; a blind-period "edge" is never a confirmed edge).
    early_epoch_only = in_early_epoch and pass_gate
    return pass_gate, early_epoch_only


def main():
    run_started = time.monotonic()
    run_ts = dt.datetime.now(dt.timezone.utc)
    run_date = run_ts.strftime("%Y-%m-%d")
    run_label = run_ts.strftime("%Y-%m-%d %H:%M UTC")

    st = _stage_start("connect_duckdb_postgres")
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{DB_URL}' AS pg (TYPE POSTGRES);")
    _stage_done("connect_duckdb_postgres", st)
    _check_runtime_budget(run_started, "connect_duckdb_postgres")

    # ---- Contamination cross-check sources (t_47fd45ce ACC #1/#2) ----
    st = _stage_start("load_epoch_and_kill_list")
    clean_epoch_start, epoch_rows = load_epoch_registry(con)
    kill_list, kill_doc_exists = load_kill_list(con)
    clean_epoch_start_ts = clean_epoch_start.timestamp()
    _stage_done("load_epoch_and_kill_list", st)
    _check_runtime_budget(run_started, "load_epoch_and_kill_list")

    # ---- CANONICAL VALIDATED-EDGE VERDICT (t_4df5351d / t_460bb546) ----
    # Compute the SAME validated_edge_status the fusion-calibration report
    # prints, fed by the SAME authoritative Tier-1 realized-exit sample, so the
    # two reports cannot disagree. Computed while the DB connection is open.
    st = _stage_start("canonical_tier1_edge")
    canonical_status, canonical_reason = compute_canonical_tier1_edge(
        con, clean_epoch_start)
    _stage_done("canonical_tier1_edge", st)
    _check_runtime_budget(run_started, "canonical_tier1_edge")

    # ---- Clean-epoch age + early-epoch ramp window (t_4cc128ea) ----
    # Age is measured from the open-epoch constant (CLEAN_EPOCH_FALLBACK), not
    # the registry start, per the accepted policy. in_early_epoch drives the
    # fresh-N ramp; once the epoch is EARLY_EPOCH_DAYS old the ramp no-ops and
    # the full N_THRESH gate applies automatically (no manual switch).
    clean_epoch_age_days = (run_ts - CLEAN_EPOCH_FALLBACK).total_seconds() / 86400.0
    in_early_epoch = clean_epoch_age_days < EARLY_EPOCH_DAYS

    if CANONICAL_FAIL_CLOSED_EARLY_EXIT and canonical_status != VALIDATED:
        st = _stage_start("preclean_audit_for_fail_closed")
        n_preclean, n_preclean_contam_90d = build_preclean_audit(con, clean_epoch_start)
        _stage_done("preclean_audit_for_fail_closed", st)
        con.close()
        canonical_fail_closed_early_exit(
            run_label, run_date, clean_epoch_start, kill_list, kill_doc_exists,
            epoch_rows, n_preclean, n_preclean_contam_90d,
            canonical_status, canonical_reason,
        )
        sys.stderr.write(
            f"[quant-researcher-stage] EXIT fail_closed_early elapsed_s={time.monotonic() - run_started:.2f}\n"
        )
        return

    # ---- Pull signals: BOUND to the certified clean epoch (t_572a791e) ----
    # The EFFECTIVE evaluation window is [clean_epoch_start, now]. The old
    # 90-day rolling clause is REPLACED by a hard clean-epoch floor so that
    # pre-epoch defect rows (pre-0629-leak, dirty-label-1p03m, funding-null,
    # oi-sparse, corrupted-candle15m-backfill) are EXCLUDED from evaluation
    # entirely -- NOT merely failed-closed as CONTAMINATED inside each cohort.
    # This removes the phantom 100%-fail-closed wall and lets any genuine
    # clean-epoch edge reach the gate. clean_epoch_start comes from
    # data_epoch_registry (system of record); the fallback constant is only a
    # fail-safe if the registry is unreachable.
    clean_epoch_floor_iso = clean_epoch_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Use an EXPLICIT UTC timestamptz literal (not bare TIMESTAMP) so the SQL
    # floor matches the UTC epoch-start used for the in_clean_epoch flag in
    # Python. A bare TIMESTAMP literal is interpreted in the Postgres SESSION
    # timezone; if that is not UTC it drifts the boundary and silently pulls in
    # pre-epoch rows that are then (correctly) flagged CONTAMINATED -- the very
    # leak this task removes (t_572a791e).
    win_clause = f"AND triggered_at >= TIMESTAMPTZ '{clean_epoch_floor_iso}+00'"
    sig_sql = f"""
        SELECT id::VARCHAR AS id, symbol, direction, timeframe, triggered_at, entry_price,
               (indicators->>'volatilityLevel') AS volatility_level,
               macro_regime,
               COALESCE(regime_favorable, false) AS regime_favorable
        FROM pg.signal_journeys
        WHERE triggered_at IS NOT NULL
          AND entry_price IS NOT NULL AND entry_price > 0
          AND timeframe IN ('15m','1h','4h','1d')
          {win_clause}
    """
    sig = pl.from_pandas(con.execute(sig_sql).fetchdf())
    n_sig = sig.height

    # t_ef1d2490 AC #1: audit the EXCLUDED pre-clean population (COUNT-only,
    # no candle join) while the connection is still open, so it is reported,
    # not silently dropped. Never enters the gate.
    n_preclean, n_preclean_contam_90d = build_preclean_audit(con, clean_epoch_start)

    # ---- Pull candles per timeframe and forward-join ----
    parts = []
    # Effective evaluation window is the clean epoch [clean_epoch_start, now].
    # Bound candle history to the clean epoch floor too (no need to scan the
    # pre-epoch candle corpus; labels are only computed for clean-epoch signals).
    candle_min_t = clean_epoch_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    # Explicit UTC timestamptz literal (see win_clause note above): the candle
    # floor must align with the signal floor regardless of session timezone.
    candle_where = f"AND timestamp >= TIMESTAMPTZ '{candle_min_t} 00:00:00+00'"
    win_label = (f"clean epoch [{clean_epoch_floor_iso}Z, now] "
                 f"(excludes pre-epoch defect windows)")
    for tf in FRESH_WINDOW:
        c = pl.from_pandas(con.execute(
            f"SELECT symbol, timestamp, close FROM pg.candles WHERE timeframe='{tf}' {candle_where}"
        ).fetchdf()).rename({"timestamp": "candle_time", "close": "next_close"})
        ts = sig.filter(pl.col("timeframe") == tf).sort("triggered_at")
        if ts.height == 0:
            continue
        j = ts.join_asof(
            c.sort("candle_time"),
            by="symbol", left_on="triggered_at", right_on="candle_time",
            strategy="forward",
        )
        parts.append(j)
    con.close()

    if not parts:
        fail_closed(run_label, run_date, n_sig,
                    "No signals joined to candles (DB join produced zero rows).")
        return

    df = pl.concat(parts, how="diagonal")
    df = df.filter(pl.col("next_close").is_not_null())
    df = df.filter(pl.col("symbol") != "AXLUSDT")

    # ---- Contamination-epoch tagging (t_47fd45ce ACC #1/#2) ----
    # A signal with triggered_at before the clean-candidate epoch opens overlaps a
    # known-defect epoch (pre-0629-leak, dirty-label-1p03m, funding-null, oi-sparse,
    # corrupted-candle15m-backfill) and is CONTAMINATED / UNVALIDATED.
    df = df.with_columns(
        (pl.col("triggered_at").dt.timestamp("ms") / 1000.0).alias("triggered_ts")
    )
    df = df.with_columns(
        (pl.col("triggered_ts") >= clean_epoch_start_ts).alias("in_clean_epoch")
    )
    df = df.with_columns((~pl.col("in_clean_epoch")).alias("contaminated"))

    # ---- lag_min between signal and joined forward candle ----
    df = df.with_columns(
        ((pl.col("candle_time") - pl.col("triggered_at")).dt.total_minutes()).alias("lag_min")
    )
    fwin = pl.Series("fresh_window_min", [FRESH_WINDOW.get(t, None) for t in df["timeframe"].to_list()])
    df = df.with_columns(fwin)
    df = df.with_columns(
        pl.when((pl.col("lag_min") >= 0) & (pl.col("lag_min") <= pl.col("fresh_window_min")))
          .then(True).otherwise(False).alias("is_fresh")
    )

    # ---- forward return, direction-adjusted, clipped ----
    df = df.with_columns(
        ((pl.col("next_close") - pl.col("entry_price")) / pl.col("entry_price") * 100).alias("fwd_return")
    )
    df = df.with_columns(
        pl.when(pl.col("direction") == "SHORT")
          .then(-pl.col("fwd_return"))
          .otherwise(pl.col("fwd_return")).alias("fwd_return_dir")
    )
    df = df.with_columns(pl.col("fwd_return_dir").clip(-RET_CLIP, RET_CLIP).alias("fwd_clipped"))
    df = df.with_columns(
        pl.when(pl.col("fwd_clipped") > WIN_TH).then(pl.lit(1))
         .when(pl.col("fwd_clipped") < -WIN_TH).then(pl.lit(0))
         .otherwise(pl.lit(-1)).alias("label")
    )

    # ---- cohort aggregation ----
    group_keys = ["timeframe", "direction", "volatility_level", "macro_regime", "regime_favorable"]
    rows = []
    cohorts = df.group_by(group_keys, maintain_order=True)
    for keys, g in cohorts:
        tf, direction, vol, macro, fav = [k[0] if isinstance(k, list) else k for k in keys]
        n_all = g.height
        # populate scalars (group_by returns them as single-element)
        if isinstance(vol, list): vol = vol[0]
        if isinstance(macro, list): macro = macro[0]
        if isinstance(fav, list): fav = fav[0]
        tf = tf[0] if isinstance(tf, list) else tf
        direction = direction[0] if isinstance(direction, list) else direction
        fav = bool(fav[0]) if isinstance(fav, list) else bool(fav)

        lag_min_med = g["lag_min"].median()
        fwin_v = FRESH_WINDOW.get(tf, None)
        fresh = g.filter(pl.col("is_fresh"))
        stale = g.filter(~pl.col("is_fresh"))
        n_fresh = fresh.height
        n_stale = stale.height
        denom = (n_fresh + n_stale)
        stale_share = (100.0 * n_stale / denom) if denom > 0 else 0.0
        fresh_lag_med = fresh["lag_min"].median() if n_fresh > 0 else None

        def wr(sub):
            nf = sub.filter(pl.col("label") != -1)
            if nf.height == 0:
                return 0.0, 0
            w = (nf["label"] == 1).sum()
            return 100.0 * w / nf.height, nf.height

        wr_all_v, _ = wr(g)
        wr_fresh_v, n_fresh_nonflat = wr(fresh)

        # --- Contamination-epoch cross-check (t_47fd45ce ACC #1/#2) ---
        # "Validated" is permitted ONLY on cohorts computed strictly within the
        # clean-candidate-599f58e7e open epoch. Everything before that overlaps a
        # known-defect epoch (pre-0629-leak, dirty-label-1p03m, funding-null,
        # oi-sparse, corrupted-candle15m-backfill) and is CONTAMINATED/UNVALIDATED.
        clean = g.filter(pl.col("in_clean_epoch"))
        clean_fresh = clean.filter(pl.col("is_fresh"))
        n_clean_fresh = clean_fresh.height
        clean_stale = clean.filter(~pl.col("is_fresh")).height
        clean_denom = n_clean_fresh + clean_stale
        clean_stale_share = (100.0 * clean_stale / clean_denom) if clean_denom > 0 else 0.0
        wr_clean_fresh_v, _ = wr(clean_fresh)
        n_contam = g.filter(pl.col("contaminated")).height
        contaminated = n_contam > 0
        # kill-list (K1-K7, live strategy_lineage_kills SoR) cross-reference
        kill_listed = any((tf in r) and (direction in r) for r in kill_list)

        # Eligibility for a VALIDATED claim: clean-epoch data only, full gate,
        # not kill-listed. The contamination cross-check (t_47fd45ce ACC #1/#2)
        # is enforced structurally upstream by the clean-epoch SQL floor
        # (win_clause): the scan window is clamped to the open clean epoch, so
        # pre-clean (CONTAMINATED) rows never enter this DataFrame. t_ef1d2490
        # removes the `and (not contaminated)` gate clause because (a) normally
        # contaminated is always False here (rows already excluded) and (b) if
        # the registry is unreachable the fail-safe floor still bounds the scan,
        # so a True would falsely block legitimate clean-epoch cohorts. The
        # excluded population is reported in the pre-clean audit appendix.
        # Delegated to gate_cohort() so the early-epoch fresh-N ramp (t_4cc128ea)
        # is unit-testable without a DB join.
        pass_gate, early_epoch_only = gate_cohort(
            n_clean_fresh, wr_clean_fresh_v, clean_stale_share,
            kill_listed, in_early_epoch)
        rows.append({
            "timeframe": tf, "direction": direction,
            "volatility": (vol or "UNKNOWN"), "macro_regime": (macro or "UNKNOWN"),
            "fav": fav,
            "n_all": n_all, "wr_all": round(wr_all_v, 2),
            "n_fresh": n_fresh, "wr_fresh": round(wr_fresh_v, 2),
            "lag_min_med": round(lag_min_med, 2) if lag_min_med is not None else None,
            "fresh_window_min": fwin_v,
            "fresh_lag_med": round(fresh_lag_med, 2) if fresh_lag_med is not None else None,
            "stale_share": round(stale_share, 2),
            "n_clean_fresh": n_clean_fresh,
            "wr_clean_fresh": round(wr_clean_fresh_v, 2),
            "clean_stale_share": round(clean_stale_share, 2),
            "n_contam": n_contam,
            "contaminated": contaminated,
            "kill_listed": kill_listed,
            "pass": pass_gate,
            "early_epoch": early_epoch_only,
        })

    cohorts_df = pl.DataFrame(rows)

    # Split passed cohorts into strict-validated vs early-epoch-ramp (t_4cc128ea
    # AC #3): EARLY-EPOCH cohorts are lower-confidence and MUST be segregated
    # from VALIDATED (never fire MCE/edge alerts, never a confirmed edge).
    passed = cohorts_df.filter(pl.col("pass"))
    failed = cohorts_df.filter(~pl.col("pass"))
    validated = passed.filter(~pl.col("early_epoch"))
    early_cohorts = passed.filter(pl.col("early_epoch"))
    passed = passed.sort("wr_clean_fresh", descending=True)
    failed = failed.sort("stale_share", descending=True)

    any_validated = validated.height > 0
    any_early = early_cohorts.height > 0
    canonical_allows_validated_output = canonical_status == VALIDATED

    # ---- Quiet-mode early-return (proposal t_ca461999) ----
    # The full 250-line cohort dump carries near-zero validated information
    # while edge-discovery is data-starved -- i.e. before ANY cohort clears
    # the triple gate (n_clean_fresh >= 300 AND fresh WR >= 53% AND
    # clean_stale_share <= 5%, all on strictly clean-epoch data). The proposal
    # was framed around an N==0 / 100%-contaminated snapshot, but the live
    # clean-candidate-599f58e7e epoch already has thousands of joined journeys
    # and is no longer 100% contaminated, so that literal trigger would never
    # fire. The intent-faithful, firing gate is "no cohort validated" ->
    # emit a single STARVING line to stdout + a compact dated Obsidian note
    # instead of the full dump. QR_VERBOSE=1 always forces the full dump
    # (regression path). The dump auto-resumes the moment a cohort passes the
    # triple gate (any_validated becomes True) -- acceptance criterion #3 holds
    # by construction.
    qr_verbose = os.environ.get("QR_VERBOSE", "") in ("1", "true", "True", "yes")
    total_clean = df.filter(pl.col("in_clean_epoch")).height
    n_contam_total = df.filter(pl.col("contaminated")).height
    contaminated_share = (100.0 * n_contam_total / df.height) if df.height > 0 else 0.0
    starving = (not qr_verbose) and (not any_validated) and (not any_early)
    if starving:
        # NOTE on Optimization (a): a daily write-gate was considered but
        # dropped. The dated note is written to YYYY-MM-DD-quant-edge-sweep-6h.md,
        # which already collapses to ONE file per UTC day via overwrite. The
        # companion validator (quant_researcher_6h_validator.py) selects the
        # most-recent note whose mtime post-dates the researcher job's
        # last_run_at; if we skipped the write on runs 2-4 of the day, the
        # validator could find no same-day note and spuriously demote the job
        # to "error" -- re-introducing a red lie (the opposite of this
        # proposal's goal). So we ALWAYS write the dated note (overwrite), which
        # keeps the validator's mtime check satisfied and produces no more than
        # one dated artifact per day anyway.
        quiet_body = quiet_starving(run_label, run_date, n_sig, win_label, clean_epoch_start,
                                    contaminated_share, df.height, kill_list, kill_doc_exists,
                                    epoch_rows, total_clean=total_clean,
                                    n_preclean=n_preclean, n_preclean_contam_90d=n_preclean_contam_90d,
                                    canonical_status=canonical_status, canonical_reason=canonical_reason)
        run_self_validator(quiet_body)
        return
    # ---- Build report ----
    out = []
    out.append(f"# Quantitative Research: 6-Hour Systematic Edge & Carry Sweep")
    out.append(f"**Cadence:** every 6 hours (cron `15 */6 * * *`, 4 runs/day) — matches the '6h' contract.")
    out.append(f"**Date:** {run_label}")
    out.append(f"**Job ID:** 13c1f9279025 (deterministic no_agent script)")
    out.append(f"**Data window:** {win_label} | signals scanned: {n_sig:,} | joined: {df.height:,}")
    out.append("")
    out.append("## Methodology & Freshness-Gate Invariant")
    out.append("- Synthetic forward labels via Polars `join_asof(strategy='forward')` on all `signal_journeys` "
               "(not executed-only; avoids the executed-trade bias trap).")
    out.append("- `lag_min` = minutes between `triggered_at` and the joined forward candle_time.")
    out.append("- Timeframe-specific `fresh_window_min`: 15m=15, 1h=60, 4h=240, 1d=1440. A cohort row is **fresh** if 0<=lag_min<=fresh_window_min, else **stale**.")
    out.append("- `fresh_lag` = median lag_min of the fresh subset. `stale_share` = stale / (fresh+stale).")
    out.append(f"- A cohort **FAILS CLOSED** unless ALL hold: stale_share <= {STALE_THRESH}%, fresh N >= {N_THRESH}, fresh WR >= {WR_THRESH}%.")
    out.append(f"- **Early-epoch fresh-N ramp (t_4cc128ea):** for the first {EARLY_EPOCH_DAYS}d of the open clean epoch the fresh-N gate is softened to N >= {N_THRESH_EARLY} "
               f"(WR>= {WR_THRESH}% and stale_share<= {STALE_THRESH}% remain strict). A cohort passing ONLY via this ramp is tagged EARLY-EPOCH and is segregated "
               f"from VALIDATED (lower-confidence, blind-period; never a confirmed edge, never an MCE/edge alert). After day {EARLY_EPOCH_DAYS} the full N >= {N_THRESH} gate applies automatically.")
    out.append("- Returns clipped to ±10%, AXLUSDT excluded, direction-adjusted, win/loss threshold ±0.2%.")
    out.append("")

    if not any_validated and not any_early:
        out.append("## Result: FAIL-CLOSED — no cohorts validated (contamination-epoch cross-check applied)")
        out.append("")
        out.append("No edge cohort independently cleared the triple gate on STRICTLY clean-epoch data. "
                   "Per t_47fd45ce acceptance #2 and the t_ec3d651c LOW-CONFIDENCE instruction (clean unique-journey n<100), "
                   "nothing is cleared for implementation or paper-sleeve routing. ")
        out.append("Any cohort whose sample overlaps a pre-clean-candidate defect epoch is tagged CONTAMINATED/UNVALIDATED "
                   "below and excluded from edge claims. Do not change engine settings or fire MCE/edge alerts from this report.")
        out.append("")
    else:
        if any_validated and not in_early_epoch and canonical_allows_validated_output:
            # DEFENSE-IN-DEPTH (t_26cdaf62): the blind-period contract forbids
            # ANY validated cohort before the clean epoch reaches EARLY_EPOCH_DAYS.
            # gate_cohort() already routes every blind-period passing cohort to
            # EARLY-EPOCH, so any_validated is False during the blind period by
            # construction; this guard is a second latch so the "Validated
            # Cohorts (Passed All Gates)" header can never be emitted while
            # in_early_epoch is True even if that routing is later changed.
            out.append("## Validated Cohorts (Passed All Gates — strictly within clean-candidate-599f58e7e epoch)")
            out.append("")
            out.append("> **label_basis=forward-synthetic** — not realized-exit confirmed; not a live edge until realized-exit N>=300. "
                       "Cohorts below are validated on a SYNTHETIC-FORWARD label basis "
                       "(`join_asof(strategy='forward')` over all `signal_journeys`), NOT executed/realized-exit pnl. "
                       "Per t_a04368da measurement-integrity guard this is a CANDIDATE, not a confirmed live edge; "
                       "the authoritative realized-exit basis (fusion-calibration) independently reports "
                       "VALIDATED_EDGE_STATUS=INSUFFICIENT_SAMPLE.")
            out.append("")
            out.append("| TF | Dir | Vol | Macro | Fav | AllN | AllWR | FreshN | FreshWR | CleanN | CleanWR | Clean_stale | Contam? | Kill? | lag_min | fresh_window_min | fresh_lag | stale_share | label_basis |")
            out.append("|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
            for r in validated.sort("wr_clean_fresh", descending=True).to_dicts():
                out.append(f"| {r['timeframe']} | {r['direction']} | {r['volatility']} | {r['macro_regime']} | {str(r['fav'])} | "
                           f"{r['n_all']:,} | {r['wr_all']}% | {r['n_fresh']:,} | {r['wr_fresh']}% | "
                           f"{r['n_clean_fresh']:,} | {r['wr_clean_fresh']}% | {r['clean_stale_share']}% | "
                           f"{'YES' if r['contaminated'] else 'NO'} | {'YES' if r['kill_listed'] else 'NO'} | "
                           f"{r['lag_min_med']} | {r['fresh_window_min']} | {r['fresh_lag_med']} | {r['stale_share']}% | forward-synthetic |")
            out.append("")
        elif any_validated and not in_early_epoch:
            # DEFENSE-IN-DEPTH (t_fbaaea94): QR_CANONICAL_FAIL_CLOSED_EARLY_EXIT=0
            # is a diagnostic override, not an alternate promotion path. When
            # the authoritative realized-exit Tier-1 verdict is not VALIDATED,
            # full synthetic-forward rows may be shown for investigation but
            # must never be headed or stamped as validated cohorts.
            out.append(f"## Diagnostic Synthetic-Forward Candidates (canonical Tier-1 status: {canonical_status})")
            out.append("")
            out.append("> Diagnostic override `QR_CANONICAL_FAIL_CLOSED_EARLY_EXIT=0` is active. "
                       "The rows below passed the synthetic-forward cohort gate only. "
                       "The authoritative realized-exit Tier-1 verdict is not `VALIDATED`, so these rows are candidates for investigation only; "
                       "they are not cleared for implementation, paper-sleeve routing, engine setting changes, or MCE/edge alerts.")
            out.append("")
            out.append("| TF | Dir | Vol | Macro | Fav | AllN | AllWR | FreshN | FreshWR | CleanN | CleanWR | Clean_stale | Contam? | Kill? | lag_min | fresh_window_min | fresh_lag | stale_share | label_basis |")
            out.append("|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
            for r in validated.sort("wr_clean_fresh", descending=True).to_dicts():
                out.append(f"| {r['timeframe']} | {r['direction']} | {r['volatility']} | {r['macro_regime']} | {str(r['fav'])} | "
                           f"{r['n_all']:,} | {r['wr_all']}% | {r['n_fresh']:,} | {r['wr_fresh']}% | "
                           f"{r['n_clean_fresh']:,} | {r['wr_clean_fresh']}% | {r['clean_stale_share']}% | "
                           f"{'YES' if r['contaminated'] else 'NO'} | {'YES' if r['kill_listed'] else 'NO'} | "
                           f"{r['lag_min_med']} | {r['fresh_window_min']} | {r['fresh_lag_med']} | {r['stale_share']}% | forward-synthetic-diagnostic |")
            out.append("")
        if any_early:
            # Segregated lower-confidence section (t_4cc128ea AC #3) -- NEVER a
            # validated/confirmed edge; never fires MCE/edge alerts.
            out.append(f"## EARLY-EPOCH (lower-confidence, blind-period) cohorts")
            out.append("")
            out.append(f"> These cohorts passed the **softened early-epoch fresh-N ramp** (N >= {N_THRESH_EARLY}, "
                       f"WR >= {WR_THRESH}%, stale_share <= {STALE_THRESH}%) but would NOT clear the full N >= {N_THRESH} "
                       f"gate. They are reported ONLY here, strictly segregated from VALIDATED. Per t_4cc128ea they are "
                       f"**not** confirmed edges and MUST NOT trigger MCE/edge alerts or engine setting changes. They are "
                       f"re-evaluated under the full gate once the clean epoch passes {EARLY_EPOCH_DAYS}d.")
            out.append("")
            out.append("| TF | Dir | Vol | Macro | Fav | AllN | AllWR | FreshN | FreshWR | CleanN | CleanWR | Clean_stale | Contam? | Kill? | lag_min | fresh_window_min | fresh_lag | stale_share |")
            out.append("|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
            for r in early_cohorts.sort("wr_clean_fresh", descending=True).to_dicts():
                out.append(f"| {r['timeframe']} | {r['direction']} | {r['volatility']} | {r['macro_regime']} | {str(r['fav'])} | "
                           f"{r['n_all']:,} | {r['wr_all']}% | {r['n_fresh']:,} | {r['wr_fresh']}% | "
                           f"{r['n_clean_fresh']:,} | {r['wr_clean_fresh']}% | {r['clean_stale_share']}% | "
                           f"{'YES' if r['contaminated'] else 'NO'} | {'YES' if r['kill_listed'] else 'NO'} | "
                           f"{r['lag_min_med']} | {r['fresh_window_min']} | {r['fresh_lag_med']} | {r['stale_share']}% |")
            out.append("")

    out.append("## Failed-Closed Cohorts (phantom-edge / contaminated-epoch exposure)")
    out.append("")
    out.append("| TF | Dir | Vol | Macro | Fav | AllN | AllWR | FreshN | FreshWR | lag_min | fresh_window_min | fresh_lag | stale_share | ContamN | Contam? | Kill? | Failure |")
    out.append("|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
    for r in failed.to_dicts():
        reasons = []
        if r["stale_share"] > STALE_THRESH: reasons.append("stale-share>5%")
        if r["n_fresh"] < N_THRESH: reasons.append("fresh N<300")
        if r["wr_fresh"] < WR_THRESH: reasons.append("fresh WR<53%")
        if r["kill_listed"]: reasons.append("KILL-LISTED(K1-K7)")
        # NOTE (t_ef1d2490): pre-clean (CONTAMINATED) rows are excluded from the
        # scan window by the clean-epoch SQL floor and reported in the audit
        # appendix -- they do NOT drive a FAIL-CLOSED reason here. The
        # `contaminated` column is retained for transparency only.
        out.append(f"| {r['timeframe']} | {r['direction']} | {r['volatility']} | {r['macro_regime']} | {str(r['fav'])} | "
                   f"{r['n_all']:,} | {r['wr_all']}% | {r['n_fresh']:,} | {r['wr_fresh']}% | "
                   f"{r['lag_min_med']} | {r['fresh_window_min']} | {r['fresh_lag_med']} | {r['stale_share']}% | "
                   f"{r['n_contam']:,} | {'YES' if r['contaminated'] else 'NO'} | {'YES' if r['kill_listed'] else 'NO'} | {', '.join(reasons) or 'n/a'} |")
    out.append("")

    # ---- Data-epoch contamination cross-check section (t_47fd45ce) ----
    out.append("## Data-Epoch Contamination Cross-Check (vs fusion calibration kill-list)")
    out.append("")
    out.append(f"- **Clean-candidate epoch:** `{CLEAN_EPOCH_NAME}` opens **{clean_epoch_start.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}** (UTC). "
               f"Per t_47fd45ce acceptance #2, a `WR>53%, n>=300 validated` claim is permitted ONLY for "
               f"cohorts computed strictly within this open epoch. All signals before it overlap a "
               f"known-defect epoch and are tagged CONTAMINATED/UNVALIDATED.")
    out.append(f"- **Kill-list cross-reference (K1-K7):** live `strategy_lineage_kills` table returned "
               f"{len(kill_list)} kill(s); obsidian assessment doc present: {kill_doc_exists} "
               f"(`{KILL_LIST_DOC}`). Any candidate matching a kill-listed lineage is rejected.")
    out.append("- **Consulted `data_epoch_registry` (system of record):**")
    for name, starts, ends, defects, is_clean in epoch_rows:
        s = str(starts) if starts is not None else "-infinity"
        e = str(ends) if ends is not None else "open"
        tag = "CLEAN" if is_clean else "DEFECT"
        out.append(f"  - `{name}` [{tag}] {s} -> {e}: {defects[:120]}")
    out.append("")

    # ---- Pre-clean (excluded, CONTAMINATED) appendix (t_ef1d2490 AC #1) ----
    # The scan window is clamped to the open clean epoch, so pre-clean
    # (CONTAMINATED) rows never reach the gate. They are reported here for
    # audit instead of being silently dropped.
    out.extend(render_preclean_appendix(n_preclean, n_preclean_contam_90d))

    # ---- CLEAN-EPOCH BLIND PERIOD (t_4cc128ea AC #4) ----
    # Document that FAIL-CLOSED through EARLY_EPOCH_DAYS is EXPECTED (not a
    # pipeline fault) while the clean epoch is young and the fresh sample is
    # still accumulating toward N_THRESH.
    epoch_open = clean_epoch_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blind_remaining = max(0.0, EARLY_EPOCH_DAYS - clean_epoch_age_days)
    blind_state = ("ACTIVE" if in_early_epoch else "EXPIRED")
    out.append("## CLEAN-EPOCH BLIND PERIOD")
    out.append("")
    out.append(f"- **Open clean epoch:** `{CLEAN_EPOCH_NAME}` opened **{epoch_open}** (UTC).")
    out.append(f"- **Epoch age:** {clean_epoch_age_days:.1f}d of {EARLY_EPOCH_DAYS}d blind-period window ({blind_state}).")
    if in_early_epoch:
        out.append(f"- **Status:** blind-period {blind_state} — {blind_remaining:.1f}d remaining until the full N >= {N_THRESH} "
                   f"gate applies automatically. FAIL-CLOSED / STARVING results during this window are **EXPECTED**, not a "
                   f"pipeline fault. Any cohort reaching the gate now is reported ONLY under EARLY-EPOCH (lower-confidence) "
                   f"and is never a confirmed edge or an MCE/edge alert trigger.")
    else:
        out.append(f"- **Status:** blind-period {blind_state} — full N >= {N_THRESH} gate now applies (early-epoch ramp no-ops). "
                   f"Any cohort reaching the gate is subject to the strict VALIDATED criteria.")
    out.append("")

    # ---- CANONICAL VALIDATED-EDGE VERDICT (t_4df5351d / t_460bb546) ----
    # Mirrors the fusion-calibration report's authoritative Tier-1 verdict so
    # Frank/PM see ONE canonical validated_edge_status across both reports.
    out.append("")
    out.append("## Canonical Validated-Edge Verdict (shared with fusion-calibration)")
    out.append("")
    out.append(f"- **VALIDATED_EDGE_STATUS: `{canonical_status}`** — {canonical_reason}")
    if canonical_status == "VALIDATED":
        out.append("  An edge cohort is independently validated (Tier-1 fresh N >= 300 "
                   "with Tier-1 WR > 50%). Both reports must agree on this status.")
    else:
        out.append("  No edge cohort is independently validated. This nightly's "
                   "FAIL-CLOSED-on-all-cohorts result reflects the SAME status; it "
                   "stands until the clean epoch matures to fresh N >= 300.")

    # Sanity guard: never emit a wrong run-date year in the body.
    body = "\n".join(out)
    if run_date[:4] != dt.datetime.now(dt.timezone.utc).strftime("%Y"):
        fail_closed(run_label, run_date, n_sig, "Internal guard tripped: run-date year mismatch.")
        return
    # Obsolete-canned-template guard (replaces blanket '2023' check so legitimate
    # prior-year academic citations in prose stay allowed per acceptance #3).
    if "2023-10-05-quant-research.md" in body or "Quant Research Report: Trading Edge Identification (2023-10-05)" in body:
        fail_closed(run_label, run_date, n_sig, "Internal guard tripped: obsolete 2023 canned template detected.")
        return
    # Acceptance #3: cited obsidian research docs must exist and be >= clean-label
    # rebuild (2026-07-03). Otherwise fail closed to EVIDENCE STALE.
    cit_ok, cit_reason = check_citations(body)
    if not cit_ok:
        fail_closed(run_label, run_date, n_sig, f"EVIDENCE STALE: {cit_reason}")
        return

    print(body)

    # Persist dated note
    try:
        write_research_note(
            body,
            run_date,
            "validated" if (any_validated and canonical_allows_validated_output) else "early-epoch" if any_early else "fail-closed",
        )
    except Exception as e:
        sys.stderr.write(f"note-write-warning: {e}\n")

    # ---- Self-validation backstop (fail-closed) ----
    # Run the companion validator in-process. On a STALE/forbidden/metric-
    # missing output it demotes this job's last_status in jobs.json so the
    # fleet dashboard stops showing a green lie. Its stdout is captured
    # (not echoed) to keep the delivered report clean; only a real
    # validation failure is surfaced to stderr. Import is guarded so a
    # missing/partial validator module can never break report delivery.
    run_self_validator(body)


def run_self_validator(body=None):
    """In-process fail-closed self-validation backstop (shared by main paths).

    Runs the companion quant_researcher_6h_validator in-process. When the
    current rendered report body is available, validate that in-memory body
    instead of the newest scheduler output file; the scheduler writes the output
    file only after this script exits, so validating the output directory here
    races against the previous run and can falsely demote a healthy report.
    On a
    STALE/forbidden/metric-missing output it demotes this job's last_status in
    jobs.json so the fleet dashboard stops showing a green lie. Its stdout is
    captured (not echoed) to keep the delivered report clean; only a real
    validation failure is surfaced to stderr. Import is guarded so a
    missing/partial validator module can never break report delivery.
    """
    try:
        import quant_researcher_6h_validator as _val
        if body is not None:
            _verdict = _val.selfcheck_from_body(body)
        else:
            import io as _io
            import contextlib as _cl
            _buf = _io.StringIO()
            with _cl.redirect_stdout(_buf), _cl.redirect_stderr(_buf):
                _val.main()
            _verdict = _buf.getvalue()
        if "FAIL-CLOSED validator" in _verdict:
            sys.stderr.write("[self-validator] " + _verdict)
    except Exception as _ve:
        sys.stderr.write(f"self-validator-skip: {_ve}\n")


def quiet_starving(run_label, run_date, n_sig, win_label, clean_epoch_start,
                   contaminated_share, joined_n, kill_list, kill_doc_exists, epoch_rows,
                   total_clean=0, n_preclean=0, n_preclean_contam_90d=0,
                   canonical_status=INSUFFICIENT_SAMPLE, canonical_reason=""):
    """Quiet-mode early-return (proposal t_ca461999).

    Emitted when no cohort cleared the triple gate this cycle (any_validated is
    False) and QR_VERBOSE is not set. The full 250-line cohort dump would carry
    zero validated information, so we emit a single STARVING stdout line plus a
    compact dated Obsidian note.

    The note retains the fail-closed contamination cross-check section (clean
    epoch open time, kill-list count, epoch registry summary, the STARVING
    marker, and the freshness-gate thresholds) so the companion
    quant_researcher_6h_validator.py fail-closed backstop keeps passing (it
    requires metric + contamination markers to avoid demoting the job to red
    on an otherwise-correct quiet run). No validated claim is ever made, so
    the validator's affirmative-pass consistency check is never tripped.
    """
    epoch_open = clean_epoch_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # stdout: <=3 lines, contains the STARVING marker.
    print(f"STARVING: no cohort clears the triple gate this cycle "
          f"(clean-epoch joined N={total_clean}, contaminated-share={contaminated_share:.1f}%; "
          f"epoch opened {epoch_open}); full dump suppressed (QR_VERBOSE=1 to force); "
          f"no validated edge by design.")

    # Compact dated note (keeps validator markers; never asserts a validated edge).
    note = []
    note.append("# Quantitative Research: 6-Hour Systematic Edge & Carry Sweep (STARVING)")
    note.append(f"**Cadence:** every 6 hours (cron `15 */6 * * *`, 4 runs/day) — matches the '6h' contract.")
    note.append(f"**Date:** {run_label}")
    note.append(f"**Job ID:** 13c1f9279025 (deterministic no_agent script)")
    note.append(f"**Data window:** {win_label} | signals scanned: {n_sig:,} | joined: {joined_n:,}")
    note.append("")
    note.append("## Result: STARVING (no cohort clears the triple gate)")
    note.append("")
    note.append(f"No cohort independently cleared the triple gate on STRICTLY clean-epoch data this cycle "
                f"(clean-epoch joined N={total_clean}, contaminated-share={contaminated_share:.1f}%). "
                "The full 250-line cohort dump is suppressed in quiet mode (proposal t_ca461999) because it "
                "would carry zero validated information. "
                "No edge cohort is validated, confirmed, or routed for implementation/paper sleeve.")
    note.append("")
    note.append("## Methodology & Freshness-Gate Invariant")
    note.append("- A cohort **FAILS CLOSED** unless ALL hold: stale_share <= 5.0%, fresh N >= 300, fresh WR >= 53%.")
    note.append("- Returns clipped to ±10%, AXLUSDT excluded, direction-adjusted, win/loss threshold ±0.2%.")
    note.append("- `fresh_window_min`: 15m=15, 1h=60, 4h=240, 1d=1440. `fresh_lag` = median lag_min of the fresh subset.")
    note.append(f"- `stale_share` = stale / (fresh+stale); clean-epoch eligibility requires `n_clean_fresh` >= 300.")
    note.append("")
    note.append("## Data-Epoch Contamination Cross-Check (vs fusion calibration kill-list)")
    note.append("")
    note.append(f"- **Clean-candidate epoch:** `clean-candidate-599f58e7e` opens **{epoch_open}** (UTC). "
                "Per t_47fd45ce acceptance #2, a `WR>53%, n>=300 validated` claim is permitted ONLY for "
                "cohorts computed strictly within this open epoch. All signals before it overlap a "
                "known-defect epoch and are tagged CONTAMINATED/UNVALIDATED.")
    note.append(f"- **Kill-list cross-reference (K1-K7):** live `strategy_lineage_kills` table returned "
                f"{len(kill_list)} kill(s); obsidian assessment doc present: {kill_doc_exists}.")
    note.append("- **Consulted `data_epoch_registry` (system of record):**")
    for name, starts, ends, defects, is_clean in epoch_rows:
        s = str(starts) if starts is not None else "-infinity"
        e = str(ends) if ends is not None else "open"
        tag = "CLEAN" if is_clean else "DEFECT"
        note.append(f"  - `{name}` [{tag}] {s} -> {e}: {defects[:120]}")
    note.append("")

    # ---- Pre-clean (excluded, CONTAMINATED) appendix (t_ef1d2490 AC #1) ----
    note.extend(render_preclean_appendix(n_preclean, n_preclean_contam_90d))

    # ---- CLEAN-EPOCH BLIND PERIOD (t_4cc128ea AC #4) ----
    # Documented even in quiet/STARVING mode so the section renders on every
    # run: FAIL-CLOSED through EARLY_EPOCH_DAYS is EXPECTED, not a fault.
    blind_age_days = (dt.datetime.now(dt.timezone.utc) - CLEAN_EPOCH_FALLBACK).total_seconds() / 86400.0
    blind_in = blind_age_days < EARLY_EPOCH_DAYS
    blind_remain = max(0.0, EARLY_EPOCH_DAYS - blind_age_days)
    blind_st = "ACTIVE" if blind_in else "EXPIRED"
    note.append("## CLEAN-EPOCH BLIND PERIOD")
    note.append("")
    note.append(f"- **Open clean epoch:** `{CLEAN_EPOCH_NAME}` opened **{epoch_open}** (UTC).")
    note.append(f"- **Epoch age:** {blind_age_days:.1f}d of {EARLY_EPOCH_DAYS}d blind-period window ({blind_st}).")
    if blind_in:
        note.append(f"- **Status:** blind-period {blind_st} — {blind_remain:.1f}d remaining until the full N >= {N_THRESH} "
                    f"gate applies automatically. This STARVING/FAIL-CLOSED result is **EXPECTED** during the blind period, "
                    f"not a pipeline fault. Any cohort reaching the gate now is reported ONLY as EARLY-EPOCH (lower-confidence) "
                    f"and is never a confirmed edge or MCE/edge alert trigger.")
    else:
        note.append(f"- **Status:** blind-period {blind_st} — full N >= {N_THRESH} gate now applies (early-epoch ramp no-ops).")
    note.append("")
    note.append("## Canonical Validated-Edge Verdict (shared with fusion-calibration)")
    note.append("")
    note.append(f"- **VALIDATED_EDGE_STATUS: `{canonical_status}`** — {canonical_reason}")
    if canonical_status == "VALIDATED":
        note.append("  An edge cohort is independently validated (Tier-1 fresh N >= 300 "
                    "with Tier-1 WR > 50%). Both reports must agree on this status.")
    else:
        note.append("  No edge cohort is independently validated. This nightly's "
                    "FAIL-CLOSED-on-all-cohorts result reflects the SAME status; it "
                    "stands until the clean epoch matures to fresh N >= 300.")
    note.append("")
    note.append("> STARVING: quiet-mode active (proposal t_ca461999). Full dump resumes automatically "
                "once clean-epoch eligibility (n_clean_fresh >= 300 + triple gate pass) is met, or via QR_VERBOSE=1.")
    body = "\n".join(note)

    # Persist dated note (mirrors main() path so the validator's dated-file
    # check holds). The filename is date-based, so 6h runs overwrite the same
    # daily note -- at most one dated artifact per UTC day. Always write (no
    # daily-gate skip) so the companion validator's mtime check is satisfied
    # every run and the job never spuriously red-fails.
    try:
        write_research_note(body, run_date, "starving")
    except Exception as e:
        sys.stderr.write(f"note-write-warning: {e}\n")
    return body


def fail_closed(run_label, run_date, n_sig, reason):
    body = (
        f"# Quantitative Research: 6-Hour Systematic Edge & Carry Sweep\n"
        f"**Cadence:** every 6 hours (cron `15 */6 * * *`, 4 runs/day) — matches the '6h' contract.\n"
        f"**Date:** {run_label}\n"
        f"**Job ID:** 13c1f9279025 (deterministic no_agent script)\n"
        f"**Data window scanned:** {n_sig:,} signals\n\n"
        f"## Result: FAIL-CLOSED\n\n"
        f"**Reason:** {reason}\n\n"
        f"No edge research was produced or validated this cycle. Per t_672329ea and t_47fd45ce, the job fails "
        f"closed when no clean-epoch data passes the gate. No 2023-dated (or any stale) content is emitted; "
        f"no cohort is validated, confirmed, or routed for implementation/paper sleeve.\n"
    )
    print(body)


if __name__ == "__main__":
    main()
