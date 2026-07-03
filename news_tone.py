# -*- coding: utf-8 -*-
# =====================================================================
#  뉴스톤 프로젝트 - GitHub Actions 자동실행용 (news_tone.py)
# ---------------------------------------------------------------------
#  로컬 PC용(02_뉴스톤.py)과 로직은 동일하되, 아래만 다릅니다:
#   - 키를 파일에 적지 않고 "환경변수"(GitHub Secrets)에서 읽음
#   - 실행 시각을 KST(한국시간) 기준으로 명시 계산 (서버는 UTC로 돌기 때문)
#   - 대시보드를 docs/index.html 로 저장 (GitHub Pages가 이 폴더를 웹으로 공개)
#   - 자동 실행이라 브라우저 열기/키입력 대기 없이 조용히 끝남
# =====================================================================

import os, sys, time, html, re, sqlite3, datetime as dt, email.utils
from zoneinfo import ZoneInfo
import requests

NAVER_ID     = os.environ.get("NAVER_ID", "")
NAVER_SECRET = os.environ.get("NAVER_SECRET", "")

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

USE_FILTER   = True
FILTER_WORDS = [
    "주가","실적","영업이익","매출","순이익","수주","증시","코스피","코스닥",
    "목표주가","상한가","하한가","급등","급락","컨센서스","어닝","호실적","부진",
    "흑자","적자","수출","증설","공급계약","상장","공모","배당","자사주",
    "시가총액","외국인","기관","수급","실적발표","증권","분기"
]

DB_PATH   = "newstone.db"
DASH_PATH = "docs/index.html"     # GitHub Pages가 docs 폴더를 웹으로 공개
MAX_PER_KEYWORD = 100
MIN_ARTICLES    = 5
EVENING_CUTOFF_HOUR = 15

def log(*a): print(*a, flush=True)

if not NAVER_ID or not NAVER_SECRET:
    log("[중지] NAVER_ID / NAVER_SECRET 환경변수(Secrets)가 없습니다.")
    sys.exit(1)

# 실행 시각은 반드시 KST 기준으로 판단 (Actions 서버는 UTC로 돌기 때문)
now = dt.datetime.now(ZoneInfo("Asia/Seoul"))
if now.hour >= EVENING_CUTOFF_HOUR:
    TARGET = now.date(); _mode = f"오후/저녁 실행 → {TARGET} (오늘) 기사 수집"
else:
    TARGET = now.date() - dt.timedelta(days=1)
    _mode = f"오전 실행 → {TARGET} (어제) 기사 수집  ※ 전날 보완 모드"
log(f"== 뉴스톤 실행 (KST {now:%Y-%m-%d %H:%M}) ==")
log(f"   {_mode}")
log(f"   증시 필터: {'ON' if USE_FILTER else 'OFF'}\n")

log("[1/4] 뉴스 분석모델(KR-FinBERT) 로드 중...")
from transformers import pipeline
clf = pipeline("text-classification", model="snunlp/KR-FinBert-SC", truncation=True, max_length=256)

def label_kind(label):
    l = label.lower()
    if "pos" in l: return "pos"
    if "neg" in l: return "neg"
    return "neu"

def clean_text(t):
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()

def passes_filter(text):
    if not USE_FILTER or not FILTER_WORDS:
        return True
    return any(w in text for w in FILTER_WORDS)

def collect(keywords):
    seen, kept, total = set(), [], 0
    for kw in keywords:
        try:
            r = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET},
                params={"query": kw, "display": MAX_PER_KEYWORD, "sort": "date"}, timeout=15)
            r.raise_for_status()
            for it in r.json().get("items", []):
                link = it.get("originallink") or it.get("link", "")
                if link in seen: continue
                try: pub = email.utils.parsedate_to_datetime(it["pubDate"]).date()
                except Exception: pub = None
                if pub != TARGET: continue
                seen.add(link); total += 1
                text = (clean_text(it.get("title","")) + ". " + clean_text(it.get("description","")))[:256]
                if passes_filter(text):
                    kept.append(text)
            time.sleep(0.3)
        except Exception as e:
            log(f"   (수집 경고: '{kw}' -> {type(e).__name__}: {e})")
    return kept, total

