#!/usr/bin/env python3
"""
Local approval step for FR-05's "request tracking" flow: the dashboard's
Account Status tab has no backend to add an account and start crawling
on the spot (it's a static site), so it opens a GitHub issue instead.
Run this script locally to approve a request - it registers the handle
in accounts_config.json and (where possible) kicks off scraping for it.

Usage:
    python3 add_account.py X technews_tw2
    python3 add_account.py Facebook SomeCompetitorPage
    python3 add_account.py LinkedIn some-company-slug
    python3 add_account.py LinkedIn https://www.linkedin.com/company/some-company/
    python3 add_account.py LinkedIn https://www.linkedin.com/in/some-person/    (personal profile)
    python3 add_account.py X technews_tw --own      (register/promote as an own account, not a competitor)

Marking an account 'own' changes real behavior elsewhere: FR-01/02's
competitor-gap analysis stops counting its posts as competitor activity,
and FR-05's reply-drafting queue starts drafting suggested replies for
ITS posts too (account_comment_management.py never touches anything but
own accounts). Only mark an account own if TrendForce actually operates it.

This is the entire "accept a request" flow in one command: it registers
the handle, triggers a real one-off scrape (Facebook via
scrape_facebook.js + parse_facebook.py; X via scrape_accounts.js, which
writes csv/<handle>.csv directly; LinkedIn company pages via
scrape_accounts_linkedin.js - see normalize_linkedin_slug; LinkedIn
personal profiles via the separate scrape_profiles_linkedin.js - see
is_linkedin_profile_url/normalize_linkedin_profile_slug, added
2026-07-23 after two profile requests sat rejected since the original
LinkedIn support only covered company pages), then runs
`run_pipeline.sh core` to sync, rebuild every analysis file, regenerate
the dashboard, and publish it to GitHub Pages - so accepting a request
never needs a second command.

X's KNOWN_ACCOUNTS list in scraper.js still needs the handle added by
hand for it to stay covered by *future* scheduled runs (there's no CLI
hook for that file) - this script prints a reminder but can't do that
part for you. LinkedIn's own ACCOUNTS/PROFILES lists in
scrape_accounts_linkedin.js/scrape_profiles_linkedin.js ARE updated
automatically (small, simple-enough arrays to edit safely).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / 'accounts_config.json'
# Sibling repos, derived relative to this file rather than a hardcoded
# absolute path - works regardless of machine/username/OS as long as the
# 4 repos stay laid out as siblings under one parent directory (the
# convention documented in ONBOARDING.md), so this doesn't need editing
# on every handoff to a new machine.
FACEBOOK_SCRAPER_DIR = BASE.parent / 'TrendforceFacebookScraper'
TWITTER_SCRAPER_DIR = BASE.parent / 'TrendforceTwitterScraper'
LINKEDIN_SCRAPER_DIR = BASE.parent / 'TrendforceLinkedinScraper'
LINKEDIN_ACCOUNTS_JS = LINKEDIN_SCRAPER_DIR / 'scrape_accounts_linkedin.js'
LINKEDIN_PROFILES_JS = LINKEDIN_SCRAPER_DIR / 'scrape_profiles_linkedin.js'
# Mirrors cluster_topics.py's _DEFAULT_OWN - duplicated rather than imported
# so this script stays lightweight (no sklearn/pandas import chain) for a
# one-off local CLI action.
_DEFAULT_OWN = {'X': ['TrendForce'], 'Facebook': ['TrendForce.tw'], 'LinkedIn': ['TrendForce']}

# A brand-new account has no history yet, so its first scrape goes deeper
# than the daily top-up (X: 15 scrolls, Facebook light: 20, LinkedIn: 15)
# but well short of a full backfill (Facebook: 400, which took ~1.5h
# onboarding technewsinside; LinkedIn: 200, which pulled 800+ posts in
# testing) - deep enough to be useful, fast enough not to block on it.
ONBOARDING_SCROLLS = {'X': 75, 'Facebook': 250, 'LinkedIn': 100}


def _default_own(platform):
    return _DEFAULT_OWN[platform]


def normalize_handle(raw):
    """Accepts a bare handle or a pasted profile URL - the dashboard's
    request form normalizes client-side too, but a request can still reach
    here typed straight from a GitHub issue (one came through as
    "https://x.com/tphuang" before the form-side fix), so normalize here
    too rather than trusting the caller."""
    h = raw.strip()
    h = re.sub(r'^https?://(www\.)?(x\.com|twitter\.com|facebook\.com)/', '', h, flags=re.IGNORECASE)
    h = h.lstrip('@').rstrip('/')
    h = re.split(r'[/?#]', h)[0]
    return h


class UnsupportedLinkedInAccount(Exception):
    pass


def is_linkedin_profile_url(raw):
    """A /in/ URL is a personal profile, not a company page - handled by a
    completely different scraper (scrape_profiles_linkedin.js, see
    normalize_linkedin_profile_slug/add_linkedin_profile_to_scraper below).
    Checked before normalize_linkedin_slug so a profile request takes that
    separate path instead of being rejected."""
    return bool(re.search(r'linkedin\.com/in/', raw, re.IGNORECASE))


def normalize_linkedin_slug(raw):
    """LinkedIn COMPANY PAGES - scrape_accounts_linkedin.js's technique
    (positional URN matching against a company-feed GraphQL endpoint)
    doesn't apply to personal profiles, which is why that's a separate
    scraper/path (see is_linkedin_profile_url) rather than something this
    function should ever need to handle - if a /in/ URL somehow reaches
    here anyway (should be caught by the caller first), reject loudly
    rather than silently mis-parsing it into a bogus "slug".

    Accepts either a bare slug or a full /company/<slug>/ URL. The slug
    itself becomes both the accounts_config.json handle AND the CSV
    filename for newly added accounts (see main()) - no separate
    display-name mapping needed, unlike the legacy 'TrendForce' entry."""
    h = raw.strip()
    if is_linkedin_profile_url(h):
        raise UnsupportedLinkedInAccount(
            f"{raw!r} is a personal profile URL (/in/...), not a company page (/company/...) - "
            f"this should have been routed to the profile path instead, not normalize_linkedin_slug."
        )
    h = re.sub(r'^https?://(www\.)?linkedin\.com/company/', '', h, flags=re.IGNORECASE)
    h = h.rstrip('/')
    h = re.split(r'[/?#]', h)[0]
    return h


def normalize_linkedin_profile_slug(raw):
    """Accepts either a bare /in/ slug or a full profile URL. The slug
    becomes the accounts_config.json handle AND the CSV filename
    (csv/profiles/<slug>.csv, per scrape_profiles_linkedin.js) - mirrors
    normalize_linkedin_slug's company-page convention exactly."""
    h = raw.strip()
    h = re.sub(r'^https?://(www\.)?linkedin\.com/in/', '', h, flags=re.IGNORECASE)
    h = h.rstrip('/')
    return re.split(r'[/?#]', h)[0]


