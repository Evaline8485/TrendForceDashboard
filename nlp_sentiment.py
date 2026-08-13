"""
FR-03 NLP Sentiment Dashboard.

Analyzes audience preferences via NLP (tokenization already done upstream by
the scrapers into `translated_text`/`keywords`) + sentiment analysis + a
fuzzy decision layer that fuses volume/engagement into heat & focus scores.

NER (named entity recognition), per FR-03's Processing row: each post's
entities (people/orgs/products/places) are extracted alongside sentiment
scoring. Chinese-majority text is tagged with jieba's POS tagger
(proper-noun tags nr/ns/nt/nz - jieba has no dedicated Chinese NER model,
so this is a lightweight proxy, not true NER); English/translated text uses
spaCy's en_core_web_sm NER model (PERSON/ORG/GPE/PRODUCT/NORP/FAC/LOC),
routed by the same CJK-vs-Latin character count used for sentiment routing
below. Surfaced as a new `named_entities` widget (top entities overall) and
as an `entities` field per topic in `temperature_bar`.

Sentiment is bilingual per NFR-07: VADER (same engine as
TrendforceTwitterScraper/sentiment.py) scores English/Latin-majority text
(X's translated_text); Traditional Chinese text (Facebook's native posts)
routes to cnsenti's dictionary instead, since VADER's English-only lexicon
silently scored 100% of Chinese text as neutral (compound 0.0) - not a
partial gap, every Facebook post was affected. cnsenti's dictionary is
simplified-Chinese, so Traditional input is converted via OpenCC first.
Routing is by each text's actual character composition (CJK vs Latin count),
not by platform, so it still works on self-service uploads or mixed text.

Time range is selectable (4h / 8h / 1d / 1w / 1q - see time_ranges.py, shared
with FR-01/FR-02 so all three line up); all widgets recompute over the
selected range. Reuses FR-01's topic clusters (cluster_topics.py) for
topic-shaped widgets.

Widgets (FR-03-01..09), plus named_entities (NER, not one of the numbered
09 - see the NER note above):
  01 sentiment_overview        - real-time snapshot of volume/sentiment/topics
  02 temperature_bar           - heat score per topic (hot -> cold), now also
                                  carries each topic's top named entities
  03 sentiment_trend_curve     - positive/neutral/negative counts over time
  04 competitor_mentions       - mention counts of a keyword across accounts
  05 platform_share_bar        - share-of-voice of a keyword across platforms
  06 platform_keyword_ranking  - per-platform ranking for a keyword
  07 coverage_focus_ranking    - each account's top-covered topic
  08 top_engagement_ranking    - highest-engagement topics
  09 posting_timeslot_analysis - Mon-Fri by time slot: volume/likes/engagement
  -- named_entities            - top mentioned entities window-wide (NER)

Platforms are derived from whatever FR-01's load_posts() returns (currently
X and Facebook; LinkedIn - one of the SRS's three named target platforms,
cover page / Section 7 - is not yet scraped. Not one of the SRS's 7
numbered Open Issues itself, just an unaddressed scope gap).

Time-range gap vs. spec: FR-03's own Time Range row asks for hourly /
4h / daily / monthly / quarterly. This shares FR-01/02's range set
instead (4h/8h/1d/1w/1q) for one consistent vocabulary across all three
dashboards - no hourly option, weekly substituted for monthly.

Output: analysis/sentiment_dashboard_<range>.json for each range, plus
analysis/sentiment_dashboard.json mirroring the 1d range (this script's
original default) for scripts that just want "the" dashboard.
"""
import json
import os
import re
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from cnsenti import Sentiment as ChineseSentiment
from opencc import OpenCC
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS
import jieba
import jieba.posseg as pseg

from cluster_topics import (N_CLUSTERS, load_posts, label_cluster, cluster_posts, OWN_HANDLES,
                             EN_NOISE_WORDS, LINK_NOISE, is_chinese_noise_token, is_common_english_word)
