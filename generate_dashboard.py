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


def load_topic_name_en():
    """code -> English topic name (topic_taxonomy.json's own 'name' field
    is Chinese-only - the source spreadsheet never had an English topic-
    name column, just per-keyword zh/en pairs - so name_en is a hand-
    written translation added alongside it, not derived from the sheet."""
    path = os.path.join(BASE, 'topic_taxonomy.json')
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        taxonomy = json.load(f)
    return {t['code']: t.get('name_en', '') for t in taxonomy}


def load_topic_keyword_pairs():
    """code -> list of {zh, en} keyword pairs, straight from the
    關鍵字(中文)/關鍵字(英文/別名) columns of the source spreadsheet
    (topic_taxonomy.json's keyword_pairs, one dict per original row -
    unlike its flat 'keywords' list used for matching, this keeps each
    row's zh/en pairing intact for display)."""
    path = os.path.join(BASE, 'topic_taxonomy.json')
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        taxonomy = json.load(f)
    return {t['code']: t.get('keyword_pairs', []) for t in taxonomy}


def matched_keyword_pairs(pairs, texts, max_pairs=4):
    """Which of this topic's zh/en keyword pairs actually appear in the
    given post texts (case-insensitive on the English side, plain
    substring on the Chinese side) - a Topic Digest card's tags should be
    the real matched evidence for why these posts landed in this topic,
    not a guess split from the topic's own name."""
    joined = ' '.join(texts)
    joined_lower = joined.lower()
    matched = []
    for pair in pairs:
        zh, en = pair.get('zh'), pair.get('en')
        hit = (zh and zh in joined) or (en and en.lower() in joined_lower)
        if hit:
            matched.append(pair)
        if len(matched) >= max_pairs:
            break
    return matched


def pair_label(pair):
    zh, en = pair.get('zh'), pair.get('en')
    if zh and en and zh != en:
        return f"{zh} ({en})"
    return zh or en or ''


