"""行情数据：腾讯行情接口（免费、无需 key）。

实时报价  http://qt.gtimg.cn/q=sh600519,sz000001
日/周/月K https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,120,qfq
分钟K    https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m5,,60
"""
import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone  # noqa: F401  (timedelta 供其他模块使用)

log = logging.getLogger("market")
CN_TZ = timezone(timedelta(hours=8))

INDEXES = [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
           ("sh000300", "沪深300"), ("sh000688", "科创50")]


def now():
    return datetime.now(CN_TZ)


def today_str():
    return now().strftime("%Y-%m-%d")


def day_start_ts(dt=None):
    dt = dt or now()
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def normalize(sym):
    """'600519' -> 'sh600519'；已带前缀的原样返回。"""
    s = str(sym).strip().lower().replace(".", "")
    m = re.match(r"^(sh|sz|bj)?(\d{6})(sh|sz|bj)?$", s)
    if not m:
        raise ValueError("无效股票代码: %s" % sym)
    pre = m.group(1) or m.group(3)
    code = m.group(2)
    if pre:
        return pre + code
    if code.startswith(("92", "4", "8")):
        return "bj" + code
    if code[0] in "569":
        return "sh" + code
    return "sz" + code


def code6(sym):
    return normalize(sym)[2:]


def is_fund(sym):
    c = code6(sym)
    return c.startswith(("51", "52", "56", "58", "15", "16", "18"))


def board(sym):
    """main=沪深主板 gem=创业板 star=科创板 bj=北交所 etf=场内基金"""
    s = normalize(sym)
    c = s[2:]
    if is_fund(s) or (s.startswith("sh") and c.startswith("50")):
        return "etf"
    if s.startswith("bj"):
        return "bj"
    if c.startswith("688") or c.startswith("689"):
        return "star"
    if c.startswith("30"):
        return "gem"
    return "main"


BOARD_NAMES = {"main": "主板", "gem": "创业板", "star": "科创板", "bj": "北交所", "etf": "ETF"}


def limit_pct(sym, name=""):
    """涨跌停幅度（%）。"""
    b = board(sym)
    if "ST" in (name or "").upper():
        return 5.0
    return {"gem": 20.0, "star": 20.0, "bj": 30.0, "etf": 10.0}.get(b, 10.0)


def lot_rule(sym):
    """返回 (最小买入股数, 递增步长)。"""
    s = normalize(sym)
    if s[2:].startswith("688"):
        return 200, 1
    if s.startswith("bj"):
        return 100, 1
    return 100, 100


# 东方财富 push2 行情主机偶尔对某些机房 IP 持续返回 502（服务器 11-11 晚起全市场快照、实时资金流都拿不到，
# 选股连续两天失败）。同一套接口有多个主机名，主的不通就换下一个；都不通且设置里开了「行情代理」就走代理。
# 哪条路通了就先记住用它 10 分钟。
EM_HOSTS = ("push2.eastmoney.com", "82.push2.eastmoney.com", "push2delay.eastmoney.com")
_em_ok = {"host": None, "until": 0}
_HDR = {"User-Agent": "Mozilla/5.0 (Macintosh) ai-stock/1.0", "Referer": "https://gu.qq.com/"}


