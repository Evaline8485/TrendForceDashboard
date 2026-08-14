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

# Preflight: if a PRIOR run's push failed badly enough to leave the repo
# mid-rebase or detached (both now meant to be structurally impossible
# with the merge-based retry below, but this guards against a run that
# happened before that fix, or any other way the repo could end up here),
# recover before doing anything else - otherwise every step below (git
# add/commit/push) either fails outright or silently operates on the
# wrong ref. Stash first: by the time publish.sh runs, generate_dashboard.py
# etc. have already written this run's fresh output to the working tree,
# uncommitted - `git reset --hard`/`rebase --abort` would otherwise
# discard it along with whatever mess the prior run left behind.
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] || ! git symbolic-ref -q HEAD >/dev/null; then
  echo "[WARN] Repo isn't in a clean, publishable state (stuck rebase or detached HEAD) - recovering before continuing."
  git stash push -u -m "preflight: this run's fresh output" >/dev/null 2>&1
  git rebase --abort 2>/dev/null
  git checkout main 2>/dev/null
  git reset --hard origin/main 2>/dev/null
  git stash pop >/dev/null 2>&1
  if git status --porcelain=1 | grep -q '^UU'; then
    CONFLICTED=$(git diff --name-only --diff-filter=U)
    echo "[WARN] Stash-pop conflict in: $CONFLICTED - keeping this run's fresh output."
    echo "$CONFLICTED" | xargs git checkout --theirs --
    echo "$CONFLICTED" | xargs git add --
    git stash drop >/dev/null 2>&1
  fi
fi

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
  echo "[WARN] git push failed (attempt $attempt/3), retrying after merge..."
  # Merge, not rebase: a rebase replays our commit elsewhere and can leave
  # HEAD detached mid-conflict (found repeatedly 2026-08-13/14 on
  # Evaline's Mac - every occurrence needed someone to manually
  # `git rebase --abort` and reset before the repo could push again). A
  # merge never detaches HEAD - we stay on main throughout - and its
  # --ours/--theirs mean what they intuitively sound like (unlike a
  # rebase, where they're swapped), which matters here since we're about
  # to resolve conflicts unattended.
  #
  # Every conflict seen in practice has been in pipeline-generated output
  # (analysis/*.json, docs/index.html, synced csv/) - never hand-edited -
  # so keeping OUR side (this run's freshly regenerated data, which
  # reflects whatever sync_data.sh just pulled) and discarding the
  # incoming side is safe: nothing hand-authored is ever at stake here,
  # and the next run regenerates everything again regardless of which
  # side "won".
  git pull --no-rebase --no-edit 2>&1
  if git status --porcelain=1 | grep -q '^UU'; then
    CONFLICTED=$(git diff --name-only --diff-filter=U)
    echo "[WARN] Merge conflict in: $CONFLICTED - auto-resolving (keeping this run's own freshly-generated version)."
    echo "$CONFLICTED" | xargs git checkout --ours --
    echo "$CONFLICTED" | xargs git add --
    git commit --no-edit >/dev/null 2>&1
  fi
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

  # A 'cancelled' (or never-concluded) result usually just means a NEWER
  # commit already landed and superseded this deploy - GitHub cancels an
  # in-progress Pages deployment whenever a later one is queued for the
  # same branch. Found 2026-07-30: during a backlog of queued
  # run_pipeline.sh invocations (multiple independent scan/core/accounts/
  # daily schedules, plus every scraper repo's own post-scrape sync call,
  # all serialized behind the same lock), 8 commits landed within ~3
  # minutes and each got cancelled by the next - and this script's OWN
  # forced empty-commit "redeploy" on every cancellation just added MORE
  # pushes into that same congested window, burning ~10min per run on
  # retries that couldn't possibly land while the backlog kept incoming.
  # If a newer commit already exists upstream, it already got (or will
  # get) its own deployment attempt - forcing another push here can only
  # make the pile-up worse, never better, so skip the redeploy entirely.
  git fetch origin --quiet 2>/dev/null
  REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "$SHA")
  if [ "$REMOTE_HEAD" != "$SHA" ]; then
    echo "  Superseded by a newer commit (${REMOTE_HEAD:0:7}) already - that push gets its own deployment attempt. Not redeploying."
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
