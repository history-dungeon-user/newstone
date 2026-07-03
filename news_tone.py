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
con.close()
log("\n뉴스 수집·채점 완료 (DB 저장됨)")