def _open(url, timeout, encoding):
    req = urllib.request.Request(url, headers=_HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(encoding, errors="replace")


def _via_proxy(url, timeout, encoding):
    from . import proxy
    return proxy.get(url, _HDR, timeout, encoding)


def _proxy_mode():
    """'' 不用代理 / 'fallback' 直连不通才用 / 'always' 东方财富行情一律走代理。"""
    try:
        from . import proxy
        if not proxy.enabled():
            return ""
        return "always" if proxy.conf().get("mode") == "always" else "fallback"
    except Exception:
        return ""


def _http_get(url, timeout=8, encoding="utf-8"):
    pre = "https://" + EM_HOSTS[0] + "/"
    if not url.startswith(pre):
        if ".eastmoney.com/" in url and _proxy_mode() == "always":
            try:
                return _via_proxy(url, timeout, encoding)
            except Exception:
                pass                                # 代理不行就直连，别因为代理把本来能用的接口弄坏
        return _open(url, timeout, encoding)
    pm = _proxy_mode()
    routes = list(EM_HOSTS) + (["proxy"] if pm else [])
    if pm == "always":
        routes = ["proxy"] + list(EM_HOSTS)
    elif _em_ok["host"] in routes and _em_ok["until"] > time.time():
        routes.remove(_em_ok["host"])
        routes.insert(0, _em_ok["host"])
    err = None
    for h in routes:
        try:
            if h == "proxy":
                text = _via_proxy(url, timeout, encoding)
            else:
                text = _open("https://" + h + "/" + url[len(pre):], timeout, encoding)
        except Exception as e:                      # 502/超时/连接被拒：换下一条路
            err = err or e
            continue
        if h != _em_ok["host"]:
            if err is not None:
                log.warning("东方财富行情：%s 不通（%s），改用 %s", EM_HOSTS[0], err, "代理" if h == "proxy" else h)
            _em_ok["host"] = h
        _em_ok["until"] = time.time() + 600
        return text
    raise err


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_quote(key, f):
    price, prev = _f(f[3]), _f(f[4])
    suspended = price <= 0
    q = {
        "symbol": key, "code": f[2], "name": f[1],
        "price": price if not suspended else prev, "prev_close": prev, "open": _f(f[5]),
        "volume": int(_f(f[6]) * 100), "time": f[30] if len(f) > 30 else "",
        "change": _f(f[31]), "pct": _f(f[32]), "high": _f(f[33]), "low": _f(f[34]),
        "amount": _f(f[37]) * 1e4, "turnover": _f(f[38]), "pe": _f(f[39]),
        "circ_mv": _f(f[44]) if len(f) > 44 else 0, "total_mv": _f(f[45]) if len(f) > 45 else 0,
        "pb": _f(f[46]) if len(f) > 46 else 0,
        "limit_up": _f(f[47]) if len(f) > 47 else 0, "limit_down": _f(f[48]) if len(f) > 48 else 0,
        "vol_ratio": _f(f[49]) if len(f) > 49 else 0,
        "bids": [[_f(f[9 + i * 2]), int(_f(f[10 + i * 2]))] for i in range(5)],
        "asks": [[_f(f[19 + i * 2]), int(_f(f[20 + i * 2]))] for i in range(5)],
        "suspended": suspended, "fetched_at": time.time(),
    }
    q["bid1"], q["ask1"] = q["bids"][0][0], q["asks"][0][0]
    q["date"] = "%s-%s-%s" % (q["time"][:4], q["time"][4:6], q["time"][6:8]) if len(q["time"]) >= 8 else ""
    return q


def get_quotes(symbols):
    """批量实时报价，返回 {symbol: quote}。"""
    syms = []
    for s in symbols:
        try:
            n = normalize(s)
        except ValueError:
            continue
        if n not in syms:
            syms.append(n)
    out = {}
    for i in range(0, len(syms), 60):
        chunk = syms[i:i + 60]
        text = _http_get("http://qt.gtimg.cn/q=" + ",".join(chunk), encoding="gbk")
        for line in text.split(";"):
            m = re.search(r'v_(\w+)="(.*)"', line, re.S)
            if not m or "~" not in m.group(2):
                continue
            f = m.group(2).split("~")
            if len(f) < 35:
                continue
            out[m.group(1)] = parse_quote(m.group(1), f)
    return out


def get_quote(symbol):
    s = normalize(symbol)
    q = get_quotes([s]).get(s)
    if not q:
        raise ValueError("取不到行情: %s" % symbol)
    return q


_down = {}          # 数据源熔断：source -> 恢复时间戳
EM_KLT = {"day": 101, "week": 102, "month": 103, "m1": 1, "m5": 5, "m15": 15, "m30": 30, "m60": 60}


def get_kline(symbol, period="day", count=120):
    """period: day/week/month/m1/m5/m15/m30/m60。返回 [{t,o,c,h,l,v}] 时间升序（前复权）。
    数据源：腾讯优先，失败自动切换东方财富；失败的源熔断10分钟。"""
    errs = []
    for name, fn in (("tencent", _kline_tencent), ("eastmoney", _kline_eastmoney)):
        if _down.get(name, 0) > time.time():
            continue
        try:
            rows = fn(symbol, period, count)
            if rows:
                return rows
            errs.append("%s:空" % name)
        except Exception as e:
            errs.append("%s:%s" % (name, e))
            if "HTTP Error 5" in str(e) or "timed out" in str(e):
                _down[name] = time.time() + 600
                log.warning("K线源 %s 不可用，熔断10分钟：%s", name, e)
    if all(_down.get(n, 0) > time.time() for n in ("tencent", "eastmoney")):
        _down.clear()   # 全挂了就别一直熔断，下次重试
    raise ValueError("K线获取失败 %s：%s" % (symbol, "；".join(errs)))


def _kline_eastmoney(symbol, period="day", count=120):
    s = normalize(symbol)
    secid = ("1." if s.startswith("sh") else "0.") + s[2:]
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&klt=%d&fqt=1&lmt=%d&end=20500101"
           "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56" % (secid, EM_KLT[period], count))
    data = json.loads(_http_get(url)).get("data") or {}
    out = []
    for r in data.get("klines") or []:
        x = r.split(",")
        out.append({"t": x[0], "o": _f(x[1]), "c": _f(x[2]), "h": _f(x[3]), "l": _f(x[4]), "v": _f(x[5])})
    return out


