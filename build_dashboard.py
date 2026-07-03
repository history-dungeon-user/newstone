# -*- coding: utf-8 -*-
# =====================================================================
#  뉴스톤 프로젝트 - 대시보드 생성 (GitHub Actions용) (build_dashboard.py)
# ---------------------------------------------------------------------
#  news_tone.py(뉴스 수집·채점) + krx_market.py(시장데이터) 실행 후,
#  이 스크립트가 DB를 읽어 최종 docs/index.html 대시보드를 만듭니다.
# =====================================================================

import os, sqlite3, datetime as dt
from zoneinfo import ZoneInfo

def log(*a): print(*a, flush=True)

DB_PATH   = "newstone.db"
DASH_PATH = "docs/index.html"
MIN_ARTICLES = 5

SECTORS = {
    "시장전체":   ["코스피", "코스닥", "증시"],
    "반도체":     ["반도체주", "반도체 실적", "반도체 수출"],
    "디스플레이": ["디스플레이주", "OLED 수주", "디스플레이 실적"],
    "2차전지":    ["2차전지주", "배터리 수주", "양극재 실적"],
    "방산":       ["방산주", "방산 수출", "방위산업 수주"],
    "조선":       ["조선주", "조선 수주", "선박 수주"],
    "바이오":     ["바이오주", "바이오 임상", "신약 기술수출"],
    "제약":       ["제약주", "제약 실적"],
    "화장품":     ["화장품주", "화장품 수출", "화장품 실적"],
    "유통":       ["유통주", "백화점 실적", "이커머스 실적"],
    "원전":       ["원전주", "원전 수주", "원자력 수출"],
}
USE_FILTER = True

# 실행 시각(KST) 표시용 - 대상 날짜는 DB에 이미 저장된 최신 날짜를 사용
now_kst = dt.datetime.now(ZoneInfo("Asia/Seoul"))

os.makedirs("docs", exist_ok=True)
con = sqlite3.connect(DB_PATH)

# 대상 날짜 = tone 테이블에 저장된 가장 최근 날짜 (수집 스크립트가 이미 결정한 날짜를 그대로 사용)
_row = con.execute("SELECT MAX(date) FROM tone").fetchone()
if not _row or not _row[0]:
    log("[중지] tone 테이블에 데이터가 없습니다. news_tone.py를 먼저 실행하세요.")
    raise SystemExit(1)
TARGET = dt.date.fromisoformat(_row[0])
log(f"대시보드 생성 대상 날짜: {TARGET}")


log("\n[3/4] 기준선·그래프 계산")
import pandas as pd, numpy as np

def history(sector):
    df = pd.read_sql("SELECT * FROM tone WHERE sector=? ORDER BY date", con, params=(sector,))
    if df.empty: return None
    df["ma20"] = df["net"].rolling(20, min_periods=1).mean()
    df["sd20"] = df["net"].rolling(20, min_periods=2).std().fillna(0.0)
    return df

def market_latest(sector):
    """market_sector 테이블에서 해당 섹터의 최신 등락률 데이터를 가져옴. 없으면 None."""
    try:
        df = pd.read_sql("SELECT * FROM market_sector WHERE sector=? ORDER BY date", con, params=(sector,))
    except Exception:
        return None
    return df.iloc[-1] if not df.empty else None

def market_history(sector):
    """market_sector 테이블에서 해당 섹터의 날짜별 등락률 전체를 가져옴. 없으면 빈 DataFrame."""
    try:
        df = pd.read_sql("SELECT date, avg_return FROM market_sector WHERE sector=? ORDER BY date", con, params=(sector,))
    except Exception:
        return pd.DataFrame(columns=["date", "avg_return"])
    return df

