"""
FR-05 Account & Comment Management.

Compliance note (SRS Open Issue #2 - still TBC): platforms prohibit
inauthentic accounts and unauthorized automated interaction. This script
therefore only ever DRAFTS replies for our own official accounts
(TrendForce on X, TrendForce.tw on Facebook) and never sends anything -
a human must mark a draft 'approved' and, after posting it themselves
through the official account, mark it 'sent'. It never manages or
auto-replies through competitor or third-party accounts; those are
monitoring-only, matching FR-01/02/03's scope.

Processing:
  - Account status: platform, last-post recency, computed status
    (active/stale/inactive) for every account load_posts() tracks (own +
    competitors). Follower count isn't part of FR-05's spec (Processing
    row: "Account status management, comment aggregation, reply-draft
    suggestions, response tracking" - no follower metric named anywhere
    in the SRS), so it isn't collected or shown here.
  - Comment aggregation: for OUR OWN posts only, flag ones with a reply
    count at or above NEEDS_RESPONSE_THRESHOLD as needing a response. The
    count used is max(scraper's CSV reply metadata, actual scraped comment
    count) - the metadata can undercount/go stale, and once a real scrape
    exists it's ground truth and should never be overridden downward by
    that staler number (see build_comment_queue's docstring).
  - Reply-draft suggestions: informed by that post's FR-01 topic and FR-03
    sentiment. When scrape_own_comments.js has actually found comments for
    a post (own_comments.json is nonempty for that URL), select_top_comments()
    picks the top (most-liked) comments still within RECENT_WITHIN_DAYS and
    craft_comment_reply() addresses the top one by name, quoting what they
    said - a real response, not a generic one. A post with zero scraped
    comments (not scraped yet, or genuinely none) falls back to the
    original generic topic-based draft_reply. Queued for human approval in
    analysis/reply_queue.json.
  - Response tracking: status lifecycle drafted -> approved -> sent, or
    dismissed, persisted across runs like FR-04's review queue.

Output: analysis/account_status.json, analysis/reply_queue.json

CLI:
  python3 account_comment_management.py build
  python3 account_comment_management.py list [--status drafted]
  python3 account_comment_management.py show <id>
  python3 account_comment_management.py approve <id> --reviewer <name>
  python3 account_comment_management.py sent <id> --reviewer <name>
  python3 account_comment_management.py dismiss <id> --reviewer <name> [--notes "..."]
"""
import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from cluster_topics import PLATFORM_ACCOUNTS, OWN_HANDLES, N_CLUSTERS, parse_count, cluster_posts, label_cluster
from nlp_sentiment import load_dashboard_posts

BASE = os.path.dirname(__file__)
STATUS_FILE = os.path.join(BASE, 'analysis', 'account_status.json')
REPLY_QUEUE_FILE = os.path.join(BASE, 'analysis', 'reply_queue.json')
# Populated by each platform's own scrape_own_comments.js (a separate,
# deliberately narrow scrape of just our own flagged posts' actual reply
# text - see those scripts' docstrings, in TrendforceTwitterScraper and
# TrendforceFacebookScraper respectively), synced in by sync_data.sh.
# Both keyed by post URL: {url: [{author, text, ...}, ...]} - X comments
# carry a real timestamp/likes field; Facebook comments carry relativeTime
# + scrapedAt instead (no absolute-time signal was available there without
# a much slower hover-per-comment pass) - load_own_comments() approximates
# an absolute 'timestamp' for those from the two (see
# normalize_comment_timestamps below) so select_top_comments' recency
# filter applies to both platforms the same way. Optional either way - the
# reply queue works fine without either, just with reply_count instead of
# real comment content.
OWN_COMMENTS_FILES = [
    os.path.join(BASE, 'analysis', 'own_comments.json'),
    os.path.join(BASE, 'analysis', 'own_comments_facebook.json'),
]

# Facebook's relative-time strings ("5 days ago", "3 hrs", "Just now") -
# both the full and Facebook's own abbreviated units observed in practice.
# Deliberately approximate (30-day months, 365-day years); good enough for
# a 3-day recency filter, not for anything needing real precision.
RELATIVE_TIME_RE = re.compile(
    r'^(\d+)\s*(second|sec|s|minute|min|m|hour|hr|h|day|d|week|wk|w|month|mo|year|yr|y)s?\b', re.IGNORECASE)