from time_ranges import RANGE_HOURS, RANGE_ORDER, MIN_WINDOW_POSTS, window_dict, TAIWAN_TZ

try:
    import spacy
    _NLP_EN = spacy.load('en_core_web_sm', disable=['parser', 'lemmatizer'])
except Exception:
    # Missing/unavailable model - degrade to Chinese-only entity extraction
    # rather than failing the whole pipeline over an optional enrichment.
    _NLP_EN = None

# Proper-noun POS tags jieba can assign: nr=person, ns=place, nt=organization,
# nz=other proper noun.
CHINESE_ENTITY_POS_TAGS = {'nr', 'ns', 'nt', 'nz'}
EN_ENTITY_LABELS = {'PERSON', 'ORG', 'GPE', 'PRODUCT', 'NORP', 'FAC', 'LOC'}

# jieba's default dictionary either splits known company names apart
# (e.g. "台積電" -> "台積" + "電") or tags them with a non-proper-noun POS
# (e.g. "輝達" comes back as an adjective) - either way extract_entities
# silently drops them, since it only keeps CHINESE_ENTITY_POS_TAGS. Loading
# this covers TrendForce's actual semiconductor/tech beat; see the file
# itself for the reasoning on the forced pos=nz.
jieba.load_userdict(os.path.join(os.path.dirname(__file__), 'jieba_userdict.txt'))

BASE = os.path.dirname(__file__)
OUT_FILE = os.path.join(BASE, 'analysis', 'sentiment_dashboard.json')
LEGACY_RANGE = '1d'  # analysis/sentiment_dashboard.json mirrors this range
KEYWORD_INDEX_FILE = os.path.join(BASE, 'analysis', 'keyword_index.json')


def range_out_file(range_key):
    return os.path.join(BASE, 'analysis', f'sentiment_dashboard_{range_key}.json')


TIME_RANGES = {key: timedelta(hours=hours) for key, hours in RANGE_HOURS.items()}
DEFAULT_RANGE = LEGACY_RANGE

TIME_SLOTS = [
    ('morning', 6, 12),
    ('afternoon', 12, 18),
    ('evening', 18, 24),
    ('late_night', 0, 6),
]

_analyzer = SentimentIntensityAnalyzer()
_zh_analyzer = ChineseSentiment()
_tw2sp = OpenCC('tw2sp')  # Traditional (Taiwan) -> Simplified, for cnsenti's dictionary
CJK_RE = re.compile(r'[一-鿿]')
LATIN_RE = re.compile(r'[A-Za-z]')


def extract_entities(text):
    """Named-entity-like terms for one post's text, routed by script: mostly
    Chinese text goes through jieba's POS tagger (proper-noun tags), mostly
    English/translated text through spaCy's NER model. Same CJK-vs-Latin
    routing signal as score_sentiment, reused here rather than duplicated."""
    text = text or ''
    if not text:
        return []
    if len(CJK_RE.findall(text)) > len(LATIN_RE.findall(text)):
        return [w for w, flag in pseg.cut(text) if flag in CHINESE_ENTITY_POS_TAGS and len(w) > 1]
    if _NLP_EN is not None:
        doc = _NLP_EN(text[:2000])  # cap input length - entity extraction, not full-doc NLP
        return [ent.text for ent in doc.ents if ent.label_ in EN_ENTITY_LABELS]
    return []


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None


def score_sentiment(text):
    """NFR-07 requires supporting both Traditional Chinese and English -
    VADER's lexicon is English-only and silently scores all-Chinese text as
    neutral (compound 0.0), which was happening for every Facebook post.
    Route by actual character composition (not platform - self-service
    uploads and mixed-language text have no reliable platform signal)."""
    text = text or ''
    if len(CJK_RE.findall(text)) > len(LATIN_RE.findall(text)):
        return _score_sentiment_zh(text)
    return _score_sentiment_en(text)


