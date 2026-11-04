"""外盘联动：隔夜美股 / 港股 → A 股相关板块。

时间关系（北京时间）：美股 21:30~次日 4:00/5:00 交易 → 影响的是 A 股**当天开盘**，午后影响通常减弱。
所以外盘信息用于：盘前简报、AI 上下文、委员会情绪分析师、大盘状态的轻微修正。
收盘后选股不使用（昨晚的外盘已经反映在今天 A 股价格里，再算一次就是重复计算）。

数据：腾讯行情（免费），美股代码 usXXX，港股 hkXXX。
"""
import logging
import re
import threading
import time

from . import market

log = logging.getLogger("global")

DRIVERS = [
    ("usDJI", "道琼斯"), ("usIXIC", "纳斯达克"), ("usINX", "标普500"),
    ("usSOXX", "费城半导体ETF"), ("usNVDA", "英伟达"), ("usAAPL", "苹果"), ("usTSLA", "特斯拉"),
    ("usHXC", "纳斯达克中国金龙"), ("hkHSI", "恒生指数"), ("hkHSTECH", "恒生科技"),
]
NAMES = dict(DRIVERS)

# A股行业关键词 → 外盘驱动（按相关性排序）
LINKS = [
    (("半导体", "芯片", "集成电路", "电子化学品", "分立器件"), ("usSOXX", "usNVDA"), "半导体链"),
    (("通信设备", "光模块", "光通信", "计算机设备", "服务器", "算力", "元件", "印制电路"), ("usNVDA", "usIXIC"), "AI算力/硬件"),
    (("消费电子", "光学光电子", "电子"), ("usAAPL", "usIXIC"), "消费电子/苹果链"),
    (("汽车", "乘用车", "电池", "能源金属", "锂", "电机"), ("usTSLA",), "新能源车/特斯拉链"),
    (("软件", "IT服务", "互联网", "游戏", "传媒", "数字媒体", "计算机"), ("usIXIC", "usHXC", "hkHSTECH"), "软件/互联网"),
]

_cache = {"ts": 0, "data": None}
_lock = threading.Lock()


def overnight(ttl=600):
    """外盘最新（或上一交易日收盘）行情。失败返回 None。"""
    with _lock:
        if _cache["data"] and time.time() - _cache["ts"] < ttl:
            return _cache["data"]
    try:
        text = market._http_get("http://qt.gtimg.cn/q=" + ",".join(c for c, _ in DRIVERS), encoding="gbk")
    except Exception as e:
        log.warning("外盘行情获取失败: %s", e)
        return _cache["data"]
    out = {}
    for line in text.split(";"):
        m = re.search(r'v_(\w+)="(.*)"', line, re.S)
        if not m:
            continue
        f = m.group(2).split("~")
        if len(f) < 33:
            continue
        try:
            out[m.group(1)] = {"name": NAMES.get(m.group(1), f[1]), "price": float(f[3]), "prev": float(f[4]),
                               "pct": float(f[32]), "time": f[30]}
        except ValueError:
            continue
    data = {"quotes": out, "summary": _summary(out), "fetched": market.now().strftime("%m-%d %H:%M")}
    with _lock:
        _cache.update(ts=time.time(), data=data)
    return data


def _p(q, code):
    return (q.get(code) or {}).get("pct")


def _summary(q):
    notes = []
    for code in ("usIXIC", "usSOXX", "usNVDA", "usINX", "hkHSTECH"):
        p = _p(q, code)
        if p is not None and abs(p) >= 2:
            notes.append("%s %+.2f%%" % (NAMES[code], p))
    ixic, sox = _p(q, "usIXIC"), _p(q, "usSOXX")
    if ixic is not None and sox is not None and (ixic > 0.5 > sox + 1.5 or sox > 0.5 > ixic + 1.5):
        notes.append("注意分化：纳指%+.2f%% 但半导体%+.2f%%，按细分板块看" % (ixic, sox))
    tech = [x for x in (_p(q, "usIXIC"), sox, _p(q, "usNVDA")) if x is not None]
    avg = sum(tech) / len(tech) if tech else 0
    mood = "外盘科技大涨" if avg >= 2 else "外盘科技偏强" if avg >= 0.8 else \
        "外盘科技大跌" if avg <= -2 else "外盘科技偏弱" if avg <= -0.8 else "外盘平稳"
    return {"tech_avg_pct": round(avg, 2), "mood": mood, "notes": notes}


def link_for(industry, ov=None):
    """某 A 股行业受哪些外盘驱动、隔夜表现如何。无关联返回 None。"""
    if not industry:
        return None
    ov = ov or overnight()
    if not ov:
        return None
    for kws, drivers, label in LINKS:
        if any(k in industry for k in kws):
            ds = [(NAMES[d], ov["quotes"][d]["pct"]) for d in drivers if d in ov["quotes"]]
            if not ds:
                return None
            avg = sum(p for _, p in ds) / len(ds)
            hint = ("隔夜外盘%s%.1f%%，今日开盘%s" % ("大涨+" if avg >= 2 else "大跌" if avg <= -2 else "",
                                                avg, "可能高开（注意高开低走）" if avg >= 2 else "可能承压低开"))\
                if abs(avg) >= 2 else "外盘影响不大"
            return {"chain": label, "drivers": [{"name": n, "pct": p} for n, p in ds], "avg_pct": round(avg, 2),
                    "hint": hint}
    return None


def regime_adjust(ov=None):
    """给大盘状态的轻微修正（只在早盘有意义）：返回 (score_delta, reason)。"""
    ov = ov or overnight()
    if not ov:
        return 0, None
    n = market.now()
    if n.hour * 100 + n.minute >= 1300:      # 午后外盘影响减弱，不再修正
        return 0, None
    q = ov["quotes"]
    worst = min([x for x in (_p(q, "usINX"), _p(q, "usIXIC"), _p(q, "hkHSI")) if x is not None] or [0])
    best = max([x for x in (_p(q, "usINX"), _p(q, "usIXIC"), _p(q, "hkHSI")) if x is not None] or [0])
    if worst <= -2.5:
        return -1, "隔夜外盘大跌（最差%.1f%%）" % worst
    if worst <= -1.5:
        return -0.5, "隔夜外盘走弱（最差%.1f%%）" % worst
    if best >= 2:
        return 0.5, "隔夜外盘大涨（最高+%.1f%%）" % best
    return 0, None