RELATIVE_UNIT_SECONDS = {
    'second': 1, 'sec': 1, 's': 1,
    'minute': 60, 'min': 60, 'm': 60,
    'hour': 3600, 'hr': 3600, 'h': 3600,
    'day': 86400, 'd': 86400,
    'week': 604800, 'wk': 604800, 'w': 604800,
    'month': 2592000, 'mo': 2592000,
    'year': 31536000, 'yr': 31536000, 'y': 31536000,
}


def parse_relative_time(relative_str):
    """"5 days ago" / "3 hrs" / "Just now" -> a timedelta offset before
    scrapedAt. Returns None if unparseable (leaves the comment's absolute
    timestamp unset, same as before this existed)."""
    if not relative_str:
        return None
    s = relative_str.strip().lower()
    if 'just now' in s or s in ('now', 'moments ago'):
        return timedelta(0)
    m = RELATIVE_TIME_RE.match(s)
    if not m:
        return None
    seconds = RELATIVE_UNIT_SECONDS.get(m.group(2))
    if seconds is None:
        return None
    return timedelta(seconds=int(m.group(1)) * seconds)


def normalize_comment_timestamps(comments_by_url):
    """Fill in an approximate absolute 'timestamp' for comments that only
    have Facebook's relativeTime + scrapedAt (X comments already have a
    real 'timestamp' and are left untouched)."""
    for comments in comments_by_url.values():
        for c in comments:
            if c.get('timestamp'):
                continue
            scraped_at = parse_ts(c.get('scrapedAt'))
            offset = parse_relative_time(c.get('relativeTime'))
            if scraped_at is not None and offset is not None:
                c['timestamp'] = (scraped_at - offset).isoformat()
    return comments_by_url

NEEDS_RESPONSE_THRESHOLD = 5  # replies on a post before it's queued for a draft
STALE_AFTER_DAYS = 3
INACTIVE_AFTER_DAYS = 14
RECENT_WITHIN_DAYS = 3  # only draft replies for posts still recent enough that a reply is timely


def focus_phrase(topic_label, text_excerpt):
    """The topic cluster's label is 4 generic terms; pick whichever one
    actually appears in THIS post's text so the reply names what the post
    is actually about, rather than every draft reading identically."""
    terms = [t.strip() for t in (topic_label or '').split('/') if t.strip()]
    text_lower = (text_excerpt or '').lower()
    for term in sorted(terms, key=len, reverse=True):  # most specific first
        if term and term.lower() in text_lower:
            return term
    return terms[0] if terms else ''


TOP_COMMENTS_PER_POST = 3  # how many top comments a crafted reply can reference


def select_top_comments(comments, now, within_days=RECENT_WITHIN_DAYS, top_n=TOP_COMMENTS_PER_POST):
    """Comments worth crafting a response around: recent (within
    `within_days`) and ranked by likes. X comments carry a 'timestamp'
    (from scrape_own_comments.js's toTaiwanISOString) so recency is checked
    directly; Facebook comments only carry a relative-time string with no
    reliable absolute timestamp (see that scraper's own docstring), so a
    comment with no parseable timestamp is treated as still-eligible rather
    than silently excluded - excluding every Facebook comment by default
    would be wrong more often than including a stale one."""
    if not comments:
        return []
    eligible = []
    for c in comments:
        ts = parse_ts(c.get('timestamp')) if c.get('timestamp') else None
        if ts is not None and (now - ts).days >= within_days:
            continue
        eligible.append(c)
    eligible.sort(key=lambda c: parse_count(c.get('likes')) if c.get('likes') is not None else 0, reverse=True)
    return eligible[:top_n]


def craft_comment_reply(sentiment, topic_label, text_excerpt, top_comments):
    """Like draft_reply, but addresses the top (most-liked, recent) comment
    directly by name and references what they actually said - only called
    when select_top_comments() found at least one qualifying comment;
    falls back to the generic topic-based draft_reply otherwise."""
    if not top_comments:
        return draft_reply(sentiment, topic_label, text_excerpt)

    top = top_comments[0]
    topic = focus_phrase(topic_label, text_excerpt)
    about = f" about {topic}" if topic else ""
    commenter = (top.get('author') or '').strip()
    who = commenter if commenter else "everyone"
    comment_excerpt = (top.get('text') or '').strip()[:80]
    quoted = f' — you mentioned "{comment_excerpt}"' if comment_excerpt else ''

    if sentiment == 'positive':
        return (f"Thank you so much, {who}, for the kind words on our post{about}{quoted}! "
                 "We're so glad this resonated with you, and we'll keep the great content coming.")
    elif sentiment == 'negative':
        return (f"Thank you, {who}, for taking the time to share your thoughts on our post{about}{quoted}. "
                 "We really do appreciate the feedback, and we'd love to hear more so we can make things better.")
    else:
        return (f"Thanks so much, {who}, for reading our post{about} and sharing your thoughts{quoted}! "
                 "We'd love to hear what you'd like us to cover next.")


