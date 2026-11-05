"""A 股情绪周期 + 热门题材（给「情绪短线」打法用）。数据：东方财富涨停/炸板/跌停/昨日涨停池（免费）。

情绪短线的核心：
  - 钱跟着情绪走。先看**市场情绪处在哪个阶段**（冰点 → 修复 → 升温 → 高潮 → 退潮），再看**哪个题材最热**，
    只做最热题材里的强势股；情绪退潮时空仓。
  - 任何主力出货都要配合好消息。消息是真是假我们判断不了，**只要卖得比别人快**：
    高开低走就走、跌破分时均价就走、题材降温就走、利好兑现冲高就卖、拿不到预期就走。

指标：
  涨停数 / 跌停数 / 炸板数、炸板率 = 炸板 /(涨停 + 炸板)、最高连板高度、
  赚钱效应 = 昨天涨停的股今天平均涨幅（> 3% 很好，< 0 说明追涨的人在亏钱）
"""
import json
import logging
import threading
import time
from collections import defaultdict

from . import db, market

log = logging.getLogger("emotion")
_cache = {}
_lock = threading.Lock()
UT = "7eea3edcaed734bea9cbfc24409ed989"
POOLS = {"zt": ("getTopicZTPool", "fbt:asc"), "zb": ("getTopicZBPool", "fbt:asc"),
         "dt": ("getTopicDTPool", "fund:asc"), "yzt": ("getYesterdayZTPool", "zs:desc")}

# 情绪阶段 → 新开仓总仓位上限（情绪短线模式用）
STAGE_CAPS = {"冰点": 0.0, "退潮": 0.2, "修复": 0.5, "升温": 0.8, "高潮": 0.5}
STAGE_TIPS = {"冰点": "亏钱效应最强的时候，空仓等修复，不抄底",
              "退潮": "高位股补跌、炸板多，只卖不买",
              "修复": "情绪刚回暖，只做最强题材的龙头，小仓位",
              "升温": "赚钱效应好，主线题材积极参与",
              "高潮": "涨停一片但次日容易分歧，持仓逢高兑现，不追新高"}


def _sym(r):
    return ("sh" if r.get("m") == 1 else ("bj" if str(r.get("c", "")).startswith(("8", "4", "92")) else "sz")) + r["c"]


def pool(kind, date=None):
    """kind: zt 涨停 / zb 炸板 / dt 跌停 / yzt 昨日涨停（今天表现）。返回 list。"""
    date = date or market.now().strftime("%Y%m%d")
    key = (kind, date)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
    fn, sort = POOLS[kind]
    url = ("https://push2ex.eastmoney.com/%s?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=%s&date=%s"
           % (fn, UT, sort.replace(":", "%3A"), date))
    d = json.loads(market._http_get(url, timeout=8)).get("data") or {}
    rows = []
    for r in d.get("pool") or []:
        rows.append({"symbol": _sym(r), "name": (r.get("n") or "").replace(" ", ""), "price": (r.get("p") or 0) / 1000,
                     "pct": r.get("zdp") or 0, "industry": r.get("hybk") or "", "lbc": r.get("lbc") or r.get("ylbc") or 0,
                     "first_seal": r.get("fbt") or r.get("yfbt") or 0, "open_times": r.get("zbc") or 0,
                     "amount": r.get("amount") or 0, "float_mv": r.get("ltsz") or 0, "turnover": r.get("hs") or 0,
                     "seal_fund": r.get("fund") or 0})
    with _lock:
        _cache[key] = (time.time(), rows)
    return rows


def _last_trade_date():
    """今天是交易日且已开盘就用今天，否则往前找最近一个有涨停池的日期。"""
    d = market.now()
    for i in range(8):
        day = (d - market.timedelta(days=i))
        if day.weekday() >= 5:
            continue
        if i == 0 and d.hour * 100 + d.minute < 926:
            continue
        ds = day.strftime("%Y%m%d")
        try:
            if pool("zt", ds):
                return ds
        except Exception:
            continue
    return d.strftime("%Y%m%d")


def _prev_trade_date(date):
    """date（YYYYMMDD）之前最近一个有涨停池数据的交易日。"""
    d = market.datetime.strptime(date, "%Y%m%d")
    for i in range(1, 10):
        day = d - market.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        ds = day.strftime("%Y%m%d")
        try:
            if pool("zt", ds):
                return ds
        except Exception:
            continue
    return None


