"""内置策略。每个都可以当模板改。

日线指标类（on_bar）：MACD、KDJ、RSI、均线交叉、布林带、多指标共振
秒级盘口类（on_tick）：盘口买卖力量失衡、短时急涨急跌（带量）
"""
from core import indicators as ind_
from .base import Signal, Strategy


class MACDCross(Strategy):
    name = "macd"
    label = "MACD 金叉/死叉"
    description = "DIF 上穿 DEA 买入信号，下穿卖出信号；零轴上方金叉更强"
    params = {"fast": 12, "slow": 26, "signal": 9}

    def on_bar(self, sym, q, kline, ind):
        closes = [b["c"] for b in kline]
        m = ind_.macd(closes, self.p["fast"], self.p["slow"], self.p["signal"])
        if not m or not m["cross"]:
            return None
        above = m["dif"] > 0
        if m["cross"] == "金叉":
            return Signal("buy", "MACD%s金叉 DIF=%.3f DEA=%.3f" % ("零轴上" if above else "零轴下", m["dif"], m["dea"]),
                          0.7 if above else 0.55)
        return Signal("sell", "MACD%s死叉 DIF=%.3f DEA=%.3f" % ("零轴上" if above else "零轴下", m["dif"], m["dea"]),
                      0.55 if above else 0.7)


class KDJSignal(Strategy):
    name = "kdj"
    label = "KDJ 超买超卖"
    description = "K<20 区域金叉买入、K>80 区域死叉卖出；J 值极端提醒"
    params = {"n": 9, "low": 20, "high": 80}

    def on_bar(self, sym, q, kline, ind):
        k = ind_.kdj(kline, self.p["n"])
        if not k:
            return None
        if k["cross"] == "金叉" and k["k"] < self.p["low"] + 10:
            return Signal("buy", "KDJ 低位金叉 K=%.1f D=%.1f J=%.1f" % (k["k"], k["d"], k["j"]),
                          0.65 if k["k"] < self.p["low"] else 0.5)
        if k["cross"] == "死叉" and k["k"] > self.p["high"] - 10:
            return Signal("sell", "KDJ 高位死叉 K=%.1f D=%.1f J=%.1f" % (k["k"], k["d"], k["j"]),
                          0.65 if k["k"] > self.p["high"] else 0.5)
        if k["j"] < 0:
            return Signal("watch", "KDJ J值<0 极度超卖 J=%.1f" % k["j"], 0.4)
        if k["j"] > 100:
            return Signal("watch", "KDJ J值>100 极度超买 J=%.1f" % k["j"], 0.4)
        return None


class RSISignal(Strategy):
    name = "rsi"
    label = "RSI 超买超卖"
    description = "RSI 从超卖区上穿 low 买入，从超买区下穿 high 卖出"
    params = {"period": 14, "low": 30, "high": 70}

    def on_bar(self, sym, q, kline, ind):
        closes = [b["c"] for b in kline]
        n = self.p["period"]
        now, prev = ind_.rsi(closes, n), ind_.rsi(closes[:-1], n)
        if now is None or prev is None:
            return None
        if prev < self.p["low"] <= now:
            return Signal("buy", "RSI%d 上穿%d（%.1f→%.1f）超卖反弹" % (n, self.p["low"], prev, now), 0.6)
        if prev > self.p["high"] >= now:
            return Signal("sell", "RSI%d 下穿%d（%.1f→%.1f）超买回落" % (n, self.p["high"], prev, now), 0.6)
        return None


class MACross(Strategy):
    name = "ma_cross"
    label = "均线金叉/死叉"
    description = "快线上穿慢线（默认 MA5/MA20）"
    params = {"fast": 5, "slow": 20}

    def on_bar(self, sym, q, kline, ind):
        c = [b["c"] for b in kline]
        f, s = self.p["fast"], self.p["slow"]
        if len(c) < s + 1:
            return None
        f0, s0 = ind_.sma(c[:-1], f), ind_.sma(c[:-1], s)
        f1, s1 = ind_.sma(c, f), ind_.sma(c, s)
        if f0 <= s0 and f1 > s1:
            return Signal("buy", "MA%d 上穿 MA%d（%.3f > %.3f）" % (f, s, f1, s1), 0.6)
        if f0 >= s0 and f1 < s1:
            return Signal("sell", "MA%d 下穿 MA%d（%.3f < %.3f）" % (f, s, f1, s1), 0.6)
        return None