def _score_sentiment_en(text):
    compound = _analyzer.polarity_scores(text)['compound']
    if compound >= 0.05:
        label = 'positive'
    elif compound <= -0.05:
        label = 'negative'
    else:
        label = 'neutral'
    return label, compound


def _score_sentiment_zh(text):
    """cnsenti's bundled dictionary is simplified-Chinese only; our data is
    Traditional Chinese (Taiwan Facebook pages), so convert first or every
    lookup silently misses. Compound-style score: (pos - neg) / (pos + neg),
    same [-1, 1] range and thresholds as the English VADER path.

    cnsenti's bundled pos.pkl contains a literal whitespace character as a
    "positive word" - jieba tokenizes each run of spaces (common here since
    clean_text() replaces stripped digits/punctuation with spaces) into
    individual space tokens, and every one matched, making nearly all
    Chinese text score as strongly positive regardless of content. Collapse
    whitespace before scoring; Chinese segmentation doesn't need it anyway."""
    simplified = re.sub(r'\s+', '', _tw2sp.convert(text))
    counts = _zh_analyzer.sentiment_count(simplified)
    pos, neg = counts['pos'], counts['neg']
    compound = (pos - neg) / (pos + neg) if (pos + neg) else 0.0
    if compound >= 0.05:
        label = 'positive'
    elif compound <= -0.05:
        label = 'negative'
    else:
        label = 'neutral'
    return label, compound


def load_dashboard_posts():
    """Like cluster_topics.load_posts, but keeps keywords/timestamp/sentiment
    separately since the dashboard widgets need them raw (not merged into a
    single TF-IDF document)."""
    posts = load_posts()
    for p in posts:
        p['ts'] = parse_ts(p['timestamp'])
        p['sentiment'], p['sentiment_score'] = score_sentiment(p['text'])
        p['entities'] = extract_entities(p['text'])
    return posts


def in_range(post, now, span):
    return post['ts'] is not None and now - span <= post['ts'] <= now


# --- Fuzzy decision layer: fuse volume + engagement into heat/focus. -------
def tri(x, a, b, c):
    if x <= a or x >= c:
        return 0.0
    if x == b:
        return 1.0
    return (x - a) / (b - a) if x < b else (c - x) / (c - b)


def fuzzy_fuse(volume_norm, engagement_norm):
    """Simple weighted-centroid fusion of two normalized [0,1] inputs into a
    0-100 heat score: both signals must fuzzily agree to reach the extremes."""
    bands = {'low': (-0.01, 0.0, 0.5), 'medium': (0.0, 0.5, 1.0), 'high': (0.5, 1.0, 1.01)}
    rank = {'low': 0, 'medium': 1, 'high': 2}
    out_score = {'low': 10, 'medium': 50, 'high': 90}

    v_mem = {k: tri(volume_norm, *b) for k, b in bands.items()}
    e_mem = {k: tri(engagement_norm, *b) for k, b in bands.items()}

    weighted_sum, weight_total = 0.0, 0.0
    for vl, vv in v_mem.items():
        for el, ev in e_mem.items():
            strength = min(vv, ev)
            if strength <= 0:
                continue
            out_rank = round((rank[vl] + rank[el]) / 2)
            label = ['low', 'medium', 'high'][out_rank]
            weighted_sum += strength * out_score[label]
            weight_total += strength
    return round(weighted_sum / weight_total, 1) if weight_total else 0.0


def normalize(values):
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-9:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


# --- Widgets ----------------------------------------------------------------
EN_TOKEN_RE = re.compile(r"[a-zA-Z']+")
_TOP_TERMS_STOP_WORDS = ENGLISH_STOP_WORDS | LINK_NOISE | EN_NOISE_WORDS


