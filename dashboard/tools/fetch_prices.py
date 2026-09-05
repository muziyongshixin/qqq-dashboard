#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ-RP 看板 · 价格取数层（tdxrs 权威源 + 按比例拼接的 HTTP 降级）
================================================================
设计目标：**任何情况下都不产出脏数据**。

数据源优先级
------------
1. **tdxrs 0.6.7（通达信直连）— 权威源**
   · 官方除权除息（get_fund_xdxr_info）→ 精确前复权，无需启发式猜跳变
   · 历史最长（最早 2011-09）、无 HTTP 限流
   · 硬要求 **Python ≥ 3.11**（只提供 cp311 wheel，其余是 Rust 源码包需编译链）
   · 有快照时走**增量**（只取最后几十根），无快照或除权变动时才**全量重建**

增量更新机制（★ 每日 CI 的默认路径）
------------------------------------
实测（14 只标的，本机）：

    全量 get_fund_bars_all ×14 → 38,686 根  4.55s
    增量 get_fund_bars(start=0, count=60) ×14 →  840 根  1.52s
                                        ↑ 取数量降到 2.2%，提速 3.0 倍

但增量有一个**必须守卫的正确性陷阱**：

  **前复权的定义是"把除权日之前的所有历史价格整条改写"**。
  所以只要出现一个新的分红/拆股事件，整条基线序列就全部失效了——
  此时"只把新几天接到末尾"会得到一条前后不一致的曲线。

守卫办法：**xdxr 指纹比对**。
  每次先取 get_fund_xdxr_info（14 次请求约 1s，很便宜），
  对全部除权事件算一个哈希存进 meta.xdxrFingerprint。
    · 指纹一致 → 历史不可能变过 → 安全地走增量
    · 指纹变化 → **强制全量重建**（并在日志中说明是哪只触发的）
    · 快照里没有指纹（旧版产出）→ 也走全量重建，顺便补上指纹

第二道保险：**重叠段逐点校验**。
  增量取回的是不复权价，按重叠日的比例锚定后，
  重叠段每一天都必须与基线吻合（容差 1bp）。
  任何一只不吻合 → 放弃增量、转全量。这能兜住数据源自身的历史修订。

2. **东方财富 HTTP（fqt=1 前复权）— 仅用于增量补最后几天**
   · 只在 tdxrs 不可用时启用（例如 GitHub Actions runner 在境外连不通行情服务器）
   · ⚠ 关键：**绝不直接使用东财的绝对价格**，而是把东财的
     「日收益率」按重叠日锚定后接到 tdxrs 基线末尾。
     原因：两家的前复权基准日/分红处理不同，绝对价位不可比；
     但**日收益率是可比的**。按比例拼接可保证序列内部自洽，
     不会在拼接点产生虚假跳变。

3. 两者都失败 → **保留原快照不动**，并把 stale 标记写进 meta，
   由前端显著提示「数据未更新」。绝不写入半截数据。

用法
----
  # 权威全量重建（需 Python 3.11 + tdxrs）
  python3 tools/fetch_prices.py --full

  # 增量更新（tdxrs 可用则仍走 tdxrs 全量；否则东财补增量）
  python3 tools/fetch_prices.py

输出
----
  data/prices.json
    {
      "meta": {
        "source": "tdxrs" | "tdxrs+eastmoney" | "eastmoney" | "cache",
        "updatedAt": "2026-09-05T16:20:00+08:00",
        "lastTradeDate": "2026-09-04",
        "stale": false,
        "codes": {code: {"name":..., "isBond":..., "bars":..., "range":[..]}}
      },
      "prices": {code: [[date, close_qfq], ...]}
    }
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
OUT_PATH = os.path.join(DATA_DIR, "prices.json")

CST = timezone(timedelta(hours=8))

