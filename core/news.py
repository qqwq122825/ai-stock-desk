"""消息面 + 资金面（东方财富 / 新浪，免费无 key）。

A股的特点：散户多、资金博弈重，消息本身往往不是买点，**市场对消息的反应**才是。
所以这里把"新闻/公告"和"主力资金流向/龙虎榜"放在一起给 AI 看：
  利好 + 高开低走 + 放量 + 主力净流出 + 小单净流入  → 疑似借利好出货（筹码从主力转到散户）
  利空 + 不跌或快速收回 + 主力净流入                 → 疑似利空洗盘 / 利空出尽

数据：
  资金流（分钟/日）  push2.eastmoney.com  主力=超大单+大单；单位 元
  公告              np-anotice-stock.eastmoney.com
  个股新闻           search-api-web.eastmoney.com
  7x24 快讯         zhibo.sina.com.cn
  龙虎榜             datacenter-web.eastmoney.com
"""
import json
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import market

log = logging.getLogger("news")

POS_WORDS = ["中标", "增持", "回购", "预增", "扭亏", "大增", "签订", "签署", "合同", "订单", "获批", "批准", "重组",
             "收购", "分红", "派息", "涨价", "突破", "战略合作", "入选", "超预期", "创新高", "上调", "首发", "量产"]
NEG_WORDS = ["减持", "质押", "立案", "调查", "问询", "关注函", "警示", "处罚", "预亏", "亏损", "下滑", "下降", "解禁",
             "退市", "违规", "诉讼", "冻结", "终止", "风险提示", "异常波动", "被执行", "爆雷", "下调", "暂停", "失败"]

_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def _get(url, timeout=8):
    return market._http_get(url, timeout=timeout)


def secid(sym):
    s = market.normalize(sym)
    return ("1." if s.startswith("sh") else "0.") + s[2:]


def tag(text):
    """简单关键词情绪：利好 / 利空 / 中性（只做提示，最终让 AI 结合价格反应判断）。"""
    p = [w for w in POS_WORDS if w in text]
    n = [w for w in NEG_WORDS if w in text]
    if n and len(n) >= len(p):
        return "利空", n
    if p:
        return "利好", p
    return "中性", []


# ---------------- 资金流 ----------------
def money_flow_today(sym):
    """当日分钟级累计资金流。返回 {main, small, mid, large, xlarge (元), series:[[HH:MM, main]...]}"""
    def f():
        url = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?lmt=0&klt=1&secid=%s"
               "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57" % secid(sym))
        d = json.loads(_get(url)).get("data") or {}
        rows = [r.split(",") for r in d.get("klines") or []]
        if not rows:
            return None
        last = rows[-1]
        series = [[r[0][-5:], float(r[1])] for r in rows]
        res = {"time": last[0], "main": float(last[1]), "small": float(last[2]), "mid": float(last[3]),
               "large": float(last[4]), "xlarge": float(last[5]), "series": series[::5]}
        # 最近30分钟主力变化：判断尾盘/盘中是否在加速流出
        if len(series) > 30:
            res["main_last30m"] = series[-1][1] - series[-31][1]
        return res
    return _cached(("flow", sym), 50, f)


def money_flow_daily(sym, days=10):
    def f():
        url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=%d&klt=101&secid=%s"
               "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
               % (days, secid(sym)))
        d = json.loads(_get(url)).get("data") or {}
        out = []
        for r in d.get("klines") or []:
            x = r.split(",")
            out.append({"date": x[0], "main": float(x[1]), "small": float(x[2]), "mid": float(x[3]),
                        "large": float(x[4]), "xlarge": float(x[5]), "main_pct": float(x[6]),
                        "close": float(x[11]), "pct": float(x[12])})
        return out
    return _cached(("flowd", sym, days), 600, f)


# ---------------- 公告 / 新闻 / 快讯 / 龙虎榜 ----------------
def announcements(sym, n=10):
    def f():
        url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=%d&page_index=1"
               "&ann_type=A&client_source=web&stock_list=%s" % (n, market.code6(sym)))
        items = (json.loads(_get(url)).get("data") or {}).get("list") or []
        out = []
        for it in items:
            title = it.get("title_ch") or it.get("title", "")
            t, words = tag(title)
            out.append({"time": (it.get("display_time") or it.get("notice_date", ""))[:16], "title": title,
                        "type": ",".join(c.get("column_name", "") for c in it.get("columns") or []),
                        "tag": t, "keywords": words})
        return out
    return _cached(("ann", sym, n), 300, f)


