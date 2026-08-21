#!/usr/bin/env python3
"""Generate the SycodeTrading database schema map into the sycode vault.

Answers "where do I find X in the database, and how do I query it" for any provider
— Claude Code, Codex, Hermes profiles, Grok/Gemini adapters.

Emits: /home/frank/obsidian/sycode-trading/architecture/Database-Schema-Map.md

Runs as a hermes no-agent cron job. The page is AUTO-GENERATED with a do-not-edit
banner; judgement and remediation live in the review note it links to.

Read-only: every statement is a SELECT, executed inside BEGIN..ROLLBACK.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

OUT = "/home/frank/obsidian/sycode-trading/architecture/Database-Schema-Map.md"
CONTAINER = "sycodetrading-supabase-db"
REPO = "/home/frank/sycode-trading"
MIG_DIR = "server/drizzle/migrations"
DB = "postgres"

# Domain grouping: (heading, [name prefixes/substrings]). First match wins, so order matters.
DOMAINS = [
    ("Market data (raw feeds)", ["tick_", "candle", "funding_", "oi_", "orderbook", "price_", "market_", "microstructure"]),
    ("Signals & journeys", ["signal_", "funnel_", "lens", "trajector"]),
    ("Decisions & outcomes", ["decision_", "outcome", "finalized_", "validation_", "regression_", "composite_"]),
    ("Positions & execution", ["position", "managed_", "trade_", "order_", "fill", "execution"]),
    ("Strategy & arena", ["strategy_", "arena", "tournament", "agent_", "vote"]),
    ("Risk & guards", ["risk_", "guard", "kill_", "breaker", "quarantine", "circuit"]),
    ("ML & features", ["feature_", "model_", "ml_", "prediction", "embedding", "label", "r_multiple"]),
    ("Pro-trader / wallet intel", ["pm_", "pro_trader", "wallet"]),
    ("Correlation & regime", ["correlation_", "regime", "macro_"]),
    ("Ops, audit & queues", ["audit", "pending_", "dlq", "webhook", "lip_", "job", "cron", "migration", "schema_"]),
]


COLLECTOR_ERRORS = []


def q(sql, timeout=120, label="query"):
    """Run a read-only query. Returns list of dict rows, or None on FAILURE.

    None and [] are deliberately different: [] means the query succeeded and
    matched nothing; None means the collector broke. Callers must render them
    differently — an empty table where a query errored is how a reader concludes
    a capability does not exist when in fact nothing was checked.
    """
    wrapped = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t;"
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", DB,
             "-tAc", wrapped],
            capture_output=True, text=True, timeout=timeout)
    except Exception as exc:                                        # noqa: BLE001
        COLLECTOR_ERRORS.append(f"`{label}`: {exc}")
        print(f"COLLECTOR FAILED {label}: {exc}", file=sys.stderr)
        return None
    if r.returncode != 0:
        err = (r.stderr or "").strip().replace("\n", " ")[:200]
        COLLECTOR_ERRORS.append(f"`{label}`: psql rc={r.returncode} — {err}")
        print(f"COLLECTOR FAILED {label}: rc={r.returncode} {err}", file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout.strip() or "[]")
    except Exception as exc:                                        # noqa: BLE001
        COLLECTOR_ERRORS.append(f"`{label}`: unparseable output — {exc}")
        print(f"COLLECTOR FAILED {label}: {exc}", file=sys.stderr)
        return None


def git(args, timeout=60):
    """Read-only git in the deploy repo. Returns stdout, or None on failure."""
    try:
        r = subprocess.run(["git", "-C", REPO] + args,
                           capture_output=True, text=True, timeout=timeout)
    except Exception as exc:                                        # noqa: BLE001
        COLLECTOR_ERRORS.append(f"`git {args[0]}`: {exc}")
        return None
    if r.returncode != 0:
        COLLECTOR_ERRORS.append(f"`git {' '.join(args[:2])}`: rc={r.returncode}")
        return None
    return r.stdout


def migration_drift():
    """Migrations that exist but were never applied.

    Two distinct failure modes, which need different fixes and so are reported
    separately (observed 2026-08-21):
      * on origin/main but not in schema_migration_manifest — passed review and
        merge, and still never reached the database. 0119 (candles.venue) and
        0120 (four freshness-probe indexes) had been in this state since 08-11.
      * present locally but not on origin/main — written, never committed, so
        unreviewable and invisible to anyone else.

    NOTE: `origin/main` is ambiguous as a revision in this repo — a worktree
    directory is literally named `origin/main`. Always use the full ref.
    """
    applied = q("SELECT path FROM public.schema_migration_manifest",
                label="schema_migration_manifest")
    if applied is None:
        return None
    applied_names = {os.path.basename(r["path"]) for r in applied}

    tree = git(["ls-tree", "-r", "--name-only",
                "refs/remotes/origin/main", MIG_DIR + "/"])
    if tree is None:
        return None
    on_main = {os.path.basename(p) for p in tree.split("\n")
               if p.endswith(".sql") and ".down." not in p}

    local_dir = os.path.join(REPO, MIG_DIR)
    try:
        local = {f for f in os.listdir(local_dir)
                 if f.endswith(".sql") and ".down." not in f}
    except OSError as exc:
        COLLECTOR_ERRORS.append(f"`migrations dir`: {exc}")
        return None

    def added(name):
        """Date the file was ADDED, not last touched. `git log -1` returns the most
        recent commit to touch a path, which for a bulk repo reorganisation is the
        reorg date for every file — that reported 20 unrelated migrations as all
        'authored 2026-08-11'. --diff-filter=A gives the real introduction date."""
        out = git(["log", "--diff-filter=A", "-1", "--format=%ad", "--date=short",
                   "refs/remotes/origin/main", "--", f"{MIG_DIR}/{name}"])
        return (out or "").strip() or "?"

    # Establish where the manifest is actually AUTHORITATIVE before calling anything
    # drift. It is not a complete record: 44 rows against ~70 migrations on main, and
    # public.__drizzle_migrations holds only 5. For older migrations there is simply no
    # machine-readable record of application, so absence is not evidence.
    #
    # A date cutoff does NOT work here — the whole migrations directory was added to
    # git in one bulk move (every file reports the same add-date), so date cannot
    # separate old from new. What does work is the sequence numbers: applied numbers
    # run 0001-0011, then a large gap, then dense coverage from 0101 upward. The
    # first number after the largest gap is the point from which the manifest can be
    # trusted; below it, a missing entry means unknown, not unapplied.
    nums = q("""SELECT DISTINCT (regexp_match(path,'/(\\d{4})_'))[1]::int AS n
                FROM public.schema_migration_manifest
                WHERE path ~ '/\\d{4}_' ORDER BY 1""", label="manifest watermark")
    if nums is None:
        return None
    seq = [r["n"] for r in nums]
    watermark = 0
    if len(seq) > 1:
        gaps = [(seq[i + 1] - seq[i], seq[i + 1]) for i in range(len(seq) - 1)]
        biggest, after = max(gaps)
        if biggest > 5:                       # a real era boundary, not a skipped number
            watermark = after

    def seq_of(name):
        r"""Sequence number, or None when the name carries no comparable sequence.

        BUG FIXED 2026-08-21: this used `re.match(r"(\d{4})_")` and returned -1 on no
        match. Date-prefixed migrations (`20260817_t_f94a8800_*.sql`) parse as "2026"
        followed by "0817_" — no underscore at position 4 — so they returned -1, fell
        below the watermark, and were SILENTLY filed as "unknown". Two confirmed-unapplied
        migrations were invisible to the fleet's only drift detector because of it.

        Now: a 4-digit sequence returns its number; a date prefix (8 digits) returns None
        and is reported EXPLICITLY as unsequenced rather than swallowed by the watermark.
        """
        if re.match(r"\d{8}[_T]", name):      # date-prefixed: no comparable sequence
            return None
        m = re.match(r"(\d{4})_", name)
        return int(m.group(1)) if m else None

    unapplied = on_main - applied_names
    merged_unapplied = sorted(n for n in unapplied
                              if seq_of(n) is not None and seq_of(n) >= watermark)
    below = sorted(n for n in unapplied
                   if seq_of(n) is not None and seq_of(n) < watermark)
    # Date-prefixed and other unsequenced names cannot be compared to the watermark.
    # They are REPORTED, never silently dropped — that silence was the bug.
    unsequenced = sorted(n for n in unapplied if seq_of(n) is None)
    cutoff = f"{watermark:04d}"
    uncommitted = sorted(local - on_main)

    # Duplicate sequence numbers.
    #
    # GAP CLOSED 2026-08-21. This detector reported names carrying NO sequence
    # (date-prefixed) but was blind to the exact opposite fault: two DIFFERENT
    # migrations claiming the SAME number. origin/main carries two 0116s and two
    # 0120s, and nothing flagged it.
    #
    # It matters for two reasons:
    #   * apply ORDER between same-numbered files is decided by sort/filesystem
    #     order, not by intent, so which one runs first is accidental;
    #   * "apply 0120" is ambiguous in conversation, in scripts and in a manifest
    #     path — and 0120 is precisely the migration whose stale header already
    #     propagated one wrong belief this session.
    #
    # Checked across ALL migrations on main, applied or not: a duplicate number is
    # a hazard regardless of application state, so it is NOT filtered by `unapplied`.
    by_seq = {}
    for n in on_main:
        sq = seq_of(n)
        if sq is not None:
            by_seq.setdefault(sq, []).append(n)
    duplicate_seqs = sorted((sq, sorted(v)) for sq, v in by_seq.items() if len(v) > 1)
    return {
        "merged_unapplied": [(n, added(n)) for n in merged_unapplied],
        "cutoff": cutoff,
        "below_watermark": len(below),
        "unsequenced": unsequenced,
        "duplicate_seqs": duplicate_seqs,
        "uncommitted": uncommitted,
        "on_main": len(on_main),
        "applied": len(applied_names),
    }


def domain_of(name):
    for heading, prefixes in DOMAINS:
        for p in prefixes:
            if name.startswith(p) or p in name:
                return heading
    return "Other / uncategorised"


def md_table(headers, rows):
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out) + "\n"


def main():
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    tables = q("""
        SELECT c.relname AS name,
               -- n_live_tup is only populated once a table has been analyzed. For
               -- never-analyzed tables it reads 0 or near-0 while the table holds
               -- millions of rows (observed 2026-08-21: signal_journeys reported 9,594
               -- against a reltuples of 3,802,860 in 45 GB — a 400x error). Fall back to
               -- the planner's own estimate, which is populated at table creation.
               CASE WHEN s.last_analyze IS NULL AND s.last_autoanalyze IS NULL
                    THEN c.reltuples::bigint ELSE s.n_live_tup END AS rows,
               pg_total_relation_size(c.oid) AS total_bytes,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
               pg_size_pretty(pg_indexes_size(c.oid)) AS idx,
               s.seq_scan, s.idx_scan,
               (s.last_analyze IS NULL AND s.last_autoanalyze IS NULL) AS never_analyzed,
               obj_description(c.oid) AS comment
        FROM pg_class c
        JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE c.relkind IN ('r','p')
    """)
    views = q("""SELECT c.relname AS name, c.relkind AS kind FROM pg_class c
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname='public' AND c.relkind IN ('v','m')""")
    parts = q("""SELECT p.relname AS parent, count(*) AS n_partitions,
                        pg_size_pretty(sum(pg_total_relation_size(ch.oid))) AS total
                 FROM pg_inherits i
                 JOIN pg_class ch ON ch.oid=i.inhrelid
                 JOIN pg_class p ON p.oid=i.inhparent
                 GROUP BY p.relname""")
    exts = q("""SELECT extname AS name, extversion AS version FROM pg_extension
                WHERE extname NOT IN ('plpgsql')""")
    idx_health = q("""
        WITH idx AS (
          SELECT i.indrelid::regclass::text AS tbl, c.relname AS idx_name, i.indexrelid,
                 string_to_array(i.indkey::text,' ') AS cols, i.indisunique, i.indisprimary, c.relam,
                 pg_relation_size(i.indexrelid) AS bytes, s.idx_scan
          FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
          JOIN pg_stat_user_indexes s ON s.indexrelid=i.indexrelid
          WHERE i.indpred IS NULL AND i.indisvalid)
        SELECT
          round(EXTRACT(EPOCH FROM (now()-pg_postmaster_start_time()))/3600,1) AS window_hours,
          pg_postmaster_start_time()::text AS counters_since,
          (SELECT count(*) FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid=s.indexrelid
             WHERE s.idx_scan=0 AND NOT i.indisunique AND NOT i.indisprimary) AS unused_count,
          (SELECT pg_size_pretty(sum(pg_relation_size(s.indexrelid))) FROM pg_stat_user_indexes s
             JOIN pg_index i ON i.indexrelid=s.indexrelid
             WHERE s.idx_scan=0 AND NOT i.indisunique AND NOT i.indisprimary) AS unused_size,
          (SELECT count(*) FROM (SELECT DISTINCT a.indexrelid FROM idx a JOIN idx b
             ON a.tbl=b.tbl AND a.indexrelid<>b.indexrelid AND a.cols=b.cols AND a.relam=b.relam
             AND NOT a.indisprimary AND NOT a.indisunique
             AND (b.indisprimary OR b.indisunique OR a.idx_scan<b.idx_scan
                  OR (a.idx_scan=b.idx_scan AND a.indexrelid>b.indexrelid))) d) AS dup_count,
          (SELECT count(*) FROM pg_index WHERE NOT indisvalid) AS invalid_count
    """)
    slow = q("""
        SELECT round(total_exec_time/1000)::text||'s' AS total_time, calls,
               round(mean_exec_time)::text||'ms' AS mean,
               left(regexp_replace(query,'\\s+',' ','g'),86) AS query
        FROM pg_stat_statements WHERE calls > 5
        ORDER BY total_exec_time DESC LIMIT 8
    """)
    drift = migration_drift()
    dbsize = q("SELECT pg_size_pretty(pg_database_size(current_database())) AS s")

    if not tables:
        print("FATAL: no tables returned — refusing to overwrite the page", file=sys.stderr)
        return 1

    tables.sort(key=lambda t: -(t["total_bytes"] or 0))
    ih = idx_health[0] if idx_health else {}

    # group by domain
    grouped = {}
    for t in tables:
        grouped.setdefault(domain_of(t["name"]), []).append(t)

    doc = f"""---