# ── 资产池：13 只风险资产 + 1 只现金腿 ──────────────────────────────
# ⚠ 现金腿必须用 511880 银华日利（价格累积型）。
#   不要用 511990 华宝添益：它是"份额结转型"，场内价恒约 100 元，
#   收益体现在份额增长而非价格，用其价格序列回测必然得到零收益。
POOL = {
    "513100": {"name": "纳指ETF",      "isBond": False},
    "513500": {"name": "标普500ETF",   "isBond": False},
    "510300": {"name": "沪深300ETF",   "isBond": False},
    "159915": {"name": "创业板ETF",    "isBond": False},
    "512480": {"name": "半导体ETF",    "isBond": False},
    "512890": {"name": "红利低波ETF",  "isBond": False},
    "513050": {"name": "中概互联ETF",  "isBond": False},
    "518880": {"name": "黄金ETF",      "isBond": False},
    "159985": {"name": "豆粕ETF",      "isBond": False},
    "162411": {"name": "华宝油气",     "isBond": False},
    "501018": {"name": "南方原油",     "isBond": False},
    "511010": {"name": "国债ETF",      "isBond": True},
    "511260": {"name": "十年国债ETF",  "isBond": True},
    "511880": {"name": "银华日利",     "isBond": False, "isCash": True},
}
CASH_CODE = "511880"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_BARS = 8000
XDXR_SPLIT = 11              # category=11 → 份额变动（拆股/缩股）


def market_of(code):
    """上交所=1，深交所=0（tdxrs 约定）"""
    return 1 if code[0] in "56" else 0


def latest_expected_trade_date(now=None):
    """
    粗略推算"最近一个应有行情的交易日"（只排除周末，不含节假日表）。
    · 周一~周五 15:00 之后 → 今天；15:00 之前 → 上一个工作日
    · 周六/周日 → 上一个周五
    用途：避免在没有新交易日时（周末/盘中）做无意义的 HTTP 请求，
         也避免把"本来就没有新数据"误报成"取数失败"。
    节假日误判由调用方的"距今 N 天"检查兜底。
    """
    d = (now or datetime.now(CST))
    if d.weekday() < 5 and d.hour >= 15:
        cur = d.date()
    else:
        cur = d.date() - timedelta(days=1)
    while cur.weekday() >= 5:               # 5=周六 6=周日
        cur -= timedelta(days=1)
    return cur.isoformat()


# ══════════════════════════════════════════════════════════════════
# 数据源 1：tdxrs（权威）
# ══════════════════════════════════════════════════════════════════
def adjust_forward(bars, xdxr):
    """
    用官方除权除息数据做精确前复权（后向乘以调整比）。

    ⚠ 通达信 xdxr 字段的两个坑（都踩过，勿改回）：
      1. **fenhong / songzhuangu 均为"每 10 份"**，按"每 1 份"用会过度调整 10 倍，
         表现为复权后出现 +25%~+61% 的虚假正跳变（510300、511880 曾中招）。
      2. **suogu 可以 < 1**（缩股 / 份额合并）。统一规律是 `ratio = 1 / suogu`，
         对 >1（拆股，如 513100 的 suogu=5）和 <1（缩股，如 511030 的 suogu=0.1）
         **同一公式都成立**。加 `suogu > 1` 的守卫会漏掉缩股，
         导致复权后出现 +170%~+900% 的虚假正跳变。
    """
    rows = [[b["datetime"], float(b["close"])] for b in bars]
    events = []
    for e in xdxr or []:
        try:
            d = "%04d-%02d-%02d" % (e["year"], e["month"], e["day"])
        except (KeyError, TypeError):
            continue
        suogu = e.get("suogu") or 0
        fenhong = (e.get("fenhong") or 0) / 10.0        # 每10份 → 每1份
        songzhuangu = (e.get("songzhuangu") or 0) / 10.0

        ratio = 1.0
        if e.get("category") == XDXR_SPLIT and suogu and suogu > 0 \
                and abs(suogu - 1.0) > 1e-9:
            ratio /= float(suogu)
        if songzhuangu > 0:
            ratio /= (1.0 + songzhuangu)
        if fenhong > 0:
            prev = None
            for dd, px in rows:
                if dd < d:
                    prev = px
                else:
                    break
            if prev and prev > fenhong:
                ratio *= (prev - fenhong) / prev
        if abs(ratio - 1.0) > 1e-9:
            events.append({"date": d, "ratio": round(ratio, 8)})
            for r in rows:
                if r[0] < d:
                    r[1] = round(r[1] * ratio, 6)
    return rows, events


def sanity_check(rows, code, name):
    """
    质量自检：复权后不应残留巨幅单日跳变。
    正常 ETF 单日 ±20% 已极罕见；>35% 基本可判定为复权错误。
    """
    bad = []
    for i in range(1, len(rows)):
        p0, p1 = rows[i - 1][1], rows[i][1]
        if p0 and p1:
            chg = p1 / p0 - 1
            if abs(chg) > 0.35:
                bad.append((rows[i][0], round(chg * 100, 1)))
    if bad:
        print("  [WARN] %s %s 残留异常 %d 处: %s"
              % (code, name, len(bad), bad[:3]), file=sys.stderr)
    return bad