def _closing(date):
    """某个交易日收盘后的完整情绪（优先用已保存的；没有就现算并保存）。"""
    try:
        row = db.one("SELECT data FROM emotion_days WHERE date=?", (date,))
        d = db.jloads(row["data"], {}) if row else {}
        if d.get("final") and d.get("stage"):
            return dict(d, date=date)
    except Exception:
        pass
    return sentiment(date)


def sentiment(date=None):
    """市场情绪：阶段 + 关键数字 + 理由。失败返回 {"error": ...}。

    盘中（交易日 9:26~15:00）当天的涨停池还没走完，早上 9:35 只有十几家涨停，直接算会误判成「冰点」。
    所以盘中**以上一个交易日收盘的情绪为准**，当天的数字只用来**往下调**（昨天的涨停今天大面积被砸、跌停很多、
    炸板率很高），不会因为盘中热闹就往上调。收盘后（15:00 起）才用当天完整数据定今天的阶段，并保存下来给明天用。"""
    now = market.now()
    hm = now.hour * 100 + now.minute
    today = now.strftime("%Y%m%d")
    if date is None and market.is_trading_day(now) and 926 <= hm < 1500:
        return _intraday(today, hm)
    try:
        date = date or _last_trade_date()
        zt, zb, dt, yzt = (pool(k, date) for k in ("zt", "zb", "dt", "yzt"))
    except Exception as e:
        return {"error": "情绪数据获取失败：%s" % e}
    val = _compute(date, zt, zb, dt, yzt)
    final = date < today or hm >= 1500
    val["final"] = final
    val["basis"] = "%s 收盘数据" % date if final else "%s 盘中数据（未收盘）" % date
    if final:
        _save_day(date, val)
    return val


def _compute(date, zt, zb, dt, yzt):
    n_zt, n_zb, n_dt = len(zt), len(zb), len(dt)
    zb_rate = n_zb / (n_zt + n_zb) * 100 if n_zt + n_zb else 0
    max_lb = max([r["lbc"] for r in zt] or [0])
    yzt_avg = sum(r["pct"] for r in yzt) / len(yzt) if yzt else None
    try:
        prev = db.one("SELECT data FROM emotion_days WHERE date<? ORDER BY date DESC LIMIT 1", (date,))
    except Exception:
        prev = None
    prev = db.jloads(prev["data"], {}) if prev else {}
    why = ["涨停%d 跌停%d 炸板%d（炸板率%.0f%%）" % (n_zt, n_dt, n_zb, zb_rate), "最高%d连板" % max_lb]
    if yzt_avg is not None:
        why.append("昨日涨停今天平均%+.1f%%" % yzt_avg)
    # 阶段判断（经验规则，宁可保守）
    if n_zt < 30 or n_dt >= 20 or (yzt_avg is not None and yzt_avg < -2):
        stage = "冰点"
    elif zb_rate >= 40 or (yzt_avg is not None and yzt_avg < 0) or \
            (prev.get("max_lb") and max_lb < prev["max_lb"] - 1 and n_zt < prev.get("zt", 0)):
        stage = "退潮"
    elif n_zt >= 100 and zb_rate < 25 and max_lb >= 6:
        stage = "高潮"
    elif yzt_avg is not None and yzt_avg >= 3 and zb_rate < 35:
        stage = "升温"
    else:
        stage = "修复"
    if prev.get("zt"):
        why.append("涨停数较上个交易日 %+d" % (n_zt - prev["zt"]))
    return {"date": date, "stage": stage, "position_cap": STAGE_CAPS[stage], "tip": STAGE_TIPS[stage],
            "zt": n_zt, "dt": n_dt, "zb": n_zb, "zb_rate": round(zb_rate, 1), "max_lb": max_lb,
            "yzt_avg_pct": round(yzt_avg, 2) if yzt_avg is not None else None, "reasons": why,
            "themes": hot_themes(zt)[:8]}