def top_keyword_terms(posts, top_n=10):
    """Same noise filtering as cluster_topics.py's TF-IDF pipeline (sklearn's
    English stopwords + LINK_NOISE + EN_NOISE_WORDS + is_common_english_word's
    general-frequency sweep + the Chinese noise pattern), applied to plain
    word-frequency counting instead of TF-IDF - this widget just needs "what
    terms come up most," not a vectorizer. A prior version used raw
    text.split() with no filtering at all, which is how generic words like
    "according"/"pro"/"color"/"already"/"expected" were showing up here
    despite already being filtered out of FR-01's cluster labels."""
    counts = Counter()
    for p in posts:
        text = p.get('text', '')
        seen_in_post = set()
        for w in EN_TOKEN_RE.findall(text.lower()):
            if (len(w) > 2 and w not in _TOP_TERMS_STOP_WORDS and w not in seen_in_post
                    and not is_common_english_word(w)):
                seen_in_post.add(w)
                counts[w] += 1
        for run in re.findall(r'[一-鿿]{2,}', text):
            for length in (2, 3, 4):
                if length > len(run):
                    break
                for i in range(len(run) - length + 1):
                    term = run[i:i + length]
                    if term in seen_in_post or is_chinese_noise_token(term):
                        continue
                    seen_in_post.add(term)
                    counts[term] += 1
    return [t for t, _ in counts.most_common(top_n)]


def widget_sentiment_overview(posts):
    counts = Counter(p['sentiment'] for p in posts)
    total = len(posts)
    return {
        'total_posts': total,
        'sentiment_counts': dict(counts),
        'sentiment_share': {k: round(v / total, 3) for k, v in counts.items()} if total else {},
        'top_terms': top_keyword_terms(posts),
    }


def widget_temperature_bar(posts_by_topic, topic_labels):
    raw_volume = {cid: len(ps) for cid, ps in posts_by_topic.items()}
    raw_engagement = {cid: sum(p['interaction'] for p in ps) for cid, ps in posts_by_topic.items()}
    norm_v, norm_e = normalize(raw_volume), normalize(raw_engagement)
    bars = []
    for cid, ps in posts_by_topic.items():
        heat = fuzzy_fuse(norm_v.get(cid, 0), norm_e.get(cid, 0))
        entity_counts = Counter(e for p in ps for e in p.get('entities', []))
        bars.append({'topic_id': cid, 'label': topic_labels[cid], 'heat': heat,
                     'volume': raw_volume[cid], 'engagement': raw_engagement[cid],
                     'entities': [e for e, _ in entity_counts.most_common(5)]})
    bars.sort(key=lambda b: b['heat'], reverse=True)
    return bars


def widget_named_entities(posts, top_n=15):
    """FR-03 NER widget: most-mentioned entities across the window's posts,
    paired with keyword statistics per the Processing row's grouping."""
    counts = Counter(e for p in posts for e in p.get('entities', []))
    return [{'entity': e, 'count': c} for e, c in counts.most_common(top_n)]


MAX_TREND_TOP_TOPICS = 3
MAX_TREND_TOP_POSTS = 2
TREND_POST_TEXT_MAXLEN = 200
TREND_SMOOTH_RADIUS = 2  # 5-point moving average (i-2..i+2), matches the mockup's window
TREND_ANOMALY_Z = 2.0  # volume z-score above this flags a bucket as anomalous
TREND_ANOMALY_NEG_SHARE = 0.38  # ...or a high negative share at real volume, even without a volume spike
TREND_ANOMALY_MIN_VOLUME = 30  # "real volume" floor for the negative-share anomaly path (3x generate_dashboard.py's LOW_SAMPLE_THRESHOLD)


def bucket_bounds(now, span, buckets):
    """Chronological (oldest-first) list of (start, end) tuples - the one
    definition of "what counts as bucket j" shared by
    widget_sentiment_trend_curve and widget_topic_sentiment_heatmap so
    their x-axes line up exactly."""
    bucket_span = span / buckets
    return [(now - bucket_span * i, now - bucket_span * (i - 1)) for i in range(buckets, 0, -1)]


