"""
FR-01 Competitor Topic Clustering.

Clusters posts from our own accounts and competitor accounts, across
platforms, into topics using TF-IDF + K-Means over each post's normalized
text (X posts are pre-translated into `translated_text` by the existing
scraper; Facebook posts are native zh-TW and pass through as-is - the
Chinese-character range in NON_WORD_RE keeps them usable for clustering
alongside the translated English X text).

Platforms covered (PLATFORM_ACCOUNTS below):
  - X:        TrendForce (own) vs. dylan522p, SemiAnalysis_, jukan05,
              QQ_Timmy, technews_tw
  - Facebook: TrendForce.tw (own) vs. ctee.fans, yutinghaosfinance

Output: analysis/topic_clusters_<range>.json for each of time_ranges.RANGE_ORDER
(4h/8h/1d/1w/1q), plus analysis/topic_clusters.json for the broadest (1q)
range, kept for scripts that just want "the" topic tree without picking a
window.
  - clusters: [{id, label, top_terms, size, accounts: {handle: count}}]
  - gaps: topics with strong competitor presence but weak/no own-account coverage
  - suggested_entry_points: gap topics ranked by competitor engagement

NER lives in FR-03 (nlp_sentiment.py), not here - the SRS lists NER under
FR-03's Processing row, alongside tokenization/sentiment/keyword-stats, not
under FR-01's Method row (which specifies cross-lingual embeddings +
clustering, see the Method note above). An earlier version of this file
added entity extraction here instead; moved to match the spec.

The K-Means tree itself is fit ONCE on all available posts (clustering needs
enough documents to be stable - a 4h window rarely has enough). Each range
just filters which posts count toward cluster sizes/gaps using that same
shared tree, so "topic labels" are identical across ranges - only the
volume/engagement numbers (and which gaps clear the threshold) change.
Windows with fewer than time_ranges.MIN_WINDOW_POSTS matching posts are
skipped rather than reported as a misleadingly precise zero.

Scope note: the SRS names Facebook, LinkedIn, and X as the three target
platforms (cover page, Section 7 Data Requirements), but no LinkedIn
crawler exists - X and Facebook only, here and everywhere else in this
pipeline, until one is built. Not one of the SRS's 7 numbered Open
Issues itself, just an unaddressed scope gap against Section 7.

Method note: FR-01 specifies "cross-lingual text embedding (zh/en) +
unsupervised clustering" - this uses TF-IDF + K-Means instead of true
embeddings (LASER/mBERT/etc). Output requirements (clusters, topic
labels, gap list, entry points) are met; the specified technique isn't.
"""
import csv
import json
import os
import re
import warnings
from collections import defaultdict
from datetime import datetime, timezone

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
from wordfreq import zipf_frequency

from time_ranges import RANGE_HOURS, RANGE_ORDER, MIN_WINDOW_POSTS, parse_ts, window_bounds, window_dict, TAIWAN_TZ

BASE = os.path.dirname(__file__)
CSV_DIR = os.path.join(BASE, 'csv')
FACEBOOK_CSV_DIR = os.path.join(CSV_DIR, 'facebook')
LINKEDIN_CSV_DIR = os.path.join(CSV_DIR, 'linkedin')
OUT_FILE = os.path.join(BASE, 'analysis', 'topic_clusters.json')
LEGACY_RANGE = '1q'  # analysis/topic_clusters.json mirrors this range
TAXONOMY_FILE = os.path.join(BASE, 'topic_taxonomy.json')


def range_out_file(range_key):
    return os.path.join(BASE, 'analysis', f'topic_clusters_{range_key}.json')

# Own and competitor handles live in accounts_config.json (not hardcoded
# here) so add_account.py can register a newly-approved account - see
# FR-05's "request tracking" flow on the dashboard - without editing this
# file. 'own' is a list, not a single handle: a platform can have more than
# one account that's actually ours (e.g. a second outlet TrendForce runs).
ACCOUNTS_CONFIG_PATH = os.path.join(BASE, 'accounts_config.json')
_DEFAULT_OWN = {
    'X': ['TrendForce'],
    'Facebook': ['TrendForce.tw'],
    'LinkedIn': ['TrendForce'],
}
_DEFAULT_COMPETITORS = {
    'X': ['dylan522p', 'SemiAnalysis_', 'jukan05', 'QQ_Timmy', 'technews_tw'],
    'Facebook': ['ctee.fans', 'yutinghaosfinance'],
    'LinkedIn': [],
}


