"""
FR-02 Fuzzy Trend Prediction.

Predicts rising topics and rising KOLs from the same account/post pool as
FR-01 (cluster_topics.py), reusing its clustering step so the topic
hierarchy matches the FR-01 tree per the SRS.

Method: for each entity (topic, sub-topic, or account-within-topic), derive
three inputs over a recent vs. prior time window -
  - volume growth rate      (recent post count vs prior post count)
  - engagement acceleration (recent engagement-rate vs prior engagement-rate)
  - spread breadth          (share of the platform's tracked accounts active
                             in the recent window)
fuzzify each into low/medium/high triangular membership, run a Mamdani-style
rule base, and defuzzify (weighted centroid) into a 0-100 rising score.

Runs the FR-02-01..04 expansions in order:
  01 horizontal: top-10 rising topics per platform
  02 vertical:   rising KOLs within each (platform x topic)
  03 vertical:   sub-topic drill-down of the top-10 topics
  04 horizontal: rising KOLs within each (platform x sub-topic)

Platforms are derived from whatever FR-01's load_posts() returns (currently
X and Facebook; LinkedIn - one of the SRS's three named target platforms,
cover page / Section 7 - is not yet scraped. Not one of the SRS's 7
numbered Open Issues itself, just an unaddressed scope gap).

The "recent vs. prior" window is one of time_ranges.RANGE_ORDER (4h/8h/1d/
1w/1q) - recent covers the window itself, prior covers the equal-length
period immediately before it. Output: analysis/fuzzy_trends_<range>.json for
each range, plus analysis/fuzzy_trends.json mirroring the 1w range (this
script's original fixed window) for scripts that just want "the" rising list.

Frequency (SRS Open Issue #1 - "detect every 4 hours" vs "run every 6
hours"): implements the SRS's own proposed resolution, a two-tier schedule.
  - main(mode='scan'): FR-02-01 only (top rising topics per platform, no
    KOL/sub-topic drill-down) for the 4h range, run every 4h - writes
    straight into fuzzy_trends_4h.json with rising_kols/sub_topics left
    empty and 'scan_only': true, so a scan-only refresh doesn't clobber a
    prior full run's depth with a missing field (consumers just see less
    until the next full run fills it back in).
  - main(mode='full') (default): the complete FR-02-01..04 chain across all
    5 ranges, run every 6h - this is what every range file other than a
    just-scanned 4h one always contains.
"""
import json
import os
from collections import defaultdict
from datetime import timedelta, timezone

from cluster_topics import (load_posts, label_cluster, cluster_posts,
                             load_topic_taxonomy, build_topic_matchers, classify_post_topics)
from time_ranges import RANGE_HOURS, RANGE_ORDER, MIN_WINDOW_POSTS, parse_ts, format_window, window_dict

BASE = os.path.dirname(__file__)
OUT_FILE = os.path.join(BASE, 'analysis', 'fuzzy_trends.json')
LEGACY_RANGE = '1w'  # analysis/fuzzy_trends.json mirrors this range


def range_out_file(range_key):
    return os.path.join(BASE, 'analysis', f'fuzzy_trends_{range_key}.json')


TOP_N_TOPICS = 10
SUB_CLUSTERS_PER_TOPIC = 4
MIN_SUBCLUSTER_DOCS = 5


def split_windows(items, now, hours):
    """items: list of dicts with a parsed 'ts' datetime. Returns (recent, prior)."""
    recent_start = now - timedelta(hours=hours)
    prior_start = recent_start - timedelta(hours=hours)
    recent = [p for p in items if p['ts'] and p['ts'] >= recent_start]
    prior = [p for p in items if p['ts'] and prior_start <= p['ts'] < recent_start]
    return recent, prior


# --- Fuzzy inference -------------------------------------------------------
# Triangular membership over inputs pre-normalized to roughly [0, 1].
def tri(x, a, b, c):
    if x <= a or x >= c:
        return 0.0
    if x == b:
        return 1.0
    if x < b:
        return (x - a) / (b - a)
    return (c - x) / (c - b)


def fuzzify(x):
    x = max(0.0, min(1.0, x))
    return {
        'low': tri(x, -0.01, 0.0, 0.5),
        'medium': tri(x, 0.0, 0.5, 1.0),
        'high': tri(x, 0.5, 1.0, 1.01),
    }


# Rule base over (growth, acceleration, spread) -> output rank (0=low..2=high).
# Weighted mean of growth+acceleration (demand signal) with spread as a
# corroborating/tempering factor, expressed as fuzzy rule strengths.
OUTPUT_SCORE = {'low': 10, 'medium': 50, 'high': 90}


