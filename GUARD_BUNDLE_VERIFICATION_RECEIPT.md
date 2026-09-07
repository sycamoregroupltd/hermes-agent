Guard-bundle observation contract verification receipt
Task: jarvis-os/t_c3c9d971
Date: 2026-09-07

Scope and safety
- Candidate only; no live scripts, cron stores, gateway, board state, credentials, or provider settings were mutated.
- Worktree: /home/frank/.hermes-worktrees/t_c3c9d971
- Delivery target: jarvis-os Kanban board.
- Named consumer: report-to-board.py -> jarvis-os card/voice-board reader; report cards are assigned to fleet-engineer on jarvis-os, with jarvis-os-pm as board triage owner.
- Opt-in remains limited to the 15m shim; other cadence shims were not opted in.

Executed-copy hashes (candidate worktree)
- profiles/jarvis/scripts/cron_guard_bundle_runner.py: 2c684099df266471fbb41ba9fa4b6bdd681d0730b00d0ddc12f206773db454d8
- scripts/report-to-board.py: 284db22ffa5a704491a069a69607ded690d3d68dd406bb8334e69c35e30d05f6
- profiles/jarvis/scripts/guard_bundle_tick_15m.sh: da51f0a9f34381706cf0cc18b707c0b2d4b4c8a62c006c6adfe54702a9bb6223
- scripts/guard_bundle_run.sh: 16be6773ae8f4693b47b33bbc62cc214a8c29de73f7f84412718a03c7ea6dfa9
- scripts/tests/test_guard_bundle_observation.py: 2bfac8e73d979c5b6b3aa45609dedfa63fcb9c1a27cff47a18761187096f8bfb

Live-versus-candidate boundary
- Live source hashes at verification: runner 80e2f3ffbd880185c43e69182c1ccaca0d050f3a8ce1a0f9a1a12317616b4fba; report-to-board ece141efa993caaf8430bcfda44684ee3a4502773f30ce649c96675ff08a467d; 15m shim 0d1cbcc5c8761856ff5cbe8cfc06c23cfe7eebdffc23bf8278d60e6bdebd3db3; wrapper 8a7375f2a54b28ac7c8fbb7a7e35cac8f8ad842aa89c8c0ace0522f7354292a9.
- The candidate was not installed. The live tree therefore still has the pre-candidate hashes above.

Store, scheduler, and liveness evidence
- Authoritative store row read from /home/frank/.hermes/profiles/jarvis/cron/jobs.json: id 83cf8659dc32, name guard-bundle-tick-15m, script guard_bundle_tick_15m.sh, no_agent=true, enabled=true, state=scheduled, schedule=every 15m, deliver=local.
- At 2026-09-07 08:37:19+01:00 the scheduler wrote its own output artifact /home/frank/.hermes/profiles/jarvis/cron/output/83cf8659dc32/2026-09-07_08-37-19.md with status script failed and exit 1. This proves the registered row fired; it is not a claim that the candidate is live.
- Hermes gateway service evidence: hermes-gateway-jarvis.service ActiveState=active, SubState=running, ExecMainPID=3754252, ActiveEnterTimestamp=Sun 2026-09-06 17:16:37 BST.
- Current live failure remains visible in the operator report (cron-health-canary and related issues); no attempt was made to manufacture a clean live canary.

Isolated end-to-end chain proof
- Test test_isolated_real_entrypoint_chain_and_exact_copies copied the candidate shim -> report-to-board.py -> guard_bundle_run.sh -> runner into a temporary HERMES_HOME, supplied fixture checks for every manifest member, removed inherited HERMES_KANBAN_* routing variables, used a fake hermes board CLI, and asserted zero stdout on qualified clean.
- The same test asserted each copied entrypoint bytes/hash exactly matched its candidate source, observed board show/complete/archive calls, verified the temporary RTB map was retired, and verified pending_recheck became empty.
- No external CLI, network, live-store, or live-board write was used by the isolated chain.

Focused verification
- Command: python3 -m unittest discover -s scripts/tests -p 'test_guard_bundle_observation.py' -v
- Result: 32 tests passed, 0 failures, exit 0.
- Commands: python3 -m py_compile profiles/jarvis/scripts/cron_guard_bundle_runner.py scripts/report-to-board.py scripts/tests/test_guard_bundle_observation.py; bash -n profiles/jarvis/scripts/guard_bundle_tick_15m.sh scripts/guard_bundle_run.sh; git diff --check
- Result: all exit 0.

Required matrix receipt
- R01: test_no_due_active_incident_emits_no_due_and_stays_open; no due work emits NO_DUE_CHECKS and retains pending debt.
- R02: test_due_clean_clears_pending_debt; qualified CLEAN clears pending debt.
- R03: test_no_incident_no_due_emits_no_due_without_state_change.
- R04: test_legacy_nonzero_nonempty_report_remains_one_key_failure.
- R05: test_nonzero_empty_legacy_report_stays_loud; synthesized failure remains nonzero.
- R06: test_sibling_clean_does_not_clear_failed_member; emits DEFERRED.
- R07: test_failure_dominates_deferred_work.
- R08: test_inner_and_outer_lock_contention_are_silent.
- R09: test_budget_refusal_is_deferred_without_timestamp_advance.
- R10: test_due_clean_clears_pending_debt covers all due work passing without inventing residual elapsed-time debt.
- R11: test_crash_after_prelaunch_keeps_in_flight_pending and test_crash_after_prelaunch_adds_new_identity_to_pending.
- R12: test_missing_sidecar_bootstraps_all_members_before_partial_pass.
- R13: test_malformed_observation_state_is_visible_failure, test_observation_write_failure_is_visible_failure, and test_timestamp_write_failure_is_visible_failure.
- R14: test_protocol_malformed_success_is_loud_and_creates_card.
- R15: test_protocol_nonzero_marker_is_failure_not_clear and test_protocol_disabled_marker_is_ordinary_legacy_report.
- R16: test_echo_suppresses_control_records_but_preserves_human_failure.
- R17: test_running_owner_gets_recovery_comment_not_lifecycle_mutation and test_unknown_card_status_is_fail_closed_and_preserves_mapping.
- R18: test_repeated_failure_digest_does_not_duplicate_comment.
- R19: test_rtb_state_file_vetoes_clean_but_failure_bypasses_veto.
- R20: test_board_api_failure_preserves_mapping_and_is_loud and test_archive_api_failure_preserves_mapping_and_is_loud.
- R21: test_same_key_lock_spans_slow_producer_and_application.
- R22: test_child_rtb_environment_isolated_from_parent.
- R23: test_membership_change_retains_removed_pending_identity.
- R24: test_legacy_success_and_timeout_behavior_is_preserved.

Residual baseline risk
- Full command python3 -m unittest discover -s scripts/tests -v remains non-green independently of this focused suite: 32 guard tests passed, while the pre-existing test_service_gate_escalation_watchdog_remint_guard module imports with SystemExit(1) and reports 14 unrelated fixture checks failed. This task did not edit that test or its implementation.

Review state
- Candidate requires independent os-reviewer review before landing. No live rollout is claimed.