def load_competitors_config():
    if os.path.exists(ACCOUNTS_CONFIG_PATH):
        with open(ACCOUNTS_CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}


_competitors_cfg = load_competitors_config()

PLATFORM_ACCOUNTS = {
    'X': {
        'dir': CSV_DIR,
        'own': _competitors_cfg.get('X', {}).get('own', _DEFAULT_OWN['X']),
        'competitors': _competitors_cfg.get('X', {}).get('competitors', _DEFAULT_COMPETITORS['X']),
    },
    'Facebook': {
        'dir': FACEBOOK_CSV_DIR,
        'own': _competitors_cfg.get('Facebook', {}).get('own', _DEFAULT_OWN['Facebook']),
        'competitors': _competitors_cfg.get('Facebook', {}).get('competitors', _DEFAULT_COMPETITORS['Facebook']),
    },
    'LinkedIn': {
        'dir': LINKEDIN_CSV_DIR,
        'own': _competitors_cfg.get('LinkedIn', {}).get('own', _DEFAULT_OWN['LinkedIn']),
        'competitors': _competitors_cfg.get('LinkedIn', {}).get('competitors', _DEFAULT_COMPETITORS['LinkedIn']),
    },
}
OWN_HANDLES = {h for p in PLATFORM_ACCOUNTS.values() for h in p['own']}
ACCOUNTS = [h for p in PLATFORM_ACCOUNTS.values() for h in p['own'] + p['competitors']]

# Backward-compat aliases (X was the only platform when these were introduced).
OWN_ACCOUNT = PLATFORM_ACCOUNTS['X']['own'][0]
COMPETITOR_ACCOUNTS = PLATFORM_ACCOUNTS['X']['competitors']

# 18 -> 19 -> 20 (2026-08-10, same day, two steps): every time a term gets
# removed from the vectorizer's vocabulary (see EN_NOISE_WORDS below),
# every document's TF-IDF vector shifts enough that K-Means' fixed-size
# partition can reshuffle and silently merge a real topic gap into a
# larger, already-well-covered cluster - happened twice today: removing
# 'opinion'/'edition'/'ft'/'trib' (a junk cluster) merged away 3 real
# HBM/DRAM/NAND gaps (18->19 recovered them), then separately removing
# 'trendforce' (self-referential noise - TrendForce's own name, not a
# topic) merged the SAME HBM gap away again (19->20 recovered it again).
# Each bump effectively restores the cluster-count "slot" the removed
# term's noise used to occupy. Verified each time by re-running against
# the same day's data before/after - this is not a one-time fluke, it's
# how this pipeline responds to any vocabulary change: expect to
# re-check gap counts (not just "does it still run") after editing
# EN_NOISE_WORDS, and expect boundaries to differ slightly from before -
# K-Means has no obligation to reproduce the same partition after any
# vocabulary change, so some reshuffling here is inherent, not fully
# eliminable.
N_CLUSTERS = 22

URL_RE = re.compile(r'https?://\S+')
NON_WORD_RE = re.compile(r'[^a-zA-Z一-鿿\s#]')
# Shortlink-domain fragments (dlvr.it, buff.ly, t.co, etc.) that leak into
# TF-IDF terms as meaningless noise once URLs are stripped to bare tokens.
LINK_NOISE = {'dlvr', 'buff', 'ly', 'tt', 'http', 'https', 'www', 'com'}

