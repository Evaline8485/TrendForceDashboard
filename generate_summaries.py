"""
FR-06 Daily Executive Summaries.

Generates a concise zh-TW key-point summary per analysis item, drawing on
FR-01 (topic gaps), FR-02 (rising topics/KOLs), and FR-03 (sentiment
temperature/shifts, engagement highlights). Each summary is templated from
the underlying numbers rather than free-form generated text, so it stays
factual and reproducible; FR-04's manual review layer spot-checks the
result (see manual_review.py's 'summary' record type).

Length constraint: 100-170 characters (counted as Python string length,
i.e. one count per character including any English terms embedded in the
zh-TW text; raised from the original 80-120 on 2026-08-13 to make room
for a concrete observation+suggestion clause per item - naming a specific
account/topic to act on, not just a generic "建議持續追蹤"). build_summary()
assembles short factual clauses one at a time and stops before the next
clause would exceed the max, then pads with a final factual monitoring
note if still under the minimum.

Coverage (one summary per item, per SRS 4.6):
  - topic_gap        top N FR-01 gaps by competitor engagement
  - rising_topic     top N FR-02 rising topics across platforms
  - sentiment        one daily sentiment-temperature-and-shift summary
  - engagement       top N FR-03 highest-engagement topics

Output: analysis/daily_summaries.json
"""
import json
import os
from datetime import datetime, timezone

import nlp_sentiment

BASE = os.path.dirname(__file__)
TOPIC_CLUSTERS_FILE = os.path.join(BASE, 'analysis', 'topic_clusters.json')
FUZZY_TRENDS_FILE = os.path.join(BASE, 'analysis', 'fuzzy_trends.json')
OUT_FILE = os.path.join(BASE, 'analysis', 'daily_summaries.json')

MIN_LEN, MAX_LEN = 100, 170
TOP_N_GAPS = 5
TOP_N_RISING = 5
TOP_N_ENGAGEMENT = 3

CLOSING_NOTES = [
    "建議持續追蹤後續發展, 適時調整報導與互動策略。",
    "建議納入每日監測重點, 觀察後續數據變化。",
    "建議提前規劃因應方案, 掌握議題主導權。",
]


def build_summary(clauses, closing_idx=0):
    """Join clauses with '。' up to MAX_LEN; pad with a closing note if
    still under MIN_LEN once all clauses are used."""
    text = ''
    for clause in clauses:
        candidate = text + clause + '。'
        if len(candidate) > MAX_LEN and text:
            continue  # this clause doesn't fit; a later, shorter one might
        text = candidate
    if len(text) < MIN_LEN:
        note = CLOSING_NOTES[closing_idx % len(CLOSING_NOTES)]
        candidate = text + note
        text = candidate[:MAX_LEN] if len(candidate) > MAX_LEN else candidate
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN]
    return text


def fmt_int(n):
    return f"{n:,}"


def summarize_topic_gaps():
    if not os.path.exists(TOPIC_CLUSTERS_FILE):
        return []
    with open(TOPIC_CLUSTERS_FILE, encoding='utf-8') as f:
        data = json.load(f)

    summaries = []
    gaps = sorted(data.get('gaps', []), key=lambda g: g['competitor_engagement'], reverse=True)[:TOP_N_GAPS]
    for i, g in enumerate(gaps):
        competitors = '、'.join(g['competitors_covering'][:3])
        # sample_posts is already sorted by interaction desc and filtered
        # to competitor-authored posts (see cluster_topics.py), so [0] is
        # literally the single highest-engagement competitor post in this
        # gap - naming its account/engagement gives a concrete benchmark
        # ("go look at what THIS account did") instead of a vague "建議
        # 優先規劃選題切入".
        leading = g['sample_posts'][0] if g.get('sample_posts') else None
        clauses = [
            f"「{g['label']}」議題出現內容缺口",
            f"對手{competitors}等已發布{fmt_int(g['competitor_count'])}篇, 互動量達{fmt_int(g['competitor_engagement'])}",
            f"我方僅{fmt_int(g['own_count'])}篇覆蓋",
        ]
        if leading:
            clauses.append(f"其中{leading['handle']}單篇互動即達{fmt_int(leading['interaction'])}, 可作為切角參考")
            clauses.append(f"建議優先參考{leading['handle']}報導方向, 規劃「{g['label']}」原創內容搶佔話題")
        else:
            clauses.append("建議優先規劃選題切入, 縮小報導落差")
        text = build_summary(clauses, closing_idx=i)
        summaries.append({
            'category': 'topic_gap',
            'ref': {'cluster_id': g['cluster_id'], 'label': g['label']},
            'text': text,
            'char_count': len(text),
        })
    return summaries