def draft_reply(sentiment, topic_label='', text_excerpt=''):
    # Per elainekao's feedback: calling out the reply count ("with 25
    # people weighing in...") read as impersonal/off, so volume never
    # factors into the tone - but the post's own topic still does, so
    # replies don't all read as one identical form letter.
    topic = focus_phrase(topic_label, text_excerpt)
    about = f" about {topic}" if topic else ""

    if sentiment == 'positive':
        return (f"Thank you so much for the kind words on our post{about}, it truly means a lot to us! "
                 "We're so glad this resonated with you, and we'll keep the great content coming.")
    elif sentiment == 'negative':
        return (f"Thank you for taking the time to share your thoughts on our post{about}. "
                 "We really do appreciate the feedback, and we'd love to hear more so we can make things better.")
    else:
        return (f"Thanks so much for reading our post{about} and taking the time to comment! "
                 "We'd love to hear what you'd like us to cover next.")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def record_id(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:16]


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None


def build_account_status(posts, now):
    by_handle = defaultdict(list)
    for p in posts:
        by_handle[p['handle']].append(p)

    accounts = []
    for platform, cfg in PLATFORM_ACCOUNTS.items():
        for handle in cfg['own'] + cfg['competitors']:
            handle_posts = by_handle.get(handle, [])
            timestamps = [parse_ts(p['timestamp']) for p in handle_posts]
            timestamps = [t for t in timestamps if t]
            last_post_at = max(timestamps) if timestamps else None
            days_since = (now - last_post_at).days if last_post_at else None

            if days_since is None:
                status = 'no_data'
            elif days_since <= STALE_AFTER_DAYS:
                status = 'active'
            elif days_since <= INACTIVE_AFTER_DAYS:
                status = 'stale'
            else:
                status = 'inactive'

            accounts.append({
                'handle': handle,
                'platform': platform,
                'is_own': handle in OWN_HANDLES,
                'post_count': len(handle_posts),
                'last_post_at': last_post_at.isoformat() if last_post_at else None,
                'days_since_last_post': days_since,
                'status': status,
            })
    return accounts


def build_comment_queue(posts, topic_labels_by_cluster, cluster_id_by_post, now, own_comments):
    """Flag our own high-reply *recent* posts and draft a suggested response
    for each. A reply is only worth drafting while it's still timely - a
    3-day-old post getting a reply today reads as stale/random to whoever
    sees it, so posts older than RECENT_WITHIN_DAYS are skipped even if
    they'd otherwise qualify on reply count.

    The draft itself is crafted around the post's top (most-liked, recent)
    actual comment when we have one - select_top_comments() only returns
    something when scrape_own_comments.js found at least one comment within
    the recency window, so a post with zero scraped comments (not scraped
    yet, or the scrape genuinely found none) falls back to the generic
    topic-based draft_reply exactly as before.

    reply_count sanity check: the scraper's own CSV-reported reply/comment
    count (p['replies']) is metadata scraped at a different time than
    scrape_own_comments.js's actual comment scrape, and can undercount
    (staleness, a metadata-scraping quirk). Once we've genuinely scraped a
    post's comments, that direct count is ground truth for "how many
    comments exist" and should never be overridden by a smaller, staler
    metadata number - so the effective count is whichever is higher. It's
    a max(), not a replace(): an empty/absent scrape (not scraped yet)
    must not zero out a real metadata count and wrongly exclude the post."""
    flagged = []
    for p, cid in zip(posts, cluster_id_by_post):
        if p['handle'] not in OWN_HANDLES:
            continue
        if not p.get('ts') or (now - p['ts']).days >= RECENT_WITHIN_DAYS:
            continue
        comments = own_comments.get(p.get('url', ''), [])
        replies = max(p.get('replies', 0), len(comments))
        if replies < NEEDS_RESPONSE_THRESHOLD:
            continue
        rid = record_id('reply', p['platform'], p['handle'], p['timestamp'], p['text'][:80])
        text_excerpt = p['text'][:200]
        topic_label = topic_labels_by_cluster[cid]
        sentiment_score = p.get('sentiment_score', 0.0)
        top_comments = select_top_comments(comments, now)
        flagged.append({
            'id': rid,
            'platform': p['platform'],
            'handle': p['handle'],
            'timestamp': p['timestamp'],
            'url': p.get('url', ''),
            'text_excerpt': text_excerpt,
            'reply_count': replies,
            'topic_label': topic_label,
            'sentiment': p['sentiment'],
            'sentiment_score': sentiment_score,
            'comments': comments,
            'draft_reply': craft_comment_reply(p['sentiment'], topic_label, text_excerpt, top_comments),
        })
    flagged.sort(key=lambda r: r['reply_count'], reverse=True)
    return flagged


