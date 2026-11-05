"""选股漏斗 + 大盘情绪择时（波段 1~4 周风格）。

漏斗：
  全A快照(东财, ~5900只) + 场内ETF
   → 硬过滤：交易权限板块 / ST / 停牌 / 次新 / 成交额 / 一手买得起（≤单票仓位上限）
   → 快照预打分取前 kline_candidates 只
   → 拉日K细算波段分：趋势、位置、动量、量能、板块强度、波动、追高惩罚
   → 前 40 只再查近5日主力资金 + 近7日公告（减持/问询等扣分）
   → Top N 候选池（写入 candidates 表），收盘复盘时送投研委员会审核前几名

情绪短线（style=momentum 的账户，见 core/emotion.py）：
   → 只在热门题材（涨停最多的行业）和最强板块里选
   → 今天涨 2%~5%（追高上限和风控一致）、没封死涨停、放量、主力流入、收在高位、没在高位连续暴涨的
   → 总仓位上限 = min(大盘状态上限, 情绪周期上限)（情绪：冰点 0 / 退潮 20% / 修复 50% / 升温 80% / 高潮 50%）

大盘状态（regime）：上涨家数占比、涨停/跌停家数、两市成交额、沪深300 相对 MA20/MA60
  → 强势 / 震荡 / 弱势 / 冰点，对应不同总仓位上限（config.regime.caps）
"""
import json
import logging
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import broker, db, indicators, market, news
from .config import load_config, style as account_style

log = logging.getLogger("screener")

CLIST = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=%s&fs=%s"
         "&fields=f2,f3,f5,f6,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f23,f26,f62,f100,f184")
FS_STOCK = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FS_ETF = "b:MK0021,b:MK0022,b:MK0023,b:MK0024"
FS_SECTOR = "m:90+t:2"

_cache = {}
_lock = threading.Lock()


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _clist(fs, fid="f6", max_pages=80, workers=6):
    first = json.loads(market._http_get(CLIST % (1, fid, fs), timeout=10))["data"]
    rows = list(first["diff"])
    pages = min(max_pages, (first["total"] + 99) // 100)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for d in ex.map(lambda p: json.loads(market._http_get(CLIST % (p, fid, fs), timeout=10)).get("data"),
                            range(2, pages + 1)):
                if d:
                    rows.extend(d["diff"])
    return rows


def _norm(r, is_etf=False):
    code = str(r.get("f12"))
    if len(code) != 6 or not code.isdigit():
        return None
    if r.get("f13") == 1:
        sym = "sh" + code
    else:
        sym = ("bj" if code.startswith(("8", "4", "92")) else "sz") + code
    return {"symbol": sym, "name": r.get("f14", ""), "price": _num(r.get("f2")), "pct": _num(r.get("f3")),
            "amount": _num(r.get("f6")), "turnover": _num(r.get("f8")), "pe": _num(r.get("f9")),
            "vol_ratio": _num(r.get("f10")), "high": _num(r.get("f15")), "low": _num(r.get("f16")),
            "open": _num(r.get("f17")), "prev_close": _num(r.get("f18")), "total_mv": _num(r.get("f20")),
            "pb": _num(r.get("f23")), "list_date": str(r.get("f26") or ""), "main_net": _num(r.get("f62")),
            "main_pct": _num(r.get("f184")), "industry": r.get("f100") or ("ETF" if is_etf else ""),
            "board": "etf" if is_etf else market.board(sym)}


def snapshot_all(ttl=60):
    """全A股 + ETF 快照（缓存 ttl 秒）。"""
    with _lock:
        hit = _cache.get("all")
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    stocks = [x for x in (_norm(r) for r in _clist(FS_STOCK)) if x]
    etfs = [x for x in (_norm(r, True) for r in _clist(FS_ETF, max_pages=4)) if x]
    val = {"stocks": stocks, "etfs": etfs}
    with _lock:
        _cache["all"] = (time.time(), val)
    return val


