# Review routing policy

`/home/frank/.hermes/scripts/verdict_router.py` is the canonical review-routing mechanism. Its `classify_risk(changed_paths, change_flags)` function is deterministic and consumes only an explicit changed-path manifest plus structured boolean flags; title prose and free-form keywords cannot approve or downgrade a change.

High-risk signals require `requires_standalone_risk_review=true` and use stable reason codes: `money`, `live_execution`, `access_material`, `ddl_or_irreversible_data`, and `measurement_write_path`. Missing, malformed, unknown, or conflicting input adds `unknown_input` and sets `fail_closed=true`. Matching is case-insensitive and normalizes separators, including generated paths.

The canonical card-creation entrypoint is `/home/frank/.hermes/scripts/kanban_review_required_auto_router.py`; it consumes the same explicit `change_manifest` JSON line from the source task body. Known paper-only docs, research, tests, and explicitly flagged refactors are suppressed there (no standalone risk-review card) and remain subject to CI-green plus inline independent review. High-risk, missing, malformed, unknown, and conflicting manifests stay on the standalone `trading-risk-reviewer` lane. The router does not grant deploy authority, weaken Frank/A3 gates, or alter existing verdict-router approval and independent-review evidence rules.

Verification: `python3 -m unittest scripts.test_verdict_router scripts.test_kanban_review_required_auto_router_risk` exercises every high-risk class, paper-only safe class, malformed/unknown/conflicting inputs, title-only negation, generated/case-variant paths, stable reason ordering, and the real card-discovery entrypoint.
