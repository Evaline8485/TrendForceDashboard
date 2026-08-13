"""
FR-04 Manual Review & Detail View.

Human review of automated outputs from FR-01 (topic clustering), FR-02
(rising-KOL calls), FR-03 (sentiment), and FR-06 (executive summaries) to
catch misclassifications before they reach stakeholders or self-service
export (FR-07).

Three record types are queued for review:
  - 'post'      one per scraped post: its assigned topic (FR-01) and
                sentiment label (FR-03), correctable to a different topic
                or sentiment.
  - 'kol_rising' one per (topic, handle) the last fuzzy_trend.py run flagged
                as a rising KOL (FR-02), correctable to confirm/reject the call.
  - 'summary'   one per generate_summaries.py daily executive summary
                (FR-06), correctable to a rewritten summary text.

Review state persists in analysis/review_queue.json, keyed by a content
hash so re-running `build` after a data refresh adds newly-seen records
without clobbering existing review status/corrections. Every correction is
also appended to analysis/correction_log.jsonl (append-only, one line per
correction) - the "corrections fed back for model improvement" trail the
SRS calls for.

CLI:
  python3 manual_review.py build                          # refresh the queue
  python3 manual_review.py list [--status pending] [--type post] [--limit 20]
  python3 manual_review.py show <record_id>
  python3 manual_review.py approve <record_id> --reviewer <name>
  python3 manual_review.py correct <record_id> --reviewer <name>
        [--topic-label "..."] [--sentiment positive|neutral|negative]
        [--is-rising true|false] [--notes "..."]
"""
import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

from cluster_topics import N_CLUSTERS, cluster_posts, label_cluster
from nlp_sentiment import load_dashboard_posts

BASE = os.path.dirname(__file__)
QUEUE_FILE = os.path.join(BASE, 'analysis', 'review_queue.json')
CORRECTION_LOG_FILE = os.path.join(BASE, 'analysis', 'correction_log.jsonl')
FUZZY_TRENDS_FILE = os.path.join(BASE, 'analysis', 'fuzzy_trends.json')
DAILY_SUMMARIES_FILE = os.path.join(BASE, 'analysis', 'daily_summaries.json')


def record_id(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:16]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return {}
    with open(QUEUE_FILE, encoding='utf-8') as f:
        return json.load(f)


def save_queue(queue):
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


