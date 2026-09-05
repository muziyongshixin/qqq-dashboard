#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建看板数据 data/dashboard.json
=================================
输入：data/prices.json（由 tools/fetch_prices.py 产出，tdxrs 官方复权）
输出：data/dashboard.json（前端直接消费，无需任何后端计算）

产出内容
--------
1. latest      本周调仓动作（目标权重、与上周对比的买/卖/加/减、下单金额）
2. curve       日频净值曲线（含回撤序列、基准对比）
3. windows     近 1/3/6/12 月 + 今年以来 + 全样本 的指标
4. yearly      逐年收益
5. underwater  水下期分段（最长水下期）
6. rebalances  最近调仓历史
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

import engine as E                                        # noqa: E402

DATA_DIR = os.path.join(BASE, "data")
PRICES = os.path.join(DATA_DIR, "prices.json")
OUT = os.path.join(DATA_DIR, "dashboard.json")
CST = timezone(timedelta(hours=8))

BACKTEST_START = "2014-01-20"
DEFAULT_CAPITAL = 100000.0
# 基准：沪深300（策略与它相关性低，用于展示分散化价值）
BENCH_CODE = "510300"


def action_of(old, new, eps=0.005):
    """对比上下两期权重，给出人类可读的动作"""
    if old < eps and new >= eps:
        return "买入"
    if old >= eps and new < eps:
        return "清仓"
    if new - old >= eps:
        return "加仓"
    if old - new >= eps:
        return "减仓"
    return "持有"


def build_latest(res, meta, capital, cash_code):
    """本周调仓动作明细"""
    if not res["rebalances"]:
        return None
    last = res["rebalances"][-1]
    prev = last["prevWeights"]
    cur = last["weights"]

    rows = []
    for c in sorted(set(cur) | set(prev), key=lambda x: -cur.get(x, 0)):
        ow, nw = prev.get(c, 0.0), cur.get(c, 0.0)
        act = action_of(ow, nw)
        rows.append({
            "code": c,
            "name": meta.get(c, {}).get("name", c),
            "isBond": bool(meta.get(c, {}).get("isBond")),
            "prevWeight": round(ow * 100, 2),
            "weight": round(nw * 100, 2),
            "delta": round((nw - ow) * 100, 2),
            "action": act,
            "amount": round(nw * capital, 0),
            "deltaAmount": round((nw - ow) * capital, 0),
        })

    cash_prev = max(0.0, 1 - sum(prev.values()))
    cash_now = last["cashWeight"]
    rows.append({
        "code": cash_code,
        "name": meta.get(cash_code, {}).get("name", "现金腿"),
        "isCash": True,
        "prevWeight": round(cash_prev * 100, 2),
        "weight": round(cash_now * 100, 2),
        "delta": round((cash_now - cash_prev) * 100, 2),
        "action": action_of(cash_prev, cash_now),
        "amount": round(cash_now * capital, 0),
        "deltaAmount": round((cash_now - cash_prev) * capital, 0),
    })

    return {
        "signalDate": last["date"],
        "effectiveDate": last["effectiveDate"],
        "nSelected": last["nSelected"],
        "nEligible": last["nEligible"],
        "exAnteVol": (round(last["exAnteVol"] * 100, 2)
                      if last["exAnteVol"] else None),
        "scale": last["scale"],
        "riskWeight": round(last["riskWeight"] * 100, 2),
        "cashWeight": round(last["cashWeight"] * 100, 2),
        "turnover": round(last["turnover"] * 100, 2),
        "cost": round(last["cost"] * 100, 4),
        "capital": capital,
        "rows": rows,
    }


def build_benchmark(prices, curve, code=BENCH_CODE):
    """基准归一化到与策略同起点同初值，便于同图对比"""
    if code not in prices:
        return None
    bm = dict(prices[code])
    dates = [d for d, _ in curve]
    base = None
    out = []
    for d in dates:
        p = bm.get(d)
        if p is None:
            if out:
                out.append([d, out[-1][1]])
            continue
        if base is None:
            base = p
        out.append([d, round(curve[0][1] * p / base, 4)])
    return out


