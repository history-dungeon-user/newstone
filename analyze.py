# -*- coding: utf-8 -*-
# =====================================================================
#  뉴스톤 프로젝트 - 톤 vs 주가 상관관계 분석 (analyze.py)
# ---------------------------------------------------------------------
#  newstone.db 를 읽어 아래 4단계를 수행하고 docs/analysis.html 을 만듭니다.
#    ① 데이터 품질 점검
#    ② 리드-래그 교차상관 (뉴스가 주가를 선행? 후행?)  + FDR 다중검정 보정
#    ③ 시장요인 제거(초과수익·초과톤) 후 재검증
#    ④ 극단 톤 이벤트 스터디 (역발상 가설 검증)
#
#  ※ 표본이 부족하면 '판단 보류'를 명시합니다. 억지 결론을 내지 않습니다.
#  ※ 외부 라이브러리 추가 설치 불필요 (pandas/numpy만 사용)
# =====================================================================

import os, math, sqlite3, datetime as dt
import numpy as np, pandas as pd

DB_PATH  = "newstone.db"
OUT_PATH = "docs/analysis.html"

MIN_ARTICLES   = 5     # 이 미만 기사수인 날은 톤이 노이즈 -> 제외
MIN_DAYS_PRELIM = 60   # 예비 분석 최소 거래일
MIN_DAYS_FULL   = 120  # 본 분석 권장 거래일
LAGS = range(-5, 6)    # 톤(t) vs 수익률(t+k), k = -5 ... +5
EVENT_Z = 1.5          # 극단 이벤트 기준 (z-score)
HORIZONS = [1, 5, 20]  # 이벤트 이후 측정 구간(거래일)

def log(*a): print(*a, flush=True)

# ---------------- 통계 헬퍼 (scipy 불필요) ----------------
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def spearman(x, y):
    """순위상관 계수와 양측 p-value (Fisher z 근사). 이상치·비정규성에 강건."""
    s = pd.DataFrame({"x": x, "y": y}).dropna()
    n = len(s)
    if n < 8:
        return np.nan, np.nan, n
    r = s["x"].rank().corr(s["y"].rank())
    if pd.isna(r) or abs(r) >= 1:
        return r, np.nan, n
    z = 0.5 * math.log((1 + r) / (1 - r)) * math.sqrt(n - 3)
    return r, 2 * (1 - norm_cdf(abs(z))), n

def bh_fdr(pvals, alpha=0.05):
    """Benjamini-Hochberg FDR 보정. 다중검정 시 거짓양성 폭증을 막는다."""
    p = np.asarray(pvals, dtype=float)
    ok = ~np.isnan(p); idx = np.where(ok)[0]
    sig = np.zeros(len(p), dtype=bool)
    if len(idx) == 0:
        return sig
    sub = p[idx]; order = np.argsort(sub); ranked = sub[order]
    thresh = alpha * (np.arange(1, len(sub) + 1) / len(sub))
    passed = ranked <= thresh
    if passed.any():
        kmax = int(np.max(np.where(passed)[0]))
        sig[idx[order[:kmax + 1]]] = True
    return sig

# ---------------- 데이터 적재 ----------------
if not os.path.exists(DB_PATH):
    log("[중지] newstone.db 가 없습니다."); raise SystemExit(0)

con = sqlite3.connect(DB_PATH)
try:
    tone = pd.read_sql("SELECT date, sector, n, net FROM tone", con)
    mkt  = pd.read_sql("SELECT date, sector, avg_return FROM market_sector", con)
except Exception as e:
    log(f"[중지] 테이블을 읽을 수 없습니다: {e}"); con.close(); raise SystemExit(0)
con.close()

if tone.empty or mkt.empty:
    log("[중지] 분석할 데이터가 아직 없습니다."); raise SystemExit(0)

# 기사 수가 너무 적은 날은 제외 (순점수가 노이즈)
tone_used = tone[tone["n"] >= MIN_ARTICLES].copy()

# 거래일 기준 병합 (주가가 있는 날만 남음 -> 주말/휴장일 자동 제외)
df = tone_used.merge(mkt, on=["date", "sector"], how="inner")
df = df.sort_values(["sector", "date"]).reset_index(drop=True)

# 시장요인 제거: 초과톤 / 초과수익 (섹터 - 시장전체)
base = df[df["sector"] == "시장전체"][["date", "net", "avg_return"]]
base = base.rename(columns={"net": "mkt_net", "avg_return": "mkt_ret"})
df = df.merge(base, on="date", how="left")
df["ex_net"] = df["net"] - df["mkt_net"]
df["ex_ret"] = df["avg_return"] - df["mkt_ret"]