# Generic financial/temporal boilerplate that shows up in nearly every
# Traditional Chinese finance-news post (億元, 年增, 今年, 月增, 個百分點, ...)
# - it's high-frequency but carries no topic identity, so it crowds out the
# actual technical/product terms a topic label should surface. The X CSVs'
# pre-extracted `keywords` column hands the vectorizer whole boilerplate
# phrases as single tokens (兆日圓, 個百分點, 創下歷史新高, ...), so an exact
# stop-word set only catches the literal phrases it lists - a combinatorial
# family like [億|萬|千|兆][元|日圓|美元|韓元] needs a pattern, not a list.
CHINESE_NOISE_SUBSTRINGS = [
    '年增', '年減', '月增', '月減', '季增', '季減', '去年', '今年', '明年',
    '本季', '上季', '下季', '本月', '上月', '下月', '同期', '同比',
    '目前', '近期', '日前', '日起', '報導', '指出', '表示', '預估', '預計', '據悉',
    '營收', '獲利', '毛利率', '目標價', '股價', '創新高', '創下', '新高', '新低',
    '百分點', '歷史新高', '央行', '因此', '經濟日報', '導讀', 'reurl',
    '年的', '年至', '年間', '年以來', '過去',
]
CHINESE_NOISE_RE = re.compile('|'.join(re.escape(w) for w in CHINESE_NOISE_SUBSTRINGS))
# Pure currency/magnitude tokens (億元, 兆日圓, 萬美元, ...) carry no topic
# identity on their own - they're units, not subjects.
CHINESE_UNIT_TOKEN_RE = re.compile(r'^[0-9億萬千兆]+[元日圓韓美歐]{0,2}$')

# General-purpose sweep for ordinary Chinese words, same idea as
# is_common_english_word() below: CHINESE_NOISE_SUBSTRINGS only catches
# financial-report boilerplate that's specifically been noticed and listed
# (營收, 股價, 創新高, ...) - it does nothing for ordinary connective/hedging
# words (雖然, 但是, 根據, 認為, 進行, 相關, 影響, ...) that would otherwise
# need listing one at a time too. zipf_frequency('zh') scores how common a
# word is in everyday Chinese; 5.1 was picked by checking where genuine
# connectives (指出=5.32, 表示=5.62, 目前=5.62, 因此=5.66, 雖然=5.66,
# 但是=5.85, 根據=5.68, 進行=5.97, 相關=5.56, 有關=5.56, 影響=5.74) clear the
# highest real industry/company term found (蘋果=5.00 - it's also the
# ordinary word for "apple", so it registers as fairly common too;
# 三星=4.45, 半導體=4.01, 台積電=2.14, 輝達=2.59 all sit well below). Financial
# jargon (營收=3.69, 股價=4.21, 創新高=4.05) still scores as uncommon in a
# general corpus, same as English "contract"/"progress" did, so
# CHINESE_NOISE_SUBSTRINGS above still does the work for that category.
COMMON_CHINESE_ZIPF_THRESHOLD = 5.1


def is_common_chinese_word(token):
    return zipf_frequency(token, 'zh') >= COMMON_CHINESE_ZIPF_THRESHOLD


def is_chinese_noise_token(token):
    return (bool(CHINESE_UNIT_TOKEN_RE.match(token)) or bool(CHINESE_NOISE_RE.search(token))
            or is_common_chinese_word(token))


# General-purpose sweep for ordinary English words (not industry jargon) -
# a hand-picked EN_NOISE_WORDS list below keeps missing words one at a time
# ("lol", then "true", then "new", ...) because a fixed list can only ever
# catch what's already been noticed. zipf_frequency scores how common a word
# is in everyday English (higher = more common); 5.0 was picked by checking
# where ordinary filler words ("lol"=5.05, "true"=5.40, "new"=6.25,
# "thanks"=5.43, "like"=6.41) separate cleanly from real industry terms this
# pipeline actually cares about (max observed: "apple"=4.76, with
# "chip"=4.31, "samsung"=4.12, "tsmc"=2.09, "nvidia"=3.25 well below that) -
# every industry term checked stayed under the cutoff, nothing on the noise
# side got close to it. This alone doesn't replace EN_NOISE_WORDS: slang
# like "bruh" scores 3.51 (below the cutoff, since general-English corpora
# barely register it) and still needs a hand-picked entry.
COMMON_ENGLISH_ZIPF_THRESHOLD = 5.0