def stock_news(keyword, n=10):
    def f():
        param = {"uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web",
                 "clientVersion": "curr", "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                                                         "pageIndex": 1, "pageSize": n,
                                                                         "preTag": "", "postTag": ""}}}
        url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param=" +
               urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
        text = _get(url)
        m = re.search(r"cb\((.*)\)\s*;?\s*$", text, re.S)
        data = json.loads(m.group(1) if m else text)
        out = []
        for it in (data.get("result") or {}).get("cmsArticleWebOld") or []:
            title = re.sub(r"<[^>]+>", "", it.get("title", ""))
            content = re.sub(r"<[^>]+>", "", it.get("content", ""))[:160]
            t, words = tag(title + content)
            out.append({"time": it.get("date", "")[:16], "title": title, "summary": content,
                        "media": it.get("mediaName"), "url": it.get("url"), "tag": t, "keywords": words})
        return out
    return _cached(("news", keyword, n), 300, f)


def flash_news(n=20):
    """新浪财经 7x24 快讯（宏观/政策/板块）。"""
    def f():
        url = "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=%d&zhibo_id=152" % n
        lst = json.loads(_get(url))["result"]["data"]["feed"]["list"]
        out = []
        for it in lst:
            txt = re.sub(r"<[^>]+>", "", it.get("rich_text", "")).strip()
            out.append({"time": it.get("create_time", "")[5:16], "text": txt[:200]})
        return out
    return _cached(("flash", n), 120, f)


def dragon_tiger(sym, n=5):
    def f():
        flt = '(SECURITY_CODE="%s")' % market.code6(sym)
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW"
               "&columns=ALL&pageSize=%d&sortColumns=TRADE_DATE&sortTypes=-1&filter=%s"
               % (n, urllib.parse.quote(flt)))
        res = json.loads(_get(url)).get("result") or {}
        out = []
        for r in res.get("data") or []:
            out.append({"date": (r.get("TRADE_DATE") or "")[:10], "reason": r.get("EXPLANATION"),
                        "pct": r.get("CHANGE_RATE"), "net_yi": round((r.get("BILLBOARD_NET_AMT") or 0) / 1e8, 3),
                        "buy_yi": round((r.get("BILLBOARD_BUY_AMT") or 0) / 1e8, 3),
                        "sell_yi": round((r.get("BILLBOARD_SELL_AMT") or 0) / 1e8, 3),
                        "next_day_pct": r.get("D1_CLOSE_ADJCHRATE")})
        return out
    return _cached(("lhb", sym, n), 1800, f)


# ---------------- 基本面 ----------------
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get?"


def _dc(params):
    return (json.loads(_get(DC + urllib.parse.urlencode(params))).get("result") or {}).get("data") or []


