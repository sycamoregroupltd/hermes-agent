# Review routing policy

`/home/frank/.hermes/scripts/verdict_router.py` is the canonical review-routing mechanism. Its `classify_risk(changed_paths, change_flags)` function is deterministic and consumes only an explicit changed-path manifest plus structured boolean flags; title prose and free-form keywords cannot approve or downgrade a change.

High-risk signals require `requires_standalone_risk_review=true` and use stable reason codes: `money`, `live_execution`, `access_material`, `ddl_or_irreversible_data`, and `measurement_write_path`. Missing, malformed, unknown, or conflicting input adds `unknown_input` and sets `fail_closed=true`. Matching is case-insensitive and normalizes separators, including generated paths.

Known paper-only docs, research, tests, and explicitly flagged refactors remain below the standalone risk-review line. They still require CI-green and inline independent review. This classifier does not create review cards, grant deploy authority, weaken Frank/A3 gates, or alter the existing verdict-router approval and independent-review evidence rules. Standalone risk-review cards must not be created below the line; a high-risk result is a routing requirement for the upstream orchestration layer.

Verification: `python3 -m unittest scripts.test_verdict_router` exercises every high-risk class, paper-only safe class, malformed/unknown/conflicting inputs, title-only negation, generated/case-variant paths, and stable reason ordering.