def sectors(ttl=120):
    with _lock:
        hit = _cache.get("sec")
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    rows = _clist(FS_SECTOR, fid="f3", max_pages=5)
    out = {}
    n = len(rows)
    for i, r in enumerate(sorted(rows, key=lambda x: -_num(x.get("f3")))):
        out[r.get("f14")] = {"pct": _num(r.get("f3")), "main_net_yi": round(_num(r.get("f62")) / 1e8, 2),
                             "rank": i + 1, "rank_pct": round((i + 1) / n * 100, 1)}
    with _lock:
        _cache["sec"] = (time.time(), out)
    return out


def market_regime(ttl=120):
    """大盘情绪状态。"""
    with _lock:
        hit = _cache.get("regime")
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    snap = snapshot_all()["stocks"]
    live = [s for s in snap if s["price"] > 0]
    up = sum(1 for s in live if s["pct"] > 0)
    down = sum(1 for s in live if s["pct"] < 0)
    lu = sum(1 for s in live if s["pct"] >= market.limit_pct(s["symbol"], s["name"]) - 0.25)
    ld = sum(1 for s in live if s["pct"] <= -market.limit_pct(s["symbol"], s["name"]) + 0.25)
    amount = sum(s["amount"] for s in live) / 1e8
    med = statistics.median([s["pct"] for s in live]) if live else 0
    idx = {}
    try:
        kl = market.get_kline("sh000300", "day", 80)
        c = [b["c"] for b in kl]
        idx = {"hs300": c[-1], "ma20": round(indicators.sma(c, 20), 2), "ma60": round(indicators.sma(c, 60), 2),
               "chg5d_pct": round((c[-1] / c[-6] - 1) * 100, 2)}
    except Exception as e:
        log.warning("指数K线失败: %s", e)
    score, why = 0.0, []
    ur = up / max(1, up + down)
    if ur >= 0.6:
        score += 1; why.append("上涨家数占比%.0f%%" % (ur * 100))
    elif ur <= 0.35:
        score -= 1; why.append("下跌家数占比%.0f%%" % ((1 - ur) * 100))
    if lu >= 80:
        score += 1; why.append("涨停%d家" % lu)
    if ld >= 50:
        score -= 2; why.append("跌停%d家" % ld)
    elif ld >= 20:
        score -= 1; why.append("跌停%d家" % ld)
    if idx:
        if idx["hs300"] > idx["ma20"]:
            score += 1; why.append("沪深300站上MA20")
        else:
            score -= 1; why.append("沪深300跌破MA20")
        if idx["ma20"] > idx["ma60"]:
            score += 0.5
        else:
            score -= 0.5; why.append("MA20<MA60 中期走弱")
    if not idx:
        why.append("沪深300数据缺失（未计入，结论偏乐观）")
    glob = None
    try:
        from . import global_market
        d, reason = global_market.regime_adjust()
        if reason:
            score += d; why.append(reason)
        ov = global_market.overnight()
        glob = ov["summary"] if ov else None
    except Exception as e:
        log.warning("外盘修正失败: %s", e)
    if amount >= 15000:
        score += 0.5; why.append("成交额%.0f亿 活跃" % amount)
    elif amount < 8000:
        score -= 0.5; why.append("成交额%.0f亿 缩量" % amount)
    state = "强势" if score >= 2 else "震荡" if score >= 0 else "弱势" if score >= -2 else "冰点"
    cap = load_config()["regime"]["caps"].get(state, 0.6)
    val = {"state": state, "score": score, "position_cap": cap, "reasons": why, "up": up, "down": down,
           "limit_up": lu, "limit_down": ld, "amount_yi": round(amount), "median_pct": round(med, 2),
           "index": idx, "global": glob, "as_of": market.now().strftime("%m-%d %H:%M")}
    with _lock:
        _cache["regime"] = (time.time(), val)
    return val