def _intraday(today, hm):
    prev_date = _prev_trade_date(today)
    base = _closing(prev_date) if prev_date else {"error": "找不到上一个交易日"}
    if base.get("error"):
        return base
    try:
        zt, zb, dt, yzt = (pool(k, today) for k in ("zt", "zb", "dt", "yzt"))
    except Exception:
        zt = zb = dt = yzt = None
    stage, why = base["stage"], ["以上个交易日（%s）收盘情绪「%s」为准" % (prev_date, base["stage"])]
    live = None
    if zt is not None:
        n_zt, n_zb, n_dt = len(zt), len(zb), len(dt)
        zb_rate = n_zb / (n_zt + n_zb) * 100 if n_zt + n_zb else 0
        yzt_avg = sum(r["pct"] for r in yzt) / len(yzt) if yzt else None
        live = {"zt": n_zt, "dt": n_dt, "zb": n_zb, "zb_rate": round(zb_rate, 1),
                "max_lb": max([r["lbc"] for r in zt] or [0]),
                "yzt_avg_pct": round(yzt_avg, 2) if yzt_avg is not None else None}
        why.append("盘中：涨停%d 跌停%d 炸板率%.0f%%%s" % (n_zt, n_dt, zb_rate,
                                                 "，昨日涨停今天平均%+.1f%%" % yzt_avg if yzt_avg is not None else ""))
        # 只往下调：昨天的涨停今天被砸（亏钱效应）、跌停多 → 降级；10 点前数据太少，只看跌停
        down = None
        if n_dt >= 20 or (hm >= 1000 and yzt_avg is not None and yzt_avg < -2):
            down = "冰点"
        elif hm >= 1000 and ((yzt_avg is not None and yzt_avg < 0) or (hm >= 1030 and n_zt + n_zb >= 20 and zb_rate >= 50)):
            down = "退潮"
        if down and STAGE_CAPS[down] < STAGE_CAPS[stage]:
            why.append("盘中亏钱效应明显，下调为「%s」" % down)
            stage = down
    themes = base.get("themes") or []
    if zt is not None and hm >= 1030:
        themes = hot_themes(zt)[:8] or themes      # 10:30 以后当天的题材才有参考价值
    return dict(live or {k: base.get(k) for k in ("zt", "dt", "zb", "zb_rate", "max_lb", "yzt_avg_pct")},
                date=today, stage=stage, position_cap=STAGE_CAPS[stage], tip=STAGE_TIPS[stage], reasons=why,
                themes=themes, final=False, intraday=True, prev_stage=base["stage"], prev_date=prev_date,
                basis="盘中：以 %s 收盘情绪为准，当天数据只往下调" % prev_date)


def hot_themes(zt=None, top=10):
    """按涨停数排的热门题材（行业），附龙头（连板最高、封板最早）。"""
    if zt is None:
        try:
            zt = pool("zt", _last_trade_date())
        except Exception:
            return []
    by = defaultdict(list)
    for r in zt:
        if r["industry"]:
            by[r["industry"]].append(r)
    out = []
    for ind, rows in by.items():
        rows.sort(key=lambda r: (-r["lbc"], r["first_seal"]))
        out.append({"name": ind, "zt": len(rows), "max_lb": rows[0]["lbc"],
                    "leaders": [{"symbol": r["symbol"], "name": r["name"], "lbc": r["lbc"]} for r in rows[:3]],
                    "heat": len(rows) + rows[0]["lbc"] * 0.5})
    out.sort(key=lambda x: -x["heat"])
    for i, t in enumerate(out):
        t["rank"] = i + 1
    return out[:top]


def theme_of(sym, industry=None):
    """某只股票所在题材今天的热度（rank=None 表示不在热门题材里）。"""
    sym = market.normalize(sym)
    themes = hot_themes(top=30)
    ind = industry
    if not ind:
        try:
            from . import news
            ind = news.industry(sym)
        except Exception:
            ind = ""
    t = next((x for x in themes if x["name"] == ind), None)
    lead = None
    try:
        lead = next((r for r in pool("zt", _last_trade_date()) if r["symbol"] == sym), None)
    except Exception:
        pass
    return {"industry": ind, "rank": t["rank"] if t else None, "theme_zt": t["zt"] if t else 0,
            "is_limit_up": bool(lead), "lbc": lead["lbc"] if lead else 0,
            "leaders": t["leaders"] if t else []}


# ---------------- 每日记录（判断"退潮"要和前一天比；表在 db.SCHEMA 里） ----------------
def _save_day(date, val):
    try:
        keep = {k: val[k] for k in ("stage", "zt", "dt", "zb", "zb_rate", "max_lb", "yzt_avg_pct")}
        keep.update(final=True, themes=[{x: t[x] for x in ("name", "zt", "max_lb", "leaders", "rank", "heat")}
                                        for t in val.get("themes") or []])
        db.run("INSERT OR REPLACE INTO emotion_days VALUES(?,?,?)", (date, json.dumps(keep, ensure_ascii=False), time.time()))
    except Exception as e:
        log.debug("保存情绪失败: %s", e)