def is_common_english_word(token):
    return token.isalpha() and zipf_frequency(token, 'en') >= COMMON_ENGLISH_ZIPF_THRESHOLD


# English-language counterpart to CHINESE_NOISE_SUBSTRINGS: internet-slang/
# conversational filler and generic reporting-verb noise that sklearn's
# built-in 'english' stopword list doesn't catch (it's ordinary vocabulary,
# not textbook stopwords like "the"/"and") AND that is_common_english_word()
# above can't catch either (too slang-y/niche to register as "common" in a
# general-English frequency corpus). A cluster of casual social-media
# commentary (e.g. a competitor account's chattier posting style) lets these
# dominate frequency and crowd out the actual product/company names that
# should label the topic - seen in practice as a cluster labeled
# "image / bruh / buy / told" instead of anything industry-specific.
# English tokens are already whole words after tokenization (unlike Chinese,
# which needs substring matching since there's no word-boundary signal), so
# an exact-match set is sufficient here.
EN_NOISE_WORDS = {
    'bruh', 'lol', 'lmao', 'rofl', 'omg', 'smh', 'tbh', 'imo', 'imho', 'fyi', 'btw',
    'yeah', 'yep', 'nah', 'gonna', 'wanna', 'kinda', 'sorta', 'gotta',
    'dude', 'bro', 'guys', 'guy', 'weebs', 'weeb',
    'image', 'images', 'img', 'photo', 'photos', 'pic', 'pics', 'picture', 'pictures',
    'video', 'videos', 'gif', 'gifs', 'thread', 'threads',
    'said', 'says', 'saying', 'told', 'telling', 'according', 'reportedly', 'reported',
    'claims', 'claimed', 'believe', 'believes', 'believed', 'think', 'thinks', 'thought', 'thoughts',
    'wow', 'damn', 'literally', 'actually', 'basically', 'honestly', 'seriously',
    'totally', 'definitely', 'probably', 'maybe', 'buy', 'buying', 'bought',
    # Generic descriptive/hedging words that show up across nearly every
    # industry, not just semiconductors/tech - carry no topic identity on
    # their own (e.g. "pro"/"color" are usually a product-variant suffix,
    # not a subject; "already"/"expected" are hedging/temporal filler).
    'pro', 'color', 'colors', 'colour', 'colours', 'already', 'expected', 'expects', 'expect',
    'progress', 'contract', 'contracts', 'published', 'publish', 'publishes', 'publishing',
    # Not in sklearn's built-in English stopword list either, despite being
    # just as generic/non-industry-specific.
    'like', 'likes', 'liked', 'year', 'years', 'thanks', 'thank',
    # Found 2026-08-10: a cluster labeled "opinion / trib / ft / edition"
    # (no industry content at all) - 'opinion' (zipf 4.94) and 'edition'
    # (zipf 4.80) just missed COMMON_ENGLISH_ZIPF_THRESHOLD's 5.0 cutoff;
    # 'ft' (4.52) is a generic short token/account-name artifact (the FT
    # account's own name, not a real topic); 'trib' (zipf 2.25) is too
    # rare to be a real word at all - likely a tokenization fragment.
    'opinion', 'opinions', 'edition', 'editions', 'ft', 'trib',
    # Self-referential - TrendForce's own name shows up constantly across
    # its own posts (and others quoting/crediting it), but it's not a
    # topic descriptor - "trendforce / dram / nand / hbm" describes DRAM/
    # NAND/HBM coverage, not "the topic of TrendForce".
    'trendforce',
    # Domain-wide glue terms (2026-08-12): this whole feed IS
    # semiconductor/AI/chip news, so these words appear in nearly every
    # post regardless of actual subject - instead of differentiating
    # topics they were dominating cluster centroids and pulling
    # unrelated companies (nvidia, openai, google, tsmc, samsung, ...)
    # into one ~14k-post catch-all cluster labeled "ai / memory / tsmc /
    # chip / google / samsung / openai". Removing them let K-Means
    # separate by what actually distinguishes posts - the specific
    # company/product (nvidia+jensen+rubin, openai+anthropic+chatgpt,
    # google+deepmind+gemini, sk+hynix, ...) - while genuinely related
    # terms (memory/dram/hbm/nand) still cluster together on their own
    # merit, unaffected. Verified 2026-08-12 by comparing cluster
    # top-terms before/after on the same data (see N_CLUSTERS's own
    # comment above for why any vocabulary change here needs this kind
    # of before/after check, not just "does it still run").
    'ai', 'chip', 'chips', 'semiconductor', 'semiconductors', 'supply', 'chain', 'gpu', 'gpus',
}


