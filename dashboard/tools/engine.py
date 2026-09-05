#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ-RP 策略引擎（纯标准库，零第三方依赖）
==========================================
与 tools/verify_grok.py 的 `risk="daily", warmup="listing"` 配置逐条对齐，
该配置已通过聚宽平台交叉验证（最大回撤窗口两边精确到天完全一致）。

三层逻辑
--------
① 挑菜：5 个窗口（4/8/13/26/52 周）中"上涨窗口占比 > 0.5"才入选
② 配比：权重 ∝ 动量得分 / 年化波动；债券合计 ≤ 35%；单腿 < 2% 丢弃
③ 总量：EWMA 协方差算组合事前波动，> 10% 则等比降仓；余额买 511880

两处关键时钟（2026-09-05 核验第三方方案时确认，勿改回）
------------------------------------------------------
· **风险时钟 = 日频**：RiskMetrics(1996) 的 λ=0.94 是**日频**参数
  （半衰期约 11 个交易日），月频才用 0.97。用在**周**收益上半衰期会变成
  约 11 周，危机时降仓过慢。实测改日频后：夏普 1.332→1.415、MDD -13.22%→-11.40%。
  信号仍按周决策（周五调仓），只有协方差用日收益估计 —— 两个时钟可以分离。
· **预热时钟 = 上市以来真实历史**：从回测起点重新数 53 周会让首年整体空仓
  （白丢一年收益）。用起点**之前**的已发生数据预热动量与 EWMA **不是前视**。
  实测该项贡献 +0.114 夏普，比日频 EWMA(+0.083) 更大。