class BollBand(Strategy):
    name = "boll"
    label = "布林带突破"
    description = "收盘突破上轨（强势/追高风险）或跌破下轨（超跌）。只发关注提醒，不发买卖信号"
    params = {"n": 20, "k": 2}
    default_enabled = False
    backtestable = False

    def on_bar(self, sym, q, kline, ind):
        c = [b["c"] for b in kline]
        b = ind_.boll(c, self.p["n"], self.p["k"])
        if not b:
            return None
        if c[-1] > b["up"]:
            return Signal("watch", "突破布林上轨 %.3f（强势，注意追高）" % b["up"], 0.45)
        if c[-1] < b["low"]:
            return Signal("watch", "跌破布林下轨 %.3f（超跌）" % b["low"], 0.45)
        return None


class Resonance(Strategy):
    """推荐的主策略：多个指标同时满足才发信号，减少单指标噪音。"""
    name = "resonance"
    label = "多指标共振（推荐）"
    description = "MACD/KDJ/RSI/均线/量能 打分，≥阈值发买入，≤负阈值发卖出"
    params = {"buy_score": 4, "sell_score": -3}
    cooldown_min = 360

    def on_bar(self, sym, q, kline, ind):
        if len(kline) < 40:
            return None
        c = [b["c"] for b in kline]
        score, why = 0, []
        m = ind_.macd(c)
        if m:
            if m["cross"] == "金叉" or (m["hist"] > 0 >= m["hist_prev"]):
                score += 1; why.append("MACD转多")
            elif m["cross"] == "死叉" or (m["hist"] < 0 <= m["hist_prev"]):
                score -= 1; why.append("MACD转空")
            if m["dif"] > 0:
                score += 0.5
        k = ind_.kdj(kline)
        if k:
            if k["k"] > k["d"] and k["k"] < 80:
                score += 1; why.append("KDJ多头K=%.0f" % k["k"])
            elif k["k"] < k["d"] and k["k"] > 20:
                score -= 1; why.append("KDJ空头K=%.0f" % k["k"])
        r = ind_.rsi(c, 14)
        if r is not None:
            if 45 <= r <= 70:
                score += 1; why.append("RSI强势%.0f" % r)
            elif r > 80:
                score -= 1; why.append("RSI过热%.0f" % r)
            elif r < 40:
                score -= 0.5
        ma20 = ind_.sma(c, 20)
        ma5 = ind_.sma(c, 5)
        if ma20 and c[-1] > ma20 and ma5 > ma20:
            score += 1; why.append("站上MA20")
        elif ma20 and c[-1] < ma20:
            score -= 1; why.append("跌破MA20")
        vr = ind.get("vol_vs_ma5") or 0
        if vr > 1.3 and c[-1] > c[-2]:
            score += 1; why.append("放量上涨x%.1f" % vr)
        elif vr > 1.5 and c[-1] < c[-2]:
            score -= 1; why.append("放量下跌x%.1f" % vr)
        if score >= self.p["buy_score"]:
            return Signal("buy", "共振得分 %.1f：%s" % (score, "、".join(why)), min(0.5 + score / 12, 0.9), score=score)
        if score <= self.p["sell_score"]:
            return Signal("sell", "共振得分 %.1f：%s" % (score, "、".join(why)), min(0.5 + -score / 10, 0.9), score=score)
        return None


