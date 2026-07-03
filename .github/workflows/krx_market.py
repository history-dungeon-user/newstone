# -*- coding: utf-8 -*-
# =====================================================================
#  뉴스톤 프로젝트 - KRX 시장데이터 수집 (GitHub Actions용) (krx_market.py)
# ---------------------------------------------------------------------
#  하는 일:
#   1) KRX 공식 API로 KOSPI+KOSDAQ 전종목 등락률을 받아옴
#      (당일 데이터가 없으면 최근 영업일까지 자동으로 거슬러 감 - 휴장일 대응)
#   2) WICS 소분류 매핑표로 종목을 섹터별로 묶음
#   3) 섹터별 평균 등락률·상승/하락 종목 수를 계산해 DB에 저장
#  news_tone.py 와 같은 newstone.db 를 공유합니다.
# =====================================================================

import os, sys, datetime as dt, sqlite3
import requests, pandas as pd
from zoneinfo import ZoneInfo

KRX_AUTH_KEY = os.environ.get("KRX_AUTH_KEY", "")
DB_PATH   = "newstone.db"
WICS_PATH = "wics_mapping.csv"   # 저장소에 함께 커밋된 매핑표

def log(*a): print(*a, flush=True)

if not KRX_AUTH_KEY:
    log("[중지] KRX_AUTH_KEY 환경변수(Secret)가 없습니다.")
    sys.exit(1)

if not os.path.exists(WICS_PATH):
    log(f"[중지] {WICS_PATH} 파일이 저장소에 없습니다. 매핑표를 먼저 커밋하세요.")
    sys.exit(1)

# 뉴스 섹터 <-> WICS 소분류 매핑 (실제 매핑표에서 검증된 명칭만 사용)
SECTOR_WICS = {
    "반도체":     ["반도체와반도체장비"],
    "디스플레이": ["디스플레이장비및부품", "디스플레이패널"],
    "방산":       ["우주항공과국방"],
    "조선":       ["조선"],
    "제약":       ["제약"],
    "화장품":     ["화장품"],
    "바이오":     ["생물공학", "생명과학도구및서비스"],
    "유통":       ["백화점과일반상점", "전문소매", "인터넷과카탈로그소매", "식품과기본식료품소매"],
}

# 2차전지·원전은 WICS 소분류가 없어 대표 종목 바스켓으로 대체.
# ⚠ 대표성 확보를 위한 표본이며, 시가총액 상위 위주로 구성. 필요시 조정하세요.
THEME_BASKETS = {
    "2차전지": ["373220","006400","247540","003670","066970"],  # LG에너지솔루션,삼성SDI,에코프로비엠,포스코퓨처엠,엘앤에프
    "원전":    ["034020","052690","051600"],                    # 두산에너빌리티,한전기술,한전KPS
}

API_BASE = "https://data-dbg.krx.co.kr/svc/apis/sto"
ENDPOINTS = {"KOSPI": "/stk_bydd_trd", "KOSDAQ": "/ksq_bydd_trd"}

def fetch_market(endpoint, bas_dd):
    headers = {"AUTH_KEY": KRX_AUTH_KEY.strip(),
               "Content-Type": "application/json", "Accept": "application/json"}
    r = requests.post(API_BASE + endpoint, headers=headers, json={"basDd": bas_dd}, timeout=30)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    rows = data.get("OutBlock_1") or []
    return rows if rows else None

# 최근 영업일 찾기 (오늘부터 최대 10일 거슬러 감 - 휴장일/데이터 지연 대응)
now_kst = dt.datetime.now(ZoneInfo("Asia/Seoul"))
found_date, kospi_rows, kosdaq_rows = None, None, None
for d in range(0, 10):
    cand = (now_kst.date() - dt.timedelta(days=d)).strftime("%Y%m%d")
    kospi_rows = fetch_market(ENDPOINTS["KOSPI"], cand)
    kosdaq_rows = fetch_market(ENDPOINTS["KOSDAQ"], cand)
    if kospi_rows and kosdaq_rows:
        found_date = cand
        break

if not found_date:
    log("[실패] 최근 10일 내 KRX 데이터를 받지 못했습니다.")
    sys.exit(1)

log(f"[1/3] KRX 데이터 수신: {found_date} (KOSPI {len(kospi_rows)}종목, KOSDAQ {len(kosdaq_rows)}종목)")

all_rows = pd.DataFrame(kospi_rows + kosdaq_rows)
all_rows["ISU_CD"] = all_rows["ISU_CD"].astype(str).str.zfill(6)
all_rows["FLUC_RT"] = pd.to_numeric(all_rows["FLUC_RT"], errors="coerce")
all_rows = all_rows.dropna(subset=["FLUC_RT"])

wics = pd.read_csv(WICS_PATH, dtype=str)
wics["종목코드"] = wics["종목코드"].str.zfill(6)
merged = all_rows.merge(wics[["종목코드", "WICS소"]], left_on="ISU_CD", right_on="종목코드", how="left")

log("[2/3] 섹터별 집계")
con = sqlite3.connect(DB_PATH)
con.execute("""CREATE TABLE IF NOT EXISTS market_sector(
    date TEXT, sector TEXT, avg_return REAL, n_stocks INT, n_up INT, n_down INT,
    PRIMARY KEY(date, sector))""")

target_iso = dt.datetime.strptime(found_date, "%Y%m%d").date().isoformat()
results = []

for sector, wics_list in SECTOR_WICS.items():
    sub = merged[merged["WICS소"].isin(wics_list)]
    if sub.empty: continue
    avg = round(sub["FLUC_RT"].mean(), 3)
    up = int((sub["FLUC_RT"] > 0).sum()); down = int((sub["FLUC_RT"] < 0).sum())
    results.append((target_iso, sector, avg, len(sub), up, down))
    log(f"   - {sector:7s}: 평균등락률 {avg:+.2f}%  (상승{up}/하락{down}/{len(sub)}종목)")

for sector, codes in THEME_BASKETS.items():
    codes6 = [c.zfill(6) for c in codes]
    sub = all_rows[all_rows["ISU_CD"].isin(codes6)]
    if sub.empty: continue
    avg = round(sub["FLUC_RT"].mean(), 3)
    up = int((sub["FLUC_RT"] > 0).sum()); down = int((sub["FLUC_RT"] < 0).sum())
    results.append((target_iso, sector, avg, len(sub), up, down))
    log(f"   - {sector:7s}: 평균등락률 {avg:+.2f}%  (상승{up}/하락{down}/{len(sub)}종목) [테마바스켓]")

# 시장전체 = KOSPI+KOSDAQ 전종목 평균
avg_all = round(all_rows["FLUC_RT"].mean(), 3)
up_all = int((all_rows["FLUC_RT"] > 0).sum()); down_all = int((all_rows["FLUC_RT"] < 0).sum())
results.append((target_iso, "시장전체", avg_all, len(all_rows), up_all, down_all))
log(f"   - {'시장전체':7s}: 평균등락률 {avg_all:+.2f}%  (상승{up_all}/하락{down_all}/{len(all_rows)}종목)")

con.executemany("INSERT OR REPLACE INTO market_sector VALUES (?,?,?,?,?,?)", results)
con.commit(); con.close()
log(f"\n[3/3] 완료: {target_iso} 기준 {len(results)}개 섹터 저장")