sectors = [s for s in df["sector"].unique() if s != "시장전체"]
n_days = df["date"].nunique()

log(f"[1/4] 데이터 점검: 거래일 {n_days}일, 섹터 {len(sectors)}개, 관측 {len(df)}건")

# 표본 충분성 판정
if n_days < MIN_DAYS_PRELIM:
    stage, stage_msg, stage_color = "표본부족", (
        f"거래일 {n_days}일 — 예비 분석 최소 기준({MIN_DAYS_PRELIM}일) 미달. "
        "아래 수치는 참고용이며 통계적 판단을 내리지 마십시오."), "#c0392b"
elif n_days < MIN_DAYS_FULL:
    stage, stage_msg, stage_color = "예비분석", (
        f"거래일 {n_days}일 — 예비 분석 단계. 방향성 참고는 가능하나 "
        f"확정 결론은 {MIN_DAYS_FULL}일 이후에 내리는 것이 안전합니다."), "#e67e22"
else:
    stage, stage_msg, stage_color = "본분석", (
        f"거래일 {n_days}일 — 본 분석 가능 구간입니다."), "#27ae60"

# ---------------- ② 리드-래그 교차상관 ----------------
log("[2/4] 리드-래그 교차상관 계산")

def leadlag(tone_col, ret_col):
    """톤(t) vs 수익률(t+k) 상관. k>0이면 뉴스 선행, k<0이면 뉴스 후행."""
    rows = []
    for k in LAGS:
        xs, ys = [], []
        for sec in sectors:
            g = df[df["sector"] == sec].sort_values("date")
            t_ = g[tone_col].values
            r_ = g[ret_col].shift(-k).values   # k>0 -> 미래 수익률
            xs.extend(t_); ys.extend(r_)
        r, p, n = spearman(xs, ys)
        rows.append({"lag": k, "r": r, "p": p, "n": n})
    out = pd.DataFrame(rows)
    out["sig"] = bh_fdr(out["p"].values)
    return out

ll_raw = leadlag("net", "avg_return")
ll_ex  = leadlag("ex_net", "ex_ret")

# ---------------- ③ 섹터별 상관 (동시점 + t+1) ----------------
log("[3/4] 섹터별 상관 계산")
sec_rows = []
for sec in sectors:
    g = df[df["sector"] == sec].sort_values("date")
    r0, p0, n0 = spearman(g["ex_net"], g["ex_ret"])
    r1, p1, n1 = spearman(g["ex_net"], g["ex_ret"].shift(-1))
    sec_rows.append({"sector": sec, "n": n0, "r0": r0, "p0": p0, "r1": r1, "p1": p1})
sec_df = pd.DataFrame(sec_rows)
if not sec_df.empty:
    sec_df["sig0"] = bh_fdr(sec_df["p0"].values)
    sec_df["sig1"] = bh_fdr(sec_df["p1"].values)

# ---------------- ④ 극단 톤 이벤트 스터디 ----------------
log("[4/4] 극단 이벤트 스터디")
ev_rows = []
for sec in sectors:
    g = df[df["sector"] == sec].sort_values("date").copy()
    if len(g) < 25:
        continue
    g["ma"] = g["net"].rolling(20, min_periods=10).mean()
    g["sd"] = g["net"].rolling(20, min_periods=10).std()
    g["z"] = (g["net"] - g["ma"]) / g["sd"]
    # 이후 누적 초과수익
    for h in HORIZONS:
        g[f"fwd{h}"] = g["ex_ret"][::-1].rolling(h, min_periods=h).sum()[::-1].shift(-1)
    for _, row in g.iterrows():
        if pd.isna(row["z"]):
            continue
        if row["z"] >= EVENT_Z:
            kind = "극단낙관"
        elif row["z"] <= -EVENT_Z:
            kind = "극단비관"
        else:
            continue
        rec = {"sector": sec, "date": row["date"], "kind": kind, "z": row["z"]}
        for h in HORIZONS:
            rec[f"fwd{h}"] = row[f"fwd{h}"]
        ev_rows.append(rec)