def parse_count(val):
    if not val:
        return 0
    val = str(val).strip().replace(',', '')
    try:
        if val.endswith('K'):
            return int(float(val[:-1]) * 1000)
        if val.endswith('M'):
            return int(float(val[:-1]) * 1_000_000)
        return int(float(val))
    except ValueError:
        return 0


def clean_text(text):
    text = URL_RE.sub('', text or '')
    text = NON_WORD_RE.sub(' ', text)
    return text.strip()


def parse_facebook_timestamp(row):
    """Facebook rows carry a human-readable exactDate ("Thursday, July 2,
    2026 at 1:00 PM") when the scraper resolved one, else fall back to the
    machine-readable scrapedAt (when the post was scraped, not posted).

    exactDate is in the viewer's local time - Taiwan (UTC+8), confirmed by
    comparing it against scrapedAt on real rows (e.g. exactDate "12:45 PM"
    lands ~8 minutes before a scrapedAt of 04:53 UTC, which is 12:53 in
    Taiwan - consistent; interpreting exactDate as UTC directly would put
    the post 8 hours in the future). Attach Taiwan tzinfo and convert to
    UTC rather than treating the naive parse as if it were already UTC."""
    exact = row.get('exactDate')
    if exact:
        try:
            naive = datetime.strptime(exact, '%A, %B %d, %Y at %I:%M %p')
            return naive.replace(tzinfo=TAIWAN_TZ).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        except ValueError:
            pass
    return row.get('scrapedAt')


def load_x_posts(handle, path):
    posts = []
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            text = row.get('translated_text') or row.get('text') or ''
            keywords = row.get('keywords', '')
            doc = clean_text(text + ' ' + keywords.replace(';', ' '))
            if doc:
                posts.append({
                    'handle': handle,
                    'platform': 'X',
                    'text': doc,
                    'raw_text': text.strip(),
                    'timestamp': row.get('timestamp'),
                    'url': row.get('tweetUrl') or '',
                    'likes': parse_count(row.get('likes')),
                    'replies': parse_count(row.get('replies')),
                    'interaction': parse_count(row.get('likes')) + parse_count(row.get('retweets')) + parse_count(row.get('replies')),
                })
    return posts


def load_facebook_posts(handle, path):
    posts = []
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            doc = clean_text(row.get('text', ''))
            if doc:
                posts.append({
                    'handle': handle,
                    'platform': 'Facebook',
                    'text': doc,
                    'raw_text': (row.get('text') or '').strip(),
                    'timestamp': parse_facebook_timestamp(row),
                    'url': row.get('postUrl') or '',
                    'likes': parse_count(row.get('reactions')),
                    'replies': parse_count(row.get('comments')),
                    'interaction': parse_count(row.get('reactions')) + parse_count(row.get('comments')) + parse_count(row.get('shares')),
                })
    return posts


def load_linkedin_posts(handle, path):
    posts = []
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            doc = clean_text(row.get('text', ''))
            if doc:
                posts.append({
                    'handle': handle,
                    'platform': 'LinkedIn',
                    'text': doc,
                    'raw_text': (row.get('text') or '').strip(),
                    'timestamp': row.get('timestamp'),
                    'url': row.get('postUrl') or '',
                    'likes': parse_count(row.get('likes')),
                    'replies': parse_count(row.get('comments')),
                    'interaction': parse_count(row.get('likes')) + parse_count(row.get('comments')),
                })
    return posts