def widget_sentiment_trend_curve(posts, now, span, topic_labels, buckets=14):
    """Each bucket answers "what was actually happening" alongside the raw
    sentiment counts (2026-08-13) - net_sentiment (positive% - negative%,
    one number instead of three) is the line FR-03's UI plots; volume/
    engagement is what a paired bar chart plots underneath it (both
    split by sentiment AND by count vs. engagement, so the UI can toggle
    "by post count" vs. "by engagement weight" without a server round
    trip); top_topics (by post count within the bucket) and top_posts (by
    interaction) are what a hover/peak-label answers "why did this
    spike/dip" with - a temperature reading with no named driver is not
    actionable.

    net_sentiment_smoothed is a TREND_SMOOTH_RADIUS-point centered moving
    average (the raw per-bucket net_sentiment is noisy at low volume);
    is_anomaly flags a bucket whose volume z-scores > TREND_ANOMALY_Z
    against this window's OWN mean/stdev, or whose negative share is high
    at real volume. This differs from a literal "same hour of day across
    the last 7 days" baseline (which needs a fixed day-aligned bucket
    grid) - buckets here scale with whatever range the caller asked for,
    so a window-relative z-score is the baseline that generalizes across
    every range instead of only 1w-at-4h-buckets."""
    bounds = bucket_bounds(now, span, buckets)
    curve = []
    for b_start, b_end in bounds:
        bucket_posts = [p for p in posts if p['ts'] and b_start <= p['ts'] < b_end]
        counts = Counter(p['sentiment'] for p in bucket_posts)
        total = len(bucket_posts)
        pos, neg = counts.get('positive', 0), counts.get('negative', 0)
        net_sentiment = round((pos - neg) / total * 100, 1) if total else 0.0

        engagement_by_sentiment = defaultdict(int)
        for p in bucket_posts:
            engagement_by_sentiment[p['sentiment']] += p['interaction']

        topic_counts = Counter(p['cluster_id'] for p in bucket_posts)
        top_topics = [{'topic_id': cid, 'label': topic_labels.get(cid, f'cluster-{cid}'), 'count': c}
                      for cid, c in topic_counts.most_common(MAX_TREND_TOP_TOPICS)]

        top_posts_sorted = sorted(bucket_posts, key=lambda p: p['interaction'], reverse=True)
        seen_urls = set()
        top_posts = []
        for p in top_posts_sorted:
            key = p.get('url') or p.get('raw_text')
            if key in seen_urls:
                continue
            seen_urls.add(key)
            top_posts.append({
                'handle': p['handle'], 'platform': p['platform'],
                'text': (p.get('raw_text') or p['text'])[:TREND_POST_TEXT_MAXLEN],
                'url': p.get('url', ''), 'interaction': p['interaction'],
            })
            if len(top_posts) >= MAX_TREND_TOP_POSTS:
                break

        curve.append({
            'bucket_start': b_start.isoformat(),
            'bucket_end': b_end.isoformat(),
            'positive': pos,
            'neutral': counts.get('neutral', 0),
            'negative': neg,
            'positive_engagement': engagement_by_sentiment.get('positive', 0),
            'neutral_engagement': engagement_by_sentiment.get('neutral', 0),
            'negative_engagement': engagement_by_sentiment.get('negative', 0),
            'net_sentiment': net_sentiment,
            'post_count': total,
            'engagement': sum(p['interaction'] for p in bucket_posts),
            'top_topics': top_topics,
            'top_posts': top_posts,
        })

    # Smoothing and anomaly detection both need cross-bucket context, so
    # they're a second pass over the finished curve rather than computed
    # bucket-by-bucket above.
    n = len(curve)
    volumes = [b['post_count'] for b in curve]
    vol_mean = sum(volumes) / n if n else 0.0
    vol_sd = (sum((v - vol_mean) ** 2 for v in volumes) / n) ** 0.5 if n else 0.0
    for i, b in enumerate(curve):
        window = curve[max(0, i - TREND_SMOOTH_RADIUS):i + TREND_SMOOTH_RADIUS + 1]
        b['net_sentiment_smoothed'] = round(sum(w['net_sentiment'] for w in window) / len(window), 1)

        vol_z = (b['post_count'] - vol_mean) / vol_sd if vol_sd else 0.0
        neg_share = b['negative'] / b['post_count'] if b['post_count'] else 0.0
        b['is_anomaly'] = bool(
            vol_z > TREND_ANOMALY_Z
            or (neg_share > TREND_ANOMALY_NEG_SHARE and b['post_count'] > TREND_ANOMALY_MIN_VOLUME)
        )
    return curve