# ---------------- 打分 ----------------
def hard_filter(items, cfg, max_lot_value):
    u = cfg["universe"]
    today = market.now().strftime("%Y%m%d")
    out, dropped = [], {}

    def drop(k):
        dropped[k] = dropped.get(k, 0) + 1
    for s in items:
        if s["board"] not in u["boards"]:
            drop("无交易权限"); continue
        if s["price"] <= 0:
            drop("停牌"); continue
        if u["exclude_st"] and ("ST" in s["name"].upper() or "退" in s["name"]):
            drop("ST/退市"); continue
        if s["price"] < u["min_price"]:
            drop("低价股"); continue
        min_amt = u["etf_min_amount_yi"] if s["board"] == "etf" else u["min_amount_yi"]
        if s["amount"] < min_amt * 1e8:
            drop("成交额不足"); continue
        if s["board"] != "etf" and s["list_date"][:8].isdigit():
            days = (market.datetime.strptime(today, "%Y%m%d") - market.datetime.strptime(s["list_date"][:8], "%Y%m%d")).days
            if days < u["min_list_days"]:
                drop("次新股"); continue
        if s["price"] * market.lot_rule(s["symbol"])[0] > max_lot_value:
            drop("一手超出单票上限"); continue
        out.append(s)
    return out, dropped


def pre_score(s, sec):
    """仅用快照的粗分，用于决定拉哪些日K。"""
    sc = 0.0
    if 0 < s["pct"] < 6:
        sc += 1
    if 1.0 <= s["vol_ratio"] <= 3:
        sc += 0.5
    if s["main_pct"] > 0:
        sc += 0.5
    r = (sec.get(s["industry"]) or {}).get("rank_pct")
    if r is not None and r <= 30:
        sc += 0.5
    return sc + min(s["amount"] / 1e9, 1.0) * 0.5


def swing_score(s, kl, sec):
    """波段打分。返回 (score, reasons, metrics)。"""
    c = [b["c"] for b in kl]
    v = [b["v"] for b in kl]
    if len(c) < 70:
        return None
    sc, why = 0.0, []
    ma20, ma60 = indicators.sma(c, 20), indicators.sma(c, 60)
    ma20_prev = indicators.sma(c[:-5], 20)
    if c[-1] > ma20:
        sc += 1; why.append("站上MA20")
    if ma20 > ma60:
        sc += 1; why.append("MA20>MA60")
    if ma20 > ma20_prev:
        sc += 0.5; why.append("MA20向上")
    chg20 = (c[-1] / c[-21] - 1) * 100
    hi60 = max(b["h"] for b in kl[-60:])
    if 3 <= chg20 <= 30:
        sc += 1; why.append("20日涨%.0f%%(健康)" % chg20)
    elif chg20 > 40:
        sc -= 2; why.append("20日涨%.0f%%(过热)" % chg20)
    if c[-1] >= hi60 * 0.92:
        sc += 0.5; why.append("接近60日高点")
    elif c[-1] < hi60 * 0.75:
        sc -= 1; why.append("距60日高点回撤>25%")
    m = indicators.macd(c)
    if m:
        if m["dif"] > m["dea"]:
            sc += 0.5; why.append("MACD多头")
        if m["dif"] > 0:
            sc += 0.5
    r = indicators.rsi(c, 14)
    if r is not None:
        if 50 <= r <= 70:
            sc += 1; why.append("RSI%.0f" % r)
        elif r > 78:
            sc -= 1; why.append("RSI%.0f过热" % r)
    vr = (sum(v[-5:]) / 5) / (sum(v[-20:]) / 20) if sum(v[-20:]) else 1
    if 1.1 <= vr <= 2.5:
        sc += 1; why.append("温和放量x%.1f" % vr)
    elif vr > 3.5:
        sc -= 0.5; why.append("异常放量x%.1f" % vr)
    rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 20, len(c))]
    vol = statistics.pstdev(rets) * 100
    if vol > 5:
        sc -= 0.5; why.append("波动大%.1f%%" % vol)
    if s["pct"] >= 7:
        sc -= 1; why.append("今日涨%.1f%%不追" % s["pct"])
    prev = s["prev_close"] or c[-2]
    if prev and (s["high"] - s["price"]) / prev * 100 >= 4:
        sc -= 0.5; why.append("长上影")
    if s["main_pct"] > 3:
        sc += 0.5; why.append("今日主力净占比%.1f%%" % s["main_pct"])
    sr = sec.get(s["industry"])
    if sr:
        if sr["rank_pct"] <= 20:
            sc += 1; why.append("板块强(%s 前%.0f%%)" % (s["industry"], sr["rank_pct"]))
        elif sr["rank_pct"] >= 80:
            sc -= 0.5; why.append("板块弱")
    metrics = {"ma20": round(ma20, 3), "ma60": round(ma60, 3), "chg20d": round(chg20, 2), "rsi14": r and round(r, 1),
               "vol5_20": round(vr, 2), "volatility20": round(vol, 2), "hi60": hi60}
    return sc, why, metrics