ev = pd.DataFrame(ev_rows)
ev_summary = []
if not ev.empty:
    for kind in ["극단비관", "극단낙관"]:
        sub = ev[ev["kind"] == kind]
        if sub.empty:
            continue
        rec = {"kind": kind, "count": len(sub)}
        for h in HORIZONS:
            vals = sub[f"fwd{h}"].dropna()
            if len(vals) >= 8:
                m = vals.mean()
                se = vals.std(ddof=1) / math.sqrt(len(vals))
                tstat = m / se if se > 0 else np.nan
                pv = 2 * (1 - norm_cdf(abs(tstat))) if not pd.isna(tstat) else np.nan
                rec[f"m{h}"], rec[f"p{h}"], rec[f"n{h}"] = m, pv, len(vals)
            else:
                rec[f"m{h}"], rec[f"p{h}"], rec[f"n{h}"] = np.nan, np.nan, len(vals)
        ev_summary.append(rec)
ev_sum = pd.DataFrame(ev_summary)

# ---------------- HTML 리포트 ----------------
def fmt(v, nd=3, suf=""):
    return "—" if (v is None or (isinstance(v, float) and pd.isna(v))) else f"{v:+.{nd}f}{suf}"

def ll_table(tbl, title):
    rows = ""
    for _, r in tbl.iterrows():
        k = int(r["lag"])
        if k > 0:   mean = f"뉴스가 {k}일 선행"
        elif k < 0: mean = f"뉴스가 {-k}일 후행"
        else:       mean = "동시점"
        mark = "<b style='color:#27ae60'>유의</b>" if r["sig"] else "<span style='color:#bbb'>–</span>"
        hl = " style='background:#f0f8f4'" if r["sig"] else ""
        pstr = "—" if pd.isna(r["p"]) else f"{r['p']:.3f}"
        nstr = "—" if pd.isna(r["n"]) else str(int(r["n"]))
        rows += (f"<tr{hl}><td class='num'>{k:+d}</td><td>{mean}</td>"
                 f"<td class='num'>{fmt(r['r'])}</td>"
                 f"<td class='num'>{pstr}</td>"
                 f"<td class='num'>{nstr}</td><td>{mark}</td></tr>")
    return (f"<h3>{title}</h3><table><tr><th>시차 k</th><th>의미</th><th>순위상관 r</th>"
            f"<th>p-value</th><th>표본</th><th>FDR 보정 후</th></tr>{rows}</table>")

sec_rows_html = ""
for _, r in (sec_df.iterrows() if not sec_df.empty else []):
    m0 = "<b style='color:#27ae60'>유의</b>" if r["sig0"] else "–"
    m1 = "<b style='color:#27ae60'>유의</b>" if r["sig1"] else "–"
    sec_rows_html += (f"<tr><td class='sec'>{r['sector']}</td><td class='num'>{int(r['n'])}</td>"
                      f"<td class='num'>{fmt(r['r0'])}</td><td>{m0}</td>"
                      f"<td class='num'>{fmt(r['r1'])}</td><td>{m1}</td></tr>")

ev_rows_html = ""
if not ev_sum.empty:
    for _, r in ev_sum.iterrows():
        cells = ""
        for h in HORIZONS:
            m, p, n = r.get(f"m{h}"), r.get(f"p{h}"), r.get(f"n{h}", 0)
            if pd.isna(m):
                cells += f"<td class='num' style='color:#bbb'>표본부족(n={int(n)})</td>"
            else:
                col = "#27ae60" if m > 0 else "#c0392b"
                star = " *" if (not pd.isna(p) and p < 0.05) else ""
                cells += (f"<td class='num' style='color:{col}'>{m:+.2f}%p{star}"
                          f"<br><span style='font-size:10px;color:#999'>n={int(n)}</span></td>")
        ev_rows_html += f"<tr><td class='sec'>{r['kind']}</td><td class='num'>{int(r['count'])}</td>{cells}</tr>"
else:
    ev_rows_html = f"<tr><td colspan='{2+len(HORIZONS)}' style='color:#bbb'>아직 극단 이벤트가 충분히 발생하지 않았습니다.</td></tr>"