def append_correction_log(entry):
    os.makedirs(os.path.dirname(CORRECTION_LOG_FILE), exist_ok=True)
    with open(CORRECTION_LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def build_post_records(posts, topic_labels_by_post_idx, topic_labels):
    records = {}
    for p, cid in zip(posts, topic_labels_by_post_idx):
        rid = record_id('post', p['platform'], p['handle'], p['timestamp'], p['text'][:80])
        records[rid] = {
            'id': rid,
            'type': 'post',
            'platform': p['platform'],
            'handle': p['handle'],
            'timestamp': p['timestamp'],
            'text_excerpt': p['text'][:200],
            'automated': {
                'topic_id': int(cid),
                'topic_label': topic_labels[cid],
                'sentiment': p['sentiment'],
                'sentiment_score': round(p['sentiment_score'], 4),
            },
        }
    return records


def build_kol_rising_records():
    """One record per (topic, handle) FR-02 flagged as a rising KOL, so a
    reviewer can confirm or reject the automated call."""
    if not os.path.exists(FUZZY_TRENDS_FILE):
        return {}
    with open(FUZZY_TRENDS_FILE, encoding='utf-8') as f:
        trends = json.load(f)

    records = {}
    for platform, data in trends.get('platforms', {}).items():
        for topic in data.get('top_rising_topics', []):
            for kol in topic.get('rising_kols', []):
                rid = record_id('kol_rising', platform, topic['topic_id'], kol['handle'], trends['generated_at'])
                records[rid] = {
                    'id': rid,
                    'type': 'kol_rising',
                    'platform': platform,
                    'topic_id': topic['topic_id'],
                    'topic_label': topic['label'],
                    'handle': kol['handle'],
                    'generated_at': trends['generated_at'],
                    'automated': {
                        'rising_score': kol['rising_score'],
                        'rationale': kol['rationale'],
                    },
                }
    return records


def build_summary_records():
    """One record per FR-06 daily executive summary, so a reviewer can spot
    check length/tone/accuracy before it goes out."""
    if not os.path.exists(DAILY_SUMMARIES_FILE):
        return {}
    with open(DAILY_SUMMARIES_FILE, encoding='utf-8') as f:
        report = json.load(f)

    records = {}
    for s in report.get('summaries', []):
        rid = record_id('summary', report['date'], s['id'])
        records[rid] = {
            'id': rid,
            'type': 'summary',
            'platform': s['ref'].get('platform', 'N/A'),
            'handle': '',
            'generated_at': report['generated_at'],
            'automated': {
                'category': s['category'],
                'text': s['text'],
                'char_count': s['char_count'],
            },
        }
    return records


def build():
    posts = load_dashboard_posts()
    if not posts:
        print("No posts available, skipping build.")
        return

    vectorizer, X, km, labels = cluster_posts(posts, N_CLUSTERS)
    posts_by_topic = defaultdict(list)
    for p, cid in zip(posts, labels):
        posts_by_topic[int(cid)].append(p)
    # See nlp_sentiment.py's build_dashboard() for why: an empty
    # label_cluster() result (every TF-IDF term filtered out as noise in a
    # sparse cluster) used to fall back to a meaningless raw cluster id.
    topic_labels = {}
    for cid, ps in posts_by_topic.items():
        terms = label_cluster(vectorizer, km.cluster_centers_[cid])
        if terms:
            topic_labels[cid] = ' / '.join(terms)
        else:
            top_entities = [e for e, _ in Counter(e for p in ps for e in p.get('entities', [])).most_common(3)]
            topic_labels[cid] = ' / '.join(top_entities) if top_entities else f'Misc topic {cid}'

    new_records = {}
    new_records.update(build_post_records(posts, labels, topic_labels))
    new_records.update(build_kol_rising_records())
    new_records.update(build_summary_records())

    queue = load_queue()
    added, refreshed = 0, 0
    for rid, rec in new_records.items():
        if rid in queue:
            # Keep reviewer status/corrections; only refresh the automated fields
            # in case a re-run produced a slightly different label/score.
            queue[rid]['automated'] = rec['automated']
            refreshed += 1
        else:
            rec['status'] = 'pending'
            rec['reviewer'] = None
            rec['reviewed_at'] = None
            rec['correction'] = None
            rec['notes'] = None
            queue[rid] = rec
            added += 1

    save_queue(queue)
    print(f"Review queue: {added} new, {refreshed} refreshed, {len(queue)} total.")


def list_records(status=None, type_=None, limit=20):
    queue = load_queue()
    records = list(queue.values())
    if status:
        records = [r for r in records if r['status'] == status]
    if type_:
        records = [r for r in records if r['type'] == type_]
    records.sort(key=lambda r: r.get('timestamp') or r.get('generated_at') or '', reverse=True)
    for r in records[:limit]:
        label = (r['automated'].get('topic_label') or r['automated'].get('rationale')
                 or r['automated'].get('text', ''))
        print(f"{r['id']}  [{r['status']:9}] {r['type']:11} {r['platform']:8} {r['handle']:20} {label}")
    print(f"({min(len(records), limit)} of {len(records)} shown)")


def show_record(rid):
    queue = load_queue()
    rec = queue.get(rid)
    if not rec:
        print(f"No record {rid}")
        return
    print(json.dumps(rec, ensure_ascii=False, indent=2))


def approve_record(rid, reviewer):
    queue = load_queue()
    rec = queue.get(rid)
    if not rec:
        print(f"No record {rid}")
        return
    rec['status'] = 'approved'
    rec['reviewer'] = reviewer
    rec['reviewed_at'] = now_iso()
    save_queue(queue)
    print(f"Approved {rid}")


def correct_record(rid, reviewer, topic_label=None, sentiment=None, is_rising=None, summary_text=None, notes=None):
    queue = load_queue()
    rec = queue.get(rid)
    if not rec:
        print(f"No record {rid}")
        return

    correction = {}
    if topic_label is not None:
        correction['topic_label'] = topic_label
    if sentiment is not None:
        correction['sentiment'] = sentiment
    if is_rising is not None:
        correction['is_rising'] = is_rising
    if summary_text is not None:
        if not (80 <= len(summary_text) <= 120):
            print(f"Warning: corrected summary is {len(summary_text)} chars, outside the 80-120 spec.")
        correction['text'] = summary_text
    if not correction:
        print("No correction fields given (use --topic-label / --sentiment / --is-rising / --summary-text).")
        return

    rec['status'] = 'corrected'
    rec['reviewer'] = reviewer
    rec['reviewed_at'] = now_iso()
    rec['correction'] = correction
    rec['notes'] = notes
    save_queue(queue)

    append_correction_log({
        'record_id': rid,
        'type': rec['type'],
        'platform': rec['platform'],
        'handle': rec['handle'],
        'automated': rec['automated'],
        'correction': correction,
        'reviewer': reviewer,
        'notes': notes,
        'corrected_at': rec['reviewed_at'],
    })
    print(f"Corrected {rid}: {correction}")


def main():
    parser = argparse.ArgumentParser(description='FR-04 Manual Review & Detail View')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('build')

    p_list = sub.add_parser('list')
    p_list.add_argument('--status', choices=['pending', 'approved', 'corrected'])
    p_list.add_argument('--type', dest='type_', choices=['post', 'kol_rising', 'summary'])
    p_list.add_argument('--limit', type=int, default=20)

    p_show = sub.add_parser('show')
    p_show.add_argument('record_id')

    p_approve = sub.add_parser('approve')
    p_approve.add_argument('record_id')
    p_approve.add_argument('--reviewer', required=True)

    p_correct = sub.add_parser('correct')
    p_correct.add_argument('record_id')
    p_correct.add_argument('--reviewer', required=True)
    p_correct.add_argument('--topic-label')
    p_correct.add_argument('--sentiment', choices=['positive', 'neutral', 'negative'])
    p_correct.add_argument('--is-rising', choices=['true', 'false'])
    p_correct.add_argument('--summary-text')
    p_correct.add_argument('--notes')

    args = parser.parse_args()
    if args.command == 'build':
        build()
    elif args.command == 'list':
        list_records(args.status, args.type_, args.limit)
    elif args.command == 'show':
        show_record(args.record_id)
    elif args.command == 'approve':
        approve_record(args.record_id, args.reviewer)
    elif args.command == 'correct':
        is_rising = {'true': True, 'false': False, None: None}[args.is_rising]
        correct_record(args.record_id, args.reviewer, args.topic_label, args.sentiment, is_rising,
                        args.summary_text, args.notes)


if __name__ == '__main__':
    main()