"""
import math
from datetime import date

# ── 理论常数（不做网格搜索）─────────────────────────────────────────
LOOKBACKS = (4, 8, 13, 26, 52)   # 动量窗口（周），等权集成 Moskowitz et al.(2012)
SCORE_THRESHOLD = 0.5            # 5 窗至少 3 窗上涨
LAMBDA_EWMA = 0.94               # RiskMetrics(1996) 日频衰减
PERIODS_PER_YEAR = 252           # 与日频 λ 匹配
TARGET_VOL = 0.10                # 组合年化波动目标
BOND_CAP = 0.35                  # 债券合计上限（久期集中度风控）
MIN_WEIGHT = 0.02                # 最小持仓（避免给宽价差品种开碎仓）
MAX_LEVERAGE = 1.0               # 不加杠杆（国内融资成本约 6%，吃掉全部增益）
MIN_HIST_WEEKS = 53              # 上市满 53 周（= 最长回看窗口 + 1）才可入选
RF_ANNUAL = 0.02                 # 无风险利率（夏普分子用）
WEEKS_PER_YEAR = 52

# 单边买卖价差（bp）。佣金万 0.5 另计。
# 窄流动性品种（豆粕/油气/原油）价差显著更高，必须按品种设。
COMMISSION_BP = 0.5
SPREAD_BP = {
    "510300": 2.0, "159915": 2.5, "512480": 4.0, "512890": 5.0,
    "513050": 4.0, "513100": 3.0, "513500": 4.0, "518880": 2.5,
    "159985": 12.0, "162411": 15.0, "501018": 18.0,
    "511010": 2.0, "511260": 2.0, "511880": 1.0,
}
DEFAULT_SPREAD_BP = 8.0


def iso_week(d):
    """'2026-09-04' → (2026, 36)"""
    return date.fromisoformat(d).isocalendar()[:2]


def to_weekly(rows):
    """
    日线 → 周线（取每 ISO 周最后一个交易日的收盘）。
    rows: [[date, close], ...] 已按日期升序
    返回 {date: close}（date 为该周最后交易日）
    """
    out, cur, last = {}, None, None
    for d, v in rows:
        k = iso_week(d)
        if cur is None:
            cur = k
        if k != cur:
            out[last[0]] = last[1]
            cur = k
        last = (d, v)
    if last:
        out[last[0]] = last[1]
    return out


class Engine:
    """
    prices : {code: [[date, close], ...]}
    meta   : {code: {"name":.., "isBond":bool}}
    """

    def __init__(self, prices, meta, cash_code="511880"):
        self.cash = cash_code
        self.meta = meta
        self.pool = [c for c in prices if c != cash_code]
        self.dpx = {c: dict(rows) for c, rows in prices.items()}
        self.wpx = {c: to_weekly(rows) for c, rows in prices.items()}
        self.bonds = {c for c in prices if meta.get(c, {}).get("isBond")}

        self.all_daily = sorted({d for m in self.dpx.values() for d in m})
        self.all_weekly = sorted({d for m in self.wpx.values() for d in m})
        self._widx = {d: i for i, d in enumerate(self.all_weekly)}
        self._snap = None

    # ── 日频 EWMA：在全部历史上递推，在每个周锚点存快照 ──────────────
    def _build_daily_ewma(self, lam=LAMBDA_EWMA):
        codes = list(self.dpx)
        var = {c: None for c in codes}
        cov = {}
        for i, a in enumerate(codes):
            for b in codes[i:]:
                cov[(a, b)] = None
        prev = {c: None for c in codes}
        wset = set(self.all_weekly)
        snap = {}
        for d in self.all_daily:
            r = {}
            for c in codes:
                p, pp = self.dpx[c].get(d), prev[c]
                r[c] = (p / pp - 1) if (p is not None and pp not in (None, 0)) else None
                if p is not None:
                    prev[c] = p
            for c in codes:
                if r[c] is not None:
                    var[c] = r[c] ** 2 if var[c] is None else \
                        lam * var[c] + (1 - lam) * r[c] ** 2
            for (a, b) in cov:
                ra, rb = r[a], r[b]
                if ra is not None and rb is not None:
                    cov[(a, b)] = ra * rb if cov[(a, b)] is None else \
                        lam * cov[(a, b)] + (1 - lam) * ra * rb
            if d in wset:
                snap[d] = (
                    {c: (var[c] * PERIODS_PER_YEAR if var[c] is not None else None)
                     for c in codes},
                    {k: (v * PERIODS_PER_YEAR if v is not None else None)
                     for k, v in cov.items()},
                )
        self._snap = snap
        return snap

    # ── 单个调仓日的目标权重 ──────────────────────────────────────
    def target_weights(self, wdate, seen=None):
        """
        wdate: 调仓日（必须是 self.all_weekly 中的日期）
        返回 (weights dict, diag dict)
        """
        if self._snap is None:
            self._build_daily_ewma()
        sv, sc = self._snap.get(wdate, ({}, {}))
        gi = self._widx[wdate]

        # 资格：上市满 53 周（以实际可用周线根数为准，自洽且不含前视）
        elig = []
        for c in self.pool:
            if wdate not in self.wpx[c]:
                continue
            n = sum(1 for d in self.all_weekly[:gi + 1] if d in self.wpx[c])
            if n >= MIN_HIST_WEEKS:
                elig.append(c)

        # ① 挑菜：多周期动量集成
        score, detail = {}, {}
        for c in elig:
            last = self.wpx[c][wdate]
            hits = tot = 0
            hit_list = []
            for lb in LOOKBACKS:
                j = gi - lb
                if j >= 0:
                    base = self.wpx[c].get(self.all_weekly[j])
                    if base not in (None, 0):
                        tot += 1
                        up = (last / base - 1) > 0
                        hits += 1 if up else 0
                        hit_list.append((lb, round((last / base - 1) * 100, 2)))
            if tot and hits / tot > SCORE_THRESHOLD:
                score[c] = hits / tot
                detail[c] = hit_list

        w, ex_ante, scale = {}, None, 1.0
        if score:
            # ② 配比：w ∝ 得分 / 年化波动
            raw = {}
            for c, s in score.items():
                v = sv.get(c)
                if v and v > 0:
                    vol = math.sqrt(v)
                    if vol > 1e-6:
                        raw[c] = s / vol
            tot_raw = sum(raw.values())
            if tot_raw > 0:
                w = {c: v / tot_raw for c, v in raw.items()}

                # 债券上限：超出部分按比例分给非债资产
                bw = sum(w[c] for c in w if c in self.bonds)
                if bw > BOND_CAP and bw > 0:
                    exc = bw - BOND_CAP
                    for c in list(w):
                        if c in self.bonds:
                            w[c] *= BOND_CAP / bw
                    oth = {c: v for c, v in w.items() if c not in self.bonds}
                    so = sum(oth.values())
                    if so > 0:
                        for c in oth:
                            w[c] += exc * oth[c] / so

                # 最小权重过滤 + 重新归一
                w = {c: v for c, v in w.items() if v >= MIN_WEIGHT}
                sw = sum(w.values())
                if sw > 0:
                    w = {c: v / sw for c, v in w.items()}

                # ③ 总量：组合波动目标
                var_p = 0.0
                for a in w:
                    for b in w:
                        k = (a, b) if (a, b) in sc else (b, a)
                        cv = sc.get(k)
                        if cv is not None:
                            var_p += w[a] * w[b] * cv
                ex_ante = math.sqrt(var_p) if var_p > 0 else None
                scale = min(MAX_LEVERAGE, TARGET_VOL / ex_ante) \
                    if (ex_ante and ex_ante > 0) else MAX_LEVERAGE
                w = {c: v * scale for c, v in w.items()}

        risk_w = sum(w.values())
        return w, {
            "date": wdate,
            "nEligible": len(elig),
            "nSelected": len(w),
            "exAnteVol": ex_ante,
            "scale": scale,
            "riskWeight": risk_w,
            "cashWeight": max(0.0, 1.0 - risk_w),
            "scores": score,
            "momentumDetail": detail,
        }

    # ── 回测：周频调仓，日频结算 ───────────────────────────────────
    def run(self, start="2014-01-02", init=100000.0, cost=True, delay=0):
        """
        delay=0：信号日收盘价成交（本地口径）
        delay=1：下一交易日收盘成交（贴近聚宽日回测的开盘成交）

        返回 dict：日频净值曲线 + 每次调仓记录
        """
        if self._snap is None:
            self._build_daily_ewma()

        reb_dates = [d for d in self.all_weekly if d >= start]
        if not reb_dates:
            raise ValueError("start 之后无调仓日")

        # 先算出每个调仓日的目标权重
        plans = []
        for d in reb_dates:
            w, diag = self.target_weights(d)
            plans.append((d, w, diag))

        didx = {d: i for i, d in enumerate(self.all_daily)}
        sched = []
        for d, w, diag in plans:
            j = didx.get(d)
            if j is None:
                continue
            j += delay
            if j < len(self.all_daily):
                sched.append((j, w, diag))
        if not sched:
            raise ValueError("无有效生效日")

        start_i = sched[0][0]
        codes = list(self.dpx)
        prev = {c: None for c in codes}
        for d in self.all_daily[:start_i + 1]:
            for c in codes:
                p = self.dpx[c].get(d)
                if p is not None:
                    prev[c] = p

        equity = init
        w_cur, ptr = {}, 0
        curve, rebalances = [], []
        turn_total = cost_total = 0.0

        for i in range(start_i, len(self.all_daily)):
            d = self.all_daily[i]

            # ① 先按昨日持仓吃掉今日收益
            if w_cur:
                rp = 0.0
                for c, wt in w_cur.items():
                    p, pp = self.dpx[c].get(d), prev[c]
                    if p is not None and pp not in (None, 0):
                        rp += wt * (p / pp - 1)
                cw = max(0.0, 1 - sum(w_cur.values()))
                pc, ppc = self.dpx[self.cash].get(d), prev[self.cash]
                if pc is not None and ppc not in (None, 0):
                    rp += cw * (pc / ppc - 1)
                equity *= (1 + rp)

            for c in codes:
                p = self.dpx[c].get(d)
                if p is not None:
                    prev[c] = p

            # ② 生效日换仓并扣成本
            while ptr < len(sched) and sched[ptr][0] == i:
                _, w_new, diag = sched[ptr]
                keys = set(w_new) | set(w_cur)
                to = sum(abs(w_new.get(c, 0) - w_cur.get(c, 0)) for c in keys)
                cst = sum(abs(w_new.get(c, 0) - w_cur.get(c, 0)) *
                          (COMMISSION_BP + SPREAD_BP.get(c, DEFAULT_SPREAD_BP) / 2)
                          / 10000 for c in keys) if cost else 0.0
                equity *= (1 - cst)
                turn_total += to
                cost_total += cst
                rebalances.append({
                    "date": diag["date"],
                    "effectiveDate": d,
                    "weights": {c: round(v, 6) for c, v in
                                sorted(w_new.items(), key=lambda kv: -kv[1])},
                    "prevWeights": {c: round(v, 6) for c, v in w_cur.items()},
                    "turnover": round(to, 6),
                    "cost": round(cst, 8),
                    "exAnteVol": diag["exAnteVol"],
                    "scale": round(diag["scale"], 6),
                    "riskWeight": round(diag["riskWeight"], 6),
                    "cashWeight": round(diag["cashWeight"], 6),
                    "nSelected": diag["nSelected"],
                    "nEligible": diag["nEligible"],
                })
                w_cur = w_new
                ptr += 1

            curve.append([d, round(equity, 4)])

        years = len(curve) / PERIODS_PER_YEAR if curve else 0
        return {
            "curve": curve,
            "rebalances": rebalances,
            "init": init,
            "turnoverPerYear": (turn_total / years) if years else 0,
            "costPerYear": (cost_total / years) if years else 0,
        }


# ══════════════════════════════════════════════════════════════════
# 指标计算
# ══════════════════════════════════════════════════════════════════
def drawdown_series(curve):
    """返回 [[date, dd_pct], ...]，dd 为负值百分比"""
    out, peak = [], None
    for d, v in curve:
        peak = v if peak is None else max(peak, v)
        out.append([d, round((v / peak - 1) * 100, 4)])
    return out


def _stats_from_daily(curve, rf=RF_ANNUAL):
    """
    用**日**收益算波动与夏普（年化系数 252）。
    注：聚宽平台的夏普口径是 (年化 − 4%) / (日收益std × √250)，
        本项目统一用 rf=2%、×252。跨平台比较时须注意口径差异
        （本策略波动 8~9% 时，rf 从 2%→4% 会让夏普少约 0.22）。
    """
    if len(curve) < 20:
        return None
    vals = [v for _, v in curve]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
    n = len(rets)
    mu = sum(rets) / n
    var = sum((x - mu) ** 2 for x in rets) / (n - 1) if n > 1 else 0.0
    vol = math.sqrt(var * PERIODS_PER_YEAR)
    years = n / PERIODS_PER_YEAR
    total = vals[-1] / vals[0] - 1
    cagr = ((vals[-1] / vals[0]) ** (1 / years) - 1) if years > 0 else 0.0

    peak, mdd, mdd_date = vals[0], 0.0, None
    for d, v in curve:
        peak = max(peak, v)
        dd = v / peak - 1
        if dd < mdd:
            mdd, mdd_date = dd, d

    # 下行波动（只统计低于无风险日收益的部分）
    dn = [min(0.0, x - rf / PERIODS_PER_YEAR) for x in rets]
    dvol = math.sqrt(sum(x * x for x in dn) / n) * math.sqrt(PERIODS_PER_YEAR)

    sharpe = (cagr - rf) / vol if vol > 1e-12 else 0.0
    sortino = (cagr - rf) / dvol if dvol > 1e-12 else 0.0
    calmar = (cagr / abs(mdd)) if mdd < -1e-12 else 0.0

    up = sum(x for x in rets if x > 0)
    dnn = -sum(x for x in rets if x < 0)
    return {
        "start": curve[0][0], "end": curve[-1][0], "days": len(curve),
        "totalReturn": round(total * 100, 3),
        "cagr": round(cagr * 100, 3),
        "vol": round(vol * 100, 3),
        "downVol": round(dvol * 100, 3),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "mdd": round(mdd * 100, 3),
        "mddDate": mdd_date,
        "calmar": round(calmar, 3),
        "winRate": round(100.0 * sum(1 for x in rets if x > 0) / n, 2),
        "profitFactor": round(up / dnn, 3) if dnn > 1e-12 else None,
        "best": round(max(rets) * 100, 3),
        "worst": round(min(rets) * 100, 3),
    }


def window_stats(curve, months):
    """
    近 N 个月指标。按自然月回溯（不是 30×N 天），更符合直觉。
    收益率为**区间累计**（不年化），因为 1 个月年化会严重放大噪声；
    夏普/波动仍按年化口径以便横向比较。
    """
    if not curve:
        return None
    end = date.fromisoformat(curve[-1][0])
    y, m = end.year, end.month - months
    while m <= 0:
        m += 12
        y -= 1
    try:
        cut = date(y, m, end.day)
    except ValueError:                       # 例如 3/31 回退到 2/31
        cut = date(y, m, 28)
    cut_s = cut.isoformat()
    sub = [p for p in curve if p[0] >= cut_s]
    if len(sub) < 15:
        return None
    st = _stats_from_daily(sub)
    if st:
        st["windowMonths"] = months
        st["periodReturn"] = st["totalReturn"]     # 区间累计收益
    return st


def yearly_returns(curve):
    """逐自然年收益（跨年用上年末净值为基准）"""
    if not curve:
        return []
    by = {}
    for d, v in curve:
        by.setdefault(d[:4], []).append(v)
    out, prev_end = [], None
    for y in sorted(by):
        vals = by[y]
        base = prev_end if prev_end is not None else vals[0]
        out.append({"year": y, "return": round((vals[-1] / base - 1) * 100, 3)})
        prev_end = vals[-1]
    return out


def underwater_segments(curve, min_days=10):
    """
    水下期分段（从历史新高跌下到再创新高）。
    用于回答"零亏损年份"与"长水下期"为何不矛盾：
    年度收益按日历年切，水下期从任意历史新高起算、可跨年。
    """
    segs, peak, start_i = [], None, None
    for i, (d, v) in enumerate(curve):
        if peak is None or v >= peak:
            if start_i is not None and i - start_i >= min_days:
                sub = curve[start_i:i + 1]
                trough = min(sub, key=lambda p: p[1])
                segs.append({
                    "from": curve[start_i][0], "to": d,
                    "days": i - start_i,
                    "depth": round((trough[1] / curve[start_i][1] - 1) * 100, 3),
                    "troughDate": trough[0],
                })
            peak, start_i = v, i
    if start_i is not None and len(curve) - 1 - start_i >= min_days:
        sub = curve[start_i:]
        trough = min(sub, key=lambda p: p[1])
        segs.append({
            "from": curve[start_i][0], "to": None,
            "days": len(curve) - 1 - start_i,
            "depth": round((trough[1] / curve[start_i][1] - 1) * 100, 3),
            "troughDate": trough[0],
        })
    segs.sort(key=lambda s: -s["days"])
    return segs