LOADERS = {
    'X': load_x_posts,
    'Facebook': load_facebook_posts,
    'LinkedIn': load_linkedin_posts,
}


def load_posts():
    """NFR-02 requires deduplication after collection. Nothing upstream
    guarantees it: sync_data.sh re-copies whole CSVs (a scraper re-run with
    an overlapping window would duplicate rows), so dedup on the way in
    rather than trusting every source file is already clean."""
    posts = []
    seen = set()
    for platform, cfg in PLATFORM_ACCOUNTS.items():
        loader = LOADERS[platform]
        for handle in cfg['own'] + cfg['competitors']:
            path = os.path.join(cfg['dir'], f'{handle}.csv')
            if not os.path.exists(path) and platform == 'LinkedIn':
                # A tracked individual's personal profile (scrape_profiles_linkedin.js) -
                # a fundamentally different scrape from a company page, but
                # sync_data.sh lands its CSV in a 'profiles' subdirectory
                # rather than alongside company pages, so check there too
                # before giving up. Same 6-column schema either way, so
                # load_linkedin_posts() needs no change.
                path = os.path.join(cfg['dir'], 'profiles', f'{handle}.csv')
            if not os.path.exists(path):
                continue
            for post in loader(handle, path):
                key = (post['platform'], post['handle'], post['timestamp'], post['text'])
                if key in seen:
                    continue
                seen.add(key)
                posts.append(post)
    return posts


def label_cluster(vectorizer, centroid, top_n=4):
    terms = vectorizer.get_feature_names_out()
    top_idx = centroid.argsort()[::-1][:top_n]
    return [terms[i] for i in top_idx if centroid[i] > 0]