def fetch_tdxrs():
    """
    全量拉取 + 官方前复权。返回 {code: [[date, close], ...]}；不可用时返回 None。
    """
    cli = connect_tdxrs()
    if cli is None:
        return None
    return _tdxrs_full(cli)[0]


def connect_tdxrs():
    """建立 tdxrs 连接；不可用时返回 None（不抛异常）。"""
    try:
        from tdxrs import TdxHqFundClient
    except ImportError:
        print("[tdxrs] 未安装或 Python < 3.11，跳过", file=sys.stderr)
        return None
    try:
        cli = TdxHqFundClient()
        if not cli.connect_to_any():
            print("[tdxrs] 无法连接任何行情服务器（境外网络常见），跳过",
                  file=sys.stderr)
            return None
        return cli
    except Exception as e:
        print("[tdxrs] 连接异常：%s，跳过" % e, file=sys.stderr)
        return None


# ══════════════════════════════════════════════════════════════════
# 除权事件监控：决定「增量」还是「全量」
# ══════════════════════════════════════════════════════════════════
def xdxr_fingerprint(events):
    """
    把一只标的的全部除权除息事件压成一个短哈希。

    为什么需要它 —— 这是增量更新能否成立的**第一前提**：
      前复权的定义是「把除权日**之前**的所有历史价格整条改写」。
      所以一旦新增一个分红/拆股事件，旧基线的**每一根**历史 K 线都失效了，
      此时把新几天接到末尾会得到一条前后断裂的曲线（且看不出明显异常，
      因为跳变被复权抹平了，只是整段历史的比例错了）。

    指纹只覆盖**影响复权结果**的字段（日期 + 分红 + 送转 + 缩股 + 类别），
    不含成交量等无关字段，避免无意义的误触发全量。
    """
    keys = []
    for e in events or []:
        d = xdxr_date(e)
        if not d:
            continue
        keys.append("%s|%s|%s|%s|%s" % (
            d, e.get("category"),
            _fp_num(e.get("fenhong")), _fp_num(e.get("songzhuangu")),
            _fp_num(e.get("suogu"))))
    keys.sort()
    return hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def xdxr_date(e):
    """从除权事件里取出 'YYYY-MM-DD'；字段异常返回 None。"""
    try:
        return "%04d-%02d-%02d" % (e["year"], e["month"], e["day"])
    except (KeyError, TypeError):
        return None


def xdxr_after(events, since):
    """
    列出**在 since 之后**发生的除权日。

    ⚠ 这是与「指纹比对」互补的第二个判据，实测踩到过（2026-09-06）：
      512480 半导体 ETF 在 2026-07-03 发生 1 拆 2（suogu=2.0）。
      如果上次全量时该事件**已经存在**，那么两次指纹是**相同**的，
      「指纹一致」会误判为可以增量 —— 但增量窗口（最近 60 根）
      横跨了这个除权日，窗口内的复权比例并不恒定
      （除权日前 k=0.5、除权日后 k=1.0，极差 100%），
      按单一锚点比例外推会把整段拼错。

    所以增量的正确前提是**两条同时满足**：
      ① 指纹未变（没有新增/修改的除权事件）
      ② 增量窗口内不含任何除权日（窗口内复权比例恒定）
    """
    out = []
    for e in events or []:
        d = xdxr_date(e)
        if d and d > since:
            out.append(d)
    return sorted(out)


def _fp_num(v):
    """数值归一化：None/0 统一成 '0'，浮点定长，避免 1.0 与 1 产生不同指纹。"""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return "0"
    return "0" if abs(f) < 1e-12 else "%.6f" % f


def collect_xdxr(cli):
    """
    取全部标的的除权事件并算指纹。
    返回 ({code: [events]}, {code: fingerprint})；任一只失败则返回 (None, None)。

    这一步很便宜（14 次请求约 1s），却是判断能否增量的依据，所以每次都做。
    """
    events, fps = {}, {}
    for code in POOL:
        try:
            ev = cli.get_fund_xdxr_info(market_of(code), code) or []
        except Exception as e:
            print("  [WARN] %s 取除权信息失败：%s → 保守起见转全量"
                  % (code, e), file=sys.stderr)
            return None, None
        events[code] = ev
        fps[code] = xdxr_fingerprint(ev)
    return events, fps