MAX_HEATMAP_TOPICS = 6


def widget_topic_sentiment_heatmap(posts, now, span, topic_labels, buckets=14, top_n=MAX_HEATMAP_TOPICS):
    """FR-03's main trend curve nets every topic's sentiment together, so
    "topic A turned negative while topic B turned positive" cancels out
    into a flat line - this widget splits that back apart: one row per
    top topic (by total volume across the window), one column per
    bucket, each cell's own net_sentiment/volume computed independently.
    Shares widget_sentiment_trend_curve's bucket_bounds() so both charts'
    x-axes line up exactly."""
    topic_totals = Counter(p['cluster_id'] for p in posts)
    top_topics = [cid for cid, _ in topic_totals.most_common(top_n)]
    top_set = set(top_topics)
    bounds = bucket_bounds(now, span, buckets)

    posts_by_topic = defaultdict(list)
    for p in posts:
        if p['cluster_id'] in top_set and p['ts']:
            posts_by_topic[p['cluster_id']].append(p)

    rows = []
    for cid in top_topics:
        topic_posts = posts_by_topic[cid]
        cells = []
        for b_start, b_end in bounds:
            bucket_posts = [p for p in topic_posts if b_start <= p['ts'] < b_end]
            total = len(bucket_posts)
            pos = sum(1 for p in bucket_posts if p['sentiment'] == 'positive')
            neg = sum(1 for p in bucket_posts if p['sentiment'] == 'negative')
            cells.append({
                'net_sentiment': round((pos - neg) / total * 100, 1) if total else 0.0,
                'volume': total,
            })
        rows.append({'topic_id': cid, 'label': topic_labels.get(cid, f'cluster-{cid}'), 'cells': cells})
    return rows


def widget_competitor_mentions(posts, keyword):
    """Returns (mentions_by_handle, mentions_by_platform) for posts matching keyword."""
    by_handle = Counter()
    by_platform = Counter()
    kw = keyword.lower()
    for p in posts:
        if kw in p['text'].lower():
            by_handle[p['handle']] += 1
            by_platform[p['platform']] += 1
    return dict(by_handle), dict(by_platform)


def widget_platform_share_bar(mentions_by_platform):
    total = sum(mentions_by_platform.values())
    return {plat: round(v / total, 3) for plat, v in mentions_by_platform.items()} if total else {}


def widget_platform_keyword_ranking(posts, keyword):
    """Per-platform ranking of handles for a keyword (FR-03-06)."""
    kw = keyword.lower()
    counts = defaultdict(Counter)
    for p in posts:
        if kw in p['text'].lower():
            counts[p['platform']][p['handle']] += 1
    return {
        platform: sorted(({'handle': h, 'mentions': c} for h, c in handle_counts.items()),
                          key=lambda r: r['mentions'], reverse=True)
        for platform, handle_counts in counts.items()
    }


def build_keyword_index(all_posts):
    """Lightweight per-post export (handle/platform/timestamp/text) so
    FR-03-04/05/06 (competitor mentions, platform share, platform ranking)
    can be searched live in the browser instead of needing a fixed keyword
    baked in at pipeline-run time - there's no backend to query on demand
    (this is a static site), so the dashboard does its own substring-match
    + aggregation client-side over this index. Range-independent (covers
    every post FR-01/02/03 see); the dashboard filters by timestamp itself
    to match whatever range is selected."""
    return [
        {'handle': p['handle'], 'platform': p['platform'], 'ts': p['timestamp'], 'text': p['text'],
         'url': p.get('url', ''), 'is_own': p['handle'] in OWN_HANDLES}
        for p in all_posts if p['timestamp']
    ]


