#!/bin/bash
# Scheduler entry point for TrendForceDash. Runs a named job (a group of
# FR scripts in dependency order) and reports failures without stopping
# the rest of the job. See scheduling.md for how this is actually
# scheduled (launchd, not cron) and SRS Section 6 (Scheduling
# Requirements) for the source frequencies.
#
# Usage: bash run_pipeline.sh <scan|core|accounts|daily>
#
# Jobs:
#   scan     (every 4h)   FR-02 (scan tier only) -> FR-04
#            Implements the SRS's own proposed resolution to Open Issue #1
#            ("detect every 4 hours" vs "run every 6 hours"): a two-tier
#            schedule. This tier is FR-02-01 only (top rising topics, no
#            KOL/sub-topic drill-down) for the 4h range - fast enough to
#            run every 4h without duplicating 'core's full 6h recompute.
#   core     (every 6h)  FR-01 -> FR-02 (full) -> FR-03 -> FR-04
#            Topic clustering feeds FR-02/03/06; FR-04 refreshes right
#            after so newly-produced topic/sentiment/KOL calls are
#            queued for review promptly. FR-02 here is the full
#            FR-02-01..04 chain across all 5 ranges - the other tier of
#            the two-tier schedule above.
#   accounts (every 8h)  FR-05
#            Independent of the core chain - only touches account
#            status + own-account reply drafts.
#   daily    (once/day)  FR-06 -> FR-04
#            generate_summaries.py runs FR-03 itself in 'daily' mode
#            internally, so this doesn't need to call nlp_sentiment.py
#            separately. FR-04 refreshes again to queue the day's
#            summaries for review.
#
# Every job starts with sync_data.sh (pulls fresh CSVs from the sibling
# scraper repos - see that script's header for why this is needed at all),
# regenerates docs/index.html (generate_dashboard.py), then publishes it
# (publish.sh: commit, push, confirm the GitHub Pages deploy) so the live
# site reflects whatever that job just produced - previously this only
# updated the local file, so scheduled runs left the public URL stale
# until someone happened to push manually.
#
# FR-07 (self-service upload) is user-triggered, not scheduled - it isn't
# part of any job here.

set -u
# /opt/homebrew/bin MUST come first - found 2026-08-12 handing off to an
# Apple Silicon Mac: with it listed after /usr/bin (an earlier attempt),
# `python3` still resolved to Apple's own /usr/bin/python3 stub instead of
# Homebrew's real install (where sklearn etc. actually got pip-installed),
# since PATH resolution stops at the FIRST match, not the "best" one - the
# order here is the fix, not just whether the entry is present at all.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Library/Frameworks/Python.framework/Versions/3.10/bin:$PATH"
cd "$(dirname "$0")"

# Validated before locking (it names the job's queue slot below), so a typo
# can't take the lock or leave a stray lock file behind.
JOB="${1:-}"
case "$JOB" in
  scan|core|accounts|daily) ;;
  *) echo "Usage: $0 <scan|core|accounts|daily>" >&2; exit 1 ;;
esac

# Locking: scan/core/accounts/daily all regenerate the SAME docs/index.html
# and do their own git commit+push (publish.sh) at the end - with no lock,
# overlapping invocations (multiple scheduled launchd jobs, plus the
# scraper-triggered chains added 2026-07-14/15) raced each other's pushes.
# In practice this showed up as GitHub Pages deployments getting cancelled
# (superseded by the next push landing seconds later) and a growing
# backlog where "pipeline run complete" timestamps were hours after
# "Starting". This used to be a mkdir lock directory with a 2h "stale"
# takeover; legitimate runs kept outliving that, so waiters stole the lock
# from live runs (307 takeovers in pipeline.log) and on 2026-11-16 four
# 'core' runs were executing at once. pipeline_lock.sh replaces it with a
# kernel lock (flock) that can't go stale, plus at most one queued run per
# job - see that file for the details, and tests/test_pipeline_lock.sh.
. ./pipeline_lock.sh
pipeline_enter "$JOB"
case $? in
  0) ;;
  75)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Skipping this '$JOB' trigger - another '$JOB' run is already queued and will sync the latest data when it starts."
    exit 0
    ;;
  *)
    echo "[ERROR] run_pipeline: could not take the pipeline lock (is python3 working?) - not running '$JOB' unlocked."
    bash alert.sh "TrendForceDash: $JOB run — lock error" "run_pipeline could not take the pipeline lock, so '$JOB' did not run. Check pipeline.log."
    exit 1
    ;;