def diff_fingerprints(old_fps, new_fps):
    """
    比对新旧指纹，返回发生除权变动的标的列表。
    old_fps 缺失（旧版快照没存指纹）时返回 None，表示「无法判断 → 必须全量」。
    """
    if not old_fps:
        return None
    changed = []
    for code in POOL:
        if code not in old_fps:
            return None                     # 有标的没有历史指纹 → 无法判断
        if old_fps[code] != new_fps.get(code):
            changed.append(code)
    return changed


# ══════════════════════════════════════════════════════════════════
def _tdxrs_full(cli, xdxr_map=None):
    """
    全量重建：每只标的取全部历史 + 官方前复权。
    返回 (prices, bad_count)；取不满 14 只则 (None, n)。
    """
    from tdxrs.constants import KLINE_DAILY

    out, total_bad = {}, 0
    for code, meta in POOL.items():
        mkt = market_of(code)
        try:
            bars = cli.get_fund_bars_all(KLINE_DAILY, mkt, code, MAX_BARS)
            if not bars:
                print("  [WARN] %s 无数据" % code, file=sys.stderr)
                continue
            if xdxr_map is not None:
                xdxr = xdxr_map.get(code) or []
            else:
                try:
                    xdxr = cli.get_fund_xdxr_info(mkt, code) or []
                except Exception:
                    xdxr = []
            rows, _ = adjust_forward(bars, xdxr)
            total_bad += len(sanity_check(rows, code, meta["name"]))
            out[code] = rows
            print("  %s %-12s %5d 根  %s → %s"
                  % (code, meta["name"], len(rows), rows[0][0], rows[-1][0]))
        except Exception as e:
            print("  [WARN] %s 失败：%s" % (code, e), file=sys.stderr)

    if len(out) < len(POOL):
        print("[tdxrs] 仅取到 %d/%d 只，视为失败以避免残缺数据"
              % (len(out), len(POOL)), file=sys.stderr)
        return None, total_bad
    print("[tdxrs] 全量完成：%d 只 %d 根，残留异常 %d 处"
          % (len(out), sum(len(v) for v in out.values()), total_bad))
    return out, total_bad


INCREMENTAL_BARS = 60        # 增量单次最多取多少根（上限，实际按需收缩）
MIN_OVERLAP_BARS = 3         # 至少保留几根重叠用于锚定与校验
OVERLAP_TOL_BP = 1.0         # 重叠段逐点校验容差（基点）


def plan_tail(cache, code, max_tail=INCREMENTAL_BARS):
    """
    为单只标的规划增量窗口大小（根数）。

    ★ 为什么要"按需收缩"而不是固定 60 根 —— 实测踩到的设计缺陷（2026-09-06）：
      511010 / 511260 国债 ETF **每季度分红**。若窗口固定 60 根（≈3 个月），
      那么几乎任何时候回溯 60 根都会横跨一个分红日，
      「窗口内含除权日 → 必须全量」于是常年成立，增量形同废纸。

      但真正需要的重叠只有几根（用于锚定 + 校验）。
      所以正确做法是：**窗口 = 缺失的交易日数 + 少量重叠**，
      这样通常只取 3~10 根，横跨除权日的概率大幅下降；
      即便真的横跨了，也只在那一天转全量，之后立刻恢复增量。

    返回 (tail, window_start_date)。
    """
    rows = ((cache or {}).get("prices") or {}).get(code) or []
    if not rows:
        return max_tail, "0000-00-00"       # 无基线 → 必然触发全量
    # 估算缺失根数：用日历天差保守放大（含周末/长假，宁多勿少）
    last = rows[-1][0]
    try:
        gap_days = (datetime.now(CST).date()
                    - datetime.strptime(last, "%Y-%m-%d").date()).days
    except ValueError:
        gap_days = max_tail
    need = max(0, gap_days) + MIN_OVERLAP_BARS
    tail = max(MIN_OVERLAP_BARS + 1, min(max_tail, need))
    return tail, rows[max(0, len(rows) - tail)][0]