def widget_coverage_focus_ranking(posts_by_topic, topic_labels):
    """Each account's dominant (most-posted) topic."""
    by_account_topic = defaultdict(Counter)
    for cid, ps in posts_by_topic.items():
        for p in ps:
            by_account_topic[p['handle']][cid] += 1

    ranking = []
    for handle, topic_counts in by_account_topic.items():
        top_cid, top_count = topic_counts.most_common(1)[0]
        total = sum(topic_counts.values())
        ranking.append({
            'handle': handle,
            'top_topic_id': top_cid,
            'top_topic_label': topic_labels[top_cid],
            'focus_share': round(top_count / total, 3),
            'post_count': total,
        })
    ranking.sort(key=lambda r: r['focus_share'], reverse=True)
    return ranking


def widget_top_engagement_ranking(posts_by_topic, topic_labels):
    """top_account/top_account_engagement (2026-08-13) let FR-06's daily
    summaries name a specific account to benchmark against, instead of a
    generic "延伸相關報導" with no concrete who/what."""
    ranking = []
    for cid, ps in posts_by_topic.items():
        total_engagement = sum(p['interaction'] for p in ps)
        by_account = defaultdict(int)
        for p in ps:
            by_account[p['handle']] += p['interaction']
        top_account, top_account_engagement = max(by_account.items(), key=lambda kv: kv[1])
        ranking.append({'topic_id': cid, 'label': topic_labels[cid],
                        'total_engagement': total_engagement, 'post_count': len(ps),
                        'top_account': top_account, 'top_account_engagement': top_account_engagement})
    ranking.sort(key=lambda r: r['total_engagement'], reverse=True)
    return ranking


def widget_posting_timeslot_analysis(posts):
    """Mon-Fri only, per SRS FR-03-09. NFR-01 requires everything in UTC+8 -
    bucketing by raw UTC weekday/hour would misfile a post like UTC Monday
    20:00 (Tuesday 04:00 in Taiwan) into the wrong day and time slot."""
    slots = {name: {'post_count': 0, 'likes': 0, 'engagement': 0} for name, _, _ in TIME_SLOTS}
    for p in posts:
        if not p['ts']:
            continue
        local_ts = p['ts'].astimezone(TAIWAN_TZ)
        if local_ts.weekday() >= 5:  # Sat=5, Sun=6
            continue
        hour = local_ts.hour
        for name, start, end in TIME_SLOTS:
            if start <= hour < end:
                slots[name]['post_count'] += 1
                slots[name]['likes'] += p['likes']
                slots[name]['engagement'] += p['interaction']
                break
    for s in slots.values():
        s['avg_engagement'] = round(s['engagement'] / s['post_count'], 1) if s['post_count'] else 0.0
    peak_slot = max(slots, key=lambda k: slots[k]['post_count']) if any(s['post_count'] for s in slots.values()) else None
    return {'slots': slots, 'peak_slot': peak_slot}