def load_reply_queue():
    if not os.path.exists(REPLY_QUEUE_FILE):
        return {}
    with open(REPLY_QUEUE_FILE, encoding='utf-8') as f:
        return json.load(f)


def load_own_comments():
    merged = {}
    for path in OWN_COMMENTS_FILES:
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as f:
            merged.update(json.load(f))
    return normalize_comment_timestamps(merged)


def save_reply_queue(queue):
    os.makedirs(os.path.dirname(REPLY_QUEUE_FILE), exist_ok=True)
    with open(REPLY_QUEUE_FILE, 'w', encoding='utf-8') as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


def build():
    posts = load_dashboard_posts()
    if not posts:
        print("No posts available, skipping build.")
        return

    timestamps = [parse_ts(p['timestamp']) for p in posts]
    timestamps = [t for t in timestamps if t]
    now = max(timestamps) if timestamps else datetime.now(timezone.utc)

    accounts = build_account_status(posts, now)
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'generated_at': now.isoformat(), 'accounts': accounts}, f, ensure_ascii=False, indent=2)

    vectorizer, X, km, labels = cluster_posts(posts, N_CLUSTERS)
    posts_by_cid = defaultdict(list)
    for p, cid in zip(posts, labels):
        posts_by_cid[int(cid)].append(p)
    # See nlp_sentiment.py's build_dashboard() for why: an empty
    # label_cluster() result (every TF-IDF term filtered out as noise in a
    # sparse cluster) used to fall back to a meaningless raw cluster id.
    topic_labels = {}
    for cid in set(labels):
        cid = int(cid)
        terms = label_cluster(vectorizer, km.cluster_centers_[cid])
        if terms:
            topic_labels[cid] = ' / '.join(terms)
        else:
            top_entities = [e for e, _ in Counter(e for p in posts_by_cid[cid] for e in p.get('entities', [])).most_common(3)]
            topic_labels[cid] = ' / '.join(top_entities) if top_entities else f'Misc topic {cid}'
    own_comments = load_own_comments()
    flagged = build_comment_queue(posts, topic_labels, [int(l) for l in labels], now, own_comments)

    queue = load_reply_queue()
    added, refreshed = 0, 0
    for rec in flagged:
        rid = rec['id']
        if rid in queue:
            queue[rid]['reply_count'] = rec['reply_count']
            queue[rid]['url'] = queue[rid].get('url') or rec['url']
            queue[rid]['topic_label'] = rec['topic_label']  # re-clustering (e.g. a noise-filter fix) should refresh stale labels
            queue[rid]['sentiment_score'] = rec['sentiment_score']
            # Always refresh - a later scrape_own_comments.js run naturally
            # supersedes an earlier, thinner comment list for the same post.
            queue[rid]['comments'] = rec['comments']
            # Regenerate the draft while it's still just a suggestion nobody
            # has acted on yet (e.g. picking up a template wording change) -
            # but never touch it once a human has approved or sent it, since
            # that's the actual record of what was reviewed/posted.
            if queue[rid]['status'] == 'drafted':
                queue[rid]['draft_reply'] = rec['draft_reply']
            refreshed += 1
        else:
            rec['status'] = 'drafted'
            rec['reviewer'] = None
            rec['reviewed_at'] = None
            rec['notes'] = None
            queue[rid] = rec
            added += 1

    # A still-'drafted' entry whose post has aged out of build_comment_queue's
    # recency window (it's no longer in `flagged`) would otherwise sit
    # untouched forever - replying to it now would read as stale/random, so
    # auto-dismiss it. Approved/sent records are left alone either way, since
    # by then the reply already happened or was consciously queued to.
    flagged_ids = {rec['id'] for rec in flagged}
    aged_out = 0
    for rid, rec in queue.items():
        if rec['status'] == 'drafted' and rid not in flagged_ids:
            rec['status'] = 'dismissed'
            rec['reviewer'] = None
            rec['reviewed_at'] = now_iso()
            rec['notes'] = f'Auto-dismissed: post is older than the {RECENT_WITHIN_DAYS}-day reply window.'
            aged_out += 1
    save_reply_queue(queue)

    print(f"Wrote account status for {len(accounts)} accounts to {STATUS_FILE}")
    print(f"Reply queue: {added} new, {refreshed} refreshed, {aged_out} aged out, {len(queue)} total "
          f"(own-account posts needing a response draft).")