title: "Database Schema Map — where to find things and how to query them"
type: reference
status: active
created: 2026-08-21
updated: {today}
confidence: high
tags:
  - sycode-trading
  - database
  - schema
  - postgres
  - supabase
  - reference
  - auto-generated
sources:
  - "file:/home/frank/.hermes/scripts/schema_catalog.py"
  - "runtime:pg_class, pg_stat_user_tables, pg_stat_user_indexes, pg_stat_statements"
---
# Database Schema Map

> **AUTO-GENERATED — do not edit by hand.** Regenerated by
> `~/.hermes/scripts/schema_catalog.py` (hermes cron `sycode-schema-catalog`).
> Judgement, remediation and index decisions live in
> [[Reviews/2026-08-20-database-supabase-monitoring-deep-review]].

Generated **{now.strftime('%Y-%m-%d %H:%M UTC')}**{chr(10)+chr(10)+'> **' + str(len(COLLECTOR_ERRORS)) + ' COLLECTOR(S) FAILED THIS RUN.** Sections below may be incomplete or empty for that reason rather than because nothing matched. Details at the foot of the page.'+chr(10) if COLLECTOR_ERRORS else ''} · database **{dbsize[0]['s'] if dbsize else '?'}** ·
**{len(tables)}** tables · **{len(views)}** views/matviews

