# Transferring TrendForceDash to a New Owner

This project is 4 repos + macOS scheduled jobs working together, not just
one codebase. A clean handoff means transferring all four pieces below,
not just `git clone`-ing the code.

## 1. What this is

`TrendForceDash` is the hub: it reads scraped CSV data from three sibling
scraper repos, runs analysis (topic clustering, sentiment, rising trends),
and publishes a static dashboard to GitHub Pages. The scrapers run on
their own schedules and each pushes fresh data back into this repo.

## 2. The four repos

| Repo | Remote | Role |
|---|---|---|
| `TrendForceDash` | `github.com/elainekaotf/TrendForceDashboard` | Analysis + dashboard generation + publish |
| `TrendforceTwitterScraper` | `github.com/elainekaotf/TrendforceTwitterScraper` | X/Twitter scraping |
| `TrendforceFacebookScraper` | `github.com/elainekaotf/TrendforceFacebookScraper` (private) | Facebook scraping |
| `TrendforceLinkedinScraper` | `github.com/elainekaotf/TrendforceLinkedinScraper` (private) | LinkedIn scraping |

All four repos now have a GitHub remote (as of 2026-08-07) - the two
scraper repos that used to be local-only were pushed as new private
repos, verified clean of any tracked credentials/session files first.

**To transfer GitHub-hosted repos**: GitHub → repo → Settings →
Collaborators (to add access) or Settings → Danger Zone → Transfer
ownership (to fully hand over the account). Transferring ownership
changes the GitHub Pages URL (`elainekaotf.github.io/...` → whatever the
new account is), which breaks any existing bookmark/link to the live
dashboard.

## 3. The hardcoded-path problem — fixed in code, still open in the plists

Every repo used to assume it lived at exactly `/Users/elainekao/<RepoName>`
and reached across to its siblings by **absolute path**. As of 2026-08-11
this is fixed for every script - each cross-repo reference now derives the
sibling repo's path relative to its own file's location instead (e.g.
`path.join(__dirname, '..', 'TrendForceDash', ...)` in JS,
`BASE.parent / 'TrendforceTwitterScraper'` in Python,
`"$(dirname "$0")/../TrendForceDash"` in bash). This works on **any
machine, username, or OS** as long as the 4 repos stay laid out as
siblings under one parent directory - nothing below needs editing by hand
for this reason anymore:

- `TrendForceDash`: `add_account.py`, `remove_account.py`, `sync_data.sh`, `video_ranking.py`
- `TrendforceTwitterScraper`: `scrape_video_discovery.js`, `enrich_video_locations.js`, `run_daily.sh`, `scrape_accounts.js`
- `TrendforceFacebookScraper`: `run_all.sh`

**What's still hardcoded**: the 10 `launchd` `.plist` files themselves
(see §5) - `.plist` XML has no relative-path or `$HOME`-expansion concept
for `ProgramArguments`/`StandardOutPath`, so each one still needs its
`/Users/elainekao/...` strings replaced with the new machine's real
username/path before loading. That's a one-time edit per plist, not a
recurring maintenance burden the way the script-level paths used to be.

## 4. Credentials & session state — hand these over out-of-band, never via git

These are what let the scrapers act as your logged-in accounts. None of
them belong in a git commit. **Verify this before transfer** — found
2026-08-04 that `session_google.json` had been tracked in
`TrendforceTwitterScraper`'s public repo since its initial commit despite
every sibling session file being gitignored (now fixed: untracked and
added to `.gitignore` as of commit `a40b4d2`, but the old committed copy
still exists in git history and that Google session should be rotated —
log out or change the account password — independent of the code fix).
Don't assume `.gitignore` coverage is complete; check with
`git ls-files | grep -E "session|\.env"` in each repo before handoff:

| File | Repo | What it is |
|---|---|---|
| `.env` | `TrendforceTwitterScraper` | API keys/tokens |
| `session.json` | `TrendforceTwitterScraper` | X/Twitter login session |
| `session_google.json` | `TrendforceTwitterScraper` | Google login session |
| `session_facebook.json` | `TrendforceFacebookScraper` | Facebook login session |
| `session_linkedin.json` | `TrendforceLinkedinScraper` | LinkedIn login session |
| `session_substack.json` | `TrendforceLinkedinScraper` | Substack login session |

Hand these over via an encrypted channel (password manager, `age`/`gpg`
encrypted file, etc.) — or simpler, have the new owner just log in fresh
under their own accounts and let each scraper regenerate its own session
file on first run (scripts already prompt for manual login when a session
file is missing/expired).

Also check for a `gh` CLI auth session (`gh auth status`) — several
scripts (`auto_approve_accounts.py`, `publish.sh`) shell out to `gh` and
need it authenticated against a GitHub account with push access to
`TrendForceDashboard`.

## 5. Scheduled automation (launchd)

Everything runs via macOS `launchd`, **not cron**, on this specific Mac.
Current jobs (schedules as of 2026-08-11 - several were cut back from
their original frequency after crawling too fast got the X account
suspended, see §5a below):