def post_matches_pair(text, pair):
    zh, en = pair.get('zh'), pair.get('en')
    return bool((zh and zh in text) or (en and en.lower() in text.lower()))



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
    topic_name_en = load_topic_name_en()
    topic_keyword_pairs = load_topic_keyword_pairs()
    cards = []
    for g in gaps:
        samples = g.get('sample_posts') or []
        if not samples:
            continue
        top = samples[0]
        name_en = topic_name_en.get(g['cluster_id'])
        pairs = topic_keyword_pairs.get(g['cluster_id'], [])
        matched = matched_keyword_pairs(pairs, [p.get('text', '') for p in samples])
        if not matched:
            matched = pairs[:4]  # no textual hit in the (truncated) sample text - fall back to the topic's own defined keywords

        # Title is the single most SPECIFIC matched keyword, not the broad
        # topic name - "台股市場" covers everything from TAIEX futures to
        # margin-trading ranking, not a useful card identity on its own.
        # Prefer whichever keyword matched the single highest-engagement
        # post (top), since the card is effectively built around that post.
        primary = next((p for p in matched if post_matches_pair(top.get('text', ''), p)), None) or (matched[0] if matched else None)
        title = pair_label(primary) if primary else (f"{g['label']} / {name_en}" if name_en else g['label'])

        # Filter the posts this card actually shows down to only those
        # containing that specific keyword - previously every post that
        # matched the whole (broad) topic showed up regardless of which
        # specific keyword it hit, so a "TAIEX futures" card could show a
        # margin-trading post with nothing to do with futures.
        filtered_samples = [p for p in samples if post_matches_pair(p.get('text', ''), primary)] if primary else samples
        if not filtered_samples:
            filtered_samples = samples

        keywords = [pair_label(p) for p in matched if p is not primary][:4]
        timestamps = [parse_ts(p['timestamp']) for p in filtered_samples if p.get('timestamp')]
        timestamps = [t for t in timestamps if t]
        latest = max(timestamps) if timestamps else None
        bullets = ''.join(
            f'<li>{esc(p["text"][:120])}{"…" if len(p["text"]) > 120 else ""}</li>'
            for p in filtered_samples[:5]
        )
        sources = ''.join(f"""
          <div class="gap-source-row">
            <a href="{esc(p['url'])}" target="_blank" rel="noopener noreferrer">{esc(p['text'][:70])}{'…' if len(p['text']) > 70 else ''}</a>
            <span class="muted">— {esc(p['handle'])} ({esc(p['platform'])}){f", {esc(taiwan_str(parse_ts(p['timestamp'])))}" if p.get('timestamp') and parse_ts(p['timestamp']) else ''}</span>
          </div>""" for p in filtered_samples)
        cards.append(f"""
        <div class="gap-card">
          <div class="gap-card-title">{esc(title)}</div>
          <div class="gap-card-meta">
            <span class="muted">{len(filtered_samples)} 篇來源</span>
            {f'<span class="muted">{esc(taiwan_str(latest))}</span>' if latest else ''}
            {''.join(f'<span class="badge cat">{esc(k)}</span>' for k in keywords)}
            <span class="badge score">{fmt_int(g["competitor_engagement"])}</span>
          </div>
          <ul class="gap-card-bullets">{bullets}</ul>
          <details class="gap-card-expand">
            <summary>展開全文（{len(filtered_samples)} 篇）</summary>
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
TREND_BUCKET_W = 64  # px per bucket column, shared by both panels
TREND_LINE_H = 190   # net-sentiment plot area height
TREND_BAR_H = 76     # volume plot area height
TREND_LABEL_PAD = 46  # headroom above the line panel for peak/trough callout labels
TREND_GAP = 16       # gap between the two panels
TREND_BAR_W = 32


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
      <line class="trend-crosshair" x1="0" y1="0" x2="0" y2="{total_h}"/>
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
     Narrower, darker, tighter padding.

     One persistent element, reused across every bucket (see
     getTrendTooltipEl in the script below) - opacity is toggled via
     .visible rather than the element being created/destroyed per hover,
     so moving the pointer across adjacent columns cross-fades smoothly
     instead of re-triggering an entry animation (and mouseleave now
     fades out instead of vanishing instantly). pointer-events stays off
     while hidden so an invisible box at opacity 0 never intercepts
     clicks/hovers underneath it. */
  .trend-tooltip {{
    position: absolute; z-index: 30; background: var(--text); color: var(--surface);
    padding: 10px 14px; box-shadow: var(--shadow); max-width: 300px;
    max-height: 260px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;
    opacity: 0; pointer-events: none; transition: opacity 0.15s ease;
  }}
  .trend-tooltip.visible {{ opacity: 1; pointer-events: auto; }}
  @media (prefers-reduced-motion: reduce) {{ .trend-tooltip {{ transition: none; }} }}
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
    display: flex; justify-content: center;
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
  .trend-hit {{ cursor: pointer; fill: transparent; outline: none; }}
  /* Thin vertical guide instead of filling the whole (chart-height-tall)
     hit column on hover - a full-column fill reads as a big blocking
     rectangle; a hairline that tracks the pointer (same idea as the
     TrendforceTwitterScraper follower chart's own hover guide) is the
     lighter, more precise way to say "you're looking at this x". */
  .trend-crosshair {{ stroke: var(--border); stroke-width: 1; opacity: 0; transition: opacity 0.1s ease; pointer-events: none; }}
  .trend-crosshair.visible {{ opacity: 1; }}
  @media (prefers-reduced-motion: reduce) {{ .trend-crosshair {{ transition: none; }} }}
  .heatmap-row-label {{ font-size: 14px; fill: var(--text); }}
  @media (max-width: 800px) {{
    header, nav, main {{ padding-left: 18px; padding-right: 18px; }}
    .col-2 {{ grid-template-columns: 1fr; }}
    .panel {{ padding: 14px 16px; }}
  }}
  @media print {{
    nav, .range-bar {{ display: none !important; }}
    body {{ background: #fff; color: #111; }}
    .panel {{ background: #fff; border: 1px solid #ccc; box-shadow: none; break-inside: avoid; }}
    th, .badge, .stat-label {{ color: #444 !important; }}
    .badge {{ background: #eee !important; }}
  }}
</style>
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
      // The trend chart's tooltip/crosshair live in document.body, outside
      // any <section> - switching tabs doesn't fire a mouseout on whatever
      // .trend-hit was under the pointer, so without this it stays stuck
      // on screen, floating over whichever tab you switch to.
      hideTrendPointPopover();
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
  // One persistent tooltip element, reused for every bucket - toggling its
  // opacity via CSS transition (rather than creating/destroying a new div
  // per hover, which re-triggers an entry animation on every column change
  // and has no fade-out at all on leave) is what makes the mockup's own
  // tooltip feel smooth on rapid mousemove across adjacent columns.
  let trendTooltipEl = null;
  function getTrendTooltipEl() {{
    if (!trendTooltipEl) {{
      trendTooltipEl = document.createElement('div');
      trendTooltipEl.className = 'trend-tooltip';
      document.body.appendChild(trendTooltipEl);
    }}
    return trendTooltipEl;
  }}
  function showTrendPointPopover(hit) {{
    let data = null;
    try {{ data = JSON.parse(hit.dataset.payload || 'null'); }} catch (e) {{ data = null; }}
    if (!data) return;

    const svg = hit.closest('.trend-svg');
    const crosshair = svg?.querySelector('.trend-crosshair');
    if (crosshair) {{
      const cx = parseFloat(hit.getAttribute('x')) + parseFloat(hit.getAttribute('width')) / 2;
      crosshair.setAttribute('x1', cx);
      crosshair.setAttribute('x2', cx);
      crosshair.classList.add('visible');
    }}

    const pop = getTrendTooltipEl();
    pop.innerHTML = '';

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
    pop.classList.add('visible');
  }}
  function hideTrendPointPopover() {{
    if (trendTooltipEl) trendTooltipEl.classList.remove('visible');
    document.querySelectorAll('.trend-crosshair.visible').forEach(c => c.classList.remove('visible'));
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
    hideTrendPointPopover(); // sentiment-content is about to be replaced - don't leave a stale tooltip floating
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

</script>
</body></html>"""

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Wrote dashboard to {OUT_FILE}")


if __name__ == '__main__':
    main()