def list_records(status=None, limit=20):
    queue = load_reply_queue()
    records = list(queue.values())
    if status:
        records = [r for r in records if r['status'] == status]
    records.sort(key=lambda r: r['reply_count'], reverse=True)
    for r in records[:limit]:
        print(f"{r['id']}  [{r['status']:9}] {r['platform']:8} {r['handle']:14} replies={r['reply_count']:<5} {r['topic_label']}")
    print(f"({min(len(records), limit)} of {len(records)} shown)")


def show_record(rid):
    queue = load_reply_queue()
    rec = queue.get(rid)
    print(json.dumps(rec, ensure_ascii=False, indent=2) if rec else f"No record {rid}")


def transition(rid, status, reviewer, notes=None, allowed_from=None):
    queue = load_reply_queue()
    rec = queue.get(rid)
    if not rec:
        print(f"No record {rid}")
        return
    if allowed_from and rec['status'] not in allowed_from:
        print(f"Cannot move {rid} from '{rec['status']}' to '{status}' (expected one of {allowed_from}).")
        return
    rec['status'] = status
    rec['reviewer'] = reviewer
    rec['reviewed_at'] = now_iso()
    if notes is not None:
        rec['notes'] = notes
    save_reply_queue(queue)
    print(f"{rid}: {status}")


def main():
    parser = argparse.ArgumentParser(description='FR-05 Account & Comment Management')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('build')

    p_list = sub.add_parser('list')
    p_list.add_argument('--status', choices=['drafted', 'approved', 'sent', 'dismissed'])
    p_list.add_argument('--limit', type=int, default=20)

    p_show = sub.add_parser('show')
    p_show.add_argument('record_id')

    p_approve = sub.add_parser('approve')
    p_approve.add_argument('record_id')
    p_approve.add_argument('--reviewer', required=True)

    p_sent = sub.add_parser('sent')
    p_sent.add_argument('record_id')
    p_sent.add_argument('--reviewer', required=True)

    p_dismiss = sub.add_parser('dismiss')
    p_dismiss.add_argument('record_id')
    p_dismiss.add_argument('--reviewer', required=True)
    p_dismiss.add_argument('--notes')

    p_urls = sub.add_parser('urls-needing-comments',
        help='Prints X post URLs currently queued (drafted/approved) that scrape_own_comments.js should scrape.')
    p_urls.add_argument('--platform', default='X', choices=['X', 'Facebook'])

    args = parser.parse_args()
    if args.command == 'build':
        build()
    elif args.command == 'list':
        list_records(args.status, args.limit)
    elif args.command == 'show':
        show_record(args.record_id)
    elif args.command == 'approve':
        transition(args.record_id, 'approved', args.reviewer, allowed_from={'drafted'})
    elif args.command == 'sent':
        # Human has posted the reply themselves through the official account;
        # this script never calls any posting API.
        transition(args.record_id, 'sent', args.reviewer, allowed_from={'approved'})
    elif args.command == 'dismiss':
        transition(args.record_id, 'dismissed', args.reviewer, args.notes, allowed_from={'drafted', 'approved'})
    elif args.command == 'urls-needing-comments':
        queue = load_reply_queue()
        urls = [r['url'] for r in queue.values()
                if r['platform'] == args.platform and r['status'] in ('drafted', 'approved') and r.get('url')]
        for url in urls:
            print(url)


if __name__ == '__main__':
    main()