def momentum_pre(s, sec, hot, mc):
    """情绪短线初筛分（快照）。hot: {行业: 题材排名}。不在热门题材/强势板块里返回 None。"""
    if s["board"] == "etf":
        return None
    sr = (sec.get(s["industry"]) or {}).get("rank_pct")
    rank = hot.get(s["industry"])
    if rank is None and (sr is None or sr > 15):
        return None
    lim = market.limit_pct(s["symbol"], s["name"])
    chase = min(mc["max_chase_pct"], load_config()["risk"]["max_chase_pct"])     # 和下单风控一致，选了也买不了的不选
    if not (mc["min_pct"] <= s["pct"] < min(chase, lim - 0.3)):
        return None
    sc = (3 - min(rank, 10) * 0.2) if rank else 1
    sc += min(s["vol_ratio"], 4) * 0.3 + (0.5 if s["main_pct"] > 0 else 0) + min(s["amount"] / 1e9, 2) * 0.3
    return sc


def momentum_score(s, kl, sec, hot, mc):
    """情绪短线打分：题材热度 > 当日强度 > 量能资金 > 位置。返回 (score, reasons, metrics)。"""
    c = [b["c"] for b in kl]
    if len(c) < 20:
        return None
    sc, why = 0.0, []
    rank = hot.get(s["industry"])
    if rank:
        sc += 2.5 if rank <= 3 else 1.5
        why.append("热门题材第%d（%s）" % (rank, s["industry"]))
    sr = sec.get(s["industry"])
    if sr and sr["rank_pct"] <= 10:
        sc += 1; why.append("板块今日前%.0f%%" % sr["rank_pct"])
    if 3 <= s["pct"] <= 7:
        sc += 1; why.append("今日涨%.1f%%（强势未封板）" % s["pct"])
    elif s["pct"] > 7:
        sc += 0.5; why.append("今日涨%.1f%%（接近涨停，次日分歧风险大）" % s["pct"])
    elif s["pct"] >= 2:
        sc += 0.5; why.append("今日涨%.1f%%" % s["pct"])
    if 1.5 <= s["vol_ratio"] <= 5:
        sc += 1; why.append("量比%.1f" % s["vol_ratio"])
    elif s["vol_ratio"] > 8:
        sc -= 0.5; why.append("量比%.1f过大（可能是出货）" % s["vol_ratio"])
    if s["main_pct"] > 5:
        sc += 1; why.append("主力净占比%.1f%%" % s["main_pct"])
    elif s["main_pct"] > 0:
        sc += 0.5
    elif s["main_pct"] < -5:
        sc -= 1; why.append("涨着但主力净流出%.1f%%（小心借涨出货）" % s["main_pct"])
    prev = s["prev_close"] or c[-2]
    if prev:
        shadow = (s["high"] - s["price"]) / prev * 100
        if shadow <= 1:
            sc += 0.5; why.append("收在最高附近")
        elif shadow >= 4:
            sc -= 1.5; why.append("冲高回落%.1f%%（上影线长）" % shadow)
    ma5, ma10 = indicators.sma(c, 5), indicators.sma(c, 10)
    if c[-1] > ma5 > ma10:
        sc += 0.5; why.append("5/10日线多头")
    chg5 = (c[-1] / c[-6] - 1) * 100
    chg10 = (c[-1] / c[-11] - 1) * 100
    if chg5 > 30 or chg10 > 50:
        sc -= 2; why.append("短期已涨太多（5日%.0f%% 10日%.0f%%）高位接力风险大" % (chg5, chg10))
    if 3 <= s["turnover"] <= 20:
        sc += 0.5
    elif s["turnover"] > 30:
        sc -= 0.5; why.append("换手%.0f%%过高" % s["turnover"])
    mv = s["total_mv"] / 1e8
    if 30 <= mv <= 500:
        sc += 0.5
    metrics = {"theme_rank": rank, "chg5d": round(chg5, 2), "chg10d": round(chg10, 2), "ma5": round(ma5, 3),
               "ma10": round(ma10, 3), "vol_ratio": s["vol_ratio"], "turnover": s["turnover"], "style": "momentum"}
    return sc, why, metrics


