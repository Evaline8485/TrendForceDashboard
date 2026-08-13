"""
Renders docs/index.html from the FR-01..06 analysis JSON files - the
missing L4 Presentation layer (SRS Section 3) that turns the JSON this
pipeline produces into something a person can actually look at.

Reads whichever of these are present (skips sections gracefully if a file
is missing, e.g. before the first pipeline run):
  analysis/topic_clusters_<range>.json     FR-01 gaps, one per time range
  analysis/fuzzy_trends_<range>.json       FR-02 rising topics/KOLs, one per range
  analysis/sentiment_dashboard_<range>.json FR-03 widgets, one per range
  analysis/daily_summaries.json            FR-06 executive summaries
  analysis/account_status.json             FR-05 account status
  analysis/video_ranking.json              X Video Ranking (replaces the old FR-04
                                            Manual Review Queue section)

Topic Gaps, Rising Trends, and Sentiment all support a client-side time-range
switch (4h/8h/1d/1w/1q, see time_ranges.py): every range's HTML is
pre-rendered at build time and embedded in the page, and a dropdown just
swaps which pre-rendered block is shown - no server or re-fetch needed.

Static HTML + inline CSS/JS, no build step - open docs/index.html directly
or serve docs/ (e.g. GitHub Pages, matching TrendforceTwitterScraper's setup).

Gap vs. spec: SRS Open Issue #7 (roles & permissions) is unresolved and,
as built, unresolvable without new infrastructure - this is a public
static site with no backend, so there's no login and no admin/analyst/
reviewer distinction. Everyone with the URL sees and can do everything
a visitor can do here (including submitting FR-05 account-tracking
requests). Real roles would need an auth provider and a backend, which
is a different architecture than "static site, no server."
"""
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone, timedelta

from time_ranges import RANGE_ORDER, RANGE_LABELS, RANGE_HOURS, MIN_WINDOW_POSTS, parse_ts, window_bounds, format_window, taiwan_str
from cluster_topics import load_posts
from video_ranking import RANGES as VIDEO_RANKING_RANGES
from video_ranking import REGION_KEYWORDS as _VIDEO_REGION_KEYWORDS

# Same 6 region labels video_ranking.py's classify_region() can produce -
# order here is just display order in the filter row, unrelated to
# REGION_KEYWORDS' own match-priority order.
VIDEO_REGIONS = ['United States', 'Europe', 'Japan', 'South Korea', 'China', 'Singapore']
assert set(VIDEO_REGIONS) == {r for r, _ in _VIDEO_REGION_KEYWORDS}, \
    'VIDEO_REGIONS must stay in sync with video_ranking.py REGION_KEYWORDS'

# FR-03 (Sentiment/Competitor Watch) explicitly spec's "hourly / every 4
# hours / daily / monthly / quarterly" - no 8h, no 1w. FR-01/02 (Topic
# Gaps/Rising Trends) only need 4h/8h/1d/1w/1q per those FRs' own
# scheduling section - no 1h, no 1mo. Derived by filtering RANGE_ORDER
# rather than hardcoding the order, so these stay consistent if
# RANGE_ORDER's own ordering ever changes.
FR0102_RANGES = [r for r in RANGE_ORDER if r in {'4h', '8h', '1d', '1w', '1q'}]
FR03_RANGES = [r for r in RANGE_ORDER if r in {'1h', '4h', '1d', '1mo', '1q'}]

BASE = os.path.dirname(__file__)
ANALYSIS_DIR = os.path.join(BASE, 'analysis')
DOCS_DIR = os.path.join(BASE, 'docs')
OUT_FILE = os.path.join(DOCS_DIR, 'index.html')
TAIWAN_TZ = timezone(timedelta(hours=8))
DEFAULT_DASHBOARD_RANGE = '1d'