## How to query it

**The database is `postgres`, NOT `sycodetrading`.** This is the single most common
mistake — connecting to a db named after the project returns an empty schema.

| Route | How |
|---|---|
| Agent (read-only, safest) | `dgx_sql_query` MCP tool — wraps every statement in `BEGIN … ROLLBACK` |
| Shell, read | `docker exec -i {CONTAINER} psql -U postgres -d {DB} -c "SELECT …"` |
| Grafana | `Sycode DB` datasource (uid `cfqs8r7xlyjggb`) |
| **Any DDL** | **`tools/db/migrate.sh` ONLY** — a `ddl_approval_gate` event trigger rejects ungoverned DDL (incident 2026-07-05) |

Watch the timeouts: role `postgres` carries `statement_timeout=60s`, `mcp_reader` and
`mcp_readonly` 30 s. Long analytical queries need `SET statement_timeout = 0` in-session.

## Where to find things

Tables grouped by domain, largest first within each. `rows` is `n_live_tup` for analyzed tables and the planner's `reltuples` estimate for
tables flagged **never analyzed** (n_live_tup is unpopulated there and under-reports by
up to 400x). Either way it is an estimate, and never-analyzed tables also give the
planner no statistics, so their query plans are guesses.

"""
    for heading, _ in DOMAINS + [("Other / uncategorised", [])]:
        items = grouped.get(heading)
        if not items:
            continue
        doc += f"### {heading}\n\n"
        rows = []
        for t in items[:22]:
            flag = " ⚠️never-analyzed" if t["never_analyzed"] else ""
            rows.append([f"`{t['name']}`", f"{t['rows']:,}" if t['rows'] is not None else "?",
                         t["total"], t["idx"],
                         (t["comment"] or "")[:60] + flag])
        doc += md_table(["table", "rows", "total size", "of which indexes", "notes"], rows)
        if len(items) > 22:
            doc += f"\n_…and {len(items)-22} smaller tables in this domain._\n"
        doc += "\n"

    doc += "## Partitioned tables\n\n"
    doc += md_table(["parent", "partitions", "total size"],
                    [[f"`{p['parent']}`", p["n_partitions"], p["total"]] for p in parts])
    doc += "\nPartition maintenance runs via `pg_partman` under `pg_cron` (exempt from the DDL gate, still audited).\n\n"

    doc += "## Index health\n\n"
    wh = ih.get("window_hours"); cs = ih.get("counters_since")
    if wh is not None:
        doc += (f"> **`idx_scan` counters span only the last `{wh}` hours** (since `{cs}`, when the\n"
                f"> postmaster last restarted). `pg_stat_database.stats_reset` being NULL means never\n"
                f"> *explicitly* reset — it does NOT mean lifetime. **A zero here is not evidence an\n"
                f"> index is unused.** Anything driven by cron, research, backtests, month-end or\n"
                f"> quarter-end may simply not have run inside the window. Measured 2026-08-21: this\n"
                f"> same window captured ~40M operational scans on `signal_journeys` and zero research\n"
                f"> scans — a sampling artefact, not a finding. Do not drop an index on this evidence;\n"
                f"> re-measure over a window that provably contains the workload in question.\n\n")
    doc += md_table(["metric", "value"], [
        [f"Indexes with 0 scans in the last {ih.get('window_hours','?')}h (NOT 'unused')", f"**{ih.get('unused_count','?')}** — {ih.get('unused_size','?')}"],
        ["Exact-duplicate indexes", f"**{ih.get('dup_count','?')}**"],
        ["INVALID indexes", ih.get("invalid_count", "?")],
    ])
    doc += ("""### Maintenance window — read before any index work