def _tdxrs_incremental(cli, cache, tails):
    """
    增量更新：按 tails[code] 规划的窗口取最近若干根，锚定后追加到基线末尾。

    ★ 调用前必须已确认：① xdxr 指纹未变 ② 各自窗口内无除权日。

    tdxrs 返回**不复权**价，而基线是**前复权**价。既然窗口内无除权事件，
    两者之间就只差一个**恒定**的比例因子 k（= 该标的历史除权调整的乘积）。
    用重叠段求出 k 后，新数据 × k 即为正确的前复权价。

    第二道保险：**重叠段逐点校验**。k 由最后一个重叠日求得，
    然后要求重叠段每一天都满足 |基线 / (不复权 × k) − 1| < 1bp。
    若不满足，说明数据源修订了历史（或除权判据没能覆盖某种变动）→ 放弃增量。
    实测该校验成功拦截过 512480 拆股窗口（偏差 5000bp）。

    返回 (prices, added, reason)；reason 非空表示增量不可用、需转全量。
    """
    from tdxrs.constants import KLINE_DAILY

    baseline = cache["prices"]
    out = {c: [list(r) for r in rows] for c, rows in baseline.items()}
    added_total, req_bars = 0, 0

    for code in POOL:
        rows = out.get(code)
        if not rows:
            return None, 0, "基线缺少 %s" % code
        tail = tails.get(code, INCREMENTAL_BARS)

        try:
            bars = cli.get_fund_bars(KLINE_DAILY, market_of(code), code, 0, tail)
        except Exception as e:
            return None, 0, "%s 增量取数失败：%s" % (code, e)
        if not bars:
            return None, 0, "%s 增量返回空" % code
        req_bars += len(bars)

        raw = {}
        for b in bars:
            try:
                raw[b["datetime"]] = float(b["close"])
            except (KeyError, TypeError, ValueError):
                continue

        have = dict(rows)
        common = sorted(set(have) & set(raw))
        if not common:
            # 基线落后超过窗口（例如停更很久）→ 没有重叠可锚定
            return None, 0, ("%s 无重叠日（基线末 %s，增量首 %s），"
                             "基线落后超过 %d 根"
                             % (code, rows[-1][0], min(raw), tail))

        anchor = common[-1]
        if not raw[anchor] or raw[anchor] <= 0:
            return None, 0, "%s 锚点价异常" % code
        k = have[anchor] / raw[anchor]

        # 重叠段逐点校验
        for d in common:
            if raw[d] <= 0:
                continue
            dev = abs(have[d] / (raw[d] * k) - 1) * 1e4
            if dev > OVERLAP_TOL_BP:
                return None, 0, ("%s 重叠日 %s 偏差 %.2fbp（>%.1fbp），"
                                 "疑似历史被修订" % (code, d, dev, OVERLAP_TOL_BP))

        for d in sorted(raw):
            if d > anchor:
                rows.append([d, round(raw[d] * k, 6)])
                added_total += 1

    total = sum(len(v) for v in out.values())
    print("[tdxrs] 增量完成：%d 只，请求 %d 根（全量需 %d 根，仅 %.2f%%），"
          "新增 %d 条"
          % (len(POOL), req_bars, total, 100.0 * req_bars / max(1, total),
             added_total))
    return out, added_total, ""


# ══════════════════════════════════════════════════════════════════
# 数据源 2：东方财富 HTTP（仅增量，按比例拼接）
# ══════════════════════════════════════════════════════════════════
def fetch_eastmoney_one(code, beg, retry=4):
    """东财日线前复权。返回 [(date, close), ...]；失败返回 None。"""
    secid = "%d.%s" % (market_of(code), code)
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=%s&fields1=f1,f2,f3,f4,f5&fields2=f51,f53&klt=101&fqt=1"
           "&beg=%s&end=20500101" % (secid, beg))
    hdr = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
           "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
           "Connection": "close"}
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers=hdr)
            raw = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            kl = (json.loads(raw).get("data") or {}).get("klines") or []
            rows = []
            for x in kl:
                p = x.split(",")
                if len(p) >= 2:
                    try:
                        rows.append((p[0], float(p[1])))
                    except ValueError:
                        pass
            if rows:
                return rows
        except Exception:
            pass
        # 东财按 IP 限流较严（密集请求会 RemoteDisconnected），指数退避
        time.sleep(1.5 * (i + 1))
    return None