def derive_profile_display_name(slug):
    """scrape_profiles_linkedin.js's PROFILES array and its CLI filtering
    (`node scrape_profiles_linkedin.js "<name>"`) key off a human-readable
    NAME, not the slug - a request only ever supplies a URL/slug, so
    approximate a name from it. LinkedIn profile slugs often end in a
    generated alnum ID (e.g. "subhash-km-6b5443123") - strip a trailing
    segment that looks like one (alnum, 6+ chars, has a digit) rather than
    showing it as part of the "name". Good enough for an automated
    approval; rename by hand in the PROFILES array afterward if the
    result reads oddly."""
    parts = slug.split('-')
    if len(parts) > 1 and re.fullmatch(r'[0-9a-z]{6,}', parts[-1]) and any(c.isdigit() for c in parts[-1]):
        parts = parts[:-1]
    name = ' '.join(p.capitalize() for p in parts if p)
    return name or slug


def add_linkedin_account_to_scraper(handle, slug):
    """Appends a new {handle, slug} entry to scrape_accounts_linkedin.js's
    ACCOUNTS array - unlike X's KNOWN_ACCOUNTS (large, hand-curated, no
    safe automatic insertion point), this array is small and has one
    unambiguous insertion point (right before its closing bracket), so
    editing it programmatically is safe rather than needing a by-hand
    reminder."""
    if not LINKEDIN_ACCOUNTS_JS.exists():
        print(f"[WARN] {LINKEDIN_ACCOUNTS_JS} not found - add the account there by hand.")
        return False
    text = LINKEDIN_ACCOUNTS_JS.read_text(encoding='utf-8')
    if f"slug: '{slug}'" in text:
        print(f"{slug} is already in {LINKEDIN_ACCOUNTS_JS.name}'s ACCOUNTS list.")
        return True
    new_entry = f"  {{ handle: '{handle}', slug: '{slug}' }},\n"
    updated = re.sub(r'(const ACCOUNTS = \[\n)', r'\1' + new_entry, text, count=1)
    if updated == text:
        print(f"[WARN] Couldn't find ACCOUNTS array in {LINKEDIN_ACCOUNTS_JS} - add the account there by hand.")
        return False
    LINKEDIN_ACCOUNTS_JS.write_text(updated, encoding='utf-8')
    print(f"Added {{ handle: '{handle}', slug: '{slug}' }} to {LINKEDIN_ACCOUNTS_JS.name}'s ACCOUNTS list.")
    return True