def svg_overlay(tone_df, price_df, w=300, h=40, pad=10):
    """뉴스 톤(남색)과 섹터 주가등락률(주황 점선)을 같은 그래프에 겹쳐 그림.
    두 값의 단위가 다르므로 각자 자기 범위로 정규화(dual-axis)한다.
    주가 데이터가 없거나 1개뿐이면 톤 선만 그림."""
    rows = tone_df.to_dict("records"); n = len(rows)
    if n == 0:
        return "<svg></svg>"
    nets = [r["net"] for r in rows]
    tmin, tmax = min(nets), max(nets)
    if tmax - tmin < 0.05: tmin -= 0.05; tmax += 0.05

    price_map = dict(zip(price_df["date"], price_df["avg_return"])) if not price_df.empty else {}
    aligned = [price_map.get(rows[i]["date"]) for i in range(n)]
    valid = [p for p in aligned if p is not None]
    has_price = len(valid) >= 2
    if has_price:
        pmin, pmax = min(valid), max(valid)
        if pmax - pmin < 0.1: pmin -= 0.1; pmax += 0.1

    def X(i): return pad + (w-2*pad)*(i/(n-1) if n > 1 else 0.5)
    def Yt(v): return pad + (h-2*pad)*(1-(v-tmin)/(tmax-tmin))
    def Yp(v): return pad + (h-2*pad)*(1-(v-pmin)/(pmax-pmin))

    p = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none">']
    p.append('<polyline points="' + "".join(f"{X(i):.1f},{Yt(nets[i]):.1f} " for i in range(n)) +
             '" fill="none" stroke="#2c3e50" stroke-width="1.6"/>')
    if has_price:
        seg = []
        for i in range(n):
            v = aligned[i]
            if v is None:
                if len(seg) > 1:
                    p.append('<polyline points="' + " ".join(seg) +
                             '" fill="none" stroke="#e67e22" stroke-width="1.6" stroke-dasharray="4 2"/>')
                seg = []
            else:
                seg.append(f"{X(i):.1f},{Yp(v):.1f}")
        if len(seg) > 1:
            p.append('<polyline points="' + " ".join(seg) +
                     '" fill="none" stroke="#e67e22" stroke-width="1.6" stroke-dasharray="4 2"/>')
    p.append("</svg>")
    return "".join(p), has_price

def svg_trend(df, w=300, h=96, pad=12):
    rows = df.to_dict("records"); n = len(rows)
    nets = [r["net"] for r in rows]; mas = [r["ma20"] for r in rows]
    ups = [r["ma20"]+1.5*r["sd20"] if r["sd20"] else r["ma20"] for r in rows]
    los = [r["ma20"]-1.5*r["sd20"] if r["sd20"] else r["ma20"] for r in rows]
    allv = nets+ups+los; ymin, ymax = min(allv), max(allv)
    if ymax-ymin < 0.1: ymin -= 0.1; ymax += 0.1
    def X(i): return pad + (w-2*pad)*(i/(n-1) if n > 1 else 0.5)
    def Y(v): return pad + (h-2*pad)*(1-(v-ymin)/(ymax-ymin))
    p = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none">']
    if ymin < 0 < ymax:
        y0 = Y(0); p.append(f'<line x1="{pad}" y1="{y0:.1f}" x2="{w-pad}" y2="{y0:.1f}" stroke="#e2e2e2" stroke-dasharray="2 2"/>')
    if n > 1:
        band = "".join(f"{X(i):.1f},{Y(ups[i]):.1f} " for i in range(n)) + \
               "".join(f"{X(i):.1f},{Y(los[i]):.1f} " for i in range(n-1,-1,-1))
        p.append(f'<polygon points="{band}" fill="#1e6fb8" opacity="0.08"/>')
        p.append('<polyline points="' + "".join(f"{X(i):.1f},{Y(mas[i]):.1f} " for i in range(n)) +
                 '" fill="none" stroke="#aaa" stroke-width="1" stroke-dasharray="3 3"/>')
        p.append('<polyline points="' + "".join(f"{X(i):.1f},{Y(nets[i]):.1f} " for i in range(n)) +
                 '" fill="none" stroke="#2c3e50" stroke-width="2"/>')
    p.append(f'<circle cx="{X(n-1):.1f}" cy="{Y(nets[-1]):.1f}" r="3.2" fill="#2c3e50"/>')
    p.append("</svg>"); return "".join(p)

