#!/bin/bash
# Copies the freshest scraped CSVs from the sibling scraper repos into
# TrendForceDash's own csv/ folder. TrendForceDash keeps its own copy on
# purpose (see cluster_topics.py's docstring) so this repo stays portable
# and self-contained on GitHub, rather than depending on
# TrendforceTwitterScraper/TrendforceFacebookScraper existing at fixed
# paths - but that means nothing here scrapes anything, so without this
# sync step the copy just goes stale forever.
#
# Run before analysis, not after: cluster_topics.py etc. only see whatever
# is already in csv/ when they start.
#
# Gap vs. spec: SRS Open Issue #3 asks whether data collection should use
# official APIs, licensed data providers, or "other" - still unresolved.
# The sibling scrapers use Playwright browser automation against real
# logged-in sessions (X: username/password; Facebook: manual login), not
# an official API with a data-use license. NFR-04 (adhere to platform ToS)
# is a standing, unaddressed risk as a result, not something this repo
# can fix on its own since the scraping itself lives in the sibling repos.
#
# Locking: run_pipeline.sh calls this at the start of every job, and the
# cron schedule has jobs that land on the same minute (scan every 4h and
# accounts every 8h both fire at 0/8/16h) - two concurrent instances both
# truncating and writing the same csv/facebook/<handle>.csv raced and
# corrupted it (a UTF-8 multi-byte sequence split mid-character) the first
# time this actually happened. mkdir is atomic on POSIX filesystems, so use
# a lock directory as a mutex; a second instance waits rather than racing.

set -u
cd "$(dirname "$0")"

LOCKDIR=".sync_data.lock"
waited=0
while ! mkdir "$LOCKDIR" 2>/dev/null; do
  if [ "$waited" -ge 60 ]; then
    echo "[WARN] sync_data: lock held for 60s - assuming it's stale (a crashed run left it behind) and taking over"
    rmdir "$LOCKDIR" 2>/dev/null
    mkdir "$LOCKDIR" 2>/dev/null
    break
  fi
  sleep 1
  waited=$((waited + 1))
done
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

# Derived relative to this script's own directory (we've already cd'd
# there above) rather than a hardcoded absolute path, so this doesn't
# need editing on every handoff to a new machine - see add_account.py's
# matching comment for the same convention on the Python side.
TWITTER_SRC="$(pwd)/../TrendforceTwitterScraper"
FACEBOOK_SRC="$(pwd)/../TrendforceFacebookScraper"
LINKEDIN_SRC="$(pwd)/../TrendforceLinkedinScraper"

# Pulled from cluster_topics.PLATFORM_ACCOUNTS (own + competitors merged,
# accounts_config.json included) rather than hardcoded here - a hardcoded
# second copy of the account list is exactly how tphuang/technewsinside
# ended up registered via add_account.py but never actually synced into
# this repo's own csv/: this file's list just didn't know they existed.
X_HANDLES=($(python3 -c "from cluster_topics import PLATFORM_ACCOUNTS as P; print(' '.join(P['X']['own'] + P['X']['competitors']))"))
FB_HANDLES=($(python3 -c "from cluster_topics import PLATFORM_ACCOUNTS as P; print(' '.join(P['Facebook']['own'] + P['Facebook']['competitors']))"))
LI_HANDLES=($(python3 -c "from cluster_topics import PLATFORM_ACCOUNTS as P; print(' '.join(P['LinkedIn']['own'] + P['LinkedIn']['competitors']))"))

synced=0
missing=0

for h in "${X_HANDLES[@]}"; do
  src="$TWITTER_SRC/csv/$h.csv"
  if [ -f "$src" ]; then
    # cp truncates-then-writes the destination, same non-atomic hazard as
    # the Facebook concatenation below - copy to a temp path and rename
    # into place instead.
    tmp="csv/$h.csv.tmp.$$"
    cp "$src" "$tmp"
    mv "$tmp" "csv/$h.csv"
    synced=$((synced + 1))
  else
    echo "[WARN] sync_data: missing $src"
    missing=$((missing + 1))
  fi
done