esac

# macOS drifts into Deep Idle/Power Nap dark-wake cycling when left alone
# for a while, even mid-run - the same bug already found and fixed in
# every sibling scraper repo's run_all.sh/run_daily.sh (2026-07-27), but
# never applied here. Found 2026-08-07: 'core'/'accounts' runs were
# legitimately taking 3+ hours instead of minutes, which pushed their
# lock hold time past STALE_AFTER (2h) while genuinely still alive -
# a queued waiter then "stole" the lock out from under the still-running
# original (the exact race this lock's own comments already describe),
# giving TWO simultaneous holders that raced each other's git pushes -
# which is what was overwhelming GitHub's Pages deploy queue ("job was
# not acquired by Runner... even after multiple attempts" on 4 separate
# deploys in one day). caffeinate -i -w $$ blocks idle sleep for exactly
# this script's own lifetime, fixing the actual slowness instead of just
# the lock's symptom of it. 9>&-: caffeinate must not inherit the pipeline
# lock's descriptor, or the lock would outlive this script for as long as
# caffeinate lingers.
caffeinate -i -w $$ 9>&- &
disown

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting '$JOB' pipeline run..."

FAILURES=()
run_step() {
  local label="$1"; shift
  if ! python3 "$@"; then
    echo "[WARN] $label failed"
    FAILURES+=("$label")
  fi
}

if ! bash sync_data.sh; then
  echo "[WARN] sync_data had missing sources - continuing with whatever csv/ already has"
  FAILURES+=("sync_data")
fi

case "$JOB" in
  scan)
    run_step "fuzzy_trend_scan" fuzzy_trend.py scan
    run_step "video_ranking"    video_ranking.py
    run_step "manual_review"    manual_review.py build
    ;;
  core)
    run_step "cluster_topics"  cluster_topics.py
    run_step "fuzzy_trend"     fuzzy_trend.py full
    run_step "nlp_sentiment"   nlp_sentiment.py
    run_step "video_ranking"   video_ranking.py
    run_step "manual_review"   manual_review.py build
    ;;
  accounts)
    run_step "account_comment_management" account_comment_management.py build
    ;;
  daily)
    run_step "generate_summaries" generate_summaries.py
    run_step "manual_review"      manual_review.py build
    ;;
  *)
    echo "Unknown job '$JOB'. Expected scan|core|accounts|daily." >&2
    exit 1
    ;;
esac

# Regenerate the dashboard (docs/index.html) after every job so it always
# reflects whichever analysis files that job just refreshed.
run_step "generate_dashboard" generate_dashboard.py

# Publish (commit + push + confirm the GitHub Pages deploy) whatever just
# got generated, even if an earlier step in this job failed - a partial
# update published beats a correct one nobody sees until someone happens
# to push manually.
if ! bash publish.sh; then
  echo "[WARN] publish failed"
  FAILURES+=("publish")
fi

if [ ${#FAILURES[@]} -gt 0 ]; then
  JOINED=$(IFS=', '; echo "${FAILURES[*]}")
  bash alert.sh "TrendForceDash: $JOB run — issues" "Steps that failed: ${JOINED}. Check pipeline.log."
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] '$JOB' pipeline run finished with failures: ${JOINED}"
  exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] '$JOB' pipeline run complete."