cards, heat, table = [], [], []
maxdays = 0
for sector in SECTORS:
    df = history(sector)
    if df is None: continue
    maxdays = max(maxdays, len(df))
    last = df.iloc[-1]
    sd = last["sd20"] if last["sd20"] > 0 else None
    z  = (last["net"]-last["ma20"])/sd if sd else 0.0
    if z >= 1.5:   flag, color = "과열극단", "#c0392b"
    elif z <= -1.5: flag, color = "공포극단", "#1e6fb8"
    else:           flag, color = "중립", "#666"
    note = "" if last["n"] >= MIN_ARTICLES else " (표본부족)"
    net = last["net"]
    bg = (f"rgba(39,174,96,{min(net/0.5,1):.2f})" if net >= 0
          else f"rgba(192,57,43,{min(-net/0.5,1):.2f})")
    heat.append(f'<div class="cell" style="background:{bg}"><b>{sector}</b><span>{net:+.2f}</span></div>')

    # 시장데이터(KRX) 매칭 - 없으면 "-"로 표시 (KRX 미연결/데이터없음 모두 이 경우)
    mkt = market_latest(sector)
    if mkt is not None:
        ret = mkt["avg_return"]
        ret_cell = f"<td class='num {'pos' if ret>=0 else 'neg'}'>{ret:+.2f}%</td>"
        # 다이버전스: 오늘 뉴스 톤의 방향(긍정/부정)과 오늘 주가 등락 방향이 엇갈리는지
        tone_dir = "up" if net >= 0 else "down"
        price_dir = "up" if ret >= 0 else "down"
        if tone_dir != price_dir:
            div_cell = "<td style='color:#c0392b;font-weight:600'>다이버전스</td>"
        else:
            div_cell = "<td style='color:#888'>일치</td>"
    else:
        ret_cell = "<td class='num' style='color:#bbb'>-</td>"
        div_cell = "<td style='color:#bbb'>-</td>"

    table.append(f"<tr><td class='sec'>{sector}</td><td class='num'>{net:+.3f}</td>"
                 f"<td class='num'>{last['ma20']:+.3f}</td><td class='num'>{z:+.2f}</td>"
                 f"<td style='color:{color};font-weight:600'>{flag}</td>"
                 f"<td class='num pos'>{int(last['pos'])}</td>"
                 f"<td class='num neu'>{int(last['neu'])}</td>"
                 f"<td class='num neg'>{int(last['neg'])}</td>"
                 f"<td class='num'>{int(last['n'])}{note}</td>"
                 f"{ret_cell}{div_cell}</tr>")
    price_hist = market_history(sector)
    overlay_svg, has_overlay = svg_overlay(df, price_hist)
    overlay_block = (f'<div class="overlay-wrap">{overlay_svg}'
                      f'<div class="overlay-legend"><span class="lg-tone">━ 뉴스톤</span>'
                      f'<span class="lg-price">┅ 섹터등락률</span></div></div>'
                      if has_overlay else "")

    cards.append(f'<div class="card"><div class="ct"><b>{sector}</b>'
                 f'<span style="color:{color}">{net:+.3f} · {flag}</span></div>'
                 f'{svg_trend(df)}{overlay_block}</div>')