os.makedirs("docs", exist_ok=True)
con = sqlite3.connect(DB_PATH)
con.execute("""CREATE TABLE IF NOT EXISTS tone(
    date TEXT, sector TEXT, n INT, pos INT, neu INT, neg INT, net REAL,
    PRIMARY KEY(date, sector))""")

log("\n[2/4] 섹터별 뉴스 수집 + 증시필터 + 점수화")
for sector, kws in SECTORS.items():
    arts, total = collect(kws)
    if not arts:
        log(f"   - {sector:7s}: 수집 {total}건 중 증시관련 0건 (건너뜀)"); continue
    preds = clf(arts)
    pos = sum(1 for p in preds if label_kind(p["label"]) == "pos")
    neg = sum(1 for p in preds if label_kind(p["label"]) == "neg")
    neu = len(preds) - pos - neg
    n   = len(preds); net = round((pos - neg) / n, 4)
    con.execute("INSERT OR REPLACE INTO tone VALUES (?,?,?,?,?,?,?)",
                (TARGET.isoformat(), sector, n, pos, neu, neg, net))
    drop = total - n
    log(f"   - {sector:7s}: 수집 {total:3d}건 → 증시관련 {n:3d}건 (제외 {drop})  "
        f"긍{pos} 중{neu} 부{neg}  순점수 {net:+.3f}")
con.commit()

log("\n[3/4] 기준선·그래프 계산")
import pandas as pd, numpy as np

def history(sector):
    df = pd.read_sql("SELECT * FROM tone WHERE sector=? ORDER BY date", con, params=(sector,))
    if df.empty: return None
    df["ma20"] = df["net"].rolling(20, min_periods=1).mean()
    df["sd20"] = df["net"].rolling(20, min_periods=2).std().fillna(0.0)
    return df

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
    table.append(f"<tr><td class='sec'>{sector}</td><td class='num'>{net:+.3f}</td>"
                 f"<td class='num'>{last['ma20']:+.3f}</td><td class='num'>{z:+.2f}</td>"
                 f"<td style='color:{color};font-weight:600'>{flag}</td>"
                 f"<td class='num pos'>{int(last['pos'])}</td>"
                 f"<td class='num neu'>{int(last['neu'])}</td>"
                 f"<td class='num neg'>{int(last['neg'])}</td>"
                 f"<td class='num'>{int(last['n'])}{note}</td></tr>")
    cards.append(f'<div class="card"><div class="ct"><b>{sector}</b>'
                 f'<span style="color:{color}">{net:+.3f} · {flag}</span></div>'
                 f'{svg_trend(df)}</div>')

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
 .ct span{{font-variant-numeric:tabular-nums}}
 .legend{{margin-top:18px;color:#666;font-size:12.5px;line-height:1.8}}
</style></head><body>
<h1>뉴스톤 대시보드</h1>
<div class="date">{TARGET} 기준 · 섹터별 뉴스 긍·부정 톤 · 누적 {maxdays}일 · 증시필터 {'ON' if USE_FILTER else 'OFF'} · 자동갱신(GitHub Actions)</div>

<h2>① 해당일 한눈에 (히트맵)</h2>
<div class="heat">{''.join(heat)}</div>

<h2>② 정확한 수치</h2>
<table>
 <tr><th>섹터</th><th>순점수</th><th>20일 평균</th><th>z-score</th><th>신호</th><th>긍정</th><th>중립</th><th>부정</th><th>기사수</th></tr>
 {''.join(table)}
</table>

<h2>③ 섹터별 추이 (진한선=톤, 점선=20일평균, 띠=정상범위)</h2>
<div class="grid">{''.join(cards)}</div>

<div class="legend">
 · <b>순점수</b> = (긍정−부정)/기사수 (−1~+1) · <b>z-score</b> ±1.5 이상이면 극단 신호<br>
 · 증시필터: 기사에 '실적·주가·수주' 등 증시 단어가 있어야 점수화 → 증시 무관 기사 제외<br>
 · 추이선이 <b>파란 띠를 위로 뚫으면 과열</b>, 아래로 뚫으면 공포 → "지금 이 가격에 사겠는가" 재점검 신호<br>
 · 매수·매도 지시가 아니라 <b>심리 경계등</b>입니다. · 매일 자동 갱신됩니다.
</div>
</body></html>"""

with open(DASH_PATH, "w", encoding="utf-8") as f:
    f.write(page)
con.close()
log(f"\n완료: {DASH_PATH}, {DB_PATH} 갱신됨")
