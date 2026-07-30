#!/bin/bash
# Commits and pushes whatever run_pipeline.sh just produced (docs/index.html,
# analysis/*.json, synced csv/), then watches the resulting GitHub Pages
# deployment with retry/backoff - adapted from
# TrendforceTwitterScraper/publish.sh's battle-tested version of this
# (that project hit real GitHub Pages race conditions where two deploys
# landing close together cancel one another, and outright push failures
# from network blips or a moved remote).
#
# Call this as the last step of run_pipeline.sh, after generate_dashboard.py
# has already run - this script only publishes, it doesn't regenerate.

cd "$(dirname "$0")"

notify() { bash alert.sh "$1" "$2"; }

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting publish..."

# 0. Validate the data is actually loadable before committing it. This
#    exists because of a real incident: a cron race between two
#    sync_data.sh instances corrupted a Facebook CSV mid-write (a split
#    UTF-8 multi-byte sequence), and it would have been silently committed
#    and published without this check.
if ! python3 -c "from cluster_topics import load_posts; load_posts()" 2>/tmp/trendforcedash_validate.log; then
  notify "TrendForceDash Publish FAILED" "Data failed to load (corrupt CSV?) - publish blocked. Check pipeline.log."
  cat /tmp/trendforcedash_validate.log
  exit 1
fi

git add -A
if git diff --cached --quiet; then
  echo "Nothing changed, skipping push."
  exit 0
fi

git commit -m "Automated pipeline update $(date '+%Y-%m-%d %H:%M')" >/dev/null

set +e  # from here on, handle failures ourselves instead of dying on the first one

# 1. Push with retry + backoff.
PUSH_OK=0
for attempt in 1 2 3; do
  PUSH_OUTPUT=$(git push 2>&1)
  if [ $? -eq 0 ]; then
    PUSH_OK=1
    break
  fi
  echo "$PUSH_OUTPUT"
  if echo "$PUSH_OUTPUT" | grep -qi "authentication failed\|invalid username or token"; then
    notify "TrendForceDash Publish FAILED" "git push auth failed - GitHub credentials need to be refreshed."
    echo "[ERROR] Authentication failure - not retrying, this needs manual credential setup."
    exit 1
  fi
  echo "[WARN] git push failed (attempt $attempt/3), retrying after rebase..."
  git pull --rebase 2>&1
  sleep $((attempt * 5))
done

if [ "$PUSH_OK" -ne 1 ]; then
  notify "TrendForceDash Publish FAILED" "git push failed after 3 attempts. Check pipeline.log."
  echo "[ERROR] git push failed after 3 attempts."
  exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushed. Watching GitHub Pages deployment..."

# 2. Watch the resulting GitHub Pages deployment and auto-redeploy if two
#    pushes race each other into a failed (not just superseded) state.
REPO="elainekaotf/TrendForceDashboard"

wait_for_run_conclusion() {
  # $1 = commit SHA to match. Polls up to ~3 minutes. Echoes the conclusion
  # ("success", "failure", "cancelled", "ratelimited", or "" if it never
  # showed up/finished).
  local sha="$1"
  for i in $(seq 1 12); do
    sleep 15
    local raw
    raw=$(curl -s "https://api.github.com/repos/${REPO}/actions/runs?per_page=10")
    if echo "$raw" | grep -qi "API rate limit exceeded"; then
      echo "ratelimited"
      return
    fi
    local run
    run=$(echo "$raw" | python3 -c "
import json, sys
sha = sys.argv[1]
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for r in d.get('workflow_runs', []):
    if r.get('head_sha') == sha:
        print(r.get('status',''), r.get('conclusion') or '')
        break
" "$sha")
    local run_status="${run%% *}"
    local run_conclusion="${run#* }"
    if [ "$run_status" = "completed" ]; then
      echo "$run_conclusion"
      return
    fi
  done
  echo ""
}

DEPLOY_OK=0
for redeploy_attempt in 1 2 3; do
  SHA=$(git rev-parse HEAD)
  CONCLUSION=$(wait_for_run_conclusion "$SHA")

  if [ "$CONCLUSION" = "success" ]; then
    DEPLOY_OK=1
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deployment succeeded (commit ${SHA:0:7})."
    break
  fi

  if [ "$CONCLUSION" = "ratelimited" ]; then
    echo "[WARN] GitHub API rate limit hit while checking deploy status - can't confirm, but the push itself succeeded. Skipping further checks."
    DEPLOY_OK=1
    break
  fi

  echo "[WARN] Deployment for commit ${SHA:0:7} concluded '$CONCLUSION' (attempt $redeploy_attempt/3)."

  # A 'cancelled' conclusion almost always means a NEWER commit already
  # landed and superseded this deploy (GitHub cancels an in-progress Pages
  # deployment when a later one is queued for the same branch) - found
  # 2026-07-30: during a backlog of queued run_pipeline.sh invocations
  # (multiple independent scan/core/accounts/daily schedules plus every
  # scraper repo's own post-scrape sync call, all serialized behind the
  # same lock), jobs finished seconds apart and each one's forced
  # empty-commit "redeploy" here just got cancelled by the NEXT queued
  # job's push in turn - burning ~10min per run on doomed retries and
  # making the backlog itself worse, not better. If HEAD has moved past
  # our own SHA, some other commit already superseded us and will get its
  # own deployment (or already has) - that's success, not a failure, so
  # skip the redeploy entirely rather than adding to the pile-up.
  git fetch origin --quiet 2>/dev/null
  CURRENT_HEAD=$(git rev-parse HEAD)
  REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "$CURRENT_HEAD")
  if [ "$CONCLUSION" = "cancelled" ] && [ "$REMOTE_HEAD" != "$SHA" ]; then
    echo "  Superseded by a newer commit (${REMOTE_HEAD:0:7}) - that push gets its own deployment. Not treating this as a failure."
    DEPLOY_OK=1
    break
  fi

  if [ "$redeploy_attempt" -lt 3 ]; then
    echo "  Backing off before redeploying..."
    sleep 20
    git commit --allow-empty -m "Redeploy dashboard (previous deploy: ${CONCLUSION:-timeout})" >/dev/null
    git push >/dev/null 2>&1
  fi
done

if [ "$DEPLOY_OK" -ne 1 ]; then
  notify "TrendForceDash Publish WARNING" "GitHub Pages deploy did not confirm success after 3 attempts. Check the Actions tab."
  echo "[WARN] Could not confirm a successful deployment after 3 attempts. Site may be stale - check Actions tab manually."
  exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done. Dashboard pushed and deployed to GitHub Pages."