# ---------------- A股资金博弈：消息 × 资金 ----------------
class DistributionWarning(Strategy):
    """A股散户多，利好消息出来时常常是主力借机出货。看的是"市场对消息的反应"而不是消息本身。"""
    name = "distribution"
    label = "借利好出货 / 利空洗盘识别"
    description = ("利好+高开/冲高回落+放量+价涨主力净流出+小单接盘+高位 → 出货预警(卖出信号)；"
                   "利空+不跌/低开回升+主力净流入 → 洗盘关注")
    params = {"min_score": 3.5}
    cooldown_min = 120
    backtestable = False

    def on_bar(self, sym, q, kline, ind):
        from core import news
        it = q.get("intel")
        if not it:
            return None
        r = news.distribution_check(q, ind, it)
        if not r:
            return None
        side, score, reasons = r
        if side == "sell" and score >= self.p["min_score"]:
            return Signal("sell", "疑似借利好出货（%.1f分）：%s" % (score, "、".join(reasons)),
                          min(0.5 + score / 12, 0.9), score=score)
        if side == "watch":
            return Signal("watch", "疑似利空洗盘/出尽（%.1f分）：%s" % (score, "、".join(reasons)), 0.5, score=score)
        return None


# ---------------- 秒级盘口 ----------------
class OrderBookPressure(Strategy):
    name = "orderbook"
    label = "盘口买卖力量（秒级）"
    description = "五档委比持续偏向一方 + 价格同向移动，提示主力挂单/抛压"
    params = {"imbalance": 0.6, "ticks": 5}
    cooldown_min = 30

    def on_tick(self, sym, q, ticks):
        n = self.p["ticks"]
        if len(ticks) < n:
            return None
        recent = ticks[-n:]
        imbs = [ind_.orderbook(t)["imbalance"] for t in recent]
        p0, p1 = recent[0]["price"], recent[-1]["price"]
        th = self.p["imbalance"]
        if all(x >= th for x in imbs) and p1 >= p0:
            return Signal("watch", "盘口买盘强势：连续%d个快照委比≥%.0f%%，买一 %.3f×%d手"
                          % (n, th * 100, q["bid1"], q["bids"][0][1]), 0.5)
        if all(x <= -th for x in imbs) and p1 <= p0:
            return Signal("watch", "盘口抛压沉重：连续%d个快照委比≤-%.0f%%，卖一 %.3f×%d手"
                          % (n, th * 100, q["ask1"], q["asks"][0][1]), 0.5)
        return None


class TickMomentum(Strategy):
    name = "tick_momentum"
    label = "短时急涨急跌（秒级，带量）"
    description = "N 秒内涨跌超过阈值，且成交量明显放大"
    params = {"seconds": 60, "pct": 1.0, "vol_mult": 3}
    cooldown_min = 15

    def on_tick(self, sym, q, ticks):
        if len(ticks) < 4:
            return None
        t_now = ticks[-1]["fetched_at"]
        win = [t for t in ticks if t_now - t["fetched_at"] <= self.p["seconds"]]
        if len(win) < 3 or win[0]["price"] <= 0:
            return None
        chg = (win[-1]["price"] / win[0]["price"] - 1) * 100
        vol_win = win[-1]["volume"] - win[0]["volume"]
        # 对比：全天平均每秒成交量
        elapsed = max(60, _seconds_since_open(ticks[-1]))
        avg = ticks[-1]["volume"] / elapsed * (win[-1]["fetched_at"] - win[0]["fetched_at"] or 1)
        surge = vol_win / avg if avg > 0 else 0
        if abs(chg) >= self.p["pct"] and surge >= self.p["vol_mult"]:
            side = "watch"
            return Signal(side, "%d秒内%s %.2f%%，成交量为均速 %.1f 倍" %
                          (self.p["seconds"], "急涨" if chg > 0 else "急跌", chg, surge), 0.6, chg=round(chg, 2))
        return None


def _seconds_since_open(q):
    t = q.get("time", "")
    if len(t) < 14:
        return 3600
    h, m, s = int(t[8:10]), int(t[10:12]), int(t[12:14])
    sec = h * 3600 + m * 60 + s
    if sec <= 11 * 3600 + 1800:
        return max(60, sec - (9 * 3600 + 1800))
    return 7200 + max(0, sec - 13 * 3600)