def _kline_tencent(symbol, period="day", count=120):
    s = normalize(symbol)
    if period in ("day", "week", "month"):
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,%s,,,%d,qfq"
               % (s, period, count))
        data = json.loads(_http_get(url))["data"][s]
        rows = data.get("qfq" + period) or data.get(period) or []
    else:
        url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=%s,%s,,%d" % (s, period, count)
        data = json.loads(_http_get(url))["data"][s]
        rows = data.get(period) or []
    out = []
    for r in rows:
        t = r[0]
        if len(t) == 12:  # 202611041500
            t = "%s-%s-%s %s:%s" % (t[:4], t[4:6], t[6:8], t[8:10], t[10:12])
        out.append({"t": t, "o": _f(r[1]), "c": _f(r[2]), "h": _f(r[3]), "l": _f(r[4]), "v": _f(r[5])})
    return out


# ---------------- 交易时间 ----------------
def is_trading_session(dt=None):
    dt = dt or now()
    if dt.weekday() >= 5:
        return False
    t = dt.hour * 100 + dt.minute
    return (930 <= t < 1130) or (1300 <= t < 1500)


_day_cache = {}


def is_trading_day(dt=None):
    """工作日且非节假日。节假日通过上证指数行情日期判断（9:26 之后才判断得出）。"""
    dt = dt or now()
    if dt.weekday() >= 5:
        return False
    d = dt.strftime("%Y%m%d")
    if dt.hour * 100 + dt.minute < 926:
        return True
    if d in _day_cache:
        return _day_cache[d]
    try:
        q = get_quotes(["sh000001"]).get("sh000001")
        ok = bool(q and q["time"][:8] == d)
    except Exception:
        return True
    _day_cache[d] = ok
    return ok


def is_market_open(dt=None):
    return is_trading_session(dt) and is_trading_day(dt)


def session_label(dt=None):
    dt = dt or now()
    if not is_trading_day(dt):
        return "休市（非交易日）"
    t = dt.hour * 100 + dt.minute
    if t < 915:
        return "盘前"
    if t < 930:
        return "集合竞价"
    if t < 1130:
        return "上午交易中"
    if t < 1300:
        return "午间休市"
    if t < 1500:
        return "下午交易中"
    return "已收盘"


# ---------------- 带缓存的报价（DB 共享）----------------
def quotes_cached(symbols, max_age=20):
    """优先用 quotes 表缓存（监控进程会持续刷新），过期则实时拉取并回写。"""
    from . import db
    syms = []
    for s in symbols:
        try:
            syms.append(normalize(s))
        except ValueError:
            pass
    syms = list(dict.fromkeys(syms))
    if not syms:
        return {}
    out, stale = {}, []
    rows = db.all("SELECT symbol,data,updated_at FROM quotes WHERE symbol IN (%s)"
                  % ",".join("?" * len(syms)), syms)
    cache = {r["symbol"]: r for r in rows}
    for s in syms:
        r = cache.get(s)
        if r and time.time() - r["updated_at"] <= max_age:
            out[s] = json.loads(r["data"])
        else:
            stale.append(s)
    if stale:
        try:
            fresh = get_quotes(stale)
            save_quotes(fresh)
            out.update(fresh)
        except Exception as e:
            log.warning("拉取行情失败: %s", e)
            for s in stale:
                if s in cache:
                    out[s] = json.loads(cache[s]["data"])
    return out


def save_quotes(quotes):
    from . import db
    if not quotes:
        return
    with db.tx() as c:
        for s, q in quotes.items():
            c.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?)",
                      (s, q["name"], json.dumps(q, ensure_ascii=False), time.time()))
