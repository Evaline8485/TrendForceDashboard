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
| `TrendforceFacebookScraper` | **none — local-only** | Facebook scraping |
| `TrendforceLinkedinScraper` | **none — local-only** | LinkedIn scraping |

The two local-only repos currently exist **only on this Mac**. If they're
part of the handoff, they need a remote created and pushed before (or as
part of) the transfer — otherwise that code and its git history don't go
anywhere.

**To transfer GitHub-hosted repos**: GitHub → repo → Settings →
Collaborators (to add access) or Settings → Danger Zone → Transfer
ownership (to fully hand over the account). Transferring ownership
changes the GitHub Pages URL (`elainekaotf.github.io/...` → whatever the
new account is), which breaks any existing bookmark/link to the live
dashboard.

## 3. The hardcoded-path problem (the biggest gotcha)

Every repo assumes it lives at exactly `/Users/elainekao/<RepoName>` and
reaches across to its siblings by **absolute path**, not a relative one or
an env var. This only works if the new machine has the identical
directory layout under the identical username. Confirmed hardcoded paths:

- `TrendForceDash`: `add_account.py`, `remove_account.py`, `sync_data.sh`, `video_ranking.py`
- `TrendforceTwitterScraper`: `scrape_video_discovery.js`, `enrich_video_locations.js`, `run_daily.sh`, `scrape_accounts.js`
- `TrendforceFacebookScraper`: `run_all.sh`
- All 10 `launchd` plists (see below) — every one of them

**Two ways to handle this:**
- **Easiest**: on the new machine, create a user named `elainekao` and put
  the repos at the exact same paths. Nothing needs editing.
- **Correct but more work**: `grep -rl "/Users/elainekao" .` in each repo
  and replace with the new machine's actual username/path, including
  inside all 10 `.plist` files.

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
Current jobs:

```
com.elainekao.trendforce-daily.plist            (TrendforceTwitterScraper, 6x/day)
com.elainekao.trendforce-facebook.plist         (light scrape, 5x/day)
com.elainekao.trendforce-facebook-full.plist    (full backfill, 1x/day)
com.elainekao.trendforce-linkedin.plist         (own-account scrape)
com.elainekao.trendforce-linkedin-profiles.plist (tracked profiles scrape)
com.elainekao.trendforcedash-scan.plist         (FR-02 scan tier, 6x/day)
com.elainekao.trendforcedash-core.plist         (FR-01/02/03 full, 4x/day)
com.elainekao.trendforcedash-accounts.plist     (FR-05, 3x/day)
com.elainekao.trendforcedash-daily.plist        (FR-06, 1x/day)
com.elainekao.trendforcedash-autoapprove.plist  (account-request polling, every 5 min)
```

To move these to a new machine: copy `~/Library/LaunchAgents/com.elainekao.*.plist`,
fix the hardcoded paths inside them (see §3), then for each:
```bash
launchctl load ~/Library/LaunchAgents/<name>.plist
```

**One more requirement**: `TrendforceTwitterScraper/run_daily.sh` calls
`sudo pmset schedule wake ...` to make sure the Mac wakes up for the next
scheduled run even if it's asleep. This needs **passwordless sudo** for
the user running it — on this Mac that's satisfied by the account having
general sudo access (`sudo -l` shows `(ALL) ALL`), not a narrowly-scoped
rule. The new machine needs the equivalent, or that step just logs a
warning and continues (doesn't block the rest of the run). Note: a
comment in `run_daily.sh` references "docs/history" for sudoers setup
notes — that file doesn't actually exist in the repo; this section
replaces it as the real source of truth.

## 6. Runtime state

`csv/`, `analysis/`, and `docs/index.html` are all committed to git, so
a normal `git clone` brings the new owner the full data history. Nothing
special needed here beyond normal repo access — just worth knowing this
is a **data-heavy** repo (large CSV history), not a small codebase.

## 7. Suggested transfer order

1. Push `TrendforceFacebookScraper` / `TrendforceLinkedinScraper` to new
   GitHub remotes (or hand over as plain file copies if git history for
   those two doesn't matter).
2. Grant the new owner access to (or transfer) the two GitHub-hosted repos.
3. New owner sets up the identical directory layout (§3) on their machine.
4. Hand over credentials/sessions out-of-band (§4), or let them re-login fresh.
5. Copy + fix + load the launchd plists (§5) on the new machine.
6. Confirm `gh auth status` and `sudo -l` are both set up under the new
   owner's account.
7. Do a manual test run of one job from each repo
   (`bash run_pipeline.sh core`, `bash run_daily.sh`, etc.) before trusting
   the schedule.