def add_linkedin_profile_to_scraper(name, slug):
    """Same safe-array-edit approach as add_linkedin_account_to_scraper,
    targeting scrape_profiles_linkedin.js's PROFILES array instead (name/slug
    pairs, not handle/slug - that script's CLI filters by name, not slug)."""
    if not LINKEDIN_PROFILES_JS.exists():
        print(f"[WARN] {LINKEDIN_PROFILES_JS} not found - add the profile there by hand.")
        return False
    text = LINKEDIN_PROFILES_JS.read_text(encoding='utf-8')
    if f"slug: '{slug}'" in text:
        print(f"{slug} is already in {LINKEDIN_PROFILES_JS.name}'s PROFILES list.")
        return True
    new_entry = f"  {{ name: '{name}', slug: '{slug}' }},\n"
    updated = re.sub(r'(const PROFILES = \[\n)', r'\1' + new_entry, text, count=1)
    if updated == text:
        print(f"[WARN] Couldn't find PROFILES array in {LINKEDIN_PROFILES_JS} - add it there by hand.")
        return False
    LINKEDIN_PROFILES_JS.write_text(updated, encoding='utf-8')
    print(f"Added {{ name: '{name}', slug: '{slug}' }} to {LINKEDIN_PROFILES_JS.name}'s PROFILES list.")
    return True


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main():
    args = [a for a in sys.argv[1:] if a != '--own']
    as_own = '--own' in sys.argv
    if len(args) != 2 or args[0] not in ('X', 'Facebook', 'LinkedIn'):
        print(__doc__)
        sys.exit(1)
    platform, raw_handle = args
    is_linkedin_profile = platform == 'LinkedIn' and is_linkedin_profile_url(raw_handle)
    profile_name = None
    if is_linkedin_profile:
        handle = normalize_linkedin_profile_slug(raw_handle)
        profile_name = derive_profile_display_name(handle)
    elif platform == 'LinkedIn':
        try:
            handle = normalize_linkedin_slug(raw_handle)
        except UnsupportedLinkedInAccount as e:
            print(f"[REJECTED] {e}")
            sys.exit(1)
    else:
        handle = normalize_handle(raw_handle)

    cfg = load_config()
    cfg.setdefault(platform, {}).setdefault('own', list(_default_own(platform)))
    cfg.setdefault(platform, {}).setdefault('competitors', [])

    if as_own:
        if handle in cfg[platform]['competitors']:
            cfg[platform]['competitors'].remove(handle)
            print(f"Moved {handle} on {platform} from competitor to own.")
        if handle in cfg[platform]['own']:
            print(f"{handle} is already marked own on {platform}.")
        else:
            cfg[platform]['own'].append(handle)
            print(f"Marked {handle} own on {platform}.")
        save_config(cfg)
    elif handle in cfg[platform]['competitors']:
        print(f"{handle} is already tracked on {platform}.")
    else:
        cfg[platform]['competitors'].append(handle)
        save_config(cfg)
        print(f"Added {handle} to {platform} in {CONFIG_PATH.name}.")

    scraped = False
    if platform == 'Facebook':
        if not FACEBOOK_SCRAPER_DIR.exists():
            print(f"[WARN] {FACEBOOK_SCRAPER_DIR} not found - skipping scrape trigger.")
        else:
            page_url = f'https://www.facebook.com/{handle}'
            scrolls = ONBOARDING_SCROLLS['Facebook']
            print(f"Starting a one-off Facebook scrape for {page_url} ({scrolls} scrolls) ...")
            subprocess.run(['node', 'scrape_facebook.js', page_url, str(scrolls)], cwd=FACEBOOK_SCRAPER_DIR)
            # scrape_facebook.js only saves the scrolled-through raw HTML
            # (raw_facebook_<slug>.html) - parse_facebook.py is the step
            # that turns it into the dated CSV sync_data.sh looks for.
            print("Parsing the scraped HTML into a CSV ...")
            subprocess.run(['python3', 'parse_facebook.py', page_url], cwd=FACEBOOK_SCRAPER_DIR)
            scraped = True
    elif platform == 'LinkedIn' and is_linkedin_profile:
        if not LINKEDIN_SCRAPER_DIR.exists():
            print(f"[WARN] {LINKEDIN_SCRAPER_DIR} not found - skipping scrape trigger.")
        else:
            add_linkedin_profile_to_scraper(profile_name, handle)
            scrolls = ONBOARDING_SCROLLS['LinkedIn']
            print(f"Starting a one-off LinkedIn profile scrape for {profile_name} ({scrolls} scrolls) ...")
            result = subprocess.run(
                ['node', 'scrape_profiles_linkedin.js', profile_name, str(scrolls)], cwd=LINKEDIN_SCRAPER_DIR,
            )
            scraped = result.returncode == 0
            if not scraped:
                print(f"[WARN] LinkedIn profile scrape exited with code {result.returncode} - check for a manual-login prompt or a bad slug.")
    elif platform == 'LinkedIn':
        if not LINKEDIN_SCRAPER_DIR.exists():
            print(f"[WARN] {LINKEDIN_SCRAPER_DIR} not found - skipping scrape trigger.")
        else:
            add_linkedin_account_to_scraper(handle, handle)
            scrolls = ONBOARDING_SCROLLS['LinkedIn']
            print(f"Starting a one-off LinkedIn scrape for {handle} ({scrolls} scrolls) ...")
            result = subprocess.run(
                ['node', 'scrape_accounts_linkedin.js', handle, str(scrolls)], cwd=LINKEDIN_SCRAPER_DIR,
            )
            scraped = result.returncode == 0
            if not scraped:
                print(f"[WARN] LinkedIn scrape exited with code {result.returncode} - check for a manual-login prompt or a bad slug.")
    else:
        if not TWITTER_SCRAPER_DIR.exists():
            print(f"[WARN] {TWITTER_SCRAPER_DIR} not found - skipping scrape trigger.")
        else:
            scrolls = ONBOARDING_SCROLLS['X']
            print(f"Starting a one-off X scrape for @{handle} ({scrolls} scrolls) ...")
            env = {**os.environ, 'MAX_SCROLLS': str(scrolls)}
            subprocess.run(['node', 'scrape_accounts.js', f'@{handle}'], cwd=TWITTER_SCRAPER_DIR, env=env)
            scraped = True
        print(
            f"[REMINDER] For @{handle} to stay covered by *future* scheduled scrapes, also add "
            f"it to KNOWN_ACCOUNTS in {TWITTER_SCRAPER_DIR / 'scraper.js'} by hand - "
            f"there's no CLI hook for that file."
        )

    if not scraped:
        print("Skipping the pipeline run since no scrape ran - nothing new to pick up.")
        return

    # 'core' rebuilds Topic Gaps/Rising Trends/Sentiment; 'accounts' rebuilds
    # the Account Status tab (FR-05) separately, on its own 8h schedule -
    # a new account showed up in the former but not the latter the first
    # time this ran, since only 'core' was being chained here.
    ok = True
    for job in ('core', 'accounts'):
        print(f"Running the TrendForceDash '{job}' pipeline job ...")
        result = subprocess.run(['bash', 'run_pipeline.sh', job], cwd=BASE)
        if result.returncode != 0:
            print(f"[WARN] run_pipeline.sh {job} exited with code {result.returncode} - check pipeline.log.")
            ok = False
    if ok:
        print(f"Done. {handle} is live on the dashboard.")


if __name__ == '__main__':
    main()