def fundamentals(sym):
    """财务摘要（近4期）+ 最新业绩预告 + 未来90天解禁 + 所属行业。各项失败互不影响。"""
    sym = market.normalize(sym)
    code = market.code6(sym)
    secu = "%s.%s" % (code, sym[:2].upper())

    def f():
        out = {}
        try:
            url = ("https://datacenter.eastmoney.com/securities/api/data/get?type=RPT_F10_FINANCE_MAINFINADATA"
                   "&sty=APP_F10_MAINFINADATA&filter=" + urllib.parse.quote('(SECUCODE="%s")' % secu) +
                   "&p=1&ps=4&sr=-1&st=REPORT_DATE&source=HSF10&client=PC")
            rows = (json.loads(_get(url)).get("result") or {}).get("data") or []
            out["financials"] = [{
                "period": r.get("REPORT_DATE_NAME"), "revenue_yi": _yi(r.get("TOTALOPERATEREVE")),
                "revenue_yoy": r.get("TOTALOPERATEREVETZ") and round(r["TOTALOPERATEREVETZ"], 1),
                "net_profit_yi": _yi(r.get("PARENTNETPROFIT")),
                "net_profit_yoy": r.get("PARENTNETPROFITTZ") and round(r["PARENTNETPROFITTZ"], 1),
                "roe": r.get("ROEJQ"), "gross_margin": r.get("XSMLL") and round(r["XSMLL"], 1),
                "debt_ratio": r.get("ZCFZL") and round(r["ZCFZL"], 1), "eps": r.get("EPSJB")} for r in rows]
        except Exception as e:
            out["financials_error"] = str(e)[:80]
        try:
            rows = _dc({"reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL", "pageSize": 2,
                        "filter": '(SECURITY_CODE="%s")' % code, "sortColumns": "NOTICE_DATE", "sortTypes": -1})
            out["forecast"] = [{"notice": (r.get("NOTICE_DATE") or "")[:10], "period": (r.get("REPORT_DATE") or "")[:10],
                                "type": r.get("PREDICT_TYPE"), "item": r.get("PREDICT_FINANCE"),
                                "change_pct": [r.get("ADD_AMP_LOWER"), r.get("ADD_AMP_UPPER")],
                                "content": (r.get("PREDICT_CONTENT") or "")[:120]} for r in rows]
        except Exception as e:
            out["forecast_error"] = str(e)[:80]
        try:
            today = market.today_str()
            end = (market.now() + market.timedelta(days=90)).strftime("%Y-%m-%d")
            rows = _dc({"reportName": "RPT_LIFT_STAGE", "columns": "ALL", "pageSize": 5,
                        "filter": "(SECURITY_CODE=\"%s\")(FREE_DATE>='%s')(FREE_DATE<='%s')" % (code, today, end),
                        "sortColumns": "FREE_DATE", "sortTypes": 1})
            out["unlock_90d"] = [{"date": (r.get("FREE_DATE") or "")[:10], "type": r.get("FREE_SHARES_TYPE"),
                                  "market_cap_yi": round((r.get("LIFT_MARKET_CAP") or 0) / 1e4, 2)} for r in rows]
        except Exception as e:
            out["unlock_error"] = str(e)[:80]
        try:
            url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=f57,f58,f127,f116,f162,f167,f189"
                   % secid(sym))
            d = json.loads(_get(url)).get("data") or {}
            out["industry"] = d.get("f127")
            out["list_date"] = d.get("f189")
        except Exception:
            pass
        return out
    return _cached(("fund", sym), 3600, f)


def industry(sym, name=""):
    """所属行业（东财行业名）。ETF 直接用名称（如"半导体ETF"含行业关键词）。缓存1天。"""
    sym = market.normalize(sym)
    if market.board(sym) == "etf":
        return name or sym

    def f():
        url = "https://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=f57,f58,f127" % secid(sym)
        return (json.loads(_get(url)).get("data") or {}).get("f127") or ""
    try:
        return _cached(("ind", sym), 86400, f)
    except Exception:
        return ""


# ---------------- 汇总 ----------------
def _yi(x):
    return None if x is None else round(x / 1e8, 3)