def infer_rising_score(growth_norm, accel_norm, spread_norm):
    g, a, s = fuzzify(growth_norm), fuzzify(accel_norm), fuzzify(spread_norm)

    rules = []
    for gl, gv in g.items():
        for al, av in a.items():
            for sl, sv in s.items():
                strength = min(gv, av, sv)
                if strength <= 0:
                    continue
                # growth and acceleration drive the verdict; spread nudges it
                # up or down by at most one band.
                rank = {'low': 0, 'medium': 1, 'high': 2}
                core = round((rank[gl] + rank[al]) / 2)
                nudge = (rank[sl] - 1)  # -1, 0, +1
                out_rank = max(0, min(2, core + (1 if nudge > 0 and core < 2 else (-1 if nudge < 0 and core > 0 else 0))))
                out_label = ['low', 'medium', 'high'][out_rank]
                rules.append((strength, OUTPUT_SCORE[out_label]))

    if not rules:
        return 0.0, []
    weighted_sum = sum(w * v for w, v in rules)
    weight_total = sum(w for w, _ in rules)
    score = weighted_sum / weight_total if weight_total else 0.0
    return score, rules


def normalize(values):
    """Min-max normalize a list of raw metric values to [0, 1]."""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-9:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def compute_entity_metrics(entities_posts, now, hours):
    """entities_posts: {entity_key: [post,...]}. Returns raw growth/accel/spread per entity."""
    raw_growth, raw_accel, raw_spread = {}, {}, {}
    for key, posts in entities_posts.items():
        recent, prior = split_windows(posts, now, hours)
        recent_vol, prior_vol = len(recent), len(prior)
        recent_eng = sum(p['interaction'] for p in recent)
        prior_eng = sum(p['interaction'] for p in prior)
        recent_rate = recent_eng / recent_vol if recent_vol else 0.0
        prior_rate = prior_eng / prior_vol if prior_vol else 0.0

        raw_growth[key] = (recent_vol - prior_vol) / max(prior_vol, 1)
        raw_accel[key] = (recent_rate - prior_rate) / max(prior_rate, 1)
        raw_spread[key] = len({p['handle'] for p in recent})
    return raw_growth, raw_accel, raw_spread


def rank_entities(entities_posts, now, hours, rationale_fn):
    raw_growth, raw_accel, raw_spread = compute_entity_metrics(entities_posts, now, hours)
    norm_growth, norm_accel, norm_spread = normalize(raw_growth), normalize(raw_accel), normalize(raw_spread)

    scored = []
    for key in entities_posts:
        score, _ = infer_rising_score(norm_growth.get(key, 0), norm_accel.get(key, 0), norm_spread.get(key, 0))
        scored.append({
            'key': key,
            'rising_score': round(score, 1),
            'volume_growth_rate': round(raw_growth[key], 3),
            'engagement_acceleration': round(raw_accel[key], 3),
            'active_accounts': raw_spread[key],
            'rationale': rationale_fn(raw_growth[key], raw_accel[key], raw_spread[key]),
        })
    scored.sort(key=lambda r: r['rising_score'], reverse=True)
    return scored


def topic_rationale(growth, accel, spread, window_label):
    parts = []
    parts.append(f"volume {'+' if growth >= 0 else ''}{growth:.0%} vs prior {window_label}")
    parts.append(f"engagement rate {'+' if accel >= 0 else ''}{accel:.0%}")
    parts.append(f"{spread} account(s) posting")
    return ', '.join(parts)


def kol_rationale(growth, accel, spread, window_label):
    return f"posts {'+' if growth >= 0 else ''}{growth:.0%}, engagement {'+' if accel >= 0 else ''}{accel:.0%} vs prior {window_label}"