def summarize_rising_topics():
    if not os.path.exists(FUZZY_TRENDS_FILE):
        return []
    with open(FUZZY_TRENDS_FILE, encoding='utf-8') as f:
        data = json.load(f)

    all_topics = []
    for platform, pdata in data.get('platforms', {}).items():
        for t in pdata.get('top_rising_topics', []):
            all_topics.append((platform, t))
    all_topics.sort(key=lambda pt: pt[1]['rising_score'], reverse=True)

    summaries = []
    for i, (platform, t) in enumerate(all_topics[:TOP_N_RISING]):
        top_kol = t['rising_kols'][0] if t['rising_kols'] else None
        # Built from t's own numeric fields rather than reusing
        # t['rationale'] (topic_rationale() in fuzzy_trend.py, English -
        # "volume +12900% vs prior 7d, engagement rate +0%, ...") which
        # read as an odd English fragment dropped into a zh-TW summary.
        topic_growth_pct = round(t['volume_growth_rate'] * 100)
        clauses = [
            f"{platform}平台「{t['label']}」議題升溫, 熱度評分{t['rising_score']}",
            f"近7日發文量成長{topic_growth_pct}%, 共{t['active_accounts']}個帳號參與討論",
        ]
        if top_kol:
            growth_pct = round(top_kol['volume_growth_rate'] * 100)
            clauses.append(f"領先帳號為{top_kol['handle']}, 發文量成長{growth_pct}%")
            clauses.append(f"建議評估跟進{top_kol['handle']}報導角度, 搶佔擴散初期聲量")
        else:
            clauses.append("建議關注後續擴散")
        text = build_summary(clauses, closing_idx=i)
        summaries.append({
            'category': 'rising_topic',
            'ref': {'platform': platform, 'topic_id': t['topic_id'], 'label': t['label']},
            'text': text,
            'char_count': len(text),
        })
    return summaries


def summarize_sentiment(daily_dashboard):
    w = daily_dashboard['widgets']
    overview = w['sentiment_overview']
    curve = w['sentiment_trend_curve']
    heat_top = w['temperature_bar'][0] if w['temperature_bar'] else None

    total = overview['total_posts']
    share = overview['sentiment_share']
    pos_pct = round(share.get('positive', 0) * 100, 1)
    neu_pct = round(share.get('neutral', 0) * 100, 1)
    neg_pct = round(share.get('negative', 0) * 100, 1)

    clauses = [f"過去24小時共監測{fmt_int(total)}篇貼文, 正面{pos_pct}%、中立{neu_pct}%、負面{neg_pct}%"]

    direction = None
    if len(curve) >= 2:
        prev, last = curve[-2], curve[-1]
        prev_total = prev['positive'] + prev['neutral'] + prev['negative']
        last_total = last['positive'] + last['neutral'] + last['negative']
        prev_pos_share = prev['positive'] / prev_total if prev_total else 0
        last_pos_share = last['positive'] / last_total if last_total else 0
        delta_pts = round((last_pos_share - prev_pos_share) * 100, 1)
        direction = '上升' if delta_pts >= 0 else '下降'
        clauses.append(f"正面聲量較前一時段{direction}{abs(delta_pts)}個百分點")

    if heat_top:
        clauses.append(f"最高熱度議題為「{heat_top['label']}」, 熱度評分{heat_top['heat']}")
        # Concrete, topic-named suggestion instead of no suggestion clause
        # at all (the pre-2026-08-13 version stopped at facts) - direction
        # of the recent shift decides whether the ask is "defend" (sinking
        # sentiment) or "capitalize" (rising sentiment) on this specific
        # heat-leading topic.
        if direction == '下降':
            clauses.append(f"建議就「{heat_top['label']}」規劃正面視角報導, 緩解負面聲量擴散")
        else:
            clauses.append(f"建議延伸「{heat_top['label']}」正面討論, 強化品牌聲量")

    text = build_summary(clauses, closing_idx=0)
    return [{
        'category': 'sentiment',
        'ref': {'total_posts': total},
        'text': text,
        'char_count': len(text),
    }]


def summarize_engagement(daily_dashboard):
    ranking = daily_dashboard['widgets']['top_engagement_ranking'][:TOP_N_ENGAGEMENT]
    summaries = []
    for i, r in enumerate(ranking):
        clauses = [
            f"互動最高議題為「{r['label']}」",
            f"累計互動{fmt_int(r['total_engagement'])}, 發文{r['post_count']}篇",
        ]
        if r.get('top_account'):
            share_pct = round(r['top_account_engagement'] / r['total_engagement'] * 100) if r['total_engagement'] else 0
            clauses.append(f"{r['top_account']}貢獻其中{share_pct}%互動, 為該議題主要聲量來源")
            clauses.append(f"建議參考{r['top_account']}內容方向, 延伸相關報導承接討論熱度")
        else:
            clauses.append("建議延伸相關報導, 承接既有討論熱度")
        text = build_summary(clauses, closing_idx=i)
        summaries.append({
            'category': 'engagement',
            'ref': {'topic_id': r['topic_id'], 'label': r['label']},
            'text': text,
            'char_count': len(text),
        })
    return summaries


def main():
    # Sentiment/engagement summaries need the "Daily" cadence the SRS
    # specifies for FR-06, regardless of what time_range nlp_sentiment.py
    # was last run with for other purposes.
    nlp_sentiment.main(time_range='1d')
    with open(nlp_sentiment.range_out_file('1d'), encoding='utf-8') as f:
        daily_dashboard = json.load(f)

    summaries = []
    summaries.extend(summarize_topic_gaps())
    summaries.extend(summarize_rising_topics())
    summaries.extend(summarize_sentiment(daily_dashboard))
    summaries.extend(summarize_engagement(daily_dashboard))

    for i, s in enumerate(summaries):
        s['id'] = f"{s['category']}-{i}"

    out_of_range = [s for s in summaries if not (MIN_LEN <= s['char_count'] <= MAX_LEN)]
    if out_of_range:
        print(f"Warning: {len(out_of_range)} summaries fell outside {MIN_LEN}-{MAX_LEN} chars.")

    now = datetime.now(timezone.utc)
    result = {'generated_at': now.isoformat(), 'date': now.date().isoformat(), 'summaries': summaries}

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(summaries)} daily summaries to {OUT_FILE}")


if __name__ == '__main__':
    main()
