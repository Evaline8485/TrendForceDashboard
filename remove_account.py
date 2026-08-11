#!/usr/bin/env python3
"""
Local approval step for the dashboard's "Remove" button on the Account
Status tab (FR-05) - same static-site constraint as add_account.py: no
backend to remove an account on the spot, so the button opens a GitHub
issue instead. Run this script locally to approve the request.

Usage:
    python3 remove_account.py X SomeCompetitorHandle
    python3 remove_account.py Facebook SomePage
    python3 remove_account.py LinkedIn some-company-slug

Removes the handle from accounts_config.json (own or competitors,
whichever list it's in) and re-runs the pipeline so it disappears from
the dashboard immediately. This does NOT delete any already-collected
CSV data - csv/<handle>.csv (or csv/facebook/<handle>.csv) is left on
disk, just no longer read, so re-adding the account later picks its
history back up rather than starting over. Delete the CSV by hand if you
actually want the data gone.

For X, the sibling scraper's KNOWN_ACCOUNTS in scraper.js will keep
scraping the handle on its own schedule even after this - there's no
CLI hook to remove an entry from that file, so remove it by hand there
too if you want the scraping itself to stop, not just the dashboard
display.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / 'accounts_config.json'
# Derived relative to this file rather than a hardcoded absolute path -
# see add_account.py's matching comment.
TWITTER_SCRAPER_DIR = BASE.parent / 'TrendforceTwitterScraper'
LINKEDIN_ACCOUNTS_JS = BASE.parent / 'TrendforceLinkedinScraper' / 'scrape_accounts_linkedin.js'


def normalize_handle(raw):
    h = raw.strip()
    h = re.sub(r'^https?://(www\.)?(x\.com|twitter\.com|facebook\.com)/', '', h, flags=re.IGNORECASE)
    h = h.lstrip('@').rstrip('/')
    h = re.split(r'[/?#]', h)[0]
    return h


def normalize_linkedin_slug(raw):
    h = raw.strip()
    h = re.sub(r'^https?://(www\.)?linkedin\.com/company/', '', h, flags=re.IGNORECASE)
    h = h.rstrip('/')
    return re.split(r'[/?#]', h)[0]


def remove_linkedin_account_from_scraper(slug):
    """Same small, safely-editable ACCOUNTS array add_account.py writes to -
    remove the matching {handle, slug} entry (matched by slug, since slug
    is the stable identifier) rather than leaving it to keep getting
    scraped after removal from accounts_config.json."""
    if not LINKEDIN_ACCOUNTS_JS.exists():
        return
    text = LINKEDIN_ACCOUNTS_JS.read_text(encoding='utf-8')
    pattern = re.compile(r"[ \t]*\{\s*handle:\s*'[^']*',\s*slug:\s*'" + re.escape(slug) + r"'\s*\},?\n?")
    updated = pattern.sub('', text)
    if updated != text:
        LINKEDIN_ACCOUNTS_JS.write_text(updated, encoding='utf-8')
        print(f"Removed {slug} from {LINKEDIN_ACCOUNTS_JS.name}'s ACCOUNTS list.")


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('X', 'Facebook', 'LinkedIn'):
        print(__doc__)
        sys.exit(1)
    platform = sys.argv[1]
    handle = normalize_linkedin_slug(sys.argv[2]) if platform == 'LinkedIn' else normalize_handle(sys.argv[2])

    cfg = load_config()
    removed_from = None
    for bucket in ('own', 'competitors'):
        handles = cfg.get(platform, {}).get(bucket, [])
        if handle in handles:
            handles.remove(handle)
            removed_from = bucket
            break

    if not removed_from:
        print(f"{handle} is not tracked on {platform} - nothing to remove.")
        sys.exit(1)

    save_config(cfg)
    print(f"Removed {handle} from {platform} ({removed_from}) in {CONFIG_PATH.name}.")
    print(f"csv data for {handle} is left on disk - delete it by hand if you want it gone, not just untracked.")

    if platform == 'X':
        print(
            f"[REMINDER] {TWITTER_SCRAPER_DIR / 'scraper.js'}'s KNOWN_ACCOUNTS still has @{handle} - "
            f"the scraper will keep collecting it on its own schedule until you remove it there by hand too."
        )
    elif platform == 'LinkedIn':
        remove_linkedin_account_from_scraper(handle)

    print("Running the TrendForceDash pipeline (sync, rebuild, regenerate, publish) ...")
    ok = True
    for job in ('core', 'accounts'):
        result = subprocess.run(['bash', 'run_pipeline.sh', job], cwd=BASE)
        if result.returncode != 0:
            print(f"[WARN] run_pipeline.sh {job} exited with code {result.returncode} - check pipeline.log.")
            ok = False
    if ok:
        print(f"Done. {handle} no longer appears on the dashboard.")


if __name__ == '__main__':
    main()