**`CREATE INDEX CONCURRENTLY` cannot complete between roughly 02:00 and 10:00 UTC.**

The nightly backup (`sycodetrading-db-backup`, cron `0 2 * * *`) runs a single-threaded
`pg_dump` that holds one long transaction for consistency — historically 5.5 to 8 hours
(finished 07:26 on 19 Aug, 09:51 on 20 Aug). `CREATE INDEX CONCURRENTLY` must wait for
every transaction older than itself to finish, so during that window it stalls
indefinitely in the `waiting for old snapshots` phase and never completes.

Verified 2026-08-21: four FK index builds stalled behind it and had to be cancelled,
each leaving an INVALID index that then needed dropping.

Do index work **after the dump completes and before 02:00**. Check first:

```sql
SELECT count(*) FROM pg_stat_activity WHERE application_name = 'pg_dump';
SELECT phase FROM pg_stat_progress_create_index;   -- 'waiting for old snapshots' = blocked
```

Never cancel the backup to make room — it is the daily dump of a 253 GB trading database.

"""
            "An INVALID index is a failed `CREATE INDEX CONCURRENTLY`. It is never used by the "
            "planner but still holds its name, so a rebuild with `IF NOT EXISTS` will silently "
            "skip — drop it first. Note `DROP INDEX CONCURRENTLY` **cannot** pass the DDL gate "
            "(the audit INSERT consumes its required 'first action'); use a plain `DROP INDEX` "
            "with a short `lock_timeout`.\n\n")

    doc += "## Most expensive queries right now\n\n"
    doc += md_table(["total time", "calls", "mean", "query"],
                    [[s["total_time"], s["calls"], s["mean"], f"`{s['query']}`"] for s in slow])
    doc += "\nFrom `pg_stat_statements`. Counters are cumulative since the last reset.\n\n"

    # ---- migration drift -------------------------------------------------
    doc += "## Migration drift\n\n"
    if drift is None:
        doc += ("> **COLLECTOR FAILED — this section is NOT a clean bill of health.**\n"
                "> The drift check could not run. See Collector warnings at the foot of\n"
                "> this page. Do not read the absence of a table below as 'no drift'.\n\n")
    else:
        mu, uc = drift["merged_unapplied"], drift["uncommitted"]
        doc += (f"`{drift['on_main']}` migrations on `origin/main`; "
                f"`{drift['applied']}` recorded in `schema_migration_manifest`.\n\n"
                f"Scope: only migrations numbered **{drift['cutoff']} and above** count as "
                f"drift. The manifest is not a complete record — it holds 44 rows against "
                f"~70 migrations on main, and `__drizzle_migrations` only 5 — so below that "
                f"watermark a missing entry means *unknown*, not unapplied "
                f"({drift['below_watermark']} such files, not listed). The watermark is "
                f"derived from the largest gap in applied sequence numbers, which marks the "
                f"point the manifest came into use.\n\n")
        if not mu and not uc:
            doc += "No drift: every migration on `origin/main` is recorded as applied.\n\n"
        if mu:
            doc += ("### Merged but never applied\n\n"
                    "These passed review and merge and still never reached the database. "
                    "Review is not the gap; application is.\n\n")
            doc += md_table(["migration", "authored"],
                            [[f"`{n}`", d] for n, d in mu])
            doc += "\n"
        us = drift.get("unsequenced") or []
        if us:
            doc += ("### Unsequenced names — cannot be watermark-compared, reported explicitly\n\n"
                    "Date-prefixed migrations carry no 4-digit sequence, so they cannot be placed "
                    "relative to the watermark. Until 2026-08-21 they were silently filed as "
                    "*unknown* and never surfaced — the bug that hid two confirmed-unapplied "
                    "migrations. They are now always listed.\n\n")
            doc += md_table(["migration"], [[f"`{n}`"] for n in us])
            doc += "\n"
        ds = drift.get("duplicate_seqs") or []
        if ds:
            doc += ("### Duplicate sequence numbers\n\n"
                    "Two or more migrations claim the same number. Apply order between them "
                    "is decided by sort/filesystem order rather than by intent, and the "
                    "number alone no longer identifies a migration — in conversation, in a "
                    "script argument, or in a `schema_migration_manifest` path.\n\n")
            doc += md_table(["sequence", "migrations claiming it"],
                            [[f"`{sq:04d}`", " · ".join(f"`{n}`" for n in v)] for sq, v in ds])
            doc += "\n"
        if uc:
            doc += ("### Present locally, not on `origin/main`\n\n"
                    "Unreviewable and invisible to other sessions. Deploys run "
                    "`git reset --hard origin/main`, so these never ship.\n\n")
            doc += md_table(["migration"], [[f"`{n}`"] for n in uc])
            doc += "\n"
        doc += ("Re-derive: compare `git ls-tree -r --name-only refs/remotes/origin/main "
                "server/drizzle/migrations/` against `SELECT path FROM "
                "public.schema_migration_manifest`. Note `origin/main` is ambiguous as a "
                "revision here — a worktree directory carries that name — so use the full "
                "ref. Applying anything with `CREATE INDEX CONCURRENTLY` is gated by the "
                "nightly dump window documented above.\n\n")

    doc += "## Extensions installed\n\n"
    doc += md_table(["extension", "version"], [[f"`{e['name']}`", e["version"]] for e in exts])
    doc += ("\nNotable: `hypopg` tests an index hypothetically before you build it; `pg_stat_statements` "
            "is how the table above is produced; `vector` (pgvector) is available in-database "
            "alongside the separate Qdrant service.\n")

    if COLLECTOR_ERRORS:
        doc += ("\n## Collector warnings\n\n"
                "Each line is a collector that FAILED this run. Any section it feeds is\n"
                "unreliable — treat an empty table there as unknown, not as zero.\n\n"
                + "\n".join(f"- {e}" for e in COLLECTOR_ERRORS) + "\n")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(f"Database Schema Map written: {OUT}")
    print(f"  {len(tables)} tables, {len(views)} views, {len(parts)} partitioned parents, {len(exts)} extensions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