log("\n[4/4] 대시보드 생성")
page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>뉴스톤 대시보드 {TARGET}</title>
<meta name="robots" content="noindex, nofollow">
<style>
 body{{font-family:'맑은 고딕','Malgun Gothic',sans-serif;background:#f6f7f9;margin:0;padding:28px;color:#222}}
 h1{{font-size:20px;margin:0 0 2px}} h2{{font-size:15px;margin:26px 0 10px;color:#2c3e50}}
 .date{{color:#888;margin-bottom:18px;font-size:13px}}
 .heat{{display:flex;flex-wrap:wrap;gap:8px}}
 .cell{{flex:1 1 110px;min-width:110px;padding:12px;border-radius:8px;text-align:center;border:1px solid #0001}}
 .cell b{{display:block;font-size:13px}} .cell span{{font-size:18px;font-variant-numeric:tabular-nums}}
 table{{border-collapse:collapse;background:#fff;box-shadow:0 1px 4px #0001;border-radius:8px;overflow:hidden;width:100%}}
 th,td{{padding:10px 14px;border-bottom:1px solid #eee;text-align:left;font-size:13.5px}}
 th{{background:#2c3e50;color:#fff;font-size:12.5px}} .sec{{font-weight:600}}
 .num{{text-align:right;font-variant-numeric:tabular-nums}} tr:last-child td{{border-bottom:none}}
 .pos{{color:#27ae60}} .neg{{color:#c0392b}} .neu{{color:#999}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px}}
 .card{{background:#fff;border-radius:8px;box-shadow:0 1px 4px #0001;padding:12px}}
 .ct{{display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px}}
 .overlay-wrap{{margin-top:6px;border-top:1px dashed #eee;padding-top:6px}}
 .overlay-legend{{display:flex;gap:12px;font-size:11px;color:#888;margin-top:2px}}
 .lg-tone{{color:#2c3e50}} .lg-price{{color:#e67e22}}
 .ct span{{font-variant-numeric:tabular-nums}}
 .legend{{margin-top:18px;color:#666;font-size:12.5px;line-height:1.8}}
</style></head><body>
<h1>뉴스톤 대시보드</h1>
<div class="date">{TARGET} 기준 · 섹터별 뉴스 긍·부정 톤 · 누적 {maxdays}일 · 증시필터 {'ON' if USE_FILTER else 'OFF'} · 자동갱신(GitHub Actions)</div>

<h2>① 해당일 한눈에 (히트맵)</h2>
<div class="heat">{''.join(heat)}</div>

<h2>② 정확한 수치</h2>
<table>
 <tr><th>섹터</th><th>순점수</th><th>20일 평균</th><th>z-score</th><th>신호</th><th>긍정</th><th>중립</th><th>부정</th><th>기사수</th><th>섹터 등락률</th><th>뉴스vs주가</th></tr>
 {''.join(table)}
</table>

<h2>③ 섹터별 추이 (진한선=톤, 점선=20일평균, 띠=정상범위)</h2>
<div class="grid">{''.join(cards)}</div>

<div class="legend">
 · <b>순점수</b> = (긍정−부정)/기사수 (−1~+1) · <b>z-score</b> ±1.5 이상이면 극단 신호<br>
 · 증시필터: 기사에 '실적·주가·수주' 등 증시 단어가 있어야 점수화 → 증시 무관 기사 제외<br>
 · <b>섹터 등락률</b>: KRX 공식데이터 기준 해당 섹터(WICS 소분류/대표종목) 평균 등락률<br>
 · <b>뉴스vs주가</b>: 오늘 뉴스 톤(긍/부정)과 오늘 주가 등락 방향이 엇갈리면 '다이버전스' 표시 (둘 중 하나가 과장 신호일 가능성)<br>
 · 추이선이 <b>파란 띠를 위로 뚫으면 과열</b>, 아래로 뚫으면 공포 → "지금 이 가격에 사겠는가" 재점검 신호<br>
 · 카드 하단의 겹친 그래프: <span style="color:#2c3e50">━ 남색 실선(뉴스톤)</span>과 <span style="color:#e67e22">┅ 주황 점선(섹터 등락률)</span> — 두 지표는 단위가 달라 각자 범위로 정규화해 겹쳐 그렸습니다. 데이터가 2일 이상 쌓인 섹터부터 표시됩니다.<br>
 · 매수·매도 지시가 아니라 <b>심리 경계등</b>입니다. · 매일 자동 갱신됩니다.
</div>
</body></html>"""

with open(DASH_PATH, "w", encoding="utf-8") as f:
    f.write(page)
con.close()
log(f"\n완료: {DASH_PATH}, {DB_PATH} 갱신됨")
