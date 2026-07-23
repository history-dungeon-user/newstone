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

# ---------------------------------------------------------------------
#  [개선] 최근 BACKFILL_DAYS일 중 'DB에 아직 없는 날짜'를 모두 찾아 채운다.
#   - 기존: 가장 최근 거래일 1개만 저장 → 실행이 하루라도 실패하면 그 거래일은 영구 누락
#   - 개선: 빠진 날을 스스로 되채움(self-healing). 주말·공휴일은 데이터가 없으므로 자동 skip
# ---------------------------------------------------------------------
BACKFILL_DAYS = 10

now_kst = dt.datetime.now(ZoneInfo("Asia/Seoul"))

con = sqlite3.connect(DB_PATH)
con.execute("""CREATE TABLE IF NOT EXISTS market_sector(
    date TEXT, sector TEXT, avg_return REAL, n_stocks INT, n_up INT, n_down INT,
    PRIMARY KEY(date, sector))""")
existing = {r[0] for r in con.execute("SELECT DISTINCT date FROM market_sector").fetchall()}

wics = pd.read_csv(WICS_PATH, dtype=str)
wics["종목코드"] = wics["종목코드"].str.zfill(6)

def agg_sector(sub, sector_name, tag="", verbose=True):
    """섹터 집계: 단순평균 대신 중앙값(median) 사용 - 감자/액면분할 등 기준가 재설정 종목의
    극단치(예: -90%)가 소수 섞여도 전체 수치가 왜곡되지 않도록 함."""
    avg = round(sub["FLUC_RT"].median(), 3)
    up = int((sub["FLUC_RT"] > 0).sum()); down = int((sub["FLUC_RT"] < 0).sum())
    if verbose:
        top3 = sub.nlargest(3, "FLUC_RT")[["ISU_NM", "FLUC_RT"]].values.tolist()
        bot3 = sub.nsmallest(3, "FLUC_RT")[["ISU_NM", "FLUC_RT"]].values.tolist()
        log(f"   - {sector_name:7s}: 중앙값등락률 {avg:+.2f}%  (상승{up}/하락{down}/{len(sub)}종목){tag}")
        log(f"       상위: {', '.join(f'{n} {v:+.1f}%' for n,v in top3)}")
        log(f"       하위: {', '.join(f'{n} {v:+.1f}%' for n,v in bot3)}")
    return avg, up, down

def process_date(bas_dd, verbose):
    """해당 날짜의 KRX 데이터를 받아 섹터별로 집계. 데이터 없으면 None(휴장일)."""
    kospi = fetch_market(ENDPOINTS["KOSPI"], bas_dd)
    kosdaq = fetch_market(ENDPOINTS["KOSDAQ"], bas_dd)
    if not kospi or not kosdaq:
        return None

    all_rows = pd.DataFrame(kospi + kosdaq)
    all_rows["ISU_CD"] = all_rows["ISU_CD"].astype(str).str.zfill(6)
    all_rows["FLUC_RT"] = pd.to_numeric(all_rows["FLUC_RT"], errors="coerce")
    all_rows = all_rows.dropna(subset=["FLUC_RT"])
    if all_rows.empty:
        return None

    merged = all_rows.merge(wics[["종목코드", "WICS소"]],
                            left_on="ISU_CD", right_on="종목코드", how="left")
    iso = dt.datetime.strptime(bas_dd, "%Y%m%d").date().isoformat()
    rows_out = []

    for sector, wics_list in SECTOR_WICS.items():
        sub = merged[merged["WICS소"].isin(wics_list)]
        if sub.empty: continue
        avg, up, down = agg_sector(sub, sector, verbose=verbose)
        rows_out.append((iso, sector, avg, len(sub), up, down))

    for sector, codes in THEME_BASKETS.items():
        codes6 = [c.zfill(6) for c in codes]
        sub = all_rows[all_rows["ISU_CD"].isin(codes6)]
        if sub.empty: continue
        avg, up, down = agg_sector(sub, sector, tag=" [테마바스켓]", verbose=verbose)
        rows_out.append((iso, sector, avg, len(sub), up, down))

    avg_all, up_all, down_all = agg_sector(all_rows, "시장전체", verbose=verbose)
    rows_out.append((iso, "시장전체", avg_all, len(all_rows), up_all, down_all))
    return iso, rows_out, len(kospi), len(kosdaq)

log(f"[1/2] 최근 {BACKFILL_DAYS}일 중 누락된 날짜 확인 (이미 보유: {len(existing)}일)")
filled, skipped, newest = 0, [], True
for d in range(0, BACKFILL_DAYS):
    day = now_kst.date() - dt.timedelta(days=d)
    iso = day.isoformat()
    if iso in existing:
        continue                      # 이미 있음 → 건너뜀 (API 호출 절약)
    bas_dd = day.strftime("%Y%m%d")
    out = process_date(bas_dd, verbose=newest)   # 가장 최근 1건만 상세 로그
    if out is None:
        skipped.append(iso)           # 휴장일 또는 미공개
        continue
    iso_done, rows_out, nk, nq = out
    con.executemany("INSERT OR REPLACE INTO market_sector VALUES (?,?,?,?,?,?)", rows_out)
    con.commit()
    log(f"   [저장] {iso_done}: KOSPI {nk} + KOSDAQ {nq}종목 → {len(rows_out)}개 섹터")
    filled += 1
    newest = False

if skipped:
    log(f"   (데이터 없음/휴장일로 건너뜀: {', '.join(skipped)})")

total_days = con.execute("SELECT COUNT(DISTINCT date) FROM market_sector").fetchone()[0]
con.close()

log(f"\n[2/2] 완료: 이번 실행에서 {filled}일 신규 저장 / DB 누적 {total_days}일")
if filled == 0:
    log("   (새로 채울 날짜 없음 - 이미 최신 상태이거나 KRX 미공개)")