def position_cap(account="paper"):
    """该账户新开仓的总仓位上限。
    波段：看大盘状态（config.regime.caps）。
    情绪短线：大盘状态上限和情绪周期上限（emotion.STAGE_CAPS）取更小的——只会比波段更严，不会更松。"""
    reg = market_regime()
    out = {"style": account_style(account), "regime": reg["state"], "cap": reg["position_cap"]}
    if out["style"] == "momentum":
        from . import emotion
        emo = emotion.sentiment()
        if emo.get("error"):
            out["emotion_error"] = emo["error"]          # 情绪数据拿不到时退回按大盘
        else:
            out["emotion"] = emo["stage"]
            out["cap"] = min(reg["position_cap"], emo["position_cap"])     # 只收紧：取大盘和情绪中更小的
    return out


# ---- 运行状态（网页按钮和 15:05 自动流程共用，同一时间只跑一个） ----
KLINE_BUDGET_SEC = 75      # 拉日K最多花这么久，超时就用已经拿到的（免费接口偶尔会被限流卡住）
ENRICH_BUDGET_SEC = 30     # 资金流/公告补充最多花这么久
_run_lock = threading.Lock()
STATE = {"running": False, "account": "paper", "stage": "", "done": 0, "total": 0, "started": None, "msg": "", "trigger": "",
         "finished": None, "seconds": None}
_kcache = {}               # 日K缓存：symbol -> (过期时间, rows)


def _stage(stage, done=0, total=0):
    STATE.update(stage=stage, done=done, total=total, msg=stage + ("（%d/%d）" % (done, total) if total else ""))


def status():
    st = dict(STATE)
    if st["running"] and st["started"]:
        st["elapsed"] = round(time.time() - st["started"], 1)
    return st


def _kline_cached(sym):
    now = time.time()
    hit = _kcache.get(sym)
    if hit and hit[0] > now:
        return hit[1]
    rows = market.get_kline(sym, "day", 130)
    # 盘中 5 分钟过期；盘后日K不会再变，缓存 3 小时
    _kcache[sym] = (now + (300 if market.is_market_open() else 10800), rows)
    return rows


def start_async(trigger="网页", account="paper"):
    """后台选股，立刻返回。已经在跑就返回当前进度（不重复跑）。"""
    if STATE["running"]:
        return dict(status(), started_now=False)
    STATE.update(running=True, account=account, started=time.time(), trigger=trigger, done=0, total=0,
                 stage="准备中", msg="准备中")

    def go():
        try:
            run(account=account)
        except Exception as e:
            log.exception("选股失败")
            STATE["msg"] = "失败：%s" % e
    threading.Thread(target=go, daemon=True, name="screener").start()
    return dict(status(), started_now=True)


