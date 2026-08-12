#!/bin/bash
# Scheduled entry point for auto_approve_accounts.py. Each approved request
# can trigger a real onboarding scrape + a full `run_pipeline.sh core` run
# (add_account.py/remove_account.py do this internally), which the earlier
# manual test run showed can take a while - a lock here prevents two
# scheduled firings from overlapping and racing each other's git
# commit/push, same mkdir-mutex pattern as sync_data.sh/run_pipeline.sh.
# /opt/homebrew/bin must come FIRST - see run_pipeline.sh's matching comment.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Library/Frameworks/Python.framework/Versions/3.10/bin:$PATH"
cd "$(dirname "$0")"

LOG="$(dirname "$0")/auto_approve.log"
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG" 2>/dev/null)" -gt 5242880 ]; then
  mv "$LOG" "$LOG.old"
fi

LOCKDIR=".auto_approve.lock"
STALE_AFTER=5400  # 90m - generously above an onboarding scrape + pipeline run
while ! mkdir "$LOCKDIR" 2>/dev/null; do
  lock_mtime=$(stat -f%m "$LOCKDIR" 2>/dev/null || stat -c%Y "$LOCKDIR" 2>/dev/null)
  lock_age=$(( $(date +%s) - ${lock_mtime:-0} ))
  if [ "$lock_age" -ge "$STALE_AFTER" ]; then
    echo "[WARN] run_auto_approve: lock directory is ${lock_age}s old - assuming a crashed run left it behind, taking over"
    rmdir "$LOCKDIR" 2>/dev/null
    mkdir "$LOCKDIR" 2>/dev/null
    break
  fi
  sleep 5
done
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Checking for account requests..."
  python3 auto_approve_accounts.py
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done."
} >> "$LOG" 2>&1