mkdir -p csv/facebook
for h in "${FB_HANDLES[@]}"; do
  # The Facebook scraper writes one dated file per day it ran
  # (facebook_<handle>_<date>.csv), each holding only that day's *new*
  # posts (parse_facebook.py dedups against all prior dated files before
  # writing). Copying only the newest file (as this used to do) silently
  # dropped every earlier day's history the moment a new day's file
  # appeared - concatenate all of them instead, keeping one header row.
  dated_files=$(ls -tr "$FACEBOOK_SRC"/csv/facebook_"$h"_*.csv 2>/dev/null)
  if [ -n "$dated_files" ]; then
    out="csv/facebook/$h.csv"
    # Merge via merge_facebook_csv.py (schema-aware: each dated file is read
    # under its OWN header, not assumed to match the first file's - a plain
    # `cat`/`tail -n +2` broke the day the scraper added a
    # reactionsBreakdown column, silently shifting every field in the newer
    # rows and leaving `text` empty), building into a temp path and
    # `mv`-ing it into place atomically for the same reason as the X copy
    # below: a concurrent reader (a *different* job's cluster_topics.py)
    # must never see a partially-written file.
    tmp="$out.tmp.$$"
    python3 merge_facebook_csv.py "$tmp" $dated_files
    mv "$tmp" "$out"
    synced=$((synced + 1))
  else
    echo "[WARN] sync_data: no dated CSV found for Facebook handle $h in $FACEBOOK_SRC/csv"
    missing=$((missing + 1))
  fi
done

mkdir -p csv/linkedin csv/linkedin/profiles
for h in "${LI_HANDLES[@]}"; do
  # Same one-file-per-handle shape as X's scraper (scrape_accounts_linkedin.js
  # keeps writing csv/<handle>.csv in place, not dated files like Facebook's),
  # so the same tmp+mv atomic-copy pattern as the X loop above applies here.
  # A tracked individual's personal profile (scrape_profiles_linkedin.js)
  # writes to csv/profiles/<handle>.csv instead of csv/<handle>.csv - a
  # company-page handle never has a profiles/ file and vice versa, so try
  # the company path first and fall back rather than needing to know in
  # advance which kind a given handle is.
  src="$LINKEDIN_SRC/csv/$h.csv"
  dest="csv/linkedin/$h.csv"
  if [ ! -f "$src" ]; then
    src="$LINKEDIN_SRC/csv/profiles/$h.csv"
    dest="csv/linkedin/profiles/$h.csv"
  fi
  if [ -f "$src" ]; then
    tmp="$dest.tmp.$$"
    cp "$src" "$tmp"
    mv "$tmp" "$dest"
    synced=$((synced + 1))
  else
    echo "[WARN] sync_data: missing $src"
    missing=$((missing + 1))
  fi
done

# Optional: real reply/comment text for our own flagged posts (FR-05),
# from each platform's scrape_own_comments.js - a separate, deliberately
# narrow scrape run on demand, not on this schedule, so it won't exist
# every cycle. Absence isn't a failure; the reply queue works fine with
# just reply_count.
mkdir -p analysis
if [ -f "$TWITTER_SRC/own_comments.json" ]; then
  tmp="analysis/own_comments.json.tmp.$$"
  cp "$TWITTER_SRC/own_comments.json" "$tmp"
  mv "$tmp" "analysis/own_comments.json"
fi
if [ -f "$FACEBOOK_SRC/own_comments_facebook.json" ]; then
  tmp="analysis/own_comments_facebook.json.tmp.$$"
  cp "$FACEBOOK_SRC/own_comments_facebook.json" "$tmp"
  mv "$tmp" "analysis/own_comments_facebook.json"
fi

# Optional: platform-wide video discovery (scrape_video_discovery.js) -
# also a separate, on-demand scrape rather than one tied to a specific
# tracked account, so it won't exist every cycle either. video_ranking.py
# just sees fewer platform-wide videos if it's missing/stale, not an error.
if [ -f "$TWITTER_SRC/csv/video_discovery.csv" ]; then
  tmp="csv/video_discovery.csv.tmp.$$"
  cp "$TWITTER_SRC/csv/video_discovery.csv" "$tmp"
  mv "$tmp" "csv/video_discovery.csv"
  synced=$((synced + 1))
fi

echo "sync_data: synced $synced account file(s), $missing missing"
[ "$missing" -eq 0 ]