def fetch_sina_one(code, datalen=40, retry=3):
    """
    新浪日线 —— ⚠ **不复权**。仅作最后一道降级。
    因为只用于"最近若干天"的按比例拼接，且调用方会做除权保护
    （单日 |收益| > 15% 视为疑似除权并拒绝拼接），风险可控。
    """
    sym = ("sh" if market_of(code) == 1 else "sz") + code
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d"
           % (sym, datalen))
    for i in range(retry):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Referer": "https://finance.sina.com.cn/"})
            txt = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            arr = json.loads(txt)
            rows = [(x["day"][:10], float(x["close"])) for x in arr if x.get("close")]
            if rows:
                return rows
        except Exception:
            pass
        time.sleep(1.2 * (i + 1))
    return None


MAX_DAILY_MOVE = 0.15        # 单日涨跌超过此阈值视为疑似除权，拒绝拼接


def splice_incremental(baseline, overlap_days=12):
    """
    把 HTTP 增量按**收益率**接到 tdxrs 基线末尾。

    为什么不用绝对价格：各家前复权的基准日与分红处理不同，
    绝对价位不可比（同一只 ETF 可能差几分钱），
    直接替换会在拼接点产生虚假跳变。
    而**日收益率**是可比的，按比例外推可保证序列内部自洽。

    返回 (新序列 dict, 实际新增天数, 失败代码列表, 实际使用的源)
    """
    out = {c: list(rows) for c, rows in baseline.items()}
    added_total, failed = 0, []

    anchor = max(rows[-1][0] for rows in baseline.values() if rows)
    beg = (datetime.strptime(anchor, "%Y-%m-%d")
           - timedelta(days=overlap_days * 3)).strftime("%Y%m%d")

    used = set()
    for code in POOL:
        rows = out.get(code)
        if not rows:
            failed.append(code)
            continue

        # 先东财（前复权，优先），失败再新浪（不复权，兜底）
        inc, src = fetch_eastmoney_one(code, beg), "eastmoney"
        time.sleep(0.8)
        if not inc:
            inc, src = fetch_sina_one(code), "sina"
            time.sleep(0.5)
        if not inc:
            failed.append(code)
            continue

        have = dict(rows)
        inc_map = dict(inc)
        common = sorted(set(have) & set(inc_map))
        if not common:
            failed.append(code)
            continue
        anchor_d = common[-1]
        base_px, inc_anchor = have[anchor_d], inc_map[anchor_d]
        if not inc_anchor or inc_anchor <= 0:
            failed.append(code)
            continue

        # 除权保护：拼接段内任一单日跳变过大 → 拒绝（新浪不复权时尤其重要）
        new_dates = [d for d, _ in sorted(inc) if d > anchor_d]
        seq = [inc_anchor] + [inc_map[d] for d in new_dates]
        suspicious = any(
            seq[i - 1] > 0 and abs(seq[i] / seq[i - 1] - 1) > MAX_DAILY_MOVE
            for i in range(1, len(seq)))
        if suspicious:
            print("  [WARN] %s 增量段存在单日 >%.0f%% 跳变（疑似除权），拒绝拼接"
                  % (code, MAX_DAILY_MOVE * 100), file=sys.stderr)
            failed.append(code)
            continue

        for d in new_dates:
            rows.append([d, round(base_px * (inc_map[d] / inc_anchor), 6)])
            added_total += 1
        rows.sort(key=lambda r: r[0])
        used.add(src)

    return out, added_total, failed, "+".join(sorted(used)) if used else ""


# ══════════════════════════════════════════════════════════════════
def load_cache():
    if not os.path.exists(OUT_PATH):
        return None
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("prices") else None
    except Exception:
        return None


def build_meta(prices, source, stale=False, note="", xdxr_fps=None, mode=""):
    codes = {}
    for c, rows in prices.items():
        m = dict(POOL.get(c, {}))
        m["bars"] = len(rows)
        m["range"] = [rows[0][0], rows[-1][0]] if rows else None
        codes[c] = m
    last = max((rows[-1][0] for rows in prices.values() if rows), default=None)
    meta = {
        "source": source,
        "mode": mode,                       # full | incremental | cache
        "updatedAt": datetime.now(CST).isoformat(timespec="seconds"),
        "lastTradeDate": last,
        "stale": stale,
        "note": note,
        "cashCode": CASH_CODE,
        "codes": codes,
    }
    if xdxr_fps:
        # 除权指纹：下次运行据此判断能否走增量（详见 xdxr_fingerprint 的注释）
        meta["xdxrFingerprint"] = xdxr_fps
    return meta


