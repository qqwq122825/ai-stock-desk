"""技术指标（纯 Python）。"""


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n and n > 0 else None


def sma_series(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema_series(vals, n):
    out, k, e = [], 2.0 / (n + 1), None
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return None
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, sig)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    cross = None
    if dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
        cross = "金叉"
    elif dif[-2] >= dea[-2] and dif[-1] < dea[-1]:
        cross = "死叉"
    return {"dif": dif[-1], "dea": dea[-1], "hist": hist[-1], "hist_prev": hist[-2], "cross": cross}


def kdj_series(kline, n=9, m1=3, m2=3):
    """经典 KDJ（同花顺/通达信算法），返回 [(k,d,j)]。"""
    out, k, d = [], 50.0, 50.0
    for i in range(len(kline)):
        w = kline[max(0, i - n + 1):i + 1]
        hh, ll = max(b["h"] for b in w), min(b["l"] for b in w)
        rsv = 50.0 if hh == ll else (kline[i]["c"] - ll) / (hh - ll) * 100
        k = (m1 - 1) / m1 * k + rsv / m1
        d = (m2 - 1) / m2 * d + k / m2
        out.append((k, d, 3 * k - 2 * d))
    return out


def kdj(kline, n=9):
    if len(kline) < n + 2:
        return None
    s = kdj_series(kline, n)
    (k0, d0, _), (k1, d1, j1) = s[-2], s[-1]
    cross = None
    if k0 <= d0 and k1 > d1:
        cross = "金叉"
    elif k0 >= d0 and k1 < d1:
        cross = "死叉"
    return {"k": k1, "d": d1, "j": j1, "cross": cross}


def rsi_series(closes, n=14):
    # 逐点计算（日K只有一两百根，足够快）
    return [rsi(closes[:i + 1], n) if i >= n else None for i in range(len(closes))]


def orderbook(quote):
    """五档盘口指标：买卖力量比、价差、委比。"""
    bids, asks = quote.get("bids") or [], quote.get("asks") or []
    bv = sum(v for p, v in bids if p > 0)
    av = sum(v for p, v in asks if p > 0)
    b1, a1 = quote.get("bid1", 0), quote.get("ask1", 0)
    return {
        "bid_vol5": bv, "ask_vol5": av,
        "imbalance": round((bv - av) / (bv + av), 3) if bv + av else 0,   # 委比 -1~1
        "spread_pct": round((a1 - b1) / b1 * 100, 3) if b1 > 0 and a1 > 0 else None,
        "at_limit_up": a1 == 0 and b1 > 0,      # 卖盘为空=涨停封板
        "at_limit_down": b1 == 0 and a1 > 0,    # 买盘为空=跌停
    }


def boll(closes, n=20, k=2):
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return {"up": m + k * sd, "mid": m, "low": m - k * sd}


def merge_live(kline, quote, today):
    """把实时价并入日K：最后一根是今天则替换收盘/高低，否则追加。"""
    kl = [dict(b) for b in kline]
    if not quote or quote.get("price", 0) <= 0:
        return kl
    bar = {"t": today, "o": quote["open"] or quote["price"], "c": quote["price"],
           "h": max(quote["high"], quote["price"]), "l": min(quote["low"] or quote["price"], quote["price"]),
           "v": quote["volume"] / 100.0}
    if kl and kl[-1]["t"] == today:
        kl[-1] = bar
    elif not kl or kl[-1]["t"] < today:
        kl.append(bar)
    return kl


def r2(x, nd=3):
    return None if x is None else round(x, nd)


def summarize(kline):
    """日K摘要，供规则和 AI 使用。kline 已合并实时价。"""
    if not kline:
        return {}
    closes = [b["c"] for b in kline]
    vols = [b["v"] for b in kline]
    last = closes[-1]
    ma = {n: sma(closes, n) for n in (5, 10, 20, 60)}
    out = {
        "close": last,
        "ma5": r2(ma[5]), "ma10": r2(ma[10]), "ma20": r2(ma[20]), "ma60": r2(ma[60]),
        "rsi14": r2(rsi(closes, 14), 1),
        "macd": None, "boll": None,
        "high20": max(b["h"] for b in kline[-21:-1]) if len(kline) > 21 else None,
        "low20": min(b["l"] for b in kline[-21:-1]) if len(kline) > 21 else None,
        "chg5d_pct": r2((last / closes[-6] - 1) * 100, 2) if len(closes) > 5 else None,
        "chg20d_pct": r2((last / closes[-21] - 1) * 100, 2) if len(closes) > 20 else None,
        "vol_vs_ma5": r2(vols[-1] / (sum(vols[-6:-1]) / 5), 2) if len(vols) > 5 and sum(vols[-6:-1]) else None,
    }
    m = macd(closes)
    if m:
        out["macd"] = {k: (r2(v) if isinstance(v, float) else v) for k, v in m.items()}
    b = boll(closes)
    if b:
        out["boll"] = {k: r2(v) for k, v in b.items()}
    kd = kdj(kline)
    if kd:
        out["kdj"] = {k: (r2(v, 1) if isinstance(v, float) else v) for k, v in kd.items()}
    if all(ma[n] for n in (5, 10, 20)):
        if ma[5] > ma[10] > ma[20]:
            out["trend"] = "多头排列"
        elif ma[5] < ma[10] < ma[20]:
            out["trend"] = "空头排列"
        else:
            out["trend"] = "震荡"
    return out