def build_dashboard(all_posts, time_range, now, keyword=None):
    """Builds the widget dict for one time range. Returns None if there
    aren't enough posts in the window to report a stable result."""
    span = TIME_RANGES[time_range]
    posts = [p for p in all_posts if in_range(p, now, span)]
    if len(posts) < MIN_WINDOW_POSTS:
        return None

    # Shared topic tree with FR-01/FR-02.
    vectorizer, X, km, labels = cluster_posts(posts, N_CLUSTERS)
    for p, label in zip(posts, labels):
        p['cluster_id'] = int(label)
    posts_by_topic = defaultdict(list)
    for p in posts:
        posts_by_topic[p['cluster_id']].append(p)
    # A sparse window (e.g. "last hour" with a dozen posts) can leave a
    # cluster with every TF-IDF term filtered out as noise - label_cluster()
    # then returns nothing, and the old fallback (f'cluster-{cid}') showed
    # a meaningless raw internal ID to the user ("what is cluster-0?").
    # Fall back to the cluster's own top named entities instead - the same
    # NER data widget_temperature_bar already surfaces as "Top entities",
    # so a real topic identity is available even when TF-IDF has nothing.
    topic_labels = {}
    for cid, ps in posts_by_topic.items():
        terms = label_cluster(vectorizer, km.cluster_centers_[cid])
        if terms:
            topic_labels[cid] = ' / '.join(terms)
        else:
            top_entities = [e for e, _ in Counter(e for p in ps for e in p.get('entities', [])).most_common(3)]
            topic_labels[cid] = ' / '.join(top_entities) if top_entities else f'Misc topic {cid}'

    result = {
        'generated_at': now.isoformat(),
        'time_range': time_range,
        'window': window_dict(now - span, now),
        'keyword': keyword,
        'widgets': {
            'sentiment_overview': widget_sentiment_overview(posts),
            'temperature_bar': widget_temperature_bar(posts_by_topic, topic_labels),
            'named_entities': widget_named_entities(posts),
            'sentiment_trend_curve': widget_sentiment_trend_curve(posts, now, span, topic_labels),
            'topic_sentiment_heatmap': widget_topic_sentiment_heatmap(posts, now, span, topic_labels),
            'coverage_focus_ranking': widget_coverage_focus_ranking(posts_by_topic, topic_labels),
            'top_engagement_ranking': widget_top_engagement_ranking(posts_by_topic, topic_labels),
            'posting_timeslot_analysis': widget_posting_timeslot_analysis(posts),
        },
    }

    if keyword:
        mentions_by_handle, mentions_by_platform = widget_competitor_mentions(posts, keyword)
        result['widgets']['competitor_mentions'] = mentions_by_handle
        result['widgets']['platform_share_bar'] = widget_platform_share_bar(mentions_by_platform)
        result['widgets']['platform_keyword_ranking'] = widget_platform_keyword_ranking(posts, keyword)
    else:
        result['widgets']['competitor_mentions'] = None
        result['widgets']['platform_share_bar'] = None
        result['widgets']['platform_keyword_ranking'] = None
        result['note'] = 'competitor_mentions/platform_share_bar/platform_keyword_ranking require a keyword argument'

    return result


def write_json(path, result):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def main(time_range=None, keyword=None, now=None):
    """time_range=None builds all of RANGE_ORDER; pass a specific range to
    build just that one (used by ad hoc CLI runs)."""
    all_posts = load_dashboard_posts()
    if not all_posts:
        print("No posts available, skipping.")
        return

    if now is None:
        timestamps = [p['ts'] for p in all_posts if p['ts']]
        now = max(timestamps) if timestamps else datetime.now(timezone.utc)

    write_json(KEYWORD_INDEX_FILE, build_keyword_index(all_posts))

    ranges = [time_range] if time_range else RANGE_ORDER
    written = 0
    for rng in ranges:
        if rng not in TIME_RANGES:
            raise ValueError(f"time_range must be one of {list(TIME_RANGES)}")
        result = build_dashboard(all_posts, rng, now, keyword)
        if result is None:
            print(f"Skipping {rng}: fewer than {MIN_WINDOW_POSTS} posts in window.")
            continue
        write_json(range_out_file(rng), result)
        if rng == LEGACY_RANGE:
            write_json(OUT_FILE, result)
        n_posts = result['widgets']['sentiment_overview']['total_posts']
        print(f"[{rng}] Wrote sentiment dashboard ({n_posts} posts) to {range_out_file(rng)}")
        written += 1

    if written == 0:
        print("No range had enough posts to build a sentiment dashboard.")


if __name__ == '__main__':
    import sys
    kw = sys.argv[2] if len(sys.argv) > 2 else None
    rng = sys.argv[1] if len(sys.argv) > 1 else None
    main(time_range=rng, keyword=kw)