def cluster_posts(posts, n_clusters=N_CLUSTERS, min_docs_per_cluster=5):
    """Shared clustering step so FR-02's topic hierarchy matches FR-01's tree.

    On a small/sparse subset (a short time window, or FR-02's sub-topic
    drill-down), min_df=2 can prune every term if nothing repeats across
    documents, which raises instead of degrading gracefully. Retry with a
    looser min_df, then without stop-word filtering, before giving up."""
    docs = [p['text'] for p in posts]
    stop_words = list(TfidfVectorizer(stop_words='english').get_stop_words()) + list(LINK_NOISE) + list(EN_NOISE_WORDS)
    # Default analyzer (lowercasing + tokenizing + stop-word removal) plus the
    # Chinese noise-pattern filter, which stop_words alone can't express
    # since it only matches whole tokens exactly.
    base_analyzer = TfidfVectorizer(stop_words=stop_words).build_analyzer()
    noise_filtered_analyzer = lambda doc: [t for t in base_analyzer(doc)
                                            if not is_chinese_noise_token(t) and not is_common_english_word(t)]

    X = vectorizer = None
    for kwargs in ({'min_df': 2, 'analyzer': noise_filtered_analyzer},
                   {'min_df': 1, 'analyzer': noise_filtered_analyzer},
                   {'min_df': 1}):
        vectorizer = TfidfVectorizer(max_features=3000, **kwargs)
        try:
            X = vectorizer.fit_transform(docs)
            break
        except ValueError:
            continue
    if X is None:
        raise ValueError(f"Could not vectorize {len(docs)} document(s) even without stop-word filtering.")

    k = max(1, min(n_clusters, len(posts) // min_docs_per_cluster))
    km = KMeans(n_clusters=k, n_init=10, random_state=42)
    # k-means++ init's cumulative-distance computation overflows float64 on
    # this TF-IDF matrix's value range (a handful of documents can have
    # scores that, once squared and summed across thousands of features,
    # overflow) - harmless (numpy still produces a usable result via inf/nan
    # handling internally) but prints on every single pipeline run. Silence
    # just this call rather than a rewrite of sklearn's own init routine.
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=RuntimeWarning, module='sklearn')
        labels = km.fit_predict(X)
    return vectorizer, X, km, labels


# --- FR-01 topic taxonomy (2026-08-13) ---
# FR-01 Topic Gaps used to run K-Means over raw TF-IDF vectors - the
# cluster boundaries (and therefore the labels/gaps) shifted every time a
# noise word was added or removed, needing a fresh N_CLUSTERS bump and a
# before/after gap-count check each time (see N_CLUSTERS's own history
# comment above). A hand-curated keyword taxonomy
# (topic_taxonomy.json, from AI關鍵字材料包_Topic分類.xlsx) replaces that
# with fixed, human-authored topics - stable across pipeline runs, and
# multi-label per the taxonomy's own recommendation ("允許一篇新聞多重標籤")
# since a post like "NVIDIA orders CoWoS from TSMC" is genuinely about
# both B (GPU) and F (foundry), not one or the other.
#
# label_cluster/cluster_posts/N_CLUSTERS above are UNCHANGED and still
# used as-is by fuzzy_trend.py (FR-02), nlp_sentiment.py (FR-03),
# account_comment_management.py, and manual_review.py - none of those
# were asked to change, so their own K-Means-based topic trees keep
# working exactly as before. Only this file's own FR-01 output (main(),
# below) switches to the taxonomy.
def load_topic_taxonomy():
    with open(TAXONOMY_FILE, encoding='utf-8') as f:
        return json.load(f)


def build_topic_matchers(taxonomy):
    """One compiled regex per topic (alternation of all its keywords).

    Keywords containing CJK characters are matched as plain substrings
    (Chinese has no whitespace word boundaries). Pure-ASCII keywords get a
    (?<![A-Za-z0-9])...(?![A-Za-z0-9]) boundary so 'sell' doesn't match
    inside 'sellers', etc. Short ASCII keywords (<=4 chars, e.g. 'GaN',
    'SiC', 'Arm', 'Fed') are matched case-sensitively - case-insensitive
    would turn these into false-positive machines ('again' contains
    'gan', 'basic' contains 'sic', 'arm'/'farm' are ordinary English
    words). Longer ASCII keywords ('NVIDIA', 'Broadcom', ...) are safe
    case-insensitive since they're not real dictionary words."""
    has_cjk = re.compile(r'[一-鿿]')
    matchers = {}
    for topic in taxonomy:
        parts = []
        for kw in topic['keywords']:
            if has_cjk.search(kw):
                parts.append(re.escape(kw))
            elif len(kw.replace(' ', '')) <= 4:
                parts.append(f'(?-i:(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9]))')
            else:
                parts.append(f'(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])')
        matchers[topic['code']] = re.compile('|'.join(parts), re.IGNORECASE)
    return matchers


def classify_post_topics(text, matchers):
    """Multi-label: returns every topic code whose taxonomy keywords hit
    this post's text. A post can (and often does) match 0, 1, or several
    topics - unmatched posts just don't show up in any topic's stats."""
    return [code for code, pattern in matchers.items() if pattern.search(text or '')]


MAX_SAMPLE_POSTS_PER_GAP = 8
SAMPLE_RAW_TEXT_MAXLEN = 300


def summarize_topics(posts, post_topics, topic_names):
    """Build clusters/gaps for whatever subset of (posts, post_topics) is
    passed in - the caller decides the time window, if any.

    post_topics[i] is the list of topic codes post i matched (multi-label,
    see classify_post_topics) - a post with 2 matches counts toward both
    topics' size/accounts/gaps, by design (see the taxonomy note above
    build_topic_matchers)."""
    clusters = []
    idxs_by_topic = defaultdict(list)
    for i, codes in enumerate(post_topics):
        for code in codes:
            idxs_by_topic[code].append(i)

    for code in sorted(idxs_by_topic):
        idxs = idxs_by_topic[code]
        by_account = defaultdict(int)
        engagement_by_account = defaultdict(int)
        for i in idxs:
            by_account[posts[i]['handle']] += 1
            engagement_by_account[posts[i]['handle']] += posts[i]['interaction']
        clusters.append({
            'id': code,
            'label': topic_names[code],
            'size': len(idxs),
            'accounts': dict(by_account),
            'engagement_by_account': dict(engagement_by_account),
        })

    # Topic gaps: topics where competitors post a lot but our own accounts post little/none.
    gaps = []
    for c in clusters:
        own_count = sum(v for k, v in c['accounts'].items() if k in OWN_HANDLES)
        competitor_count = sum(v for k, v in c['accounts'].items() if k not in OWN_HANDLES)
        competitor_engagement = sum(v for k, v in c['engagement_by_account'].items() if k not in OWN_HANDLES)
        if competitor_count >= 3 and own_count <= max(1, competitor_count // 4):
            # Real member posts (competitor-authored only, matching what
            # this gap is actually about) for the Topic Digest cards -
            # top N by engagement, deduped by URL since sync_data.sh can
            # leave near-duplicate rows across overlapping scraper runs.
            # raw_text (uncleaned, unlike 'text' which is TF-IDF fodder -
            # stripped of punctuation) is what's actually displayable.
            member_posts = [posts[i] for i in idxs_by_topic[c['id']] if posts[i]['handle'] not in OWN_HANDLES]
            member_posts.sort(key=lambda p: p['interaction'], reverse=True)
            seen_urls = set()
            sample_posts = []
            for p in member_posts:
                key = p.get('url') or p.get('raw_text')
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                sample_posts.append({
                    'handle': p['handle'],
                    'platform': p['platform'],
                    'text': (p.get('raw_text') or '')[:SAMPLE_RAW_TEXT_MAXLEN],
                    'url': p.get('url') or '',
                    'timestamp': p.get('timestamp'),
                    'interaction': p['interaction'],
                })
                if len(sample_posts) >= MAX_SAMPLE_POSTS_PER_GAP:
                    break
            gaps.append({
                'cluster_id': c['id'],
                'label': c['label'],
                'own_count': own_count,
                'competitor_count': competitor_count,
                'competitor_engagement': competitor_engagement,
                'competitors_covering': [k for k in c['accounts'] if k not in OWN_HANDLES],
                'sample_posts': sample_posts,
            })
    gaps.sort(key=lambda g: g['competitor_engagement'], reverse=True)

    return {
        'clusters': sorted(clusters, key=lambda c: c['size'], reverse=True),
        'gaps': gaps,
        'suggested_entry_points': gaps[:5],
    }


def write_json(path, result):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def main():
    posts = load_posts()
    if not posts:
        print("No posts found, skipping.")
        return

    taxonomy = load_topic_taxonomy()
    matchers = build_topic_matchers(taxonomy)
    topic_names = {t['code']: t['name'] for t in taxonomy}
    # Matched once against every post up front (fixed keyword rule, no
    # fitting needed) - every range below just filters which posts count,
    # same "compute once, slice by window" shape the old K-Means tree used.
    post_topics = [classify_post_topics(p.get('raw_text') or p['text'], matchers) for p in posts]

    timestamps = [parse_ts(p['timestamp']) for p in posts]
    now = max((t for t in timestamps if t), default=None)

    written = 0
    for range_key in RANGE_ORDER:
        if now is None:
            break
        start, end = window_bounds(range_key, now)
        idxs = [i for i, t in enumerate(timestamps) if t and start <= t <= end]
        if len(idxs) < MIN_WINDOW_POSTS:
            print(f"Skipping {range_key}: only {len(idxs)} posts in window (need {MIN_WINDOW_POSTS}).")
            continue

        window_posts = [posts[i] for i in idxs]
        window_post_topics = [post_topics[i] for i in idxs]
        result = summarize_topics(window_posts, window_post_topics, topic_names)
        result['window'] = window_dict(start, end)
        write_json(range_out_file(range_key), result)
        if range_key == LEGACY_RANGE:
            write_json(OUT_FILE, result)
        print(f"[{range_key}] Wrote {len(result['clusters'])} topics, {len(result['gaps'])} topic gaps "
              f"({len(idxs)} posts) to {range_out_file(range_key)}")
        written += 1

    if written == 0:
        print("No range had enough posts to summarize.")


if __name__ == '__main__':
    main()