def run(save=True, top_n=None, progress=None, account="paper"):
    """跑一次选股漏斗（按账户各选各的）。同一账户别处正在跑时，等它跑完直接用它的结果。"""
    if not _run_lock.acquire(blocking=False):
        same = STATE.get("account") == account
        _run_lock.acquire()
        _run_lock.release()
        lc = latest_candidates(top_n or 30, account)
        if same and lc and time.time() - lc["ts_epoch"] < 300:
            return lc
        return run(save, top_n, progress, account)
    STATE.update(running=True, account=account,
                 started=STATE["started"] if STATE["running"] and STATE["started"] else time.time())
    try:
        res = _run(save, top_n, progress, account)
        STATE.update(msg="完成：候选 %d 只，用时 %.0f 秒" % (len(res["candidates"]), res["stats"]["seconds"]),
                     seconds=res["stats"]["seconds"], finished=time.time(), stage="完成")
        return res
    except Exception as e:
        STATE.update(msg="失败：%s" % e, finished=time.time(), stage="失败")
        raise
    finally:
        STATE["running"] = False
        _run_lock.release()


def _run(save=True, top_n=None, progress=None, account="paper"):
    cfg = load_config()
    db.init_db()
    _ensure_schema()
    t0 = time.time()
    equity = broker.snapshot(account)["equity"]     # 先检查资金，不白跑接口
    if equity <= 0:
        raise ValueError("%s还没有资金，先在「持仓对账」录入券商账户的可用资金" % ("实盘" if account == "live" else "模拟盘"))
    _stage("拉全市场快照")
    snap = snapshot_all(ttl=30)
    _stage("板块和大盘状态")
    sec = sectors()
    regime = market_regime()
    mom = account_style(account, cfg) == "momentum"
    mc = cfg["momentum"]
    hot = {}
    if mom:
        from . import emotion
        _stage("情绪周期和热门题材")
        emo = emotion.sentiment()
        regime = dict(regime, style="momentum")
        if emo.get("error"):
            regime["emotion_error"] = emo["error"]
        else:
            regime["emotion"] = emo
            regime["position_cap"] = min(regime["position_cap"], emo["position_cap"])
            hot = {t["name"]: t["rank"] for t in emo["themes"] if t["rank"] <= mc["theme_top"]}

    max_lot_value = equity * cfg["risk"]["max_position_pct"]
    universe = snap["stocks"] + snap["etfs"]
    passed, dropped = hard_filter(universe, cfg, max_lot_value)
    if mom:
        ps = [(momentum_pre(s, sec, hot, mc), s) for s in passed]
        dropped["不在热门题材/不够强"] = sum(1 for p, _ in ps if p is None)
        ps = sorted([x for x in ps if x[0] is not None], key=lambda x: -x[0])
        passed = [s for _, s in ps]
    else:
        passed.sort(key=lambda s: -pre_score(s, sec))
    pool = passed[:cfg["screener"]["kline_candidates"]]
    if progress:
        progress("初筛 %d → %d，拉日K %d 只" % (len(universe), len(passed), len(pool)))
    _stage("初筛 %d → %d，拉日K细算" % (len(universe), len(passed)), 0, len(pool))
    deadline = time.time() + KLINE_BUDGET_SEC
    skipped = {"n": 0}

    def k(s):
        if time.time() > deadline:          # 超出时间预算：剩下的不拉了
            skipped["n"] += 1
            return s, None
        try:
            return s, _kline_cached(s["symbol"])
        except Exception:
            return s, None
        finally:
            STATE["done"] += 1
            STATE["msg"] = "%s（%d/%d）" % (STATE["stage"], STATE["done"], STATE["total"])
    scored = []
    with ThreadPoolExecutor(max_workers=4) as ex:   # 免费接口，别并发太猛
        for s, kl in ex.map(k, pool):
            if not kl:
                continue
            r = momentum_score(s, kl, sec, hot, mc) if mom else swing_score(s, kl, sec)
            if r:
                scored.append(dict(s, score=r[0], reasons=r[1], metrics=r[2]))
    scored.sort(key=lambda x: -x["score"])
    # 前40只补充资金流 + 公告风险
    deep = scored[:40]
    _stage("前 %d 只查资金流和公告" % len(deep), 0, len(deep))
    deadline2 = time.time() + ENRICH_BUDGET_SEC

    def enrich(x):
        STATE["done"] += 1
        STATE["msg"] = "%s（%d/%d）" % (STATE["stage"], STATE["done"], STATE["total"])
        if x["board"] == "etf" or time.time() > deadline2:
            return x
        try:
            d = news.money_flow_daily(x["symbol"], 5)
            m5 = sum(r["main"] for r in d) / 1e8
            x["metrics"]["main_5d_yi"] = round(m5, 2)
            if m5 > 0:
                x["score"] += 1; x["reasons"].append("5日主力净流入%.2f亿" % m5)
            elif m5 < -abs(x["amount"]) / 1e8 * 0.3:
                x["score"] -= 1; x["reasons"].append("5日主力净流出%.2f亿" % m5)
        except Exception:
            pass
        try:
            neg = [a for a in news.announcements(x["symbol"], 8) if a["tag"] == "利空"
                   and a["time"] >= (market.now() - market.timedelta(days=7)).strftime("%Y-%m-%d")]
            if neg:
                x["score"] -= 1.5; x["reasons"].append("近7日利空公告：" + neg[0]["title"][:30])
        except Exception:
            pass
        return x
    with ThreadPoolExecutor(max_workers=6) as ex:
        deep = list(ex.map(enrich, deep))
    deep.sort(key=lambda x: -x["score"])
    if mom:      # 同一题材最多留 4 只，避免候选池全是一个板块（题材一熄火就全军覆没）
        per, kept = {}, []
        for x in deep:
            per[x["industry"]] = per.get(x["industry"], 0) + 1
            if per[x["industry"]] <= 4:
                kept.append(x)
        deep = kept
    top = deep[:top_n or cfg["screener"]["top_n"]]
    if pool and len(scored) < len(pool) * 0.5:
        log.warning("日K获取成功率低：%d/%d", len(scored), len(pool))
    _stage("保存结果")
    if skipped["n"]:
        log.warning("日K拉取超出 %ds 预算，跳过 %d 只", KLINE_BUDGET_SEC, skipped["n"])
    stats = {"style": "momentum" if mom else "swing", "universe": len(universe), "passed": len(passed), "klined": len(scored), "kline_requested": len(pool),
             "kline_skipped": skipped["n"],
             "dropped": dropped,
             "seconds": round(time.time() - t0, 1), "max_lot_value": round(max_lot_value)}
    sid = None
    if save:
        sid = db.run("INSERT INTO screens(ts,regime,stats,account) VALUES(?,?,?,?)",
                     (time.time(), json.dumps(regime, ensure_ascii=False), json.dumps(stats, ensure_ascii=False), account))
        with db.tx() as c:
            for i, x in enumerate(top):
                c.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (sid, i + 1, x["symbol"], x["name"], x["board"], x["industry"], round(x["score"], 2),
                           x["price"], x["pct"], json.dumps({"reasons": x["reasons"], "metrics": x["metrics"]},
                                                            ensure_ascii=False)))
    return {"screen_id": sid, "account": account, "regime": regime, "stats": stats,
            "candidates": [{"rank": i + 1, "symbol": x["symbol"], "name": x["name"], "board": x["board"],
                            "industry": x["industry"], "score": round(x["score"], 2), "price": x["price"],
                            "pct": x["pct"], "reasons": x["reasons"], "metrics": x["metrics"]}
                           for i, x in enumerate(top)]}


def _ensure_schema():
    db.init_db()


def latest_candidates(limit=20, account="paper"):
    _ensure_schema()
    s = db.one("SELECT * FROM screens WHERE COALESCE(account,'paper')=? ORDER BY id DESC LIMIT 1", (account,))
    if not s:
        return None
    rows = db.all("SELECT * FROM candidates WHERE screen_id=? ORDER BY rank LIMIT ?", (s["id"], limit))
    for r in rows:
        d = db.jloads(r.pop("details"), {})
        r.update(d)
    return {"screen_id": s["id"], "account": account, "ts_epoch": s["ts"], "ts": market.datetime.fromtimestamp(s["ts"], market.CN_TZ).strftime("%m-%d %H:%M"),
            "regime": db.jloads(s["regime"], {}), "stats": db.jloads(s["stats"], {}), "candidates": rows}
