#!/usr/bin/env bash
# SHIM — approved exec wrapper for the pipeline-divergence daily detector (t_f1fa7cb0,
# migrated from profiles/devops). Canonical source is /home/frank/.hermes/scripts/pipeline-divergence-cron.sh.
#
# CANONICAL-COPY RULE (t_bad6ee2e pattern, per t_24480103 D1/D2): cron/scheduler_script.py
# ::_resolve_script_path resolves a job's relative ``script`` against THAT JOB'S PROFILE
# HERMES_HOME (~/.hermes/profiles/<profile>/scripts/), and the containment check rejects
# anything outside it. A job registered on the jarvis profile with
# ``script: pipeline-divergence-cron.sh`` resolves to THIS file and nowhere else.
# Edit the canonical implementation at /home/frank/.hermes/scripts/pipeline-divergence-cron.sh.
exec bash /home/frank/.hermes/scripts/pipeline-divergence-cron.sh "$@"