def compute_platform_trends(platform_posts, topic_names, now, hours, full=True):
    """Run the FR-02-01..04 expansion chain for one platform's posts, which
    already carry 'topic_codes' - the FR-01 taxonomy's multi-label match
    per post (see main()) - so a post counts toward every topic it hit,
    same as FR-01's own gap counting.

    full=False stops after FR-02-01 (the "scan" tier - see module docstring):
    just the top rising topics, no KOL ranking or sub-topic drill-down."""
    window_label = format_window(hours)
    topic_rat = lambda g, a, s: topic_rationale(g, a, s, window_label)
    kol_rat = lambda g, a, s: kol_rationale(g, a, s, window_label)

    posts_by_topic = defaultdict(list)
    for p in platform_posts:
        for code in p['topic_codes']:
            posts_by_topic[code].append(p)

    # FR-02-01: top-10 rising topics for this platform.
    topic_ranking = rank_entities(posts_by_topic, now, hours, topic_rat)
    for t in topic_ranking:
        t['topic_id'] = t.pop('key')
        t['label'] = topic_names[t['topic_id']]
    top_topics = topic_ranking[:TOP_N_TOPICS]

    if not full:
        for topic in top_topics:
            topic['rising_kols'] = []
            topic['sub_topics'] = []
        return top_topics

    result_topics = []
    for topic in top_topics:
        cid = topic['topic_id']
        topic_posts = posts_by_topic[cid]

        # FR-02-02: rising KOLs within this topic.
        posts_by_account = defaultdict(list)
        for p in topic_posts:
            posts_by_account[p['handle']].append(p)
        kol_ranking = rank_entities(posts_by_account, now, hours, kol_rat)
        for kol in kol_ranking:
            kol['handle'] = kol.pop('key')

        # FR-02-03: sub-topic drill-down (one level deeper in the tree).
        sub_topics_out = []
        if len(topic_posts) >= SUB_CLUSTERS_PER_TOPIC * MIN_SUBCLUSTER_DOCS:
            sub_vectorizer, sub_X, sub_km, sub_labels = cluster_posts(
                topic_posts, n_clusters=SUB_CLUSTERS_PER_TOPIC, min_docs_per_cluster=MIN_SUBCLUSTER_DOCS)
            for p, label in zip(topic_posts, sub_labels):
                p['sub_cluster_id'] = int(label)

            posts_by_subtopic = defaultdict(list)
            for p in topic_posts:
                posts_by_subtopic[p['sub_cluster_id']].append(p)

            sub_ranking = rank_entities(posts_by_subtopic, now, hours, topic_rat)
            for sub in sub_ranking:
                sid = sub.pop('key')
                sub['sub_topic_id'] = sid
                sub['label'] = ' / '.join(label_cluster(sub_vectorizer, sub_km.cluster_centers_[sid])) or f'subcluster-{sid}'

                # FR-02-04: rising KOLs within this sub-topic.
                sub_posts_by_account = defaultdict(list)
                for p in posts_by_subtopic[sid]:
                    sub_posts_by_account[p['handle']].append(p)
                sub_kol_ranking = rank_entities(sub_posts_by_account, now, hours, kol_rat)
                for kol in sub_kol_ranking:
                    kol['handle'] = kol.pop('key')
                sub['rising_kols'] = sub_kol_ranking
                sub_topics_out.append(sub)

        topic['rising_kols'] = kol_ranking
        topic['sub_topics'] = sub_topics_out
        result_topics.append(topic)

    return result_topics


def write_json(path, result):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def main(now=None, mode='full'):
    """mode='full' (default, every 6h): FR-02-01..04 across all 5 ranges.
    mode='scan' (every 4h): FR-02-01 only, just the 4h range - see module
    docstring for why this two-tier split exists."""
    if mode not in ('full', 'scan'):
        raise ValueError("mode must be 'full' or 'scan'")

    posts = load_posts()
    for p in posts:
        p['ts'] = parse_ts(p['timestamp'])

    if not posts:
        print("No posts found, skipping fuzzy trend prediction.")
        return

    if now is None:
        timestamps = [p['ts'] for p in posts if p['ts']]
        now = max(timestamps) if timestamps else datetime.now(timezone.utc)

    # Shared FR-01 taxonomy across all platforms - multi-label, same as
    # cluster_topics.py's own top-level topics (see that file's taxonomy
    # section for why this replaced K-Means here too, 2026-08-13). Each
    # post's topic_codes can be empty, one, or several; posts_by_topic in
    # compute_platform_trends fans a post out to every code it matched.
    taxonomy = load_topic_taxonomy()
    matchers = build_topic_matchers(taxonomy)
    topic_names = {t['code']: t['name'] for t in taxonomy}
    for p in posts:
        p['topic_codes'] = classify_post_topics(p.get('raw_text') or p['text'], matchers)

    platforms = sorted({p['platform'] for p in posts})
    ranges = ['4h'] if mode == 'scan' else RANGE_ORDER
    written = 0
    for range_key in ranges:
        hours = RANGE_HOURS[range_key]
        platforms_out = {}
        for platform in platforms:
            platform_posts = [p for p in posts if p['platform'] == platform
                               and p['ts'] and p['ts'] >= now - timedelta(hours=hours * 2)]
            if len(platform_posts) < MIN_WINDOW_POSTS:
                continue
            result_topics = compute_platform_trends(platform_posts, topic_names, now, hours, full=(mode == 'full'))
            if result_topics:
                platforms_out[platform] = {'top_rising_topics': result_topics}

        if not platforms_out:
            print(f"Skipping {range_key}: no platform had enough posts in the recent+prior window.")
            continue

        result = {
            'generated_at': now.isoformat(),
            'range': range_key,
            'window_hours': hours,
            'window': window_dict(now - timedelta(hours=hours), now),
            'scan_only': mode == 'scan',
            'platforms': platforms_out,
        }
        write_json(range_out_file(range_key), result)
        if range_key == LEGACY_RANGE:
            write_json(OUT_FILE, result)

        total_topics = sum(len(v['top_rising_topics']) for v in platforms_out.values())
        print(f"[{range_key}{'/scan' if mode == 'scan' else ''}] Wrote {total_topics} rising topics across "
              f"{len(platforms_out)} platform(s) to {range_out_file(range_key)}")
        written += 1

    if written == 0:
        print("No range had enough posts to run fuzzy trend prediction.")


if __name__ == '__main__':
    import sys
    main(mode=sys.argv[1] if len(sys.argv) > 1 else 'full')
