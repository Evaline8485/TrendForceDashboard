#!/bin/bash
# Locks for TrendForceDash's shell scripts. Sourced by run_pipeline.sh and
# sync_data.sh - not meant to be run on its own.
# Tests: bash tests/test_pipeline_lock.sh
#
# Replaces the mkdir lock directories both scripts used until 2026-11-16.
# Those judged a lock "stale" by age (run_pipeline: 2h old; sync_data: 60s
# of waiting) and then took it over - but a live run regularly outlasted
# that, so waiters stole the lock from jobs that were still running:
# pipeline.log had 307 run_pipeline takeovers, sometimes two in the same
# second (the takeover never checked that its own mkdir succeeded, so both
# waiters went ahead). Each thief's EXIT trap later deleted the lock out
# from under the next holder, so the overlaps cascaded - on 2026-11-16 four
# 'core' runs (started 04:08, 09:24, 09:24 and 09:55) were executing at
# once, racing each other's analysis files and git pushes, and each run
# took hours because they were all competing for the same CPU.
#
# flock(2) removes the guesswork: the kernel holds the lock exactly as long
# as this shell keeps the descriptor open and drops it the moment the
# process ends for any reason - normal exit, crash or kill -9 - so a lock
# can never go stale and nothing ever needs to be taken over or cleaned up.
# A descendant still running after its parent died (say, an orphaned python
# step) keeps holding it until it finishes too, which is what we want: it
# is still writing analysis files. macOS has no flock(1) command, so
# python3's fcntl.flock makes the flock(2) call on a descriptor this shell
# opened; a flock belongs to the open file description, so this shell keeps
# holding it after that short-lived python3 exits. macOS /bin/bash is 3.2
# (no {var}> descriptor allocation), hence fixed descriptor numbers + eval.
#
# Descriptors used: 9 = pipeline lock, 8 = per-job queue slot, 7 = sync_data.

PIPELINE_LOCK_POLL="${PIPELINE_LOCK_POLL:-5}"                 # seconds between tries while waiting
PIPELINE_LOCK_WARN_AFTER="${PIPELINE_LOCK_WARN_AFTER:-10800}" # warn once after 3h of waiting
LOCK_HELD_ELSEWHERE=75                                        # EX_TEMPFAIL

# lock_try FD FILE
#   Take FILE's lock on descriptor FD if it is free right now.
#   0 = this shell holds it now; 75 = another process holds it;
#   1 = could not even try (python3 missing/broken, unwritable file).
#   Callers must never treat 1 as "held elsewhere", or a broken python3
#   would silently skip every run.
lock_try() {
  eval "exec $1>>\"\$2\"" 2>/dev/null || return 1
  python3 -I -c '
import fcntl, sys
try:
    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)
' "$1"
  case $? in
    0) return 0 ;;
    "$LOCK_HELD_ELSEWHERE") return "$LOCK_HELD_ELSEWHERE" ;;
    *) return 1 ;;
  esac
}

# lock_wait FD FILE [WARN_MESSAGE]
#   Wait until this shell holds FILE's lock on FD, trying every
#   PIPELINE_LOCK_POLL seconds. If that takes longer than
#   PIPELINE_LOCK_WARN_AFTER, print WARN_MESSAGE once (and fire alert.sh
#   when the caller has one) - a hung holder should get noticed, not
#   quietly stall every later run. Nothing is ever taken over.
#   0 = held; 1 = could not take it.
lock_wait() {
  local fd="$1" file="$2" warn_message="${3:-}" started rc warned=0
  started=$(date +%s)
  while :; do
    lock_try "$fd" "$file"
    rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -ne "$LOCK_HELD_ELSEWHERE" ] && return 1
    if [ "$warned" -eq 0 ] && [ -n "$warn_message" ] \
       && [ $(( $(date +%s) - started )) -ge "$PIPELINE_LOCK_WARN_AFTER" ]; then
      echo "[WARN] $warn_message"
      [ -f ./alert.sh ] && bash ./alert.sh "TrendForceDash: waiting on a lock" "$warn_message"
      warned=1
    fi
    sleep "$PIPELINE_LOCK_POLL"
  done
}

# lock_release FD - close FD; the kernel drops its lock once no process
# has that descriptor open any more.
lock_release() {
  eval "exec $1>&-"
}

# pipeline_enter JOB - run_pipeline.sh's way in.
#   All jobs (scan/core/accounts/daily) share ONE pipeline-wide lock, since
#   they all rewrite docs/index.html and push it. On top of that, at most
#   one run per job waits in line: a trigger that finds another run of the
#   same job already queued is redundant - the queued run syncs the freshest
#   CSVs when it starts anyway - so it bows out instead of piling up behind
#   it (the scrapers' backgrounded chains plus the four launchd jobs start
#   19-25 runs a day, many of them duplicate 'core' runs). Once this run
#   holds the pipeline lock it leaves its queue slot, so a trigger that
#   arrives mid-run still gets a run of its own afterwards.
#   0 = holding the pipeline lock (fd 9) - go ahead;
#   75 = redundant trigger - exit quietly;
#   1 = could not take the lock - do NOT run the job unlocked.
pipeline_enter() {
  local job="$1" rc
  lock_try 8 ".run_pipeline.queued.$job"
  rc=$?
  [ "$rc" -ne 0 ] && return "$rc"
  lock_wait 9 ".run_pipeline.flock" \
    "run_pipeline '$job' has waited over $((PIPELINE_LOCK_WARN_AFTER / 60)) min for the pipeline lock - the run holding it ($(cat .run_pipeline.holder 2>/dev/null || echo unknown)) may be hung. Check pipeline.log and ps." \
    || return 1
  lock_release 8
  echo "$$ $job $(date '+%Y-%m-%d %H:%M:%S')" > .run_pipeline.holder
  return 0
}