def write_out(prices, meta):
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "prices": prices}, f,
                  ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser(description="QQQ-RP 看板价格取数")
    ap.add_argument("--full", action="store_true",
                    help="强制全量重建（要求 tdxrs 可用，否则报错退出）")
    ap.add_argument("--tail", type=int, default=INCREMENTAL_BARS,
                    help="增量模式每只取最近多少根（默认 %d）" % INCREMENTAL_BARS)
    ap.add_argument("--allow-http", action="store_true", default=True,
                    help="tdxrs 不可用时允许东财增量降级（默认开启）")
    ap.add_argument("--no-http", dest="allow_http", action="store_false",
                    help="禁用东财降级：tdxrs 不可用则保留旧快照")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    cache = load_cache()

    # ══════════════════════════════════════════════════════════════
    # 路线 A：tdxrs 可用 → 除权监控决定「增量」还是「全量」
    # ══════════════════════════════════════════════════════════════
    print("[1/3] 连接 tdxrs（权威源）…")
    cli = connect_tdxrs()

    if cli is not None:
        # ── 每次都取除权信息（14 次请求约 1s，很便宜，却是增量的前提）──
        print("[2/3] 检查除权除息事件…")
        xdxr_map, new_fps = collect_xdxr(cli)

        decision, changed, tails = "full", None, {}
        cached_last = ((cache or {}).get("meta") or {}).get("lastTradeDate") or ""
        if xdxr_map is None:
            reason = "除权信息取数失败 → 保守全量"
        elif args.full:
            reason = "命令行指定 --full"
        elif not cache:
            reason = "无历史快照，首次构建"
        else:
            old_fps = (cache.get("meta") or {}).get("xdxrFingerprint")
            changed = diff_fingerprints(old_fps, new_fps)
            # 判据②：增量窗口内若存在除权日，窗口内复权比例不恒定，
            #        按单一锚点外推必然拼错（实测 512480 于 2026-07-03 拆股触发）。
            #        窗口按"缺失天数 + 少量重叠"自适应收缩，通常只有几根，
            #        因此国债 ETF 的季度分红也很少会落进来。
            in_window = {}
            for c in POOL:
                t, wstart = plan_tail(cache, c, args.tail)
                tails[c] = t
                ds = xdxr_after(xdxr_map.get(c), wstart)
                if ds:
                    in_window[c] = ds

            if changed is None:
                reason = "快照无除权指纹（旧版产出）→ 全量重建并补齐指纹"
            elif changed:
                names = "、".join("%s %s" % (c, POOL[c]["name"]) for c in changed)
                reason = "⚠ 检测到除权变动：%s → 历史已被改写，必须全量" % names
            elif in_window:
                names = "、".join("%s %s(%s)" % (c, POOL[c]["name"], ds[0])
                                  for c, ds in in_window.items())
                reason = ("⚠ 增量窗口内含除权日：%s → 窗口内复权比例不恒定，必须全量"
                          % names)
            else:
                decision, reason = "incremental", (
                    "无除权事件（指纹一致 + 窗口内无除权日）→ 走增量，"
                    "窗口 %d~%d 根/只" % (min(tails.values()), max(tails.values())))

        print("  · %s" % reason)

        # ── 增量路径 ──
        if decision == "incremental":
            expected = latest_expected_trade_date()
            if cached_last >= expected:
                # 连增量请求都不必发：本来就没有新交易日
                d = cache
                d["meta"]["stale"] = False
                d["meta"]["mode"] = "cache"
                d["meta"]["note"] = ("无新交易日（最近应有行情日 %s，快照已是最新）"
                                     % expected)
                d["meta"]["xdxrFingerprint"] = new_fps
                d["meta"]["checkedAt"] = datetime.now(CST).isoformat(timespec="seconds")
                write_out(d["prices"], d["meta"])
                print("[3/3] 无新交易日（应有行情日 %s ≤ 快照 %s），"
                      "已跳过取数" % (expected, cached_last))
                return 0

            print("[3/3] 增量拉取（窗口 %d~%d 根/只，按缺失天数自适应）…"
                  % (min(tails.values()), max(tails.values())))
            prices, added, why = _tdxrs_incremental(cli, cache, tails)

            if prices is not None:
                old_last = (cache.get("meta") or {}).get("lastTradeDate")
                meta = build_meta(
                    prices, "tdxrs", mode="incremental", xdxr_fps=new_fps,
                    note="增量更新：%s 之后新增 %d 条（无除权事件，历史未改写）"
                         % (old_last, added))
                write_out(prices, meta)
                print("✓ 增量更新完成：新增 %d 条 → 最新交易日 %s"
                      % (added, meta["lastTradeDate"]))
                return 0

            # 增量失败（无重叠 / 历史被修订）→ 自动降级全量，绝不产出脏数据
            print("  [WARN] 增量不可用：%s → 转全量重建" % why, file=sys.stderr)
            decision = "full"

        # ── 全量路径 ──
        print("· 全量重建整条序列…")
        prices, _ = _tdxrs_full(cli, xdxr_map)
        if prices:
            meta = build_meta(prices, "tdxrs", mode="full", xdxr_fps=new_fps,
                              note=reason if decision == "full" else "")
            write_out(prices, meta)
            print("✓ tdxrs 全量重建完成 → %s（最新交易日 %s）"
                  % (OUT_PATH, meta["lastTradeDate"]))
            return 0
        print("[tdxrs] 全量重建失败，转 HTTP 降级判断", file=sys.stderr)

    # ══════════════════════════════════════════════════════════════
    # 路线 B：tdxrs 不可用 → 基线 + HTTP 增量（按比例拼接）
    # ══════════════════════════════════════════════════════════════
    if args.full:
        print("✗ --full 要求 tdxrs 可用，但当前不可用。\n"
              "  请在 Python 3.11 环境执行：pip install tdxrs", file=sys.stderr)
        return 2

    if not cache:
        print("✗ tdxrs 不可用且无历史快照，无法降级。\n"
              "  首次构建必须在本地用 Python 3.11 + tdxrs 执行：\n"
              "    python3 tools/fetch_prices.py --full", file=sys.stderr)
        return 2

    if not args.allow_http:
        print("· tdxrs 不可用且已禁用 HTTP 降级 → 保留旧快照", file=sys.stderr)
        d = cache
        d["meta"]["stale"] = True
        d["meta"]["mode"] = "cache"
        d["meta"]["note"] = "tdxrs 不可用且禁用 HTTP 降级，数据未更新"
        write_out(d["prices"], d["meta"])
        return 1

    # ── 短路：本来就没有新交易日（周末 / 盘中）→ 不做任何请求 ──
    # 否则会白白触发 HTTP 限流，还会把"无新数据"误报成"取数失败"。
    expected = latest_expected_trade_date()
    cached_last = cache["meta"].get("lastTradeDate") or ""
    if cached_last >= expected:
        d = cache
        d["meta"]["stale"] = False
        d["meta"]["mode"] = "cache"
        d["meta"]["note"] = "无新交易日（最近应有行情日 %s，快照已是最新）" % expected
        d["meta"]["checkedAt"] = datetime.now(CST).isoformat(timespec="seconds")
        write_out(d["prices"], d["meta"])
        print("· 无新交易日（应有行情日 %s ≤ 快照 %s），保持不变"
              % (expected, cached_last))
        return 0

    print("· tdxrs 不可用 → HTTP 增量补齐（按收益率拼接，不用绝对价）…")
    baseline = {c: [list(r) for r in rows] for c, rows in cache["prices"].items()}
    old_last = cache["meta"].get("lastTradeDate")
    spliced, added, failed, used_src = splice_incremental(baseline)

    if failed:
        print("  [WARN] %d 只增量失败：%s" % (len(failed), failed), file=sys.stderr)
    # 只要有任一标的失败，就不写入——避免各标的时间轴不齐导致的权重失真
    if failed or added == 0:
        note = ("HTTP 增量失败（%d 只），保留上次 tdxrs 快照" % len(failed)
                if failed else "无新交易日数据")
        d = cache
        d["meta"]["stale"] = bool(failed)
        d["meta"]["mode"] = "cache"
        d["meta"]["note"] = note
        d["meta"]["checkedAt"] = datetime.now(CST).isoformat(timespec="seconds")
        write_out(d["prices"], d["meta"])
        print("· %s" % note)
        return 0 if not failed else 1

    meta = build_meta(spliced, "tdxrs+" + used_src, stale=False,
                      mode="incremental",
                      xdxr_fps=(cache.get("meta") or {}).get("xdxrFingerprint"),
                      note="历史为 tdxrs 官方复权；%s 之后 %d 条由 %s 按收益率拼接"
                           % (old_last, added, used_src))
    write_out(spliced, meta)
    print("✓ 增量完成：新增 %d 条 → 最新交易日 %s"
          % (added, meta["lastTradeDate"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