def main():
    if not os.path.exists(PRICES):
        sys.exit("缺少 %s，请先运行 tools/fetch_prices.py --full" % PRICES)

    with open(PRICES, "r", encoding="utf-8") as f:
        pd_ = json.load(f)
    prices, pmeta = pd_["prices"], pd_["meta"]
    cash_code = pmeta.get("cashCode", "511880")
    meta = {c: {"name": m.get("name", c), "isBond": bool(m.get("isBond"))}
            for c, m in pmeta["codes"].items()}

    print("引擎回测中（%d 只标的，%s 起）…" % (len(prices), BACKTEST_START))
    eng = E.Engine(prices, meta, cash_code=cash_code)
    res = eng.run(start=BACKTEST_START, init=DEFAULT_CAPITAL, delay=0)
    curve = res["curve"]

    full = E._stats_from_daily(curve)
    windows = {}
    for m in (1, 3, 6, 12):
        s = E.window_stats(curve, m)
        if s:
            windows["m%d" % m] = s

    # 今年以来
    ytd_cut = curve[-1][0][:4] + "-01-01"
    ytd_sub = [p for p in curve if p[0] >= ytd_cut]
    if len(ytd_sub) >= 15:
        windows["ytd"] = E._stats_from_daily(ytd_sub)
    windows["all"] = full

    uw = E.underwater_segments(curve)
    dd = E.drawdown_series(curve)

    out = {
        "meta": {
            "builtAt": datetime.now(CST).isoformat(timespec="seconds"),
            "dataSource": pmeta.get("source"),
            "dataUpdatedAt": pmeta.get("updatedAt"),
            "lastTradeDate": pmeta.get("lastTradeDate"),
            "stale": bool(pmeta.get("stale")),
            "note": pmeta.get("note", ""),
            "backtestStart": BACKTEST_START,
            "capital": DEFAULT_CAPITAL,
            "cashCode": cash_code,
            "benchCode": BENCH_CODE,
            "benchName": meta.get(BENCH_CODE, {}).get("name", BENCH_CODE),
            "params": {
                "lookbacks": list(E.LOOKBACKS),
                "scoreThreshold": E.SCORE_THRESHOLD,
                "lambdaEwma": E.LAMBDA_EWMA,
                "riskClock": "daily(x252)",
                "targetVol": E.TARGET_VOL,
                "bondCap": E.BOND_CAP,
                "minWeight": E.MIN_WEIGHT,
                "maxLeverage": E.MAX_LEVERAGE,
                "minHistWeeks": E.MIN_HIST_WEEKS,
                "rf": E.RF_ANNUAL,
                "commissionBp": E.COMMISSION_BP,
            },
            "codes": meta,
            "turnoverPerYear": round(res["turnoverPerYear"] * 100, 1),
            "costPerYear": round(res["costPerYear"] * 100, 3),
        },
        "latest": build_latest(res, meta, DEFAULT_CAPITAL, cash_code),
        "curve": curve,
        "drawdown": dd,
        "benchmark": build_benchmark(prices, curve),
        "windows": windows,
        "yearly": E.yearly_returns(curve),
        "underwater": uw[:8],
        "rebalances": res["rebalances"][-60:],
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    sz = os.path.getsize(OUT) / 1024
    print("✓ 已写入 %s（%.0f KB）" % (OUT, sz))
    print("  数据源 %s | 最新交易日 %s%s"
          % (pmeta.get("source"), pmeta.get("lastTradeDate"),
             "  ⚠ STALE" if pmeta.get("stale") else ""))
    print("  全样本：年化 %.2f%% 夏普 %.3f MDD %.2f%% Calmar %.3f"
          % (full["cagr"], full["sharpe"], full["mdd"], full["calmar"]))
    for k in ("m1", "m3", "m6", "ytd"):
        if k in windows:
            w = windows[k]
            print("  %-4s：收益 %+.2f%%  夏普 %.2f  MDD %.2f%%"
                  % (k, w["totalReturn"], w["sharpe"], w["mdd"]))
    if out["latest"]:
        L = out["latest"]
        print("  本周信号 %s：选中 %d 只，风险仓位 %.1f%%，现金 %.1f%%"
              % (L["signalDate"], L["nSelected"], L["riskWeight"], L["cashWeight"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