def intel(sym, name=None, quote=None, news_n=8):
    """单只股票的消息面+资金面摘要（给 AI 和出货识别策略用）。各数据源失败互不影响。"""
    sym = market.normalize(sym)
    out = {}
    try:
        f = money_flow_today(sym)
        if f:
            amt = (quote or {}).get("amount") or 0
            out["flow_today"] = {
                "main_yi": _yi(f["main"]), "xlarge_yi": _yi(f["xlarge"]), "large_yi": _yi(f["large"]),
                "mid_yi": _yi(f["mid"]), "small_yi": _yi(f["small"]),
                "main_pct_of_amount": round(f["main"] / amt * 100, 2) if amt else None,
                "main_last30m_yi": _yi(f.get("main_last30m")), "as_of": f["time"]}
    except Exception as e:
        out["flow_today_error"] = str(e)[:80]
    try:
        d = money_flow_daily(sym, 10)
        if d:
            out["flow_days"] = [[x["date"][5:], _yi(x["main"]), x["main_pct"], x["pct"]] for x in d[-5:]]
            out["main_5d_yi"] = _yi(sum(x["main"] for x in d[-5:]))
            out["main_10d_yi"] = _yi(sum(x["main"] for x in d))
    except Exception as e:
        out["flow_days_error"] = str(e)[:80]
    since = time.time() - 3 * 86400
    items = []
    try:
        for a in announcements(sym, 8):
            items.append(dict(a, src="公告"))
    except Exception as e:
        out["ann_error"] = str(e)[:80]
    try:
        for n in stock_news(name or market.code6(sym), news_n):
            # 搜索结果可能只是顺带提到，标题或摘要里要有名字/代码才算
            if name and name[:2] not in (n["title"] + n["summary"]) and market.code6(sym) not in n["title"]:
                continue
            items.append(dict(n, src="新闻"))
    except Exception as e:
        out["news_error"] = str(e)[:80]

    def ts(x):   # 新闻时间是北京时间，不依赖电脑时区
        try:
            return market.datetime.strptime(x["time"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=market.CN_TZ).timestamp()
        except ValueError:
            return 0
    items = sorted([i for i in items if ts(i) >= since], key=ts, reverse=True)[:8]
    out["news_3d"] = [{"time": i["time"][5:], "src": i["src"], "tag": i["tag"], "title": i["title"][:80]}
                      for i in items]
    today = market.today_str()
    td = [i for i in items if i["time"].startswith(today)]
    out["news_today"] = {"count": len(td), "pos": sum(1 for i in td if i["tag"] == "利好"),
                         "neg": sum(1 for i in td if i["tag"] == "利空")}
    try:
        cutoff = (market.now() - market.timedelta(days=30)).strftime("%Y-%m-%d")
        lhb = [x for x in dragon_tiger(sym, 3) if x["date"] >= cutoff]
        if lhb:
            out["dragon_tiger_30d"] = lhb
    except Exception:
        pass
    return out


def intel_many(syms, names=None, quotes=None, workers=6):
    names, quotes = names or {}, quotes or {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {s: ex.submit(intel, s, names.get(s), quotes.get(s)) for s in syms}
        return {s: f.result() for s, f in futs.items()}


def distribution_check(q, kl_summary, it):
    """A股"借利好出货 / 利空洗盘"识别。返回 (side, score, reasons) 或 None。
    q: 实时报价；kl_summary: indicators.summarize；it: intel()。
    """
    if not q or not it:
        return None
    reasons, score = [], 0.0
    prev = q.get("prev_close") or q["price"]
    pct = q.get("pct", 0)
    vwap = q["amount"] / q["volume"] if q.get("volume") else None
    nt = it.get("news_today") or {}
    ft = it.get("flow_today") or {}
    main = ft.get("main_yi")
    small = ft.get("small_yi")
    upper = (q["high"] - q["price"]) / prev * 100 if prev else 0
    gap = (q["open"] / prev - 1) * 100 if prev and q.get("open") else 0
    # ---- 出货特征 ----
    if nt.get("pos"):
        score += 1; reasons.append("今日有%d条利好消息" % nt["pos"])
    if gap >= 3:
        score += 0.5; reasons.append("高开%.1f%%" % gap)
    if upper >= 3:
        score += 1; reasons.append("冲高回落%.1f%%（长上影）" % upper)
    if vwap and q["price"] < vwap and q["high"] > vwap * 1.02:
        score += 0.5; reasons.append("跌回分时均价下方")
    if q.get("vol_ratio", 0) >= 2:
        score += 0.5; reasons.append("放量 量比%.1f" % q["vol_ratio"])
    if main is not None and main < 0 and pct > 0:
        score += 1; reasons.append("价涨但主力净流出%.2f亿" % main)
    if main is not None and small is not None and main < 0 and small > 0:
        score += 0.5; reasons.append("小单净流入%.2f亿（散户接盘）" % small)
    if (ft.get("main_last30m_yi") or 0) < 0 and pct > 0:
        score += 0.5; reasons.append("近30分钟主力加速流出")
    hi20 = kl_summary.get("high20") if kl_summary else None
    if (kl_summary or {}).get("chg20d_pct") and kl_summary["chg20d_pct"] > 25:
        score += 1; reasons.append("20日已涨%.0f%%，位置高" % kl_summary["chg20d_pct"])
    elif hi20 and q["high"] >= hi20:
        score += 0.5; reasons.append("触及20日新高")
    if score >= 3.5 and (nt.get("pos") or gap >= 3 or (main is not None and main < 0)):
        return "sell", score, reasons
    # ---- 洗盘/利空出尽特征 ----
    r2, s2 = [], 0.0
    if nt.get("neg"):
        s2 += 1; r2.append("今日有%d条利空消息" % nt["neg"])
    if gap <= -2 and q["price"] > q["open"]:
        s2 += 1; r2.append("低开%.1f%%后回升" % gap)
    if pct > -1 and nt.get("neg"):
        s2 += 1; r2.append("利空下股价基本不跌（%.2f%%）" % pct)
    if main is not None and main > 0:
        s2 += 1; r2.append("主力净流入%.2f亿" % main)
    if vwap and q["price"] > vwap:
        s2 += 0.5; r2.append("站上分时均价")
    if s2 >= 3 and nt.get("neg"):
        return "watch", s2, r2
    return None