_FAVICON_SVG_RAW = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="#0d1117"/>
<rect x="6" y="18" width="4" height="9" rx="1" fill="#3b9eff"/>
<rect x="13" y="13" width="4" height="14" rx="1" fill="#3b9eff"/>
<rect x="20" y="6" width="4" height="21" rx="1" fill="#f0b429"/>
</svg>'''
FAVICON_SVG = urllib.parse.quote(_FAVICON_SVG_RAW)


def load(name):
    path = os.path.join(ANALYSIS_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_vader_lexicon():
    """FR-07's self-service upload runs entirely client-side (static site,
    no backend) - embed VADER's actual word->score lexicon so its sentiment
    scoring matches the rest of the dashboard (nlp_sentiment.py) instead of
    approximating with a small ad hoc word list."""
    import vaderSentiment
    path = os.path.join(os.path.dirname(vaderSentiment.__file__), 'vader_lexicon.txt')
    lexicon = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            parts = line.split('\t')
            if len(parts) >= 2:
                lexicon[parts[0]] = float(parts[1])
    return lexicon


def load_chinese_sentiment_data():
    """FR-07's self-service upload runs entirely client-side and only ever
    had VADER (English-only) - Chinese text silently scored as neutral
    regardless of actual sentiment, every time, the same bug NFR-07 already
    fixed server-side (nlp_sentiment.py). Embed the same cnsenti word lists
    plus a Traditional->Simplified single-character map (cnsenti's dictionary
    is simplified-only) so the browser can approximate the server-side path.

    This is a substring scan over the raw text, not real word segmentation -
    porting jieba's segmenter to JS is out of scope, and cnsenti's own
    sentiment_count() is just word-list membership counting (no negation/
    intensity handling) once jieba has segmented, so scanning substrings
    directly is a reasonable client-side approximation of the same idea.
    Character-level T2S (not OpenCC's full phrase-aware conversion) covers
    the large majority of Traditional/Simplified differences, which are
    per-character far more often than per-phrase.
    """
    import pickle
    import cnsenti
    cnsenti_dict_dir = os.path.join(os.path.dirname(cnsenti.__file__), 'dictionary', 'hownet')
    with open(os.path.join(cnsenti_dict_dir, 'pos.pkl'), 'rb') as f:
        pos_words = pickle.load(f)
    with open(os.path.join(cnsenti_dict_dir, 'neg.pkl'), 'rb') as f:
        neg_words = pickle.load(f)
    # Same whitespace bug fixed server-side: some dictionary entries carry a
    # trailing space, and a stray literal space would otherwise "match".
    pos_words = sorted({w.strip() for w in pos_words if w.strip()})
    neg_words = sorted({w.strip() for w in neg_words if w.strip()})

    import opencc
    opencc_dict_dir = os.path.join(os.path.dirname(opencc.__file__), 'dictionary')

    def load_table(fname):
        table = {}
        with open(os.path.join(opencc_dict_dir, fname), encoding='utf-8') as f:
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if len(parts) == 2:
                    table[parts[0]] = parts[1].split(' ')[0]  # first candidate only
        return table

    # Taiwan-specific character variants, THEN the general Traditional->
    # Simplified table, chained - same conversion_chain order as tw2sp
    # (e.g. 啟 resolves through TWVariantsRev before falling through to
    # TSCharacters). Chained, not merged with first-file-wins priority: a
    # flat merge left 為 mapped to 爲 (TWVariantsRev's Taiwan-variant
    # normalization, itself still Traditional) since that entry came first
    # and blocked TSCharacters' own 為->為 entry from ever being read -
    # running each character through BOTH tables in sequence (為->爲->为)
    # gives the actual Simplified form.
    tw_variants = load_table('TWVariantsRev.txt')
    ts_chars = load_table('TSCharacters.txt')
    t2s_map = {}
    for ch in set(tw_variants) | set(ts_chars):
        simplified = ts_chars.get(tw_variants.get(ch, ch), tw_variants.get(ch, ch))
        if simplified != ch:
            t2s_map[ch] = simplified
    return pos_words, neg_words, t2s_map


def esc(s):
    if s is None:
        return ''
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;'))


def fmt_int(n):
    return f"{n:,}" if isinstance(n, (int, float)) else esc(n)


def fmt_dt(iso_str):
    """Account status timestamps came through as raw ISO strings (some
    +08:00, some +00:00, some with microseconds - whatever the source data
    happened to have) instead of one consistent, readable format. Always
    show Taiwan time, plain "YYYY-MM-DD HH:MM"."""
    if not iso_str:
        return '—'
    try:
        return datetime.fromisoformat(iso_str).astimezone(TAIWAN_TZ).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return esc(iso_str)


def account_profile_url(platform, handle):
    """Handles are stored bare (no leading @, per accounts_config.json) -
    X and Facebook both resolve a bare handle path to the account's own
    profile page directly, no lookup needed."""
    if platform == 'X':
        return f'https://x.com/{handle}'
    if platform == 'Facebook':
        return f'https://www.facebook.com/{handle}'
    if platform == 'LinkedIn':
        # A tracked INDIVIDUAL's personal profile (scrape_profiles_linkedin.js,
        # a deliberately separate scraper from company-page tracking) - its
        # CSV landing in csv/linkedin/profiles/ rather than alongside company
        # pages (same distinction cluster_topics.py's load_posts() checks) is
        # the only signal available here for which URL shape to build, since
        # accounts_config.json doesn't otherwise distinguish "company" from
        # "person" - a bare handle string looks the same either way.
        if os.path.exists(os.path.join(BASE, 'csv', 'linkedin', 'profiles', f'{handle}.csv')):
            return f'https://www.linkedin.com/in/{handle}/'
        # Unlike X/Facebook, LinkedIn's own scraper (scrape_accounts_linkedin.js)
        # tracks accounts by a company-page URL slug - historically not the
        # same string as the display handle (e.g. handle "TrendForce" ->
        # slug "trendforce-corporation"), so check the legacy override map
        # first. Accounts added via add_account.py from here on use the
        # slug itself as the handle (simpler, no separate mapping needed),
        # so falling back to the handle itself covers those without this
        # dict needing an edit for every new account.
        slug = LINKEDIN_HANDLE_TO_SLUG.get(handle, handle)
        return f'https://www.linkedin.com/company/{slug}/'
    return None


# Legacy handle->slug overrides for LinkedIn accounts added before
# add_account.py used the slug itself as the handle - add_account.py's
# own additions don't need an entry here (see account_profile_url above).
LINKEDIN_HANDLE_TO_SLUG = {
    'TrendForce': 'trendforce-corporation',
}


def panel(body_html, title=None, eyebrow=None):
    """Consistent card wrapper for a titled block of content - every major
    piece of content (a table, a stat row, a chart) sits inside one of
    these instead of floating directly on the page background."""
    head = ''
    if title:
        eyebrow_html = f'<span class="panel-eyebrow">{esc(eyebrow)}</span>' if eyebrow else ''
        head = f'<div class="panel-head"><h3>{esc(title)}</h3>{eyebrow_html}</div>'
    return f'<div class="panel">{head}{body_html}</div>'


def table(headers, rows_html, empty_message=None):
    if not rows_html:
        return f'<p class="empty">{esc(empty_message or "No data.")}</p>'
    head_cells = ''.join(f'<th class="num">{esc(h[1:])}</th>' if h.startswith('#') else f'<th>{esc(h)}</th>' for h in headers)
    return f"""<div class="table-wrap"><table>
      <thead><tr>{head_cells}</tr></thead>
      <tbody>{rows_html}</tbody>
    </table></div>"""


# --- Section builders --------------------------------------------------
def render_topic_gaps(data):
    if not data:
        return '<p class="empty">No FR-01 data yet — run cluster_topics.py.</p>'
    gaps = sorted(data.get('gaps', []), key=lambda g: g['competitor_engagement'], reverse=True)[:10]
    rows = ''.join(f"""
      <tr>
        <td class="cell-primary">{esc(g['label'])}</td>
        <td class="num">{fmt_int(g['own_count'])}</td>
        <td class="num">{fmt_int(g['competitor_count'])}</td>
        <td class="num">{fmt_int(g['competitor_engagement'])}</td>
        <td>{esc(', '.join(g['competitors_covering'][:4]))}</td>
      </tr>""" for g in gaps)
    body = table(['Topic', '#Our posts', '#Competitor posts', '#Competitor engagement', 'Covered by'],
                 rows, 'No topic gaps detected — our coverage is keeping pace with competitors.')
    return panel(body, 'Where competitors are outpacing us', 'Top 10 by competitor engagement') \
        + render_topic_gap_digest(gaps)


SENTENCE_END_RE = re.compile(r'[。！？.!?]')


def first_sentence(text, max_len=80, search_window=140):
    """A raw post's first ~80 chars often cut off mid-clause ("00981A於...")
    - not a title, just a truncated sentence. Prefer stopping at the first
    real sentence-ending punctuation within a slightly wider window (a
    complete short sentence reads better than a longer but grammatically
    cut-off one); fall back to a hard truncation only if the text runs
    long with no punctuation in range."""
    m = SENTENCE_END_RE.search(text[:search_window])
    if m:
        return text[:m.end()]
    return text[:max_len] + ('…' if len(text) > max_len else '')


def render_topic_gap_digest(gaps):
    """A second, card-style view of the same FR-01 gaps table above (not a
    replacement - explicitly requested alongside it), styled after an
    external reference: title / time / keyword tags / bullet-point
    summary / an expand-for-full-text control / every source article
    listed underneath. Unlike the reference's "AI 彙整" (AI-compiled)
    bullets, there's no LLM summarization wired into this codebase (FR-06's
    own summaries are template-assembled from numbers, see
    generate_summaries.py) - each bullet is a real member post's own
    original text (cluster_topics.py's raw_text, uncleaned unlike the
    TF-IDF 'text' field), not a distilled rewrite. Labeled accordingly so
    this doesn't silently overclaim a summarization capability that isn't
    there."""
    cards = []
    for g in gaps:
        samples = g.get('sample_posts') or []
        if not samples:
            continue
        top = samples[0]
        title = first_sentence(top['text'])
        keywords = [k.strip() for k in g['label'].split('/') if k.strip()][:4]
        timestamps = [parse_ts(p['timestamp']) for p in samples if p.get('timestamp')]
        timestamps = [t for t in timestamps if t]
        latest = max(timestamps) if timestamps else None
        bullets = ''.join(
            f'<li>{esc(p["text"][:120])}{"…" if len(p["text"]) > 120 else ""}</li>'
            for p in samples[:5]
        )
        sources = ''.join(f"""
          <div class="gap-source-row">
            <a href="{esc(p['url'])}" target="_blank" rel="noopener noreferrer">{esc(p['text'][:70])}{'…' if len(p['text']) > 70 else ''}</a>
            <span class="muted">— {esc(p['handle'])} ({esc(p['platform'])}){f", {esc(taiwan_str(parse_ts(p['timestamp'])))}" if p.get('timestamp') and parse_ts(p['timestamp']) else ''}</span>
          </div>""" for p in samples)
        cards.append(f"""
        <div class="gap-card">
          <div class="gap-card-title">{esc(title)}</div>
          <div class="gap-card-meta">
            <span class="muted">{len(samples)} 篇來源</span>
            {f'<span class="muted">{esc(taiwan_str(latest))}</span>' if latest else ''}
            {''.join(f'<span class="badge cat">{esc(k)}</span>' for k in keywords)}
            <span class="badge score">{fmt_int(g["competitor_engagement"])}</span>
          </div>
          <ul class="gap-card-bullets">{bullets}</ul>
          <details class="gap-card-expand">
            <summary>展開全文（{len(samples)} 篇）</summary>
            <div class="gap-card-sources">
              <div class="muted" style="margin-top:10px;margin-bottom:4px;">來源文章</div>
              {sources}
            </div>
          </details>
        </div>""")
    if not cards:
        return ''
    return panel(f'<div class="gap-card-grid">{"".join(cards)}</div>', '話題摘要卡', 'Topic Gaps 細分 - 同一份資料的卡片檢視')


def render_rising_topics(data):
    if not data:
        return '<p class="empty">No FR-02 data yet — run fuzzy_trend.py.</p>'
    sections = []
    for platform, pdata in data.get('platforms', {}).items():
        topics = pdata.get('top_rising_topics', [])
        cards = ''.join(f"""
        <div class="rising-card">
          <div class="rising-card-head">
            <span class="badge score">{t['rising_score']}</span>
            <strong>{esc(t['label'])}</strong>
          </div>
          <div class="muted">{esc(t['rationale'])}</div>
          <div class="kols">{''.join(f'<span class="chip">{esc(k["handle"])} <b>{k["rising_score"]}</b></span>' for k in t['rising_kols'][:4])}</div>
        </div>""" for t in topics)
        sections.append(panel(f'<div class="card-grid">{cards}</div>', platform, f'{len(topics)} rising topic(s)'))
    return ''.join(sections)


def render_sentiment(data):
    if not data:
        return '<p class="empty">No FR-03 data yet — run nlp_sentiment.py.</p>'
    w = data['widgets']
    overview = w['sentiment_overview']
    share = overview.get('sentiment_share', {})
    stat_cards = f"""
    <div class="stat-grid">
      <div class="stat"><div class="stat-num">{fmt_int(overview['total_posts'])}</div><div class="stat-label">Posts</div></div>
      <div class="stat pos"><div class="stat-num">{round(share.get('positive', 0) * 100, 1)}%</div><div class="stat-label">Positive</div></div>
      <div class="stat neu"><div class="stat-num">{round(share.get('neutral', 0) * 100, 1)}%</div><div class="stat-label">Neutral</div></div>
      <div class="stat neg"><div class="stat-num">{round(share.get('negative', 0) * 100, 1)}%</div><div class="stat-label">Negative</div></div>
    </div>"""

    heat_rows = ''.join(f"""
      <tr><td class="cell-primary">{esc(b['label'])}</td><td class="num heat-{('hot' if b['heat']>=70 else 'warm' if b['heat']>=40 else 'cold')}">{b['heat']}</td>
      <td class="num">{fmt_int(b['volume'])}</td><td class="num">{fmt_int(b['engagement'])}</td>
      <td>{esc(', '.join(b.get('entities', [])[:4])) or '<span class="muted">—</span>'}</td></tr>"""
      for b in w['temperature_bar'][:10])

    entity_rows = ''.join(f"""
      <tr><td class="cell-primary">{esc(e['entity'])}</td><td class="num">{fmt_int(e['count'])}</td></tr>"""
      for e in w.get('named_entities', [])[:15])

    engagement_rows = ''.join(f"""
      <tr><td class="cell-primary">{esc(r['label'])}</td><td class="num">{fmt_int(r['total_engagement'])}</td><td class="num">{r['post_count']}</td></tr>"""
      for r in w['top_engagement_ranking'][:8])

    slots = w['posting_timeslot_analysis']['slots']
    peak = w['posting_timeslot_analysis']['peak_slot']
    slot_rows = ''.join(f"""
      <tr class="{'peak' if name == peak else ''}"><td class="cell-primary">{esc(name.replace('_', ' ').title())}{' <span class="badge score">peak</span>' if name == peak else ''}</td><td class="num">{s['post_count']}</td>
      <td class="num">{fmt_int(s['likes'])}</td><td class="num">{fmt_int(s['engagement'])}</td></tr>"""
      for name, s in slots.items())

    trend_html = render_trend_curve(w['sentiment_trend_curve'])
    heatmap_html = render_topic_heatmap(w.get('topic_sentiment_heatmap', []), w['sentiment_trend_curve'])

    focus_rows = ''.join(f"""
      <tr><td class="cell-primary">{esc(r['handle'])}</td><td>{esc(r['top_topic_label'])}</td>
      <td class="num">{round(r['focus_share'] * 100, 1)}%</td><td class="num">{fmt_int(r['post_count'])}</td></tr>"""
      for r in sorted(w['coverage_focus_ranking'], key=lambda r: r['focus_share'], reverse=True)[:10])

    keyword_search_html = panel(f"""
    <div class="keyword-search-bar">
      <input type="text" id="keyword-input" placeholder="Search a keyword, e.g. nvidia, tariff, dram..." autocomplete="off">
    </div>
    <div id="keyword-results"><p class="empty">Type a keyword to see mention counts by account and platform, for the currently selected time range.</p></div>
    """, 'Keyword search', 'FR-03-04 / 05 / 06')

    return f"""
    {stat_cards}
    {panel(trend_html, 'Sentiment trend curve', 'Net sentiment & volume over time, with topic call-outs')}
    {panel(heatmap_html, 'Topic × time heatmap', "Per-topic sentiment - a site-wide average can't show A turning negative while B turns positive")}
    {keyword_search_html}
    <div class="col-2">
      {panel(table(['Topic', '#Heat', '#Volume', '#Engagement', 'Top entities'], heat_rows), 'Temperature bar')}
      {panel(table(['Topic', '#Engagement', '#Posts'], engagement_rows), 'Top engagement')}
    </div>
    {panel(table(['Entity', '#Mentions'], entity_rows), 'Named entities', 'NER — most-mentioned people/orgs/products')}
    {panel(table(['Account', 'Top topic', '#Focus share', '#Posts'], focus_rows), 'Coverage focus ranking', "Each account's dominant topic")}
    {panel(table(['Time slot', '#Posts', '#Likes', '#Engagement'], slot_rows), 'Posting time-slot analysis', 'Mon–Fri, peak highlighted')}
    """


# Two-panel layout (2026-08-13, redesigned 2026-08-13 against a supplied
# mockup): a line panel plots net sentiment (positive% - negative%, one
# number instead of three) with a smoothed line + raw line + a green/red
# area fill against the zero baseline, and a bar panel underneath plots
# volume (stacked positive/neutral/negative, toggle between post-count
# and engagement-weighted), sharing the same per-bucket x-slot so a spike
# in one visually lines up with the other. Two separate y-scales stacked
# vertically (never a dual-axis single chart - see the dataviz skill's
# #1 anti-pattern) sharing one x-axis.
TREND_BUCKET_W = 52  # px per bucket column, shared by both panels
TREND_LINE_H = 150   # net-sentiment plot area height
TREND_BAR_H = 60     # volume plot area height
TREND_LABEL_PAD = 46  # headroom above the line panel for peak/trough callout labels
TREND_GAP = 16       # gap between the two panels
TREND_BAR_W = 26


def render_trend_curve(curve):
    """Line panel (smoothed net sentiment + area fill) + bar panel (volume,
    count/engagement toggle), sharing one x-axis.

    A temperature reading with no named driver isn't actionable, so every
    bucket's hover/focus target answers "why": its top 3 topics (by post
    count) and its top 1-2 posts by engagement (from
    nlp_sentiment.widget_sentiment_trend_curve). The chart doesn't wait for
    a hover to say *something*, though - the sharpest peaks/troughs get a
    direct callout naming their #1 topic right on the chart (dataviz
    skill: "never gate a value behind a tooltip" - the full breakdown is
    still hover-only, only the headline driver is always visible), and
    anomalous buckets (volume z-score > 2, or a high negative share at
    real volume - see nlp_sentiment.py) get a marked dot so a reader
    doesn't have to eyeball yesterday's curve from memory to notice one.

    Both bar variants (by post count, by engagement) are pre-rendered as
    sibling <g> layers, each scaled to its OWN max - the mode toggle just
    flips which layer is visible, no client-side math or server round
    trip needed."""
    if not curve:
        return '<p class="empty">Not enough data to plot a trend curve.</p>'

    n = len(curve)
    raw_values = [b['net_sentiment'] for b in curve]
    smoothed_values = [b['net_sentiment_smoothed'] for b in curve]
    domain_min, domain_max = min(raw_values + smoothed_values + [0]), max(raw_values + smoothed_values + [0])
    pad = max(5.0, (domain_max - domain_min) * 0.15)
    domain_min, domain_max = domain_min - pad, domain_max + pad
    if domain_max - domain_min < 1e-6:
        domain_min, domain_max = -10.0, 10.0

    def y_for(v):
        return TREND_LABEL_PAD + TREND_LINE_H - (v - domain_min) / (domain_max - domain_min) * TREND_LINE_H

    def x_for(i):
        return i * TREND_BUCKET_W + TREND_BUCKET_W / 2

    zero_y = y_for(0)
    bar_top = TREND_LABEL_PAD + TREND_LINE_H + TREND_GAP
    total_h = bar_top + TREND_BAR_H + 52  # + two-line date/time label row
    svg_w = n * TREND_BUCKET_W

    # Local extrema on the SMOOTHED line (matches what's actually drawn) -
    # an interior point strictly above/below both neighbors. Ranked by
    # value and capped at 2 of each so the chart doesn't turn into a label
    # pile-up (dataviz skill: "label selectively").
    peaks, troughs = [], []
    for i in range(1, n - 1):
        if smoothed_values[i] > smoothed_values[i - 1] and smoothed_values[i] > smoothed_values[i + 1]:
            peaks.append(i)
        elif smoothed_values[i] < smoothed_values[i - 1] and smoothed_values[i] < smoothed_values[i + 1]:
            troughs.append(i)
    peaks.sort(key=lambda i: smoothed_values[i], reverse=True)
    troughs.sort(key=lambda i: smoothed_values[i])
    top_peaks, top_troughs = set(peaks[:2]), set(troughs[:2])

    def short_label(label, maxlen=14):
        return label if len(label) <= maxlen else label[:maxlen] + '…'

    # --- Area fill under the smoothed line, split at the zero baseline ---
    up_pts = ([f"{x_for(0)},{zero_y}"] + [f"{x_for(i)},{y_for(max(v, 0))}" for i, v in enumerate(smoothed_values)]
              + [f"{x_for(n - 1)},{zero_y}"])
    dn_pts = ([f"{x_for(0)},{zero_y}"] + [f"{x_for(i)},{y_for(min(v, 0))}" for i, v in enumerate(smoothed_values)]
              + [f"{x_for(n - 1)},{zero_y}"])
    area_fill = (f'<polygon points="{" ".join(up_pts)}" fill="var(--status-good)" opacity="0.14"/>'
                 f'<polygon points="{" ".join(dn_pts)}" fill="var(--status-critical)" opacity="0.14"/>')

    raw_line = ('<polyline points="' + ' '.join(f"{x_for(i)},{y_for(v)}" for i, v in enumerate(raw_values))
                + '" class="trend-line-raw"/>')
    smoothed_line = ('<polyline points="' + ' '.join(f"{x_for(i)},{y_for(v)}" for i, v in enumerate(smoothed_values))
                      + '" class="trend-line"/>')

    # --- Anomaly dots, on the smoothed line ---
    anomaly_dots = ''.join(
        f'<circle cx="{x_for(i)}" cy="{y_for(smoothed_values[i])}" r="5" class="trend-anomaly-dot">'
        f'<title>Anomalous bucket - volume or negative-share well above this window\'s own baseline</title></circle>'
        for i, b in enumerate(curve) if b.get('is_anomaly')
    )

    # --- Callout labels on the sharpest peaks/troughs (plain text, no
    # box - a filled/bordered box here reads as a heavy chunk sitting on
    # top of an otherwise delicate line) ---
    callouts = []
    for i in top_peaks | top_troughs:
        if not curve[i]['top_topics']:
            continue
        label = short_label(curve[i]['top_topics'][0]['label'])
        x, y = x_for(i), y_for(smoothed_values[i])
        text_y = (y - 10) if i in top_peaks else (y + 20)
        callouts.append(f'<text x="{x}" y="{text_y}" text-anchor="middle" class="trend-callout">{esc(label)}</text>')

    # --- Bars: two independently-scaled variants (post count / engagement) ---
    vol_max = max((b['post_count'] for b in curve), default=0) or 1
    eng_max = max((b['engagement'] for b in curve), default=0) or 1
    bars_count, bars_eng = [], []
    for i, b in enumerate(curve):
        x = x_for(i)
        for target, total, pos, neu, neg, scale_max in (
            (bars_count, b['post_count'], b['positive'], b['neutral'], b['negative'], vol_max),
            (bars_eng, b['engagement'], b['positive_engagement'], b['neutral_engagement'], b['negative_engagement'], eng_max),
        ):
            if total <= 0:
                continue
            bar_h_total = (total / scale_max) * TREND_BAR_H
            pos_h = bar_h_total * pos / total
            neu_h = bar_h_total * neu / total
            neg_h = bar_h_total * neg / total
            y = bar_top + TREND_BAR_H
            y -= pos_h
            target.append(f'<rect x="{x - TREND_BAR_W / 2}" y="{y}" width="{TREND_BAR_W}" height="{pos_h}" fill="var(--status-good)"/>')
            y -= neu_h
            target.append(f'<rect x="{x - TREND_BAR_W / 2}" y="{y}" width="{TREND_BAR_W}" height="{neu_h}" fill="var(--muted-dim)"/>')
            y -= neg_h
            target.append(f'<rect x="{x - TREND_BAR_W / 2}" y="{y}" width="{TREND_BAR_W}" height="{neg_h}" fill="var(--status-critical)"/>')

    # --- Hit targets + date labels (unchanged interaction pattern) ---
    hit_areas = []
    for i, b in enumerate(curve):
        x = x_for(i)
        bucket_end_tw = datetime.fromisoformat(b['bucket_end']).astimezone(TAIWAN_TZ)
        payload = json.dumps({
            'date': bucket_end_tw.strftime('%b %d, %H:%M') + ' TW',
            'net': b['net_sentiment'], 'post_count': b['post_count'], 'engagement': b['engagement'],
            'positive': b['positive'], 'neutral': b['neutral'], 'negative': b['negative'],
            'is_anomaly': b.get('is_anomaly', False),
            'topics': b['top_topics'], 'posts': b['top_posts'],
        }, ensure_ascii=False)
        hit_areas.append(
            f'<rect class="trend-hit" x="{x - TREND_BUCKET_W / 2}" y="0" width="{TREND_BUCKET_W}" height="{total_h}" '
            f'fill="transparent" tabindex="0" data-payload="{esc(payload)}"/>')
        # Two stacked lines (date / time), not one - at TREND_BUCKET_W's
        # column width a single "8/12 14:13" line collides with its
        # neighbors (measured, not assumed - the first version of this
        # chart shipped that overlap). Two shorter lines each fit.
        hit_areas.append(
            f'<text x="{x}" y="{total_h - 18}" text-anchor="middle" class="trend-date-label">'
            f'{bucket_end_tw.strftime("%-m/%-d")}</text>'
            f'<text x="{x}" y="{total_h - 4}" text-anchor="middle" class="trend-date-label">'
            f'{bucket_end_tw.strftime("%H:%M")}</text>')

    top_val, bot_val = round(domain_max), round(domain_min)
    svg = f"""
    <svg class="trend-svg" viewBox="0 0 {svg_w} {total_h}" width="{svg_w}" height="{total_h}" preserveAspectRatio="xMinYMin meet">
      <line x1="0" y1="{y_for(top_val)}" x2="{svg_w}" y2="{y_for(top_val)}" class="trend-grid-line"/>
      <text x="4" y="{y_for(top_val) - 4}" class="trend-axis-label">{top_val:+d}</text>
      <line x1="0" y1="{zero_y}" x2="{svg_w}" y2="{zero_y}" class="trend-zero-line"/>
      <text x="4" y="{zero_y - 4}" class="trend-axis-label">0</text>
      <line x1="0" y1="{y_for(bot_val)}" x2="{svg_w}" y2="{y_for(bot_val)}" class="trend-grid-line"/>
      <text x="4" y="{y_for(bot_val) - 4}" class="trend-axis-label">{bot_val:+d}</text>
      {area_fill}
      <g class="trend-bars-count">{''.join(bars_count)}</g>
      <g class="trend-bars-eng" style="display:none">{''.join(bars_eng)}</g>
      {raw_line}
      {smoothed_line}
      {anomaly_dots}
      {''.join(callouts)}
      {''.join(hit_areas)}
      <text x="4" y="{bar_top - 6}" text-anchor="start" class="trend-axis-label trend-bar-axis-label-count">Volume (posts)</text>
      <text x="4" y="{bar_top - 6}" text-anchor="start" class="trend-axis-label trend-bar-axis-label-eng" style="display:none">Volume (engagement)</text>
    </svg>"""

    return f"""
    <div class="trend-controls">
      <div class="seg trend-mode-seg">
        <button class="on" data-mode="count">By post count</button>
        <button data-mode="eng">By engagement</button>
      </div>
    </div>
    <div class="trend-legend">
      <span><span class="legend-line" style="background:var(--text)"></span>Net sentiment (smoothed, 5-pt avg)</span>
      <span><span class="legend-dot" style="background:var(--status-good)"></span>Positive</span>
      <span><span class="legend-dot" style="background:var(--muted-dim)"></span>Neutral</span>
      <span><span class="legend-dot" style="background:var(--status-critical)"></span>Negative</span>
      <span><span class="legend-dot" style="background:var(--gold)"></span>Anomalous bucket</span>
      <span class="muted">Labeled peaks/troughs show the #1 topic driving that swing &middot; hover/focus any column for the top 3 topics + representative posts</span>
    </div>
    <div class="trend-chart-wrap"><div class="trend-chart">{svg}</div></div>"""


HEATMAP_ROW_H = 32
HEATMAP_LABEL_W = 190
HEATMAP_CELL_GAP = 2


def render_topic_heatmap(rows, curve):
    """Topic x Time heatmap (2026-08-13) - the main trend curve nets every
    topic's sentiment together, so "topic A turned negative while topic B
    turned positive" cancels into a flat line above. Splits it back apart:
    one row per top topic, one column per bucket (same x-axis as the main
    curve - shares nlp_sentiment.bucket_bounds()), colored by that cell's
    own net sentiment (green/red) with opacity carrying volume, so a
    reader sees at a glance WHICH topic actually moved even when the
    site-wide average didn't. Cell detail is a native SVG <title> tooltip
    (matches the supplied mockup's own choice) rather than the richer
    JS popover the main curve uses - a single number + volume doesn't
    need it."""
    if not rows or not curve:
        return '<p class="empty">Not enough data to plot a topic heatmap.</p>'

    n = len(curve)
    max_vol = max((c['volume'] for r in rows for c in r['cells']), default=0) or 1

    def x_for(i):
        return HEATMAP_LABEL_W + i * TREND_BUCKET_W

    cells, row_labels = [], []
    for r_idx, row in enumerate(rows):
        y = r_idx * HEATMAP_ROW_H
        label = row['label'] if len(row['label']) <= 22 else row['label'][:22] + '…'
        row_labels.append(f'<text x="{HEATMAP_LABEL_W - 12}" y="{y + HEATMAP_ROW_H / 2 + 4}" '
                           f'text-anchor="end" class="heatmap-row-label">{esc(label)}</text>')
        for i, cell in enumerate(row['cells']):
            vol, net = cell['volume'], cell['net_sentiment']
            opacity = 0.06 if vol == 0 else min(vol / max_vol, 1.0) * 0.82 + 0.12
            color = 'var(--status-good)' if net >= 0 else 'var(--status-critical)'
            bucket_end_tw = datetime.fromisoformat(curve[i]['bucket_end']).astimezone(TAIWAN_TZ)
            title = (f"{row['label']} · {bucket_end_tw.strftime('%b %d, %H:%M')} TW\n"
                     f"Net sentiment {net:+.0f} · {vol} post(s)")
            cells.append(
                f'<rect x="{x_for(i)}" y="{y + HEATMAP_CELL_GAP}" width="{TREND_BUCKET_W - HEATMAP_CELL_GAP}" '
                f'height="{HEATMAP_ROW_H - HEATMAP_CELL_GAP * 2}" fill="{color}" opacity="{round(opacity, 2)}">'
                f'<title>{esc(title)}</title></rect>')

    grid_h = len(rows) * HEATMAP_ROW_H
    total_h = grid_h + 52
    date_labels = []
    for i, b in enumerate(curve):
        x = x_for(i) + (TREND_BUCKET_W - HEATMAP_CELL_GAP) / 2
        bucket_end_tw = datetime.fromisoformat(b['bucket_end']).astimezone(TAIWAN_TZ)
        date_labels.append(
            f'<text x="{x}" y="{grid_h + 18}" text-anchor="middle" class="trend-date-label">'
            f'{bucket_end_tw.strftime("%-m/%-d")}</text>'
            f'<text x="{x}" y="{grid_h + 32}" text-anchor="middle" class="trend-date-label">'
            f'{bucket_end_tw.strftime("%H:%M")}</text>')

    svg_w = HEATMAP_LABEL_W + n * TREND_BUCKET_W
    svg = f"""
    <svg class="trend-svg heatmap-svg" viewBox="0 0 {svg_w} {total_h}" width="{svg_w}" height="{total_h}" preserveAspectRatio="xMinYMin meet">
      {''.join(cells)}
      {''.join(row_labels)}
      {''.join(date_labels)}
    </svg>"""
    return f"""
    <div class="trend-legend">
      <span><span class="legend-dot" style="background:var(--status-good)"></span>Net sentiment positive</span>
      <span><span class="legend-dot" style="background:var(--status-critical)"></span>Net sentiment negative</span>
      <span class="muted">Shade = volume &middot; hover any cell for the exact value</span>
    </div>
    <div class="trend-chart-wrap"><div class="trend-chart">{svg}</div></div>"""


def render_summaries(data):
    if not data:
        return '<p class="empty">No FR-06 data yet — run generate_summaries.py.</p>'
    cards = ''.join(f"""
      <div class="summary-card">
        <div class="summary-card-head"><span class="badge cat">{esc(s['category'].replace('_', ' '))}</span><span class="char-count">{s['char_count']} chars</span></div>
        <p>{esc(s['text'])}</p>
      </div>""" for s in data.get('summaries', []))
    generated_tw = datetime.fromisoformat(data['generated_at']).astimezone(TAIWAN_TZ)
    generated_label = generated_tw.strftime('%-I:%M %p')
    return panel(f"<div class='summary-grid'>{cards}</div>", 'Today’s summaries', f"Generated {generated_label}")


def render_accounts(data):
    if not data:
        return '<p class="empty">No FR-05 data yet — run account_comment_management.py build.</p>'
    rows = ''.join(f"""
      <tr>
        <td class="cell-primary"><a href="{esc(account_profile_url(a['platform'], a['handle']))}" target="_blank" rel="noopener noreferrer">{esc(a['handle'])}</a>{' <span class="badge own">own</span>' if a['is_own'] else ''}</td>
        <td>{esc(a['platform'])}</td>
        <td><span class="badge status-{esc(a['status'])}">{esc(a['status'])}</span></td>
        <td class="num">{fmt_int(a['post_count'])}</td>
        <td>{fmt_dt(a['last_post_at'])}</td>
        <td><button class="remove-account-btn" data-platform="{esc(a['platform'])}" data-handle="{esc(a['handle'])}">Remove</button></td>
      </tr>""" for a in data.get('accounts', []))
    body = table(['Handle', 'Platform', 'Status', '#Posts', 'Last post (TWN time)', ''], rows)
    body += '<p class="muted add-account-hint">"Remove" opens a GitHub issue for review - tracking stops once it\'s approved and run locally.</p>'
    accounts_panel = panel(body, 'Tracked accounts', f"{len(data.get('accounts', []))} accounts")

    # This is a static site with no backend to add an account and start
    # scraping on the spot - the request form instead opens a pre-filled
    # GitHub issue (no credentials needed client-side, just a normal issue
    # creation link) that elainekao reviews and approves locally by running
    # add_account.py, which registers the account and kicks off a one-off
    # scrape + pipeline run for it.
    request_panel = panel(f"""
    <div class="add-account-form">
      <label>Platform
        <select id="add-account-platform">
          <option value="X">X (Twitter)</option>
          <option value="Facebook">Facebook</option>
          <option value="LinkedIn">LinkedIn</option>
        </select>
      </label>
      <label>Handle
        <input type="text" id="add-account-handle" placeholder="e.g. some_competitor" autocomplete="off">
      </label>
      <button class="btn" id="add-account-btn">Request tracking</button>
    </div>
    <p class="muted add-account-hint">Opens a GitHub issue for review - tracking starts once it's approved and run locally.</p>
    """, 'Request a new account to track', 'FR-05')

    return accounts_panel + request_panel


def main():
    os.makedirs(DOCS_DIR, exist_ok=True)

    gaps_html_by_range = {}
    rising_html_by_range = {}
    sentiment_html_by_range = {}
    window_caption_by_range = {}
    window_bounds_by_range = {}
    window_by_range = {}
    available_ranges = []
    for range_key in RANGE_ORDER:
        topic_clusters = load(f'topic_clusters_{range_key}.json')
        fuzzy_trends = load(f'fuzzy_trends_{range_key}.json')
        sentiment_dashboard = load(f'sentiment_dashboard_{range_key}.json')
        if topic_clusters or fuzzy_trends or sentiment_dashboard:
            available_ranges.append(range_key)
        gaps_html_by_range[range_key] = render_topic_gaps(topic_clusters)
        rising_html_by_range[range_key] = render_rising_topics(fuzzy_trends)
        sentiment_html_by_range[range_key] = render_sentiment(sentiment_dashboard)

        # All three scripts anchor "now" to the latest *scraped post*, not
        # wall-clock time, so the window is spelled out explicitly here -
        # "last 4 hours" without a stated end time reads as "as of right
        # now," which it usually isn't.
        window = next((d.get('window') for d in (topic_clusters, fuzzy_trends, sentiment_dashboard)
                       if d and d.get('window')), None)
        window_by_range[range_key] = window
        window_bounds_by_range[range_key] = (
            {'start': window['start_utc'], 'end': window['end_utc']} if window else None
        )

    # A range's window can lag behind the others: each of the three source
    # scripts skips writing its file for a range when that window doesn't
    # clear MIN_WINDOW_POSTS, leaving the last successful (older) result on
    # disk rather than an empty/misleading one. That's the right call for
    # the *data* (a stale-but-real result beats no result), but left silent
    # it reads as a time-math bug when a shorter range's caption shows an
    # earlier end time than a longer range's. Flag it explicitly instead,
    # with the actual post count so it's clear why.
    parsed_ends = [datetime.fromisoformat(w['end_utc']) for w in window_by_range.values() if w]
    freshest_end = max(parsed_ends) if parsed_ends else None
    posts_for_staleness_check = None

    for range_key in RANGE_ORDER:
        window = window_by_range[range_key]
        if not window:
            window_caption_by_range[range_key] = 'No window data available for this range.'
            continue
        caption = f"Data window: {esc(window['start_tw'])} – {esc(window['end_tw'])} (Taiwan time)"
        window_end = datetime.fromisoformat(window['end_utc'])
        if freshest_end and (freshest_end - window_end).total_seconds() > 60:
            if posts_for_staleness_check is None:
                posts_for_staleness_check = load_posts()
                for p in posts_for_staleness_check:
                    p['_ts'] = parse_ts(p['timestamp'])
            current_start, current_end = window_bounds(range_key, freshest_end)
            current_count = sum(1 for p in posts_for_staleness_check
                                 if p['_ts'] and current_start <= p['_ts'] <= current_end)
            caption += (
                f" — showing the last window with enough data; the most recent "
                f"{format_window(RANGE_HOURS[range_key])} only has {current_count} "
                f"post{'s' if current_count != 1 else ''} (needs {MIN_WINDOW_POSTS})."
            )
        window_caption_by_range[range_key] = caption

    keyword_index = load('keyword_index.json') or []

    default_range = DEFAULT_DASHBOARD_RANGE if DEFAULT_DASHBOARD_RANGE in available_ranges else (
        available_ranges[0] if available_ranges else RANGE_ORDER[0])

    daily_summaries = load('daily_summaries.json')
    account_status = load('account_status.json')
    video_ranking = load('video_ranking.json') or {}

    now_tw = datetime.now(TAIWAN_TZ).strftime('%B %d, %Y %H:%M Taiwan Time')

    range_options = ''.join(
        f'<option value="{r}"{" selected" if r == default_range else ""}>{esc(RANGE_LABELS[r])}</option>'
        for r in RANGE_ORDER)
    range_data_json = json.dumps({
        'gaps': gaps_html_by_range,
        'rising': rising_html_by_range,
        'sentiment': sentiment_html_by_range,
    }, ensure_ascii=False)
    window_caption_json = json.dumps(window_caption_by_range, ensure_ascii=False)
    window_bounds_json = json.dumps(window_bounds_by_range, ensure_ascii=False)
    keyword_index_json = json.dumps(keyword_index, ensure_ascii=False)
    video_ranking_json = json.dumps(
        {k: v for k, v in video_ranking.items() if not k.startswith('_')}, ensure_ascii=False)
    vader_lexicon_json = json.dumps(load_vader_lexicon())
    zh_pos_words, zh_neg_words, zh_t2s_map = load_chinese_sentiment_data()
    zh_pos_words_json = json.dumps(zh_pos_words, ensure_ascii=False)
    zh_neg_words_json = json.dumps(zh_neg_words, ensure_ascii=False)
    zh_t2s_map_json = json.dumps(zh_t2s_map, ensure_ascii=False)

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrendForceDash</title>
<link rel="icon" href="data:image/svg+xml,{FAVICON_SVG}">
<style>
  :root {{
    --bg: #f4f6f9; --bg-grad: radial-gradient(ellipse 1200px 600px at 50% -10%, rgba(47,125,225,0.05), transparent);
    --surface: #ffffff; --surface-2: #eef1f5; --border: #dde3ea; --border-soft: #e8ecf1;
    --text: #1a2331; --muted: #5c6b80; --muted-dim: #8a97a8;
    --blue: #2f7de1; --blue-dim: rgba(47,125,225,0.09);
    --gold: #a5720f; --green: #1f8a4c; --red: #d1373b; --yellow: #a3720a;
    --status-good: #0ca30c; --status-critical: #d03b3b;
    --radius: 0px; --radius-sm: 0px;
    --shadow: 0 1px 2px rgba(15,23,42,0.06), 0 8px 24px -8px rgba(15,23,42,0.12);
  }}
  * {{ box-sizing: border-box; }}
  a {{ color: var(--blue); }}
  a:visited {{ color: var(--blue); }}
  body {{
    background: var(--bg-grad), var(--bg); background-attachment: fixed;
    color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; -webkit-font-smoothing: antialiased;
  }}
  header {{
    padding: 34px 32px 26px; position: relative;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }}
  header::before {{
    content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
    background: var(--blue);
  }}
  header h1 {{ margin: 0; font-size: 30px; font-weight: 800; letter-spacing: -0.02em; }}
  header .muted {{ margin-top: 7px; font-size: 14px; }}
  .muted {{ color: var(--muted); font-size: 14px; }}
  nav {{
    display: flex; gap: 2px; padding: 0 28px; border-bottom: 1px solid var(--border);
    overflow-x: auto; position: sticky; top: 0; background: rgba(255,255,255,0.92);
    backdrop-filter: blur(10px); z-index: 10;
  }}
  nav button {{
    background: none; border: none; color: var(--muted); padding: 13px 16px; font-size: 14px;
    font-weight: 500; cursor: pointer; border-bottom: 2px solid transparent; white-space: nowrap;
    transition: color 0.15s ease;
  }}
  nav button:hover {{ color: var(--text); }}
  nav button.active {{ color: var(--text); border-bottom-color: var(--blue); font-weight: 600; }}
  main {{ padding: 28px 32px 64px; max-width: 1180px; margin: 0 auto; }}
  .range-bar {{
    display: flex; align-items: center; gap: 12px; margin-bottom: 24px; flex-wrap: wrap;
    background: var(--surface); border: 1px solid var(--border); border-radius: 0;
    padding: 10px 14px;
  }}
  .range-bar label {{ color: var(--muted); font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }}
  .range-bar select {{
    background: var(--surface-2); color: var(--text); border: 1px solid var(--border);
    border-radius: 0; padding: 6px 10px; font-size: 14px; cursor: pointer;
  }}
  .keyword-search-bar input {{
    width: 100%; max-width: 460px; background: var(--surface-2); color: var(--text);
    border: 1px solid var(--border); border-radius: 0; padding: 10px 14px; font-size: 14px;
    transition: border-color 0.15s ease;
  }}
  .keyword-search-bar input::placeholder {{ color: var(--muted-dim); }}
  .keyword-search-bar input:focus {{ outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-dim); }}
  .video-region-filter {{ display: inline-flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-left: 16px; }}
  .video-region-filter label {{
    display: inline-flex; align-items: center; gap: 6px; font-size: 14px; color: var(--muted);
    cursor: pointer; white-space: nowrap; flex-shrink: 0;
  }}
  .video-region-filter input {{
    cursor: pointer; flex-shrink: 0; margin: 0;
    width: 14px; height: 14px; min-width: 14px; min-height: 14px;
  }}
  .upload-row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }}
  .upload-row label {{ color: var(--muted); font-size: 14px; }}
  .upload-row input[type="file"] {{
    color: var(--text); font-size: 14px; background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 0; padding: 8px 10px;
  }}
  .upload-row select {{
    background: var(--surface-2); color: var(--text); border: 1px solid var(--border);
    border-radius: 0; padding: 6px 10px; font-size: 14px;
  }}
  .download-row {{ display: flex; gap: 10px; margin-top: 14px; }}
  .upload-posts-toolbar {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 14px; margin: 16px 0 10px; }}
  .upload-posts-toolbar label {{
    display: flex; align-items: center; gap: 8px; font-size: 14px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.04em;
  }}
  .upload-posts-toolbar select, .upload-posts-toolbar input[type="text"] {{
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 0;
    color: var(--text); padding: 7px 10px; font-size: 14px;
  }}
  .upload-posts-toolbar input[type="text"]:focus, .upload-posts-toolbar select:focus {{
    outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-dim);
  }}
  .upload-search-label {{ flex: 1; min-width: 220px; }}
  .upload-search-label input {{ flex: 1; min-width: 180px; }}
  .pager {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }}
  .page-btn {{
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 0;
    color: var(--text); font-size: 14px; padding: 5px 11px; cursor: pointer;
    font-variant-numeric: tabular-nums; transition: background 0.1s ease, border-color 0.1s ease;
  }}
  .page-btn:hover {{ border-color: var(--blue); }}
  .page-btn.active {{ background: var(--blue); border-color: var(--blue); color: var(--surface); font-weight: 600; }}
  .btn {{
    background: var(--blue-dim); color: var(--blue); border: 1px solid transparent; border-radius: 0;
    padding: 8px 14px; font-size: 14px; font-weight: 600; cursor: pointer;
  }}
  .btn:hover {{ filter: brightness(1.15); }}
  section {{ display: none; }}
  section.active {{ display: block; animation: fadein 0.2s ease; }}
  @keyframes fadein {{ from {{ opacity: 0; transform: translateY(2px); }} to {{ opacity: 1; transform: translateY(0); }} }}
  h2 {{ font-size: 22px; font-weight: 700; margin: 0 0 22px; text-align: center; letter-spacing: -0.01em; }}
  h3 {{ font-size: 18px; font-weight: 600; color: var(--text); margin: 22px 0 12px; }}
  h3:first-child {{ margin-top: 0; }}
  .panel {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 0;
    padding: 18px 20px; margin-bottom: 18px; box-shadow: var(--shadow);
  }}
  .panel-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }}
  .panel-head h3 {{ margin: 0; }}
  .panel-eyebrow {{ color: var(--muted); font-size: 14px; }}
  .table-wrap {{ overflow-x: auto; margin: -4px -4px -2px; }}
  table {{ width: 100%; min-width: 480px; border-collapse: collapse; font-size: 14px; }}
  th, td {{ text-align: left; padding: 9px 10px; }}
  th {{
    color: var(--muted); font-weight: 600; font-size: 14px; text-transform: uppercase;
    letter-spacing: 0.04em; border-bottom: 1px solid var(--border); padding-bottom: 10px;
  }}
  td {{ border-bottom: 1px solid var(--border-soft); }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr {{ transition: background 0.1s ease; }}
  tbody tr:hover {{ background: var(--surface-2); }}
  td.num, th.num {{ text-align: center; font-variant-numeric: tabular-nums; }}
  td.cell-primary {{ font-weight: 600; }}
  tr.peak {{ background: var(--blue-dim); }}
  tr.peak:hover {{ background: var(--blue-dim); }}
  tr.kw-link-row {{ cursor: pointer; }}
  tr.kw-link-row:hover, tr.kw-link-row:focus {{ background: var(--blue-dim); outline: none; }}
  .kw-link-popover {{
    position: absolute; z-index: 30; background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 0; padding: 10px 14px; box-shadow: var(--shadow); max-width: 420px;
    max-height: 260px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;
    animation: popover-in 0.12s ease-out;
  }}
  @keyframes popover-in {{ from {{ opacity: 0; transform: translateY(-4px); }} to {{ opacity: 1; transform: translateY(0); }} }}
  @media (prefers-reduced-motion: reduce) {{ .kw-link-popover {{ animation: none; }} }}
  .kw-link-popover a {{
    color: var(--blue); font-size: 14px; line-height: 1.5; text-decoration: none;
    word-break: break-all; white-space: normal;
  }}
  .kw-link-popover a:hover {{ text-decoration: underline; }}
  .kw-link-popover .empty {{ font-size: 14px; color: var(--muted); margin: 0; }}
  /* Dark, compact tooltip (2026-08-13) - the chart's own popover used to
     share .kw-link-popover's light gray/white box, which at that size
     read as a heavy chunk sitting on top of a delicate line chart.
     Narrower, darker, tighter padding; own animation since it isn't
     .kw-link-popover anymore. */
  .trend-tooltip {{
    position: absolute; z-index: 30; background: var(--text); color: var(--surface);
    padding: 10px 14px; box-shadow: var(--shadow); max-width: 300px;
    max-height: 260px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;
    animation: popover-in 0.12s ease-out;
  }}
  @media (prefers-reduced-motion: reduce) {{ .trend-tooltip {{ animation: none; }} }}
  .trend-point-popover-head {{ font-size: 14px; font-weight: 700; color: var(--surface); }}
  .trend-point-popover-stats {{ font-size: 14px; color: var(--muted-dim); margin-bottom: 4px; }}
  .trend-point-topic-row {{ display: flex; justify-content: space-between; gap: 10px; font-size: 14px; color: var(--surface); }}
  .trend-point-topic-row span:last-child {{ color: var(--muted-dim); font-variant-numeric: tabular-nums; }}
  .trend-point-post {{
    border-top: 1px solid rgba(255,255,255,0.18); padding-top: 6px; margin-top: 4px;
    font-size: 14px; line-height: 1.5; color: var(--surface);
  }}
  .trend-point-post a {{ color: #8ec9ff; text-decoration: none; }}
  .trend-point-post a:hover {{ text-decoration: underline; }}
  .trend-point-post .muted {{ font-size: 14px; color: var(--muted-dim); }}
  .trend-tooltip .empty {{ font-size: 14px; color: var(--muted-dim); margin: 0; }}
  .add-account-form {{ display: flex; flex-wrap: wrap; align-items: end; gap: 14px; }}
  .add-account-form label {{
    display: flex; flex-direction: column; gap: 6px; font-size: 14px;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em;
  }}
  .add-account-form select, .add-account-form input {{
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 0;
    color: var(--text); padding: 9px 12px; font-size: 14px; min-width: 220px;
  }}
  .add-account-form input:focus, .add-account-form select:focus {{
    outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-dim);
  }}
  .add-account-hint {{ margin-top: 10px; font-size: 14px; }}
  .remove-account-btn {{
    background: transparent; border: 1px solid var(--border); border-radius: 0;
    color: var(--red); font-size: 14px; padding: 5px 10px; cursor: pointer;
    transition: background 0.1s ease, border-color 0.1s ease;
  }}
  .remove-account-btn:hover {{ background: rgba(248,81,73,0.16); border-color: var(--red); }}
  .remove-account-btn.confirming {{
    background: var(--red); border-color: var(--red); color: var(--surface); font-weight: 600;
  }}
  .remove-account-btn.confirming:hover {{ background: var(--red); }}
  .empty {{ color: var(--muted); font-style: italic; font-size: 14px; padding: 8px 2px; }}
  .col-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; }}
  .col-2 > .panel {{ margin-bottom: 0; }}
  .card-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }}
  .rising-card {{
    background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 0;
    padding: 12px 14px; transition: border-color 0.15s ease;
  }}
  .rising-card:hover {{ border-color: var(--border); }}
  .rising-card-head {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
  .rising-card-head strong {{ font-size: 18px; line-height: 1.35; }}
  .kols {{ margin-top: 10px; display: flex; flex-wrap: wrap; gap: 5px; }}
  .chip {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 0;
    padding: 3px 9px; font-size: 14px; color: var(--muted);
  }}
  .chip b {{ color: var(--text); font-weight: 600; }}
  .chip-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }}
  .badge {{ display: inline-block; padding: 3px 9px; border-radius: 0; font-size: 14px; font-weight: 700; letter-spacing: 0.02em; }}
  .badge.score {{ background: rgba(240,180,41,0.16); color: var(--gold); }}
  .badge.cat {{ background: var(--blue-dim); color: var(--blue); text-transform: capitalize; }}
  .badge.own {{ background: rgba(63,185,104,0.16); color: var(--green); }}
  .badge.status-active, .badge.status-sent, .badge.status-approved {{ background: rgba(63,185,104,0.16); color: var(--green); }}
  .badge.status-stale, .badge.status-drafted, .badge.status-pending {{ background: rgba(210,153,34,0.18); color: var(--yellow); }}
  .badge.status-inactive, .badge.status-dismissed {{ background: rgba(248,81,73,0.16); color: var(--red); }}
  .gap-card-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }}
  .gap-card {{
    background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 0;
    padding: 14px 16px;
  }}
  .gap-card:hover {{ border-color: var(--border); }}
  .gap-card-title {{ font-size: 18px; font-weight: 600; line-height: 1.4; margin-bottom: 8px; }}
  .gap-card-meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 14px; margin-bottom: 10px; }}
  .gap-card-bullets {{ margin: 0 0 8px; padding-left: 18px; display: flex; flex-direction: column; gap: 5px; }}
  .gap-card-bullets li {{ font-size: 14px; line-height: 1.5; color: var(--text); }}
  .gap-card-expand {{ font-size: 14px; }}
  .gap-card-expand summary {{ cursor: pointer; color: var(--blue); font-weight: 600; user-select: none; }}
  .gap-card-expand summary::-webkit-details-marker {{ color: var(--blue); }}
  .gap-source-row {{ display: flex; flex-direction: column; gap: 1px; padding: 6px 0; border-bottom: 1px solid var(--border-soft); }}
  .gap-source-row:last-child {{ border-bottom: none; }}
  .gap-source-row a {{ font-size: 14px; }}
  .gap-source-row .muted {{ font-size: 14px; }}
  .heat-hot {{ color: var(--red); font-weight: 700; }}
  .heat-warm {{ color: var(--yellow); font-weight: 600; }}
  .heat-cold {{ color: var(--muted); }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }}
  .stat {{
    background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 0;
    padding: 14px 12px; text-align: center;
  }}
  .stat-num {{ font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }}
  .stat-label {{ color: var(--muted); font-size: 14px; text-transform: uppercase; letter-spacing: 0.03em; margin-top: 3px; }}
  .stat.pos .stat-num {{ color: var(--green); }} .stat.neg .stat-num {{ color: var(--red); }} .stat.neu .stat-num {{ color: var(--muted); }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 12px; }}
  .summary-card {{
    background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 0; padding: 14px 16px;
  }}
  .summary-card-head {{ display: flex; align-items: center; justify-content: space-between; }}
  .summary-card p {{ margin: 10px 0 0; font-size: 14px; line-height: 1.55; }}
  .char-count {{ color: var(--muted-dim); font-size: 14px; }}
  .trend-legend {{ display: flex; flex-wrap: wrap; gap: 16px; font-size: 14px; color: var(--muted); margin-bottom: 14px; align-items: center; }}
  .legend-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 0; margin-right: 5px; vertical-align: middle; }}
  .legend-line {{ display: inline-block; width: 14px; height: 2px; margin-right: 5px; vertical-align: middle; }}
  .trend-controls {{ margin-bottom: 12px; }}
  .seg {{ display: inline-flex; border: 1px solid var(--border); overflow: hidden; }}
  .seg button {{
    border: 0; background: transparent; font-family: inherit; font-size: 14px;
    padding: 6px 14px; cursor: pointer; color: var(--muted);
  }}
  .seg button.on {{ background: var(--text); color: var(--surface); }}
  .trend-chart-wrap {{
    overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0 -4px; padding: 0 4px;
  }}
  .trend-chart {{ display: block; }}
  .trend-svg {{ display: block; }}
  .trend-zero-line {{ stroke: var(--border); stroke-width: 1.4; }}
  .trend-grid-line {{ stroke: var(--border-soft); stroke-width: 1; stroke-dasharray: 2 4; }}
  .trend-axis-label {{ font-size: 14px; fill: var(--muted); }}
  .trend-line {{ fill: none; stroke: var(--text); stroke-width: 2.2; stroke-linejoin: round; stroke-linecap: round; }}
  .trend-line-raw {{ fill: none; stroke: var(--muted-dim); stroke-width: 1; opacity: 0.6; }}
  .trend-anomaly-dot {{ fill: var(--gold); stroke: var(--surface); stroke-width: 1.6; }}
  .trend-callout {{ font-size: 14px; fill: var(--blue); font-weight: 600; }}
  .trend-date-label {{ font-size: 14px; fill: var(--muted); }}
  .trend-hit {{ cursor: pointer; }}
  .trend-hit:hover, .trend-hit:focus {{ fill: var(--surface-2); outline: none; }}
  .heatmap-row-label {{ font-size: 14px; fill: var(--text); }}
  @media (max-width: 800px) {{
    header, nav, main {{ padding-left: 18px; padding-right: 18px; }}
    .col-2 {{ grid-template-columns: 1fr; }}
    .panel {{ padding: 14px 16px; }}
  }}
  @media print {{
    nav, .range-bar, .upload-row, .download-row {{ display: none !important; }}
    body {{ background: #fff; color: #111; }}
    .panel {{ background: #fff; border: 1px solid #ccc; box-shadow: none; break-inside: avoid; }}
    th, .badge, .stat-label {{ color: #444 !important; }}
    .badge {{ background: #eee !important; }}
  }}
</style>
<!-- SheetJS Community Edition (Apache-2.0), vendored locally - see docs/vendor/README.md.
     Powers FR-07's Excel upload/export entirely client-side (no backend on this static site). -->
<script src="vendor/xlsx.full.min.js"></script>
</head>
<body>
<header>
  <h1>TrendForceDash</h1>
  <div class="muted">Last updated {esc(now_tw)}</div>
</header>
<nav>
  <button class="tab-btn active" data-tab="gaps">Topic Gaps</button>
  <button class="tab-btn" data-tab="rising">Rising Trends</button>
  <button class="tab-btn" data-tab="sentiment">Sentiment</button>
  <button class="tab-btn" data-tab="competitor">Competitor Watch</button>
  <button class="tab-btn" data-tab="video-ranking">X Video Ranking</button>
  <button class="tab-btn" data-tab="accounts">Accounts</button>
  <button class="tab-btn" data-tab="summaries">Daily Summaries</button>
  <button class="tab-btn" data-tab="selfservice">Self-service Upload</button>
</nav>
<main>
  <div id="range-bar" class="range-bar">
    <label for="range-select">Time range</label>
    <select id="range-select">{range_options}</select>
    <span id="range-window" class="muted"></span>
  </div>
  <section id="gaps" class="active" data-ranged="true" data-ranges="{','.join(FR0102_RANGES)}"><h2>FR-01 &middot; Topic Gaps</h2><div id="gaps-content"></div></section>
  <section id="rising" data-ranged="true" data-ranges="{','.join(FR0102_RANGES)}"><h2>FR-02 &middot; Rising Topics &amp; KOLs</h2><div id="rising-content"></div></section>
  <section id="sentiment" data-ranged="true" data-ranges="{','.join(FR03_RANGES)}"><h2>FR-03 &middot; Sentiment Dashboard</h2><div id="sentiment-content"></div></section>
  <section id="competitor" data-ranged="true" data-ranges="{','.join(FR03_RANGES)}"><h2>Competitor Watch</h2>{panel(f'''
    <div class="keyword-search-bar">
      <input type="text" id="competitor-keyword-input" placeholder="Search a topic or keyword, e.g. nvidia, tariff, dram..." autocomplete="off">
    </div>
    <div id="competitor-results"><p class="empty">Type a topic or keyword to see every non-TrendForce account's post mentioning it, for the currently selected time range.</p></div>
    ''', 'Search competitor posts', 'Every account that is not ours')}</section>
  <section id="video-ranking" data-ranged="true" data-ranges="{','.join(VIDEO_RANKING_RANGES)}">
    <h2>X Video Ranking</h2>
    <div class="keyword-search-bar">
      <label for="video-metric-select">Rank by</label>
      <select id="video-metric-select">
        <option value="views" selected>Views</option>
        <option value="likes">Likes</option>
        <option value="retweets">Reposts</option>
      </select>
      <span class="video-region-filter" id="video-region-filter">
        <span class="muted">Region:</span>
        {''.join(f'<label><input type="checkbox" class="video-region-cb" value="{r}" checked> {r}</label>' for r in VIDEO_REGIONS)}
        <label><input type="checkbox" class="video-region-cb" value="" checked> Unknown</label>
      </span>
    </div>
    <div id="video-ranking-content"></div>
  </section>
  <section id="accounts"><h2>FR-05 &middot; Account Status</h2>{render_accounts(account_status)}</section>
  <section id="summaries"><h2>FR-06 &middot; Daily Executive Summaries</h2>{render_summaries(daily_summaries)}</section>
  <section id="selfservice">
    <h2>FR-07 &middot; Self-service Data Analysis &amp; Export</h2>
    {panel(f'''
      <p class="muted" style="margin:0 0 14px">
        Upload your own CSV or Excel (.xlsx) export, analyzed entirely in your browser (this is a static
        site - nothing is uploaded anywhere). Timestamps without a timezone are assumed to be in the source
        timezone below and converted to Asia/Taipei (UTC+8), matching self_service_analysis.py's CLI behavior.
      </p>
      <div class="upload-row">
        <input type="file" id="upload-file" accept=".csv,.xlsx">
        <label for="upload-tz">Source timezone (for naive timestamps)</label>
        <select id="upload-tz">
          <option value="America/Los_Angeles" selected>America/Los_Angeles (PT)</option>
          <option value="America/New_York">America/New_York (ET)</option>
          <option value="America/Chicago">America/Chicago (CT)</option>
          <option value="UTC">UTC</option>
          <option value="Asia/Taipei">Asia/Taipei (UTC+8)</option>
        </select>
      </div>
      <div id="upload-results"><p class="empty">Choose a CSV or Excel file to analyze. Expected columns: a
        text column (text/content/message) and a timestamp column (timestamp/date/created_at) - extra
        columns are kept and passed through untouched.</p></div>
    ''', 'Upload a file', 'CSV or Excel, analyzed client-side, nothing leaves your browser')}
  </section>
</main>
<script>
  const RANGE_HTML = {range_data_json};
  const RANGE_WINDOW = {window_caption_json};
  const RANGE_BOUNDS = {window_bounds_json};
  const KEYWORD_POSTS = {keyword_index_json};
  const VIDEO_RANKING = {video_ranking_json};

  function escapeHtml(s) {{ const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }}
  function escapeAttr(s) {{ return escapeHtml(s).replace(/"/g, '&quot;'); }}

  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('main section').forEach(s => s.classList.remove('active'));
      btn.classList.add('active');
      const target = document.getElementById(btn.dataset.tab);
      target.classList.add('active');
      document.getElementById('range-bar').style.display =
        target.dataset.ranged === 'true' ? 'flex' : 'none';

      // Some sections only have data for a subset of ranges (X Video
      // Ranking has no month/quarter - video_ranking.py only computes
      // 1h/4h/8h/1d/1w) - hide options that section doesn't support
      // rather than offering a range that will always come up empty.
      const select = document.getElementById('range-select');
      const allowed = target.dataset.ranges ? target.dataset.ranges.split(',') : null;
      let currentStillValid = true;
      Array.from(select.options).forEach(opt => {{
        const visible = !allowed || allowed.includes(opt.value);
        opt.hidden = !visible;
        opt.disabled = !visible;
        if (opt.value === select.value && !visible) currentStillValid = false;
      }});
      if (!currentStillValid) {{
        const firstVisible = Array.from(select.options).find(opt => !opt.hidden);
        if (firstVisible) {{
          select.value = firstVisible.value;
          applyRange(select.value);
        }}
      }}
    }});
  }});

  // FR-03-04/05/06: no backend to query on demand (static site), so
  // mention counts / platform share / platform ranking are computed live
  // in the browser over the embedded KEYWORD_POSTS index, filtered to
  // whichever range window is currently selected.
  let currentKeyword = '';

  function renderKeywordResults(range) {{
    const container = document.getElementById('keyword-results');
    if (!container) return; // sentiment tab's DOM not present right now
    const kw = currentKeyword.trim().toLowerCase();
    if (!kw) {{
      container.innerHTML = '<p class="empty">Type a keyword to see mention counts by account and platform, for the currently selected time range.</p>';
      return;
    }}
    const bounds = RANGE_BOUNDS[range];
    if (!bounds) {{
      container.innerHTML = '<p class="empty">No data window available for this range.</p>';
      return;
    }}
    const start = new Date(bounds.start), end = new Date(bounds.end);
    const matches = KEYWORD_POSTS.filter(p => {{
      const t = new Date(p.ts);
      return t >= start && t <= end && p.text.toLowerCase().includes(kw);
    }});

    if (matches.length === 0) {{
      container.innerHTML = `<p class="empty">No mentions of "${{kw}}" in this time range.</p>`;
      return;
    }}

    const byHandle = {{}}, byPlatform = {{}}, byPlatformHandle = {{}};
    const urlsByHandle = {{}}, urlsByPlatformHandle = {{}};
    for (const p of matches) {{
      byHandle[p.handle] = (byHandle[p.handle] || 0) + 1;
      byPlatform[p.platform] = (byPlatform[p.platform] || 0) + 1;
      byPlatformHandle[p.platform] = byPlatformHandle[p.platform] || {{}};
      byPlatformHandle[p.platform][p.handle] = (byPlatformHandle[p.platform][p.handle] || 0) + 1;
      if (p.url) {{
        (urlsByHandle[p.handle] = urlsByHandle[p.handle] || []).push(p.url);
        urlsByPlatformHandle[p.platform] = urlsByPlatformHandle[p.platform] || {{}};
        (urlsByPlatformHandle[p.platform][p.handle] = urlsByPlatformHandle[p.platform][p.handle] || []).push(p.url);
      }}
    }}

    // Source-link hover box (FR-03-04/06): each account row's mention count
    // is a hit target - hovering/focusing it shows every matching post's
    // URL so the reader can jump straight to the source instead of just
    // seeing a number. Encoded as a data attribute (not inline onclick) so
    // the URLs go through textContent/href, never innerHTML string-built.
    const linkRow = (h, c, urls) =>
      `<tr class="kw-link-row" tabindex="0" data-urls="${{escapeAttr(JSON.stringify(urls || []))}}"><td>${{escapeHtml(h)}}</td><td class="num">${{c}}</td></tr>`;

    const mentionRows = Object.entries(byHandle).sort((a, b) => b[1] - a[1])
      .map(([h, c]) => linkRow(h, c, urlsByHandle[h])).join('');

    const total = matches.length;
    const shareRows = Object.entries(byPlatform).sort((a, b) => b[1] - a[1])
      .map(([plat, c]) => `<tr><td>${{plat}}</td><td class="num">${{Math.round(c / total * 1000) / 10}}%</td><td class="num">${{c}}</td></tr>`).join('');

    const rankingBlocks = Object.entries(byPlatformHandle).map(([plat, handles]) => {{
      const urls = urlsByPlatformHandle[plat] || {{}};
      const rows = Object.entries(handles).sort((a, b) => b[1] - a[1])
        .map(([h, c]) => linkRow(h, c, urls[h])).join('');
      return `<div><h3>${{escapeHtml(plat)}}</h3><div class="table-wrap"><table><thead><tr><th>Account</th><th class="num">Mentions</th></tr></thead><tbody>${{rows}}</tbody></table></div></div>`;
    }}).join('');

    container.innerHTML = `
      <p class="muted">${{total}} post(s) mention "${{kw}}" in this window.</p>
      <div class="col-2">
        <div>
          <h3>Competitor mentions (FR-03-04)</h3>
          <div class="table-wrap"><table><thead><tr><th>Account</th><th class="num">Mentions</th></tr></thead><tbody>${{mentionRows}}</tbody></table></div>
        </div>
        <div>
          <h3>Platform share of voice (FR-03-05)</h3>
          <div class="table-wrap"><table><thead><tr><th>Platform</th><th class="num">Share</th><th class="num">Mentions</th></tr></thead><tbody>${{shareRows}}</tbody></table></div>
        </div>
      </div>
      <h3>Platform keyword ranking (FR-03-06)</h3>
      <div class="col-2">${{rankingBlocks}}</div>
    `;
  }}

  // Source-link hover/focus box: shows every matching post's URL for the
  // row under the pointer/focus. A single shared popover element (not one
  // per row) so it can be positioned near whichever row is active and torn
  // down cleanly on mouseleave/blur.
  let kwLinkPopover = null;
  function showKwLinkPopover(row) {{
    hideKwLinkPopover();
    let urls = [];
    try {{ urls = JSON.parse(row.dataset.urls || '[]'); }} catch (e) {{ urls = []; }}
    const pop = document.createElement('div');
    pop.className = 'kw-link-popover';
    if (urls.length === 0) {{
      const p = document.createElement('p');
      p.className = 'empty';
      p.textContent = 'No source link recorded for these post(s).';
      pop.appendChild(p);
    }} else {{
      urls.forEach((url, i) => {{
        const a = document.createElement('a');
        a.href = url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = `${{i + 1}}. ${{url}}`;
        pop.appendChild(a);
      }});
    }}
    document.body.appendChild(pop);
    // Opens beside the row, not below it - a popover below means the mouse
    // has to cross the next row (and its own hover trigger) to reach it,
    // which flips the popover to that row before the pointer arrives.
    // Sitting to the side keeps a straight, uninterrupted path from the
    // row to the popover.
    const r = row.getBoundingClientRect();
    const spaceRight = document.documentElement.clientWidth - r.right;
    const openLeft = spaceRight < pop.offsetWidth + 20 && r.left > pop.offsetWidth + 20;
    const left = window.scrollX + (openLeft ? r.left - pop.offsetWidth - 10 : r.right + 10);
    let top = window.scrollY + r.top - 4;
    const maxTop = window.scrollY + document.documentElement.clientHeight - pop.offsetHeight - 12;
    if (top > maxTop) top = Math.max(window.scrollY + 12, maxTop);
    pop.style.top = `${{top}}px`;
    pop.style.left = `${{left}}px`;
    kwLinkPopover = pop;
  }}
  function hideKwLinkPopover() {{
    if (kwLinkPopover) {{ kwLinkPopover.remove(); kwLinkPopover = null; }}
  }}
  document.addEventListener('mouseover', e => {{
    const row = e.target.closest('.kw-link-row');
    if (row) showKwLinkPopover(row);
  }});
  document.addEventListener('focusin', e => {{
    const row = e.target.closest('.kw-link-row');
    if (row) showKwLinkPopover(row);
  }});
  document.addEventListener('mouseout', e => {{
    if (e.target.closest('.kw-link-row') && !e.relatedTarget?.closest('.kw-link-popover, .kw-link-row')) hideKwLinkPopover();
  }});
  document.addEventListener('focusout', e => {{
    if (e.target.closest('.kw-link-row') && !e.relatedTarget?.closest('.kw-link-popover, .kw-link-row')) hideKwLinkPopover();
  }});

  // Sentiment trend chart's per-bucket "why" popover - same hover/focus
  // pattern as kwLinkPopover above, showing the bucket's top 3 topics and
  // 1-2 representative posts (from nlp_sentiment.widget_sentiment_trend_curve)
  // instead of just a sentiment-count tooltip. Reuses .kw-link-popover's
  // base look (position/shadow/max-height) via a shared class, with its own
  // internal structure classes for the topic/post rows.
  let trendPointPopover = null;
  function showTrendPointPopover(hit) {{
    hideTrendPointPopover();
    let data = null;
    try {{ data = JSON.parse(hit.dataset.payload || 'null'); }} catch (e) {{ data = null; }}
    if (!data) return;
    const pop = document.createElement('div');
    pop.className = 'trend-tooltip';

    const head = document.createElement('div');
    head.className = 'trend-point-popover-head';
    head.textContent = data.date;
    pop.appendChild(head);

    const stats = document.createElement('div');
    stats.className = 'trend-point-popover-stats';
    stats.textContent = `Net sentiment ${{data.net >= 0 ? '+' : ''}}${{data.net}} · ${{data.positive}} pos / ${{data.neutral}} neu / ${{data.negative}} neg · ${{data.engagement.toLocaleString()}} engagement`;
    pop.appendChild(stats);
    if (data.is_anomaly) {{
      const anom = document.createElement('div');
      anom.className = 'trend-point-popover-stats';
      anom.style.color = 'var(--gold)';
      anom.textContent = 'Anomalous bucket (volume or negative-share well above baseline)';
      pop.appendChild(anom);
    }}

    if (!data.topics.length) {{
      const p = document.createElement('p');
      p.className = 'empty';
      p.textContent = 'No topic breakdown for this bucket.';
      pop.appendChild(p);
    }} else {{
      data.topics.forEach(t => {{
        const row = document.createElement('div');
        row.className = 'trend-point-topic-row';
        const label = document.createElement('span');
        label.textContent = t.label;
        const count = document.createElement('span');
        count.textContent = `${{t.count}} post(s)`;
        row.appendChild(label);
        row.appendChild(count);
        pop.appendChild(row);
      }});
    }}

    data.posts.forEach(p => {{
      const div = document.createElement('div');
      div.className = 'trend-point-post';
      const author = document.createElement('strong');
      author.textContent = `${{p.handle}} `;
      const eng = document.createElement('span');
      eng.className = 'muted';
      eng.textContent = `(${{p.interaction.toLocaleString()}} engagement)`;
      div.appendChild(author);
      div.appendChild(eng);
      const text = document.createElement('div');
      text.textContent = p.text;
      div.appendChild(text);
      if (p.url) {{
        const a = document.createElement('a');
        a.href = p.url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = 'Open post';
        div.appendChild(a);
      }}
      pop.appendChild(div);
    }});

    document.body.appendChild(pop);
    // hit's own rect spans the FULL chart height (it's a per-column hit
    // target, not a small mark) - anchoring beside it like kwLinkPopover
    // does for a table row lands the popover on top of neighboring bars/
    // line/callouts instead of clear of them. Anchor to the whole chart's
    // bounding box instead: always open fully above or fully below the
    // chart, horizontally centered under the hovered column.
    const chartEl = hit.closest('.trend-svg') || hit;
    const chartRect = chartEl.getBoundingClientRect();
    const hitRect = hit.getBoundingClientRect();
    const spaceBelow = window.innerHeight - chartRect.bottom;
    const openAbove = spaceBelow < pop.offsetHeight + 16 && chartRect.top > pop.offsetHeight + 16;
    const top = window.scrollY + (openAbove ? chartRect.top - pop.offsetHeight - 10 : chartRect.bottom + 10);
    let left = window.scrollX + hitRect.left + hitRect.width / 2 - pop.offsetWidth / 2;
    const minLeft = window.scrollX + 8, maxLeft = window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8;
    left = Math.max(minLeft, Math.min(left, maxLeft));
    pop.style.top = `${{top}}px`;
    pop.style.left = `${{left}}px`;
    trendPointPopover = pop;
  }}
  function hideTrendPointPopover() {{
    if (trendPointPopover) {{ trendPointPopover.remove(); trendPointPopover = null; }}
  }}
  document.addEventListener('mouseover', e => {{
    const hit = e.target.closest('.trend-hit');
    if (hit) showTrendPointPopover(hit);
  }});
  document.addEventListener('focusin', e => {{
    const hit = e.target.closest('.trend-hit');
    if (hit) showTrendPointPopover(hit);
  }});
  document.addEventListener('mouseout', e => {{
    if (e.target.closest('.trend-hit') && !e.relatedTarget?.closest('.trend-tooltip, .trend-hit')) hideTrendPointPopover();
  }});
  document.addEventListener('focusout', e => {{
    if (e.target.closest('.trend-hit') && !e.relatedTarget?.closest('.trend-tooltip, .trend-hit')) hideTrendPointPopover();
  }});

  // Sentiment trend chart's count/engagement mode toggle - both bar
  // variants are pre-rendered (each scaled to its own max, see
  // render_trend_curve), so this is just a visibility swap, no
  // recomputation. Delegated on 'main' since the trend chart's markup is
  // replaced wholesale on every range switch (see RANGE_HTML below) -
  // binding directly to '.trend-mode-seg' at page load would miss
  // whichever range's copy gets swapped in later.
  document.querySelector('main').addEventListener('click', e => {{
    const btn = e.target.closest('.trend-mode-seg button');
    if (!btn) return;
    const seg = btn.closest('.trend-mode-seg');
    seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
    const mode = btn.dataset.mode;
    const svg = seg.closest('.panel')?.querySelector('.trend-svg');
    if (!svg) return;
    svg.querySelector('.trend-bars-count').style.display = mode === 'count' ? '' : 'none';
    svg.querySelector('.trend-bars-eng').style.display = mode === 'eng' ? '' : 'none';
    svg.querySelector('.trend-bar-axis-label-count').style.display = mode === 'count' ? '' : 'none';
    svg.querySelector('.trend-bar-axis-label-eng').style.display = mode === 'eng' ? '' : 'none';
  }});

  // FR-05: no backend on a static site to add an account and start
  // crawling immediately, so the request opens a pre-filled GitHub issue
  // instead (no credentials needed client-side) for elainekao to review
  // and approve locally with add_account.py.
  // Accepts a bare handle or a pasted profile URL (people paste URLs -
  // one already came through as "https://x.com/tphuang" and needed manual
  // cleanup) and normalizes to the bare handle either way.
  function normalizeHandle(raw) {{
    let h = raw.trim();
    h = h.replace(/^https?:\/\/(www\.)?(x\.com|twitter\.com|facebook\.com)\//i, '');
    h = h.replace(/^@/, '').replace(/\/+$/, '');
    h = h.split(/[/?#]/)[0];
    return h;
  }}

  document.getElementById('add-account-btn')?.addEventListener('click', () => {{
    const platform = document.getElementById('add-account-platform').value;
    const handle = normalizeHandle(document.getElementById('add-account-handle').value);
    if (!handle) {{
      document.getElementById('add-account-handle').focus();
      return;
    }}
    const title = `Add account: ${{platform}}/${{handle}}`;
    const body = `Please start tracking this account:\n\n- Platform: ${{platform}}\n- Handle: ${{handle}}\n\nRequested from the dashboard's Account Status tab.`;
    const url = `https://github.com/elainekaotf/TrendForceDashboard/issues/new?title=${{encodeURIComponent(title)}}&body=${{encodeURIComponent(body)}}&labels=add-account`;
    window.open(url, '_blank', 'noopener,noreferrer');
  }});

  // Same static-site constraint as adding: no backend to remove an
  // account on the spot, so this opens a pre-filled GitHub issue too -
  // elainekao reviews and approves it locally with remove_account.py.
  // Two-click inline confirm (Remove -> Confirm remove?) instead of a
  // native confirm() dialog, so it matches the rest of the dashboard's
  // look instead of a jarring OS-styled popup; reverts on its own after
  // a few seconds if the second click never comes.
  document.querySelectorAll('.remove-account-btn').forEach(btn => {{
    let confirmTimer = null;
    btn.addEventListener('click', () => {{
      const {{ platform, handle }} = btn.dataset;
      if (!btn.classList.contains('confirming')) {{
        btn.classList.add('confirming');
        btn.textContent = 'Confirm remove?';
        confirmTimer = setTimeout(() => {{
          btn.classList.remove('confirming');
          btn.textContent = 'Remove';
        }}, 4000);
        return;
      }}
      clearTimeout(confirmTimer);
      btn.classList.remove('confirming');
      btn.textContent = 'Remove';
      const title = `Remove account: ${{platform}}/${{handle}}`;
      const body = `Please stop tracking this account:\n\n- Platform: ${{platform}}\n- Handle: ${{handle}}\n\nRequested from the dashboard's Account Status tab.`;
      const url = `https://github.com/elainekaotf/TrendForceDashboard/issues/new?title=${{encodeURIComponent(title)}}&body=${{encodeURIComponent(body)}}&labels=remove-account`;
      window.open(url, '_blank', 'noopener,noreferrer');
    }});
  }});

  // Competitor Watch: same substring-match-over-KEYWORD_POSTS approach as
  // the Sentiment tab's keyword search, but scoped to `!p.is_own` and
  // surfacing the actual matching posts (not aggregated counts) - "show me
  // every non-TrendForce account's post about X in this window."
  let currentCompetitorKeyword = '';
  const MAX_COMPETITOR_RESULTS = 200;

  function renderCompetitorResults(range) {{
    const container = document.getElementById('competitor-results');
    if (!container) return;
    const kw = currentCompetitorKeyword.trim().toLowerCase();
    if (!kw) {{
      container.innerHTML = '<p class="empty">Type a topic or keyword to see every non-TrendForce account\\'s post mentioning it, for the currently selected time range.</p>';
      return;
    }}
    const bounds = RANGE_BOUNDS[range];
    if (!bounds) {{
      container.innerHTML = '<p class="empty">No data window available for this range.</p>';
      return;
    }}
    const start = new Date(bounds.start), end = new Date(bounds.end);
    const matches = KEYWORD_POSTS.filter(p => {{
      const t = new Date(p.ts);
      return !p.is_own && t >= start && t <= end && p.text.toLowerCase().includes(kw);
    }}).sort((a, b) => new Date(b.ts) - new Date(a.ts));

    if (matches.length === 0) {{
      container.innerHTML = `<p class="empty">No non-TrendForce posts mention "${{escapeHtml(kw)}}" in this time range.</p>`;
      return;
    }}

    const shown = matches.slice(0, MAX_COMPETITOR_RESULTS);
    const rows = shown.map(p => `
      <tr>
        <td class="cell-primary">${{escapeHtml(p.handle)}}</td>
        <td>${{escapeHtml(p.platform)}}</td>
        <td>${{escapeHtml(new Date(p.ts).toLocaleString('en-US', {{timeZone: 'Asia/Taipei', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit'}}))}}</td>
        <td>${{escapeHtml(p.text.slice(0, 200))}}</td>
        <td>${{p.url ? `<a href="${{escapeAttr(p.url)}}" target="_blank" rel="noopener noreferrer">Open post</a>` : '—'}}</td>
      </tr>`).join('');

    const truncatedNote = matches.length > MAX_COMPETITOR_RESULTS
      ? `<p class="muted">Showing the ${{MAX_COMPETITOR_RESULTS}} most recent of ${{matches.length}} matching posts.</p>` : '';

    container.innerHTML = `
      <p class="muted">${{matches.length}} non-TrendForce post(s) mention "${{escapeHtml(kw)}}" in this window (Taiwan time).</p>
      ${{truncatedNote}}
      <div class="table-wrap"><table><thead><tr><th>Account</th><th>Platform</th><th>Time</th><th>Post</th><th>Link</th></tr></thead><tbody>${{rows}}</tbody></table></div>
    `;
  }}

  const VIDEO_METRIC_LABELS = {{ views: 'Views', likes: 'Likes', retweets: 'Reposts' }};
  const MAX_VIDEO_RESULTS = 30;

  function renderVideoRanking(range) {{
    const container = document.getElementById('video-ranking-content');
    if (!container) return;
    const metricSelect = document.getElementById('video-metric-select');
    const metric = metricSelect ? metricSelect.value : 'views';
    const allPosts = VIDEO_RANKING[range] || [];

    if (allPosts.length === 0) {{
      container.innerHTML = '<p class="empty">No video posts found on X in this time range.</p>';
      return;
    }}

    // Region is only known for handles enrich_video_locations.js has
    // already looked up (see video_ranking.py's classify_region) - most
    // accounts haven't been looked up yet (that cache started empty and
    // only grows ~40/day), so region=null is still the overwhelming
    // majority of posts. Found 2026-08-04: defaulting to just the 6 named
    // regions checked meant nearly every post got excluded and the whole
    // tab looked blank. The "Unknown" checkbox (empty-string value) is
    // its own bucket for region=null, checked by default alongside the
    // rest, so posts aren't hidden purely because enrichment hasn't
    // reached their account yet - unchecking it lets you see only
    // classified regions once coverage is good enough to matter.
    const checkedRegions = Array.from(document.querySelectorAll('.video-region-cb:checked')).map(cb => cb.value);
    const posts = allPosts.filter(p => checkedRegions.includes(p.region || ''));

    if (posts.length === 0) {{
      container.innerHTML = '<p class="empty">No video posts from the selected region(s) in this time range - try checking more regions, or note that region is only known for accounts we\\'ve already looked up.</p>';
      return;
    }}

    // Server ships up to 50 per range, pre-sorted by views - re-sort here
    // so switching the metric dropdown never needs a re-fetch.
    const ranked = [...posts].sort((a, b) => (b[metric] || 0) - (a[metric] || 0)).slice(0, MAX_VIDEO_RESULTS);
    const rows = ranked.map((p, i) => `
      <tr>
        <td class="num">${{i + 1}}</td>
        <td class="cell-primary">${{escapeHtml(p.handle)}}</td>
        <td>${{escapeHtml(p.region || '—')}}</td>
        <td>${{escapeHtml(p.text.slice(0, 160))}}</td>
        <td>${{p.topic ? escapeHtml(p.topic) : '<span class="muted">—</span>'}}</td>
        <td class="num">${{(p.views || 0).toLocaleString('en-US')}}</td>
        <td class="num">${{(p.likes || 0).toLocaleString('en-US')}}</td>
        <td class="num">${{(p.retweets || 0).toLocaleString('en-US')}}</td>
        <td>${{p.url ? `<a href="${{escapeAttr(p.url)}}" target="_blank" rel="noopener noreferrer">Open post</a>` : '—'}}</td>
      </tr>`).join('');

    container.innerHTML = `
      <div class="table-wrap"><table><thead><tr><th>#</th><th>Account</th><th>Region</th><th>Post</th><th>Topics</th><th>Views</th><th>Likes</th><th>Reposts</th><th>Link</th></tr></thead><tbody>${{rows}}</tbody></table></div>
    `;
  }}

  document.addEventListener('input', e => {{
    if (e.target.id === 'keyword-input') {{
      currentKeyword = e.target.value;
      renderKeywordResults(document.getElementById('range-select').value);
    }}
    if (e.target.id === 'competitor-keyword-input') {{
      currentCompetitorKeyword = e.target.value;
      renderCompetitorResults(document.getElementById('range-select').value);
    }}
  }});

  document.addEventListener('change', e => {{
    if (e.target.id === 'video-metric-select' || e.target.classList.contains('video-region-cb')) {{
      renderVideoRanking(document.getElementById('range-select').value);
    }}
  }});

  function applyRange(range) {{
    document.getElementById('gaps-content').innerHTML = RANGE_HTML.gaps[range] || '';
    document.getElementById('rising-content').innerHTML = RANGE_HTML.rising[range] || '';
    document.getElementById('sentiment-content').innerHTML = RANGE_HTML.sentiment[range] || '';
    document.getElementById('range-window').textContent = RANGE_WINDOW[range] || '';
    const input = document.getElementById('keyword-input');
    if (input) input.value = currentKeyword;
    renderKeywordResults(range);
    const competitorInput = document.getElementById('competitor-keyword-input');
    if (competitorInput) competitorInput.value = currentCompetitorKeyword;
    renderCompetitorResults(range);
    renderVideoRanking(range);
  }}

  document.getElementById('range-select').addEventListener('change', e => applyRange(e.target.value));
  applyRange(document.getElementById('range-select').value);

  // --- FR-07: self-service upload, analyzed entirely client-side (no
  // backend on a static site). Sentiment uses a JS port of VADER's core
  // algorithm (negation, boosters, ALLCAPS/punctuation emphasis,
  // "but"-contrast, compound normalization) over the same lexicon
  // nlp_sentiment.py uses server-side, so scores are consistent with the
  // rest of the dashboard rather than an ad hoc approximation.
  const VADER_LEXICON = {vader_lexicon_json};

  // Chinese sentiment (same NFR-07 gap fixed server-side in nlp_sentiment.py:
  // VADER is English-only and silently scored 100% of Chinese text as
  // neutral). Substring scan over cnsenti's word lists after a character-
  // level Traditional->Simplified pass - see load_chinese_sentiment_data()'s
  // docstring in generate_dashboard.py for why this isn't full jieba+OpenCC.
  const ZH_POS_WORDS = new Set({zh_pos_words_json});
  const ZH_NEG_WORDS = new Set({zh_neg_words_json});
  const ZH_T2S_MAP = {zh_t2s_map_json};
  const ZH_MAX_WORD_LEN = Math.max(...[...ZH_POS_WORDS, ...ZH_NEG_WORDS].map(w => w.length));
  const CJK_RE = /[一-鿿]/g;
  const LATIN_RE = /[A-Za-z]/g;

  function toSimplified(text) {{
    let out = '';
    for (const ch of text) out += ZH_T2S_MAP[ch] || ch;
    return out;
  }}

  function scoreChineseSentiment(text) {{
    const simplified = toSimplified(text).replace(/\s+/g, '');
    let pos = 0, neg = 0;
    for (let i = 0; i < simplified.length; i++) {{
      for (let len = 2; len <= ZH_MAX_WORD_LEN && i + len <= simplified.length; len++) {{
        const w = simplified.slice(i, i + len);
        if (ZH_POS_WORDS.has(w)) pos++;
        if (ZH_NEG_WORDS.has(w)) neg++;
      }}
    }}
    return (pos + neg) ? (pos - neg) / (pos + neg) : 0.0;
  }}
  const VADER_NEGATE = ["aint","arent","cannot","cant","couldnt","darent","didnt","doesnt",
    "ain't","aren't","can't","couldn't","daren't","didn't","doesn't",
    "dont","hadnt","hasnt","havent","isnt","mightnt","mustnt","neither",
    "don't","hadn't","hasn't","haven't","isn't","mightn't","mustn't",
    "neednt","needn't","never","none","nope","nor","not","nothing","nowhere",
    "oughtnt","shant","shouldnt","uhuh","wasnt","werent",
    "oughtn't","shan't","shouldn't","uh-uh","wasn't","weren't",
    "without","wont","wouldnt","won't","wouldn't","rarely","seldom","despite"];
  const B_INCR = 0.293, B_DECR = -0.293, C_INCR = 0.733, N_SCALAR = -0.74;
  const VADER_BOOSTER = {{
    absolutely:B_INCR,amazingly:B_INCR,awfully:B_INCR,completely:B_INCR,considerable:B_INCR,considerably:B_INCR,
    decidedly:B_INCR,deeply:B_INCR,effing:B_INCR,enormous:B_INCR,enormously:B_INCR,entirely:B_INCR,especially:B_INCR,
    exceptional:B_INCR,exceptionally:B_INCR,extreme:B_INCR,extremely:B_INCR,fabulously:B_INCR,flipping:B_INCR,
    flippin:B_INCR,frackin:B_INCR,fracking:B_INCR,fricking:B_INCR,frickin:B_INCR,frigging:B_INCR,friggin:B_INCR,
    fully:B_INCR,greatly:B_INCR,hella:B_INCR,highly:B_INCR,hugely:B_INCR,incredible:B_INCR,incredibly:B_INCR,
    intensely:B_INCR,major:B_INCR,majorly:B_INCR,more:B_INCR,most:B_INCR,particularly:B_INCR,purely:B_INCR,
    quite:B_INCR,really:B_INCR,remarkably:B_INCR,so:B_INCR,substantially:B_INCR,thoroughly:B_INCR,total:B_INCR,
    totally:B_INCR,tremendous:B_INCR,tremendously:B_INCR,uber:B_INCR,unbelievably:B_INCR,unusually:B_INCR,
    utter:B_INCR,utterly:B_INCR,very:B_INCR,
    almost:B_DECR,barely:B_DECR,hardly:B_DECR,less:B_DECR,little:B_DECR,marginal:B_DECR,marginally:B_DECR,
    occasional:B_DECR,occasionally:B_DECR,partly:B_DECR,scarce:B_DECR,scarcely:B_DECR,slight:B_DECR,
    slightly:B_DECR,somewhat:B_DECR,
  }};

  function vaderNegated(word) {{
    const w = word.toLowerCase();
    return VADER_NEGATE.includes(w) || w.includes("n't");
  }}
  function vaderScalar(word, valence, isCapDiff) {{
    const wl = word.toLowerCase();
    if (!(wl in VADER_BOOSTER)) return 0;
    let scalar = VADER_BOOSTER[wl];
    if (valence < 0) scalar *= -1;
    if (word === word.toUpperCase() && isCapDiff) scalar += (valence > 0 ? C_INCR : -C_INCR);
    return scalar;
  }}
  function stripPunc(token) {{
    const stripped = token.replace(/^[.,!?;:'"()\\[\\]{{}}\\-]+|[.,!?;:'"()\\[\\]{{}}\\-]+$/g, '');
    return stripped.length <= 2 ? token : stripped;
  }}
  function vaderScore(text) {{
    const words = (text || '').split(/\s+/).filter(Boolean).map(stripPunc);
    if (!words.length) return 0;
    const upperCount = words.filter(w => w === w.toUpperCase() && /[A-Z]/.test(w)).length;
    const isCapDiff = upperCount > 0 && upperCount < words.length;
    const sentiments = [];
    for (let i = 0; i < words.length; i++) {{
      const wl = words[i].toLowerCase();
      let valence = 0;
      if (wl in VADER_LEXICON) {{
        valence = VADER_LEXICON[wl];
        if (words[i] === words[i].toUpperCase() && isCapDiff) valence += (valence > 0 ? C_INCR : -C_INCR);
        for (let startI = 0; startI < 3; startI++) {{
          if (i > startI && !(words[i - (startI + 1)].toLowerCase() in VADER_LEXICON)) {{
            let s = vaderScalar(words[i - (startI + 1)], valence, isCapDiff);
            if (startI === 1 && s !== 0) s *= 0.95;
            if (startI === 2 && s !== 0) s *= 0.9;
            valence += s;
            if (vaderNegated(words[i - (startI + 1)])) valence *= N_SCALAR;
          }}
        }}
      }}
      sentiments.push(valence);
    }}
    // contrastive "but": halve sentiment before it, boost 1.5x after
    const lower = words.map(w => w.toLowerCase());
    const butIdx = lower.indexOf('but');
    if (butIdx !== -1) {{
      for (let i = 0; i < sentiments.length; i++) sentiments[i] *= (i < butIdx ? 0.5 : (i > butIdx ? 1.5 : 1));
    }}
    let sum = sentiments.reduce((a, b) => a + b, 0);
    const epCount = Math.min((text.match(/!/g) || []).length, 4);
    sum += (sum > 0 ? 1 : sum < 0 ? -1 : 0) * epCount * 0.292;
    return sum / Math.sqrt(sum * sum + 15);
  }}
  function classifySentiment(text) {{
    const cjkCount = (text.match(CJK_RE) || []).length;
    const latinCount = (text.match(LATIN_RE) || []).length;
    const c = cjkCount > latinCount ? scoreChineseSentiment(text) : vaderScore(text);
    return c >= 0.05 ? 'positive' : c <= -0.05 ? 'negative' : 'neutral';
  }}

  // Minimal CSV parser: handles quoted fields with embedded commas/newlines.
  function parseCsv(text) {{
    const rows = [];
    let row = [], field = '', inQuotes = false;
    for (let i = 0; i < text.length; i++) {{
      const c = text[i];
      if (inQuotes) {{
        if (c === '"' && text[i + 1] === '"') {{ field += '"'; i++; }}
        else if (c === '"') {{ inQuotes = false; }}
        else {{ field += c; }}
      }} else {{
        if (c === '"') inQuotes = true;
        else if (c === ',') {{ row.push(field); field = ''; }}
        else if (c === '\\n' || c === '\\r') {{
          if (c === '\\r' && text[i + 1] === '\\n') i++;
          row.push(field); field = '';
          if (row.length > 1 || row[0] !== '') rows.push(row);
          row = [];
        }} else {{ field += c; }}
      }}
    }}
    if (field !== '' || row.length) {{ row.push(field); rows.push(row); }}
    if (!rows.length) return [];
    const headers = rows[0];
    return rows.slice(1).map(r => Object.fromEntries(headers.map((h, i) => [h, r[i] ?? ''])));
  }}

  // Column names only ever matched exact English headers - a Traditional
  // Chinese export (e.g. 內容/時間 instead of content/timestamp) found none
  // of them and failed with "No text/content/message column found", which
  // read as broken rather than "wrong header language." Added the common
  // zh-TW header names actual exports use alongside the English ones.
  const TEXT_COLUMN_CANDIDATES = ['translated_text', 'text', 'content', 'message', 'body',
    '內容', '文字', '貼文', '貼文內容', '內文', '文章內容', '訊息', '標題'];
  const TIMESTAMP_COLUMN_CANDIDATES = ['timestamp', 'created_at', 'date', 'exactDate', 'scrapedAt',
    '時間', '日期', '發布時間', '發文時間', '貼文時間', '建立時間', '時間戳記'];
  // Interaction = every matching engagement column summed per row (likes +
  // comments + shares + ...); views is kept separate since it's a reach
  // metric, not an engagement one, and the two shouldn't be added together.
  const ENGAGEMENT_COLUMN_CANDIDATES = ['likes', 'reactions', 'retweets', 'shares', 'comments', 'replies',
    '讚', '按讚數', '讚數', '留言', '留言數', '評論', '評論數', '分享', '分享數', '轉發'];
  const VIEWS_COLUMN_CANDIDATES = ['views', 'view_count', 'impressions', '觀看數', '瀏覽數', '觀看次數', '瀏覽次數'];

  function detectColumn(fieldnames, candidates) {{
    return candidates.find(c => fieldnames.includes(c)) || null;
  }}

  function detectColumns(fieldnames, candidates) {{
    return candidates.filter(c => fieldnames.includes(c));
  }}

  // Mirrors cluster_topics.py's parse_count: strips commas, handles a
  // trailing K/M shorthand some exports use (e.g. "1.2K", "3M").
  function parseCount(v) {{
    if (v == null || v === '') return 0;
    const s = String(v).trim().replace(/,/g, '');
    const m = s.match(/^([\d.]+)\s*(K|M)?$/i);
    if (!m) return 0;
    const n = parseFloat(m[1]);
    if (isNaN(n)) return 0;
    if (/k/i.test(m[2] || '')) return Math.round(n * 1000);
    if (/m/i.test(m[2] || '')) return Math.round(n * 1000000);
    return Math.round(n);
  }}

  // Convert a wall-clock time assumed to be in `timeZone` to a real UTC
  // Date, using Intl to get that zone's offset at that instant (handles
  // DST automatically, same guarantee zoneinfo gave the Python version).
  function zonedTimeToUtc(y, mo, d, h, mi, s, timeZone) {{
    let guess = new Date(Date.UTC(y, mo - 1, d, h, mi, s));
    for (let i = 0; i < 2; i++) {{
      const dtf = new Intl.DateTimeFormat('en-US', {{
        timeZone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      }});
      const parts = Object.fromEntries(dtf.formatToParts(guess).map(p => [p.type, p.value]));
      const asIfUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
      const offsetMs = asIfUtc - guess.getTime();
      guess = new Date(Date.UTC(y, mo - 1, d, h, mi, s) - offsetMs);
    }}
    return guess;
  }}

  const MONTH_NAMES = ['january','february','march','april','may','june','july','august','september','october','november','december'];

  function to24Hour(h, ampm) {{
    h = Number(h);
    if (!ampm) return h;
    const isPM = /pm/i.test(ampm);
    if (isPM && h !== 12) h += 12;
    if (!isPM && h === 12) h = 0;
    return h;
  }}

  // datetime.fromisoformat()'s JS equivalent (the regex below) only ever
  // matched ISO 8601 - every other common export format (US-style
  // MM/DD/YYYY, or Facebook's own scraper's "Thursday, July 2, 2026 at
  // 1:00 PM" exactDate format, produced by the sibling scraper this same
  // codebase uses) silently failed, leaving converted_timestamp_utc8=null
  // for those rows with no error shown anywhere - "self-service doesn't
  // always work" was this, not a crash. Try ISO first, then these.
  function parseUploadTimestamp(raw, sourceTz) {{
    if (!raw) return {{ date: null, hadOffset: false }};
    const trimmed = raw.trim();
    // Explicit offset/Z present -> parse directly, no zone assumption needed.
    if (/[zZ]$|[+-]\d{{2}}:?\d{{2}}$/.test(trimmed)) {{
      const d = new Date(trimmed);
      return isNaN(d) ? {{ date: null, hadOffset: false }} : {{ date: d, hadOffset: true }};
    }}

    let m = trimmed.match(/^(\d{{4}})-(\d{{2}})-(\d{{2}})[ T](\d{{2}}):(\d{{2}})(?::(\d{{2}}))?/);
    if (m) {{
      const [, y, mo, d, h, mi, s] = m.map(Number);
      return {{ date: zonedTimeToUtc(y, mo, d, h, mi, s || 0, sourceTz), hadOffset: false }};
    }}

    // US-style MM/DD/YYYY[ T]HH:MM[:SS] [AM/PM]
    m = trimmed.match(/^(\d{{1,2}})\/(\d{{1,2}})\/(\d{{2,4}})[ T](\d{{1,2}}):(\d{{2}})(?::(\d{{2}}))?\s*(AM|PM)?/i);
    if (m) {{
      let [, mo, d, y, h, mi, s, ampm] = m;
      y = Number(y); if (y < 100) y += 2000;
      return {{ date: zonedTimeToUtc(y, Number(mo), Number(d), to24Hour(h, ampm), Number(mi), Number(s || 0), sourceTz), hadOffset: false }};
    }}

    // "Month DD, YYYY [at] HH:MM [AM/PM]", optionally prefixed with a
    // weekday name (Facebook's own exactDate format).
    m = trimmed.match(/^(?:[A-Za-z]+,\s*)?([A-Za-z]+)\s+(\d{{1,2}}),?\s+(\d{{4}})(?:\s+at)?\s+(\d{{1,2}}):(\d{{2}})\s*(AM|PM)?/i);
    if (m) {{
      const [, monthName, d, y, h, mi, ampm] = m;
      const mo = MONTH_NAMES.indexOf(monthName.toLowerCase()) + 1;
      if (mo > 0) {{
        return {{ date: zonedTimeToUtc(Number(y), mo, Number(d), to24Hour(h, ampm), Number(mi), 0, sourceTz), hadOffset: false }};
      }}
    }}

    return {{ date: null, hadOffset: false }};
  }}

  function csvCell(v) {{
    const s = String(v ?? '');
    return /[",\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }}

  function downloadBlob(filename, content, type) {{
    const blob = new Blob([content], {{ type }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  }}

  let uploadedRows = null, uploadedFields = null, uploadedBaseName = 'upload';

  const UPLOAD_PAGE_SIZE = 25;
  let uploadSortKey = 'time_desc';
  let uploadPage = 1;
  let uploadSearchTerm = '';

  // Standard English stopwords - the full sklearn ENGLISH_STOP_WORDS list
  // (318 words), copy-pasted rather than a small hand-picked subset, since
  // the hand-picked version kept missing ordinary stopwords ("they",
  // "these", "like", "year", "thanks" all leaked through as "topics"
  // before this). Kept in sync by hand - no shared module between the
  // Python pipeline and this embedded page's JS.
  const EN_STOPWORDS = new Set(['a','about','above','across','after','afterwards','again','against','all','almost',
    'alone','along','already','also','although','always','am','among','amongst','amoungst','amount','an','and',
    'another','any','anyhow','anyone','anything','anyway','anywhere','are','around','as','at','back','be','became',
    'because','become','becomes','becoming','been','before','beforehand','behind','being','below','beside',
    'besides','between','beyond','bill','both','bottom','but','by','call','can','cannot','cant','co','con','could',
    'couldnt','cry','de','describe','detail','do','done','down','due','during','each','eg','eight','either','eleven',
    'else','elsewhere','empty','enough','etc','even','ever','every','everyone','everything','everywhere','except',
    'few','fifteen','fifty','fill','find','fire','first','five','for','former','formerly','forty','found','four',
    'from','front','full','further','get','give','go','had','has','hasnt','have','he','hence','her','here',
    'hereafter','hereby','herein','hereupon','hers','herself','him','himself','his','how','however','hundred','i',
    'ie','if','in','inc','indeed','interest','into','is','it','its','itself','keep','last','latter','latterly',
    'least','less','ltd','made','many','may','me','meanwhile','might','mill','mine','more','moreover','most',
    'mostly','move','much','must','my','myself','name','namely','neither','never','nevertheless','next','nine','no',
    'nobody','none','noone','nor','not','nothing','now','nowhere','of','off','often','on','once','one','only',
    'onto','or','other','others','otherwise','our','ours','ourselves','out','over','own','part','per','perhaps',
    'please','put','rather','re','same','see','seem','seemed','seeming','seems','serious','several','she','should',
    'show','side','since','sincere','six','sixty','so','some','somehow','someone','something','sometime',
    'sometimes','somewhere','still','such','system','take','ten','than','that','the','their','them','themselves',
    'then','thence','there','thereafter','thereby','therefore','therein','thereupon','these','they','thick','thin',
    'third','this','those','though','three','through','throughout','thru','thus','to','together','too','top',
    'toward','towards','twelve','twenty','two','un','under','until','up','upon','us','very','via','was','we','well',
    'were','what','whatever','when','whence','whenever','where','whereafter','whereas','whereby','wherein',
    'whereupon','wherever','whether','which','while','whither','who','whoever','whole','whom','whose','why','will',
    'with','within','without','would','yet','you','your','yours','yourself','yourselves',
    // Generic filler/sentiment-adjacent/reporting words - not textbook
    // stopwords, but redundant with the Sentiment column and common enough
    // to otherwise crowd out actual entities/topics in a frequency-only
    // extraction with no TF-IDF corpus to weight against. Mirrors
    // EN_NOISE_WORDS on the server side (cluster_topics.py).
    'just','today','regarding','really','very','also','some','amazing','great','good','bad','terrible',
    'awesome','disappointing','news','update','report','story','article','like','likes','liked','year','years','thanks',
    'thank','pro','color','colors','colour','colours','expected','expects','expect',
    'progress','contract','contracts','published','publish','publishes','publishing',
    'reportedly','reported','according','says','saying','said','told','telling',
    'claims','claimed','believe','believes','believed','think','thinks','thought','thoughts',
    // Internet-slang/conversational filler and media placeholders - the
    // original EN_NOISE_WORDS entries (added for the very first "image /
    // bruh / buy / told" cluster-label fix) that this list never carried
    // over until check_js_python_sync.py caught the gap.
    'bruh','lol','lmao','rofl','omg','smh','tbh','imo','imho','fyi','btw',
    'yeah','yep','nah','gonna','wanna','kinda','sorta','gotta',
    'dude','bro','guys','guy','weebs','weeb',
    'image','images','img','photo','photos','pic','pics','picture','pictures',
    'video','videos','gif','gifs','thread','threads',
    'wow','damn','literally','actually','basically','honestly','seriously',
    'totally','definitely','probably','maybe','buy','buying','bought',
    // Geography and generic non-technical business words - "topics" here
    // are meant to read as industry/technical subject matter (chip names,
    // companies, products), not country names or generic corporate filler
    // that says nothing about what's actually being discussed.
    'china','chinese','usa','america','american','united','states','taiwan','taiwanese','japan','japanese',
    'korea','korean','europe','european','global','world','worldwide','international','domestic','local',
    'company','companies','business','businesses','market','markets','industry','industries','economy',
    'economic','government','country','countries','nation','national','region','regional','sector',
    'firm','firms','corporate','corporation','group','groups']);

  // General-purpose sweep for ordinary English words, mirroring
  // is_common_english_word() on the server side (cluster_topics.py) - every
  // word from the `wordfreq` package's English frequency list scoring
  // zipf_frequency >= 5.0 (common enough in everyday English that it's
  // filler, not industry signal), generated once and pasted here since
  // wordfreq's data isn't available client-side. A hand-picked stopword
  // list above keeps missing words one at a time ("lol", then "true", then
  // "new", ...) - this catches hundreds of them in one shot instead.
  // Doesn't replace EN_STOPWORDS above: some jargon ("contract", "pro",
  // "color") is generic/non-industry-specific for THIS use case but not
  // common enough in general English to score above the threshold, so it
  // still needs a hand-picked entry.
  const EN_COMMON_WORDS = new Set(['a','able','about','above','access','according','account','across','act','action','actually','add','added',
    'addition','after','again','against','age','ago','ahead','air','al','album','all','allow','allowed','almost',
    'alone','along','already','also','although','always','am','amazing','america','american','among','amount','an',
    'and','another','answer','anti','any','anyone','anything','april','are','area','areas','army','around','art',
    'article','as','ask','asked','ass','association','at','attack','attention','august','australia','available',
    'average','away','b','baby','back','bad','ball','bank','base','based','be','beat','beautiful','became','because',
    'become','bed','been','before','began','beginning','behind','being','believe','below','best','better','between',
    'big','bill','bit','black','blood','blue','board','body','book','books','born','both','box','boy','boys','break',
    'bring','british','brother','brought','brown','build','building','built','business','but','buy','by','c',
    'california','call','called','came','campaign','can','cannot','capital','car','card','care','career','case',
    'cases','cause','center','central','century','certain','certainly','chance','change','changed','changes',
    'character','charge','check','chief','child','children','china','chinese','choice','church','city','class',
    'clear','close','club','co','code','cold','college','come','comes','coming','committee','common','community',
    'companies','company','complete','completely','conference','considered','content','continue','control','cool',
    'cost','could','council','countries','country','county','couple','course','court','cover','crazy','create',
    'created','credit','cross','culture','cup','current','currently','cut','d','daily','damn','dark','data','date',
    'daughter','david','day','days','de','dead','deal','death','december','decided','decision','deep','department',
    'described','design','despite','development','did','die','died','difference','different','difficult','director',
    'district','do','does','dog','doing','done','door','double','down','dr','drive','due','during','e','each','early',
    'earth','east','easy','eat','economic','education','effect','eight','either','election','else','end','energy',
    'england','english','enjoy','enough','entire','especially','etc','europe','european','even','event','events',
    'ever','every','everyone','everything','evidence','exactly','example','except','expect','experience','eye','eyes',
    'f','face','fact','fall','family','fans','far','fast','father','february','federal','feel','feeling','felt',
    'female','few','field','fight','figure','film','final','finally','financial','find','fine','fire','first','five',
    'focus','follow','followed','following','food','football','for','force','foreign','forget','form','former',
    'forward','found','four','free','french','friend','friends','from','front','fuck','fucking','full','fun','funny',
    'further','future','g','game','games','gas','gave','general','george','get','gets','getting','girl','girls',
    'give','given','gives','giving','go','goal','god','goes','going','gold','gone','gonna','good','got','government',
    'great','green','ground','group','groups','growth','guess','guy','guys','h','had','hair','half','hand','hands',
    'happen','happened','happy','hard','has','hate','have','having','he','head','health','hear','heard','heart',
    'held','hell','help','her','here','hey','hi','high','higher','him','himself','his','history','hit','hold','home',
    'hope','hospital','hot','hour','hours','house','how','however','huge','human','husband','i','ice','idea','if',
    'ii','important','in','include','included','including','increase','india','individual','industry','information',
    'inside','instead','interest','interesting','international','internet','into','involved','is','island','issue',
    'issues','it','its','itself','j','james','january','job','john','join','july','june','just','keep','kept','key',
    'kids','kill','killed','kind','king','knew','know','known','knows','l','la','land','language','large','last',
    'late','later','law','lead','leading','league','learn','least','leave','led','left','legal','less','let','level',
    'life','light','like','likely','limited','line','list','listen','little','live','lives','living','local','lol',
    'london','long','longer','look','looked','looking','looks','lord','lose','loss','lost','lot','love','loved','low',
    'lower','m','made','main','major','make','makes','making','man','management','many','march','mark','market',
    'married','match','matter','may','maybe','me','mean','means','media','medical','meet','meeting','member',
    'members','men','message','met','michael','middle','might','miles','military','million','mind','mine','minister',
    'minutes','miss','missing','model','modern','mom','moment','money','month','months','more','morning','most',
    'mother','move','moved','movie','moving','mr','much','music','must','my','myself','n','name','national','natural',
    'near','nearly','need','needed','needs','network','never','new','news','next','nice','night','no','non','normal',
    'north','not','note','nothing','november','now','number','o','october','of','off','offer','office','officer',
    'official','often','oh','oil','ok','okay','old','on','once','one','ones','online','only','open','or','order',
    'original','other','others','our','out','outside','over','own','p','page','paid','pain','paper','parents','park',
    'part','particular','parts','party','pass','past','paul','pay','peace','people','per','percent','perfect',
    'performance','perhaps','period','person','personal','phone','pick','picture','piece','place','places','plan',
    'plans','play','played','player','players','playing','please','point','points','police','policy','political',
    'poor','popular','population','position','possible','post','potential','power','practice','present','president',
    'press','pressure','pretty','previous','price','private','probably','problem','problems','process','production',
    'products','professional','program','project','property','provide','provided','public','published','put',
    'quality','question','questions','quickly','quite','r','race','radio','range','rate','rather','re','read',
    'reading','ready','real','really','reason','received','recent','recently','record','red','region','related',
    'relationship','release','released','remember','report','reported','required','research','respect','response',
    'rest','result','results','return','review','right','rights','risk','river','road','rock','role','room','round',
    'rules','run','running','russian','s','safe','said','sales','same','save','saw','say','saying','says','school',
    'schools','science','sea','season','second','section','security','see','seeing','seem','seems','seen','self',
    'send','sense','sent','september','series','serious','service','services','set','seven','several','sex','shall',
    'share','she','shit','short','shot','should','show','shows','side','sign','similar','simple','simply','since',
    'single','sir','site','situation','six','size','sleep','small','so','social','society','some','someone',
    'something','sometimes','son','song','soon','sorry','sort','sound','source','south','space','speak','special',
    'specific','speed','spent','st','staff','stage','stand','standard','star','start','started','starting','state',
    'states','station','stay','step','still','stop','store','story','straight','street','strong','student','students',
    'study','stuff','style','success','such','summer','super','support','sure','system','systems','t','table','take',
    'taken','takes','taking','talk','talking','tax','team','technology','tell','ten','term','terms','test','text',
    'than','thank','thanks','that','the','their','them','themselves','then','there','these','they','thing','things',
    'think','thinking','third','this','those','though','thought','three','through','time','times','title','to',
    'today','together','told','tomorrow','tonight','too','took','top','total','towards','town','track','trade',
    'training','travel','treatment','tried','true','trust','truth','try','trying','turn','turned','tv','two','type',
    'u','uk','under','understand','union','united','university','until','up','upon','us','use','used','using',
    'usually','v','value','various','version','very','via','video','view','visit','voice','vote','w','wait','waiting',
    'walk','wall','wanna','want','wanted','wants','war','was','washington','watch','watching','water','way','ways',
    'we','website','week','weeks','weight','well','went','were','west','western','what','whatever','when','where',
    'whether','which','while','white','who','whole','whose','why','wife','will','win','wish','with','within',
    'without','woman','women','won','word','words','work','worked','working','works','world','worth','would','write',
    'writing','written','wrong','wrote','x','y','yeah','year','years','yes','yet','york','you','young','your',
    'yourself']);

  function esc(s) {{ const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }}

  // Lightweight client-side topic extraction - not the server-side pipeline's
  // TF-IDF + K-Means (that needs a Python ML stack this static page doesn't
  // have), just word/phrase frequency: English words (3+ letters, stopwords
  // dropped) and, since there's no jieba segmenter in the browser either,
  // frequent 2-4 character substrings within each contiguous CJK run as an
  // approximation of Chinese compound words. Good enough to surface "what
  // does this upload mostly talk about," not a claim of real clustering.
  // Same reasoning as EN_STOPWORDS - geography and generic business/
  // economy words read as noise in an "industry topics" list, not signal.
  const ZH_TOPIC_STOPWORDS = new Set(['中國','中國大陸','大陸','美國','台灣','日本','韓國','歐洲','全球','世界',
    '國際','國內','本地','地區','區域','公司','企業','商業','市場','產業','行業','經濟','政府','國家','國家隊',
    '集團','廠商','業者','今天','今年','昨天','明天','目前','最近','報導','新聞','指出','表示']);

  // Ported from cluster_topics.py's CHINESE_NOISE_SUBSTRINGS/CHINESE_UNIT_TOKEN_RE
  // (kept in sync by hand - no shared module between Python and this
  // embedded page's JS). An exact-match stopword set alone can't catch a
  // combinatorial family like [億|萬|千|兆][元|日圓|美元|韓元] - same gap
  // that motivated the server-side pattern version in the first place, and
  // this client-side extractor had the same fixed-list-only version of the
  // problem until now.
  const ZH_NOISE_RE = new RegExp([
    '年增','年減','月增','月減','季增','季減','去年','今年','明年',
    '本季','上季','下季','本月','上月','下月','同期','同比',
    '目前','近期','日前','日起','報導','指出','表示','預估','預計','據悉',
    '營收','獲利','毛利率','目標價','股價','創新高','創下','新高','新低',
    '百分點','歷史新高','央行','因此','經濟日報','導讀','reurl',
    '年的','年至','年間','年以來','過去',
  ].join('|'));
  const ZH_UNIT_TOKEN_RE = /^[0-9億萬千兆]+[元日圓韓美歐]{0,2}$/;

  // General-purpose sweep for ordinary Chinese words, mirroring
  // is_common_chinese_word() on the server side (cluster_topics.py) - every
  // 2-4 character word from wordfreq's Chinese frequency list scoring
  // zipf_frequency >= 5.1, generated once and pasted here since wordfreq's
  // data isn't available client-side. Catches generic connectives (雖然,
  // 但是, 根據, 認為, 進行, 相關, 影響, ...) that a hand-picked list would
  // otherwise keep missing one at a time - same reasoning as
  // EN_COMMON_WORDS above. wordfreq's Chinese wordlist is Simplified, so
  // each candidate term is converted via the existing toSimplified() (see
  // above, built for the Chinese sentiment scorer) before checking
  // membership here, rather than maintaining a second, Traditional copy of
  // this list by hand.
  const ZH_COMMON_WORDS = new Set(['一下','一个','一些','一位','一切','一名','一场','一天','一定','一年','一是','一条','一样','一次','一点','一直','一种','一般','一起','三个','上年','上海',
    '下来','不上','不了','不仅','不会','不再','不到','不同','不少','不想','不断','不是','不能','不要','不说','不过','不错','与会','专业','专家','世界','世纪',
    '业务','东西','两个','严重','个人','中共','中国','中央','中年','中心','中是','中有','中说','为了','为什么','主任','主席','主要','举办','举行','之一','之前',
    '之后','之间','也许','了解','事件','事实','事情','亚洲','交易','交流','交通','产业','产品','产生','人中','人们','人到','人口','人员','人士','人数','人有',
    '人来','人民','人物','人类','人要','什么','今天','今年','今日','介绍','仍然','他于','他们','代表','以上','以下','以为','以前','以及','以后','以来','价值',
    '价格','任何','任务','企业','会为','会后','会议','传统','但是','位于','体系','体育','作为','作品','作用','作者','你们','使用','例如','俄罗斯','保护','保持',
    '保证','信息','健康','儿子','儿童','允许','先生','免费','全国','全球','全部','全面','公司','公布','公开','公民','共同','关于','关注','关系','其中','其他',
    '其实','具有','内容','内部','军事','军队','农业','农村','农民','决定','准备','减少','几个','几乎','出来','出版','出现','分享','分别','分析','分钟','创新',
    '创造','利用','利益','别人','到底','制作','制度','制造','力量','办法','功能','加入','加强','加拿大','动物','努力','包括','北京','区域','医疗','医院','十分',
    '协会','协议','单位','印度','即使','历史','压力','原则','原因','原来','参与','参加','双方','反对','反应','发展','发布','发现','发生','发表','取得','受到',
    '变化','变成','另外','只是','只有','只能','只要','可以','可是','可能','台湾','各种','合作','同志','同意','同时','同样','名字','后人','后来','告诉','和平',
    '哪里','唯一','喜欢','回到','回来','因为','因此','团体','困难','国内','国务院','国家','国际','图片','土地','在于','地区','地方','坚持','城市','基本','基础',
    '增加','增长','声音','处理','外国','多少','大为','大学','大家','大有','大量','大陆','失去','失败','女人','女儿','女孩','女性','她们','好像','如何','如果',
    '如此','妈妈','委员','委员会','媒体','存在','学习','学校','学生','学院','孩子','它们','安全','完全','完成','宗教','官员','官方','实施','实现','实行','实际',
    '宣传','宣布','宪法','家庭','容易','对于','导致','小时','小说','尤其','就是','尽管','居民','属于','工业','工作','工具','工程','已经','市场','希望','带来',
    '帮助','平台','年代','年会','并且','广告','应用','应该','建立','建筑','建议','建设','开发','开始','开放','引起','强调','当地','当时','当然','形式','形成',
    '影响','很多','得到','德国','必须','快乐','态度','怎么','怎么样','怎样','思想','总是','总理','总统','情况','想要','意义','意思','意见','意识','感到','感觉',
    '愿意','成为','成功','成员','成立','我们','我国','我来','或者','战争','战略','所以','所有','所谓','手机','才能','执行','找到','承认','技术','投资','报告',
    '报道','担心','拥有','持续','指出','接受','控制','推动','提供','提出','提高','支持','改变','改革','攻击','政府','政权','政治','政策','故事','教师','教授',
    '教育','数字','数据','整个','文化','文章','新闻','方向','方式','方案','方法','方面','旅游','无法','无论','日本','时代','时候','时期','时间','明显','明白',
    '是不是','是否','显示','晚上','更加','曾经','最后','最大','最好','最终','最近','最高','有些','有人','有关','有效','有没有','有点','朋友','服务','期间','未来',
    '机会','机关','机场','机构','权利','权力','条件','来个','来源','来自','来说','标准','根据','根本','检查','模式','欢迎','欧洲','正在','正常','正式','正确',
    '此外','武器','死亡','母亲','每个','每天','每年','比赛','比较','民主','民族','水平','永远','汽车','没有','法国','法律','注意','活动','消息','清楚','游戏',
    '然后','然而','照片','父亲','版本','特别','状况','状态','独立','环境','现代','现在','现场','现象','理解','理论','甚至','生产','生命','生活','用户','由于',
    '申请','电子','电影','电脑','电视','电话','男人','的话','目前','目标','目的','直接','相信','相关','相当','看到','看来','看看','看见','真实','真是','真正',
    '真的','知识','知道','研究','破坏','确定','确实','社会','社区','离开','科学','科技','积极','程度','稳定','空间','突然','第一','第一次','第二','简单','管理',
    '类似','精神','系列','系统','组成','组织','终于','经历','经常','经济','经营','经过','经验','结合','结束','结构','结果','绝对','统一','继续','综合','编辑',
    '网站','网络','美元','美国','群众','老师','考虑','而且','而是','职业','联合','联盟','联系','肯定','能为','能力','能够','自己','自然','自由','至少','艺术',
    '节目','苏联','英国','范围','获得','虽然','行业','行为','行动','行政','表现','表示','西方','要求','观点','规定','规律','规模','视频','觉得','解决','解释',
    '警察','计划','认为','认识','讨论','训练','记录','记者','许多','设备','设立','设计','证明','语言','说明','说来','说话','调整','调查','谢谢','负责','责任',
    '质量','购买','资料','资本','资源','资金','起来','超过','足球','身上','身体','达到','过去','过程','运动','还是','还有','还要','这个','这么','这些','这会',
    '这是','这有','这样','这次','这种','这里','进一步','进入','进行','选举','选择','逐渐','通过','造成','那个','那么','那些','那样','那里','部分','部长','部门',
    '采取','采用','里面','重新','重点','重要','金融','银行','销售','错误','长期','问题','阅读','阶段','附近','限制','除了','集团','需求','需要','青年','非常',
    '面积','革命','韩国','音乐','项目','领域','领导','飞机','首先','香港']);

  function isChineseNoiseTerm(term) {{
    return ZH_UNIT_TOKEN_RE.test(term) || ZH_NOISE_RE.test(term) || ZH_COMMON_WORDS.has(toSimplified(term));
  }}

  function extractTopics(rows, textCol, topN) {{
    const counts = new Map();
    const bump = (term) => counts.set(term, (counts.get(term) || 0) + 1);
    for (const r of rows) {{
      const text = String(r[textCol] || '');
      const seenInRow = new Set();
      for (const w of text.toLowerCase().match(/[a-z]{{3,}}/g) || []) {{
        if (EN_STOPWORDS.has(w) || EN_COMMON_WORDS.has(w) || seenInRow.has(w)) continue;
        seenInRow.add(w);
        bump(w);
      }}
      for (const run of text.match(/[一-鿿]{{2,}}/g) || []) {{
        for (let len = 2; len <= 4 && len <= run.length; len++) {{
          for (let i = 0; i + len <= run.length; i++) {{
            const term = run.slice(i, i + len);
            if (seenInRow.has(term) || ZH_TOPIC_STOPWORDS.has(term) || isChineseNoiseTerm(term)) continue;
            seenInRow.add(term);
            bump(term);
          }}
        }}
      }}
    }}
    // A term in nearly every row is boilerplate (a shared connector word,
    // a recurring sign-off), not a topic - a real topic differentiates
    // SOME posts from others. Cap at 60% of rows so frequency alone can't
    // let a common word crowd out the actual distinguishing terms.
    const maxAllowed = Math.max(2, Math.floor(rows.length * 0.6));
    return [...counts.entries()]
      .filter(([, c]) => c >= 2 && c <= maxAllowed)
      .sort((a, b) => b[1] - a[1])
      .slice(0, topN)
      .map(([term, count]) => ({{ term, count }}));
  }}

  function assignTopic(text, topics) {{
    const lower = String(text || '').toLowerCase();
    for (const {{ term }} of topics) {{
      if (lower.includes(term)) return term;
    }}
    return '(other)';
  }}

  function sortUploadRows(rows, key) {{
    const sorted = rows.slice();
    const sentimentRank = {{ positive: 2, neutral: 1, negative: 0 }};
    switch (key) {{
      case 'time_desc': return sorted.sort((a, b) => (b.converted_timestamp_utc8 || '').localeCompare(a.converted_timestamp_utc8 || ''));
      case 'time_asc': return sorted.sort((a, b) => (a.converted_timestamp_utc8 || '').localeCompare(b.converted_timestamp_utc8 || ''));
      case 'sentiment_desc': return sorted.sort((a, b) => sentimentRank[b.sentiment] - sentimentRank[a.sentiment]);
      case 'sentiment_asc': return sorted.sort((a, b) => sentimentRank[a.sentiment] - sentimentRank[b.sentiment]);
      case 'topic': return sorted.sort((a, b) => a.topic.localeCompare(b.topic));
      case 'interaction_desc': return sorted.sort((a, b) => b._interaction - a._interaction);
      case 'views_desc': return sorted.sort((a, b) => (b._views || 0) - (a._views || 0));
      default: return sorted;
    }}
  }}

  function renderUploadPostsPage(rows, meta) {{
    const container = document.getElementById('upload-posts-table');
    const pagerEl = document.getElementById('upload-pager');
    const term = uploadSearchTerm.trim().toLowerCase();
    const filtered = term
      ? rows.filter(r => String(r[meta.textCol] || '').toLowerCase().includes(term) || r.topic.toLowerCase().includes(term))
      : rows;
    const sorted = sortUploadRows(filtered, uploadSortKey);
    const totalPages = Math.max(1, Math.ceil(sorted.length / UPLOAD_PAGE_SIZE));
    uploadPage = Math.min(Math.max(1, uploadPage), totalPages);
    const start = (uploadPage - 1) * UPLOAD_PAGE_SIZE;
    const pageRows = sorted.slice(start, start + UPLOAD_PAGE_SIZE);

    const extraCols = (meta.hasEngagement ? 1 : 0) + (meta.hasViews ? 1 : 0);
    const bodyRows = pageRows.map(r => `
      <tr><td>${{esc(r[meta.textCol] || '').slice(0, 90)}}</td>
      <td>${{esc(r.topic)}}</td>
      <td>${{esc(r.converted_timestamp_utc8 || '—')}}</td>
      <td><span class="badge status-${{r.sentiment === 'positive' ? 'active' : r.sentiment === 'negative' ? 'inactive' : 'stale'}}">${{esc(r.sentiment)}}</span></td>
      ${{meta.hasEngagement ? `<td class="num">${{r._interaction.toLocaleString()}}</td>` : ''}}
      ${{meta.hasViews ? `<td class="num">${{(r._views || 0).toLocaleString()}}</td>` : ''}}</tr>`).join('')
      || `<tr><td colspan="${{4 + extraCols}}" class="empty">No posts match "${{esc(term)}}".</td></tr>`;
    container.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Text</th><th>Topic</th><th>Converted (UTC+8)</th><th>Sentiment</th>
      ${{meta.hasEngagement ? '<th class="num">Interaction</th>' : ''}}
      ${{meta.hasViews ? '<th class="num">Views</th>' : ''}}
      </tr></thead><tbody>${{bodyRows}}</tbody></table></div>`;

    const pageBtns = [];
    for (let p = 1; p <= totalPages; p++) {{
      pageBtns.push(`<button class="page-btn${{p === uploadPage ? ' active' : ''}}" data-page="${{p}}">${{p}}</button>`);
    }}
    pagerEl.innerHTML = totalPages > 1
      ? `<div class="pager">${{pageBtns.join('')}}</div><p class="muted">Showing ${{start + 1}}-${{Math.min(start + UPLOAD_PAGE_SIZE, sorted.length)}} of ${{sorted.length}}</p>`
      : `<p class="muted">${{sorted.length}} post(s)</p>`;
    pagerEl.querySelectorAll('.page-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        uploadPage = Number(btn.dataset.page);
        renderUploadPostsPage(rows, meta);
      }});
    }});
  }}

  function renderUploadResults(rows, fields, meta) {{
    const container = document.getElementById('upload-results');
    if (!rows.length) {{
      container.innerHTML = '<p class="empty">No rows found (or no recognizable text column).</p>';
      return;
    }}
    const counts = {{ positive: 0, neutral: 0, negative: 0 }};
    let withOffset = 0;
    for (const r of rows) {{
      counts[r.sentiment]++;
      if (r._hadOffset) withOffset++;
    }}
    const total = rows.length;
    const rankRows = Object.entries(counts).map(([k, v]) =>
      `<tr><td class="cell-primary">${{k}}</td><td class="num">${{v}}</td><td class="num">${{Math.round(v/total*1000)/10}}%</td></tr>`).join('');

    const topics = extractTopics(rows, meta.textCol, 12);
    for (const r of rows) r.topic = assignTopic(r[meta.textCol], topics);
    const topicRows = topics.map(({{ term, count }}) =>
      `<tr><td class="cell-primary">${{esc(term)}}</td><td class="num">${{count}}</td></tr>`).join('');

    uploadSortKey = 'time_desc'; uploadPage = 1; uploadSearchTerm = '';

    container.innerHTML = `
      <div class="stat-grid" style="margin-bottom:16px">
        <div class="stat"><div class="stat-num">${{total}}</div><div class="stat-label">Rows</div></div>
        <div class="stat"><div class="stat-num">${{withOffset}}/${{total}}</div><div class="stat-label">Had explicit offset</div></div>
      </div>
      <div class="col-2">
        <div>${{table_(['Sentiment', '#Count', '#Share'], rankRows)}}</div>
        <div>
          ${{table_(['Topic', '#Mentions'], topicRows || '<tr><td colspan="2" class="empty">Not enough repeated words to surface a topic.</td></tr>')}}
        </div>
      </div>
      <div class="upload-posts-toolbar">
        <label class="upload-search-label">Search a topic or keyword
          <input type="text" id="upload-search-input" placeholder="e.g. nvidia, tariff, dram..." autocomplete="off">
        </label>
        <label>Sort by
          <select id="upload-sort-select">
            <option value="time_desc">Newest first</option>
            <option value="time_asc">Oldest first</option>
            <option value="sentiment_desc">Sentiment: positive first</option>
            <option value="sentiment_asc">Sentiment: negative first</option>
            <option value="topic">Topic (A-Z)</option>
            ${{meta.hasEngagement ? '<option value="interaction_desc">Interaction: most first</option>' : ''}}
            ${{meta.hasViews ? '<option value="views_desc">Views: most first</option>' : ''}}
          </select>
        </label>
      </div>
      <div id="upload-posts-table"></div>
      <div id="upload-pager"></div>
      <div class="download-row">
        <button class="btn" id="download-csv-btn">Download CSV</button>
        <button class="btn" id="download-xlsx-btn">Download Excel</button>
        <button class="btn" id="download-print-btn">Print / Save as PDF</button>
      </div>
    `;
    function table_(headers, rowsHtml) {{
      const head = headers.map(h => h.startsWith('#') ? `<th class="num">${{h.slice(1)}}</th>` : `<th>${{h}}</th>`).join('');
      return `<div class="table-wrap"><table><thead><tr>${{head}}</tr></thead><tbody>${{rowsHtml}}</tbody></table></div>`;
    }}

    renderUploadPostsPage(rows, meta);
    document.getElementById('upload-sort-select').addEventListener('change', e => {{
      uploadSortKey = e.target.value;
      uploadPage = 1;
      renderUploadPostsPage(rows, meta);
    }});
    document.getElementById('upload-search-input').addEventListener('input', e => {{
      uploadSearchTerm = e.target.value;
      uploadPage = 1;
      renderUploadPostsPage(rows, meta);
    }});

    const outFields = [...fields, 'converted_timestamp_utc8', 'sentiment', 'topic'];
    document.getElementById('download-csv-btn').onclick = () => {{
      const lines = [outFields.map(csvCell).join(',')];
      for (const r of rows) lines.push(outFields.map(f => csvCell(r[f])).join(','));
      downloadBlob(uploadedBaseName + '_analyzed.csv', lines.join('\\n'), 'text/csv');
    }};
    document.getElementById('download-xlsx-btn').onclick = () => {{
      const sheetRows = rows.map(r => Object.fromEntries(outFields.map(f => [f, r[f] ?? ''])));
      const sheet = XLSX.utils.json_to_sheet(sheetRows, {{ header: outFields }});
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, sheet, 'Analyzed');
      XLSX.writeFile(wb, uploadedBaseName + '_analyzed.xlsx');
    }};
    document.getElementById('download-print-btn').onclick = () => window.print();
  }}

  function analyzeUploadedRows(parsed) {{
    if (!parsed.length) {{
      document.getElementById('upload-results').innerHTML = '<p class="empty">Could not parse any rows from this file.</p>';
      return;
    }}
    const fields = Object.keys(parsed[0]);
    const textCol = detectColumn(fields, TEXT_COLUMN_CANDIDATES);
    const tsCol = detectColumn(fields, TIMESTAMP_COLUMN_CANDIDATES);
    if (!textCol) {{
      document.getElementById('upload-results').innerHTML = '<p class="empty">No text/content/message column found in this file.</p>';
      return;
    }}
    const engagementCols = detectColumns(fields, ENGAGEMENT_COLUMN_CANDIDATES);
    const viewsCol = detectColumn(fields, VIEWS_COLUMN_CANDIDATES);
    const sourceTz = document.getElementById('upload-tz').value;
    const rows = parsed.map(r => {{
      const {{ date, hadOffset }} = tsCol ? parseUploadTimestamp(String(r[tsCol] ?? ''), sourceTz) : {{ date: null, hadOffset: false }};
      return {{
        ...r,
        sentiment: classifySentiment(r[textCol]),
        converted_timestamp_utc8: date ? new Intl.DateTimeFormat('sv-SE', {{
          timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
        }}).format(date).replace(' ', 'T') + '+08:00' : null,
        _hadOffset: hadOffset,
        _interaction: engagementCols.reduce((sum, c) => sum + parseCount(r[c]), 0),
        _views: viewsCol ? parseCount(r[viewsCol]) : null,
      }};
    }});
    uploadedRows = rows; uploadedFields = fields;
    renderUploadResults(rows, fields, {{ textCol, tsCol, hasEngagement: engagementCols.length > 0, hasViews: !!viewsCol }});
  }}

  document.getElementById('upload-file').addEventListener('change', e => {{
    const file = e.target.files[0];
    if (!file) return;
    const isExcel = /\.xlsx$/i.test(file.name);
    uploadedBaseName = file.name.replace(/\.(csv|xlsx)$/i, '');
    const reader = new FileReader();
    reader.onload = () => {{
      if (isExcel) {{
        const wb = XLSX.read(reader.result, {{ type: 'array' }});
        const sheet = wb.Sheets[wb.SheetNames[0]];
        analyzeUploadedRows(XLSX.utils.sheet_to_json(sheet, {{ defval: '' }}));
      }} else {{
        analyzeUploadedRows(parseCsv(reader.result));
      }}
    }};
    if (isExcel) reader.readAsArrayBuffer(file);
    else reader.readAsText(file);
  }});
</script>
</body></html>"""

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Wrote dashboard to {OUT_FILE}")


if __name__ == '__main__':
    main()
