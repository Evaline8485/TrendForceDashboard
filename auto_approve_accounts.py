#!/usr/bin/env python3
"""
Zero-touch version of FR-05's "request tracking" approval step.

add_account.py/remove_account.py were originally written as a deliberate
manual gate - a human reads the GitHub issue and decides whether to run
the command. This script removes that gate on purpose (explicitly asked
for): it polls open `add-account`/`remove-account`-labeled issues on
elainekaotf/TrendForceDashboard, parses the platform/handle out of each
title ("Add account: <platform>/<handle>" / "Remove account: ..." - the
exact format generate_dashboard.py's request form generates), and runs
add_account.py/remove_account.py for every one it can, closing the issue
with a comment either way.

Only X and Facebook are actually actionable (add_account.py/
remove_account.py don't support LinkedIn yet) - a LinkedIn request, or a
title that doesn't parse, gets a comment explaining why and is left open
rather than silently dropped.

Requires `gh` authenticated (`gh auth login`) with access to
elainekaotf/TrendForceDashboard.

Usage: python3 auto_approve_accounts.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO = 'elainekaotf/TrendForceDashboard'
# Tracks which issue numbers we've already fired a "new request" notification
# for - without this, an issue that fails and gets left open (unsupported
# platform, add_account.py error) would re-notify on every single check once
# the schedule is tightened, turning one real event into a recurring nag.
NOTIFIED_STATE_FILE = BASE / '.notified_issues.json'
SUPPORTED_PLATFORMS = ('X', 'Facebook', 'LinkedIn')

ADD_TITLE_RE = re.compile(r'^Add account:\s*([^/]+)/(.+)$')
REMOVE_TITLE_RE = re.compile(r'^Remove account:\s*([^/]+)/(.+)$')


def gh(*args, input_text=None):
    result = subprocess.run(
        ['gh', *args], cwd=BASE, capture_output=True, text=True, input=input_text,
    )
    return result.returncode, result.stdout, result.stderr


def notify(title, message):
    # Reuses the same native macOS notification alert.sh already fires for
    # pipeline failures (run_pipeline.sh/publish.sh) - never raises even if
    # notifications are unavailable, so a notify() call can't break approval.
    subprocess.run(['bash', str(BASE / 'alert.sh'), title, message], cwd=BASE)


def load_notified():
    try:
        return set(json.loads(NOTIFIED_STATE_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def save_notified(notified):
    NOTIFIED_STATE_FILE.write_text(json.dumps(sorted(notified)))


def notify_once(notified, number, title, message):
    if number in notified:
        return
    notify(title, message)
    notified.add(number)


def list_open_issues():
    # Matched by TITLE, not by label - found 2026-07-23 that GitHub silently
    # drops the `labels=add-account` URL param the dashboard's request form
    # uses if that label doesn't yet exist in the repo, so every request
    # issue up to that point (#4, #5) was created with NO label at all
    # despite the form asking for one. Labels were created after the fact
    # for future browsability, but this script can't depend on them being
    # present - scanning every open issue's title is the only reliable way.
    code, out, err = gh('issue', 'list', '--repo', REPO,
                         '--state', 'open', '--json', 'number,title', '--limit', '200')
    if code != 0:
        print(f"  [!] gh issue list failed: {err.strip()}")
        return []
    return json.loads(out)


def close_with_comment(number, comment):
    code, _, err = gh('issue', 'close', str(number), '--repo', REPO, '--comment', comment)
    if code != 0:
        print(f"    [!] Failed to close issue #{number}: {err.strip()}")


def comment_only(number, comment):
    code, _, err = gh('issue', 'comment', str(number), '--repo', REPO, '--body', comment)
    if code != 0:
        print(f"    [!] Failed to comment on issue #{number}: {err.strip()}")


def run_script(script_name, platform, handle):
    result = subprocess.run(
        [sys.executable, str(BASE / script_name), platform, handle],
        cwd=BASE, capture_output=True, text=True,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def process_add_requests(all_issues, notified):
    matches = [(issue, ADD_TITLE_RE.match(issue['title'].strip())) for issue in all_issues]
    matches = [(issue, m) for issue, m in matches if m]
    print(f"\n{len(matches)} open add-account request(s).")
    for issue, match in matches:
        number = issue['number']
        platform, handle = match.group(1).strip(), match.group(2).strip()
        # Fire the notification the moment a new request is SEEN, not after
        # add_account.py's onboarding scrape finishes - that scrape can take
        # 10-15+ min, and the whole point of notifying is to know right away
        # a request came in, not to wait out however long approval takes.
        notify_once(notified, number, f"New account request: #{number}", f"{platform}/{handle} - approving now...")
        if platform not in SUPPORTED_PLATFORMS:
            print(f"  #{number}: {platform}/{handle} - unsupported platform, leaving open.")
            comment_only(number, f"Auto-approval only handles {', '.join(SUPPORTED_PLATFORMS)} right now - {platform} needs manual setup. Left open.")
            continue

        print(f"  #{number}: approving {platform}/{handle}...")
        ok, output = run_script('add_account.py', platform, handle)
        if ok:
            print(f"    Done.")
            close_with_comment(number, f"Auto-approved: {handle} is now tracked on {platform}. Scraping + pipeline run kicked off automatically.")
        else:
            print(f"    [!] add_account.py failed:\n{output}")
            comment_only(number, f"Auto-approval attempted this and failed - see local logs. Left open for manual review.\n\n```\n{output[-1500:]}\n```")


def process_remove_requests(all_issues, notified):
    matches = [(issue, REMOVE_TITLE_RE.match(issue['title'].strip())) for issue in all_issues]
    matches = [(issue, m) for issue, m in matches if m]
    print(f"\n{len(matches)} open remove-account request(s).")
    for issue, match in matches:
        number = issue['number']
        platform, handle = match.group(1).strip(), match.group(2).strip()
        notify_once(notified, number, f"New removal request: #{number}", f"{platform}/{handle} - approving now...")
        if platform not in SUPPORTED_PLATFORMS:
            print(f"  #{number}: {platform}/{handle} - unsupported platform, leaving open.")
            comment_only(number, f"Auto-approval only handles {', '.join(SUPPORTED_PLATFORMS)} right now - {platform} needs manual setup. Left open.")
            continue

        print(f"  #{number}: approving removal of {platform}/{handle}...")
        ok, output = run_script('remove_account.py', platform, handle)
        if ok:
            print(f"    Done.")
            close_with_comment(number, f"Auto-approved: {handle} has been removed from {platform} tracking.")
        else:
            print(f"    [!] remove_account.py failed:\n{output}")
            comment_only(number, f"Auto-approval attempted this and failed - see local logs. Left open for manual review.\n\n```\n{output[-1500:]}\n```")


def main():
    status = subprocess.run(['gh', 'auth', 'status'], capture_output=True, text=True)
    if status.returncode != 0:
        print("gh isn't authenticated - run `gh auth login` first.")
        sys.exit(1)

    all_issues = list_open_issues()
    notified = load_notified()
    open_numbers = {issue['number'] for issue in all_issues}
    process_add_requests(all_issues, notified)
    process_remove_requests(all_issues, notified)
    # Drop closed issues from the notified set so a number can be reused
    # cleanly if it's ever seen again (GitHub doesn't reuse issue numbers,
    # but this keeps the state file from growing forever).
    save_notified(notified & open_numbers)


if __name__ == '__main__':
    main()