os.makedirs("docs", exist_ok=True)
now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>뉴스톤 상관분석</title><meta name="robots" content="noindex, nofollow">
<style>
 body{{font-family:'맑은 고딕','Malgun Gothic',sans-serif;background:#f6f7f9;margin:0;padding:28px;color:#222;line-height:1.6}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 10px;color:#2c3e50}}
 h3{{font-size:14px;margin:18px 0 8px;color:#555}}
 .date{{color:#888;font-size:13px;margin-bottom:16px}}
 .banner{{padding:12px 16px;border-radius:8px;background:#fff;border-left:4px solid {stage_color};
          box-shadow:0 1px 4px #0001;margin-bottom:20px;font-size:13.5px}}
 table{{border-collapse:collapse;background:#fff;box-shadow:0 1px 4px #0001;border-radius:8px;
        overflow:hidden;width:100%;margin-bottom:8px}}
 th,td{{padding:9px 13px;border-bottom:1px solid #eee;text-align:left;font-size:13px}}
 th{{background:#2c3e50;color:#fff;font-size:12px}}
 .num{{text-align:right;font-variant-numeric:tabular-nums}} .sec{{font-weight:600}}
 tr:last-child td{{border-bottom:none}}
 .note{{color:#666;font-size:12.5px;margin-top:10px}}
 a{{color:#2c3e50}}
</style></head><body>

<h1>뉴스톤 — 보도 톤과 주가의 상관관계 분석</h1>
<div class="date">{now_str} 생성 · 자동 갱신 · <a href="index.html">← 대시보드로</a></div>

<div class="banner"><b>분석 단계: {stage}</b><br>{stage_msg}</div>

<h2>① 리드-래그 교차상관 — 뉴스는 주가를 선행하는가?</h2>
<div class="note">
 k &gt; 0 에서 유의하면 <b>뉴스가 주가를 선행</b>(예측력 있음), k &lt; 0 에서 유의하면
 <b>뉴스가 주가를 후행</b>(단순 반영)한다는 뜻입니다. 선행연구에서는 후자가 강하게 나타납니다.
 11개 섹터를 통합(pooled)해 계산했으며, 다중검정에 따른 거짓양성을 막기 위해
 <b>Benjamini-Hochberg FDR 보정</b>을 적용했습니다.
</div>
{ll_table(ll_raw, "원자료 기준 (섹터 톤 vs 섹터 등락률)")}
{ll_table(ll_ex, "시장요인 제거 기준 (초과톤 vs 초과수익) — 이쪽이 더 신뢰도 높음")}

<h2>② 섹터별 상관 (시장요인 제거 기준)</h2>
<table>
 <tr><th>섹터</th><th>표본</th><th>동시점 r</th><th>FDR</th><th>t+1 r</th><th>FDR</th></tr>
 {sec_rows_html if sec_rows_html else "<tr><td colspan='6' style='color:#bbb'>데이터 부족</td></tr>"}
</table>
<div class="note">개별 섹터는 표본이 작아 통합 분석보다 신뢰도가 낮습니다. 참고용으로만 보십시오.</div>

<h2>③ 극단 톤 이벤트 스터디 — 역발상 가설 검증</h2>
<div class="note">
 톤 z-score가 ±{EVENT_Z} 를 넘은 날 이후의 <b>누적 초과수익</b>(섹터−시장)입니다.
 역발상 가설이 맞다면 <b>극단비관 → 이후 (+)</b>, <b>극단낙관 → 이후 (−)</b> 가 나와야 합니다.
 <b>*</b> 는 p&lt;0.05.
</div>
<table>
 <tr><th>이벤트</th><th>건수</th>{''.join(f'<th>이후 {h}일</th>' for h in HORIZONS)}</tr>
 {ev_rows_html}
</table>

<h2>해석 시 유의사항</h2>
<div class="note">
 · <b>상관은 인과가 아닙니다.</b> 특히 주가 하락이 비관 기사를 유발하는 <b>역인과</b>가 이 분야에서 강하게 관측됩니다.<br>
 · 선행연구의 효과크기는 매우 작습니다(1 표준편차 감성 충격 ≈ 하루 12bp 수준). <b>'유의하지 않음'이 정상적인 결론</b>일 수 있습니다.<br>
 · 통합(pooled) 분석의 p-value는 섹터 간 상관 때문에 실제보다 <b>낙관적</b>입니다. 경계선상 결과는 신뢰하지 마십시오.<br>
 · 기사 수 {MIN_ARTICLES}건 미만인 날은 제외했습니다. 주말 뉴스는 거래일 기준 병합 과정에서 제외됩니다.<br>
 · 섹터 정의·필터를 변경한 시점 이전 데이터는 비교 가능성이 떨어집니다.<br>
 · 이 분석은 <b>매매 신호가 아니라 심리 국면 관찰</b>을 위한 것입니다.
</div>

</body></html>"""

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write(html)

log(f"\n완료: {OUT_PATH} 생성 (단계: {stage}, 거래일 {n_days}일)")