```
com.elainekao.trendforce-daily.plist             (TrendforceTwitterScraper, 4x/day: 0:30/8:30/12:30/16:30)
com.elainekao.trendforce-facebook.plist          (light scrape, 3x/day: 0:45/8:45/16:45)
com.elainekao.trendforce-facebook-full.plist     (full backfill, 1x/day: 4:45)
com.elainekao.trendforce-linkedin.plist          (own-account scrape, 2x/day: 9:15/21:15)
com.elainekao.trendforce-linkedin-profiles.plist (tracked profiles scrape, 2x/day: 10:15/22:15)
com.elainekao.trendforcedash-scan.plist          (FR-02 scan tier, 3x/day: 0/8/16h)
com.elainekao.trendforcedash-core.plist          (FR-01/02/03 full, 2x/day: 0/12h)
com.elainekao.trendforcedash-accounts.plist      (FR-05, 3x/day: 0/8/16h)
com.elainekao.trendforcedash-daily.plist         (FR-06, 1x/day: 7h)
com.elainekao.trendforcedash-autoapprove.plist   (account-request polling, every 5 min)
```

To move these to a new **Mac**: copy `~/Library/LaunchAgents/com.elainekao.*.plist`,
fix the hardcoded paths inside them (see §3 - this is the one place that
still needs manual editing), then for each:
```bash
launchctl load ~/Library/LaunchAgents/<name>.plist
```

### 5a. Volume reductions (why the schedule looks lighter than it used to)

The X account got suspended once for crawling too fast. Response was
two-pronged, not just "run less often":
- Cut scroll depth per run in `scrape_accounts.js`/`scrape_watchlist.js`/
  `scrape_competitors.js` (15→8→5) and `scrape_video_discovery.js` (10→6→4).
- `enrich_video_locations.js` (backs the Video Ranking region filter) went
  from sweeping the whole ~2900-account pool (up to 100 profile visits/run,
  **no pause between them**) down to a `--top30` mode that only looks up
  the current top 30 shown on the dashboard, with a 3s pause between
  lookups. This is what's actually wired into `run_daily.sh` now.
- A brand-new scraping account is generally treated with *more* suspicion
  by anti-bot systems than an aged one - if setting up fresh accounts for
  a new owner, use them normally for a few days before pointing a scraper
  at them, and don't restore the old higher scroll counts without a good
  reason to.

### 5b. If the new machine isn't a Mac

`launchd` is macOS-only. If the new owner has a Windows machine (e.g. a
Lenovo ThinkBook) instead:

- **Use Git Bash**, not WSL - the manual-login flow needs a real visible
  browser window, which Git Bash gives you natively on Windows. WSL2's GUI
  support (WSLg) is Windows-11-only and less reliable for this.
- **`launchd` → Windows Task Scheduler.** Each of the 10 jobs above becomes
  a Task Scheduler task pointed at `bash.exe` with the same trigger times.
  One thing gets *simpler*: Task Scheduler has a built-in "Wake the
  computer to run this task" checkbox, so §5c's `pmset`/sudo workaround
  becomes unnecessary entirely on Windows.
- **Path format**: §3's fix makes cross-repo references portable across
  machines/usernames, but Windows paths (`C:\Users\...` or Git Bash's
  `/c/Users/...`) are a different *format* than macOS's `/Users/...` -
  nothing in the current fix assumes a specific OS's path syntax, but this
  hasn't been tested on an actual Windows machine yet.
- Node/Python/Playwright all install and run fine on Windows - no code
  changes needed there beyond the above.

### 5c. Wake-scheduling requirement (Mac only)

`TrendforceTwitterScraper/run_daily.sh` calls `sudo pmset schedule wake ...`
to make sure the Mac wakes up for the next scheduled run even if it's
asleep. This needs **passwordless sudo** for the user running it — on
this Mac that's satisfied by the account having general sudo access
(`sudo -l` shows `(ALL) ALL`), not a narrowly-scoped rule. A new **Mac**
needs the equivalent, or that step just logs a warning and continues
(doesn't block the rest of the run). Note: a comment in `run_daily.sh`
references "docs/history" for sudoers setup notes — that file doesn't
actually exist in the repo; this section replaces it as the real source
of truth. **Not needed at all on Windows** (§5b) - Task Scheduler's own
wake checkbox replaces this entirely.

## 6. Runtime state

`csv/`, `analysis/`, and `docs/index.html` are all committed to git, so
a normal `git clone` brings the new owner the full data history. Nothing
special needed here beyond normal repo access — just worth knowing this
is a **data-heavy** repo (large CSV history), not a small codebase.

## 7. Suggested transfer order

1. ~~Push `TrendforceFacebookScraper` / `TrendforceLinkedinScraper` to new
   GitHub remotes~~ — done 2026-08-07, all four repos now have a remote.
2. ~~Make cross-repo script paths portable~~ — done 2026-08-11 (§3); the 4
   repos just need to stay laid out as siblings, any username/machine/OS.
3. Grant the new owner access to (or transfer) all four repos (§2) - need
   their GitHub username first.
4. Decide Mac vs. Windows (§5b) for the new machine, if not already known.
5. New owner sets up the directory layout (§3) on their machine - matching
   username no longer required, just the sibling-repo layout.
6. New owner creates their own scraping accounts (X/Facebook/LinkedIn,
   Google if using citation tracking) rather than inheriting sessions -
   simpler and avoids transferring a possibly-flagged account (§5a).
   Fresh accounts should see some normal use for a few days before a
   scraper touches them.
7. Set up `.env` (X credentials only - see §4) and let each scraper's
   manual-login flow create fresh session files on first run.
8. Copy + fix + load the launchd plists (§5) on a new Mac, or set up
   Task Scheduler tasks (§5b) on Windows.
9. Confirm `gh auth status` is set up under the new owner's account
   (`gh auth login`); confirm `sudo -l` too, but only if on a Mac (§5c) -
   not needed on Windows.
10. Do a manual test run of one job from each repo
    (`bash run_pipeline.sh core`, `bash run_daily.sh`, etc.) before
    trusting the schedule.
