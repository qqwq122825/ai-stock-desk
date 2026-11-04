"""预设规则（你设定的阈值）：抵达条件 → 推送微信 → 可选自动卖出/唤醒AI。

目标 symbol：
  具体代码（如 sh600519）| *WATCH 全部自选 | *POS 全部持仓 | *ALL 自选+持仓
触发方式：边沿触发（条件从不满足→满足才算一次）+ 冷却时间，避免刷屏。
"""
import json
import logging
import time

from . import broker, db, indicators, market

log = logging.getLogger("rules")

RULE_TYPES = {
    "price_above":     {"label": "价格 ≥", "params": {"price": 0}},
    "price_below":     {"label": "价格 ≤", "params": {"price": 0}},
    "pct_above":       {"label": "日涨幅 ≥ %", "params": {"pct": 5}},
    "pct_below":       {"label": "日涨幅 ≤ %", "params": {"pct": -5}},
    "rapid_move":      {"label": "N分钟内急涨急跌 %", "params": {"pct": 2, "minutes": 5}},
    "vol_ratio_above": {"label": "量比 ≥", "params": {"value": 3}},
    "bid_wall":        {"label": "买一/卖一挂单 ≥ N手", "params": {"lots": 10000, "side": "bid"}},
    "break_high":      {"label": "突破N日新高", "params": {"days": 20}},
    "break_low":       {"label": "跌破N日新低", "params": {"days": 20}},
    "rsi_above":       {"label": "RSI ≥", "params": {"value": 75, "period": 14}},
    "rsi_below":       {"label": "RSI ≤", "params": {"value": 25, "period": 14}},
    "kdj_j_above":     {"label": "KDJ J值 ≥", "params": {"value": 100}},
    "kdj_j_below":     {"label": "KDJ J值 ≤", "params": {"value": 0}},
    "stop_loss":       {"label": "持仓止损：浮亏 ≤ %", "params": {"pct": -5}, "position": True},
    "take_profit":     {"label": "持仓止盈：浮盈 ≥ %", "params": {"pct": 10}, "position": True},
    "trailing_stop":   {"label": "移动止盈：从持仓最高价回撤 ≥ %", "params": {"pct": 6}, "position": True},
}
ACTIONS = {"notify": "仅推送", "notify_ai": "推送+唤醒AI调研", "auto_sell": "推送+自动卖出(实盘为待确认)"}


def _hit(rtype, p, q, kline_fn, ticks_fn, pos):
    price = q["price"]
    if rtype == "price_above":
        return price >= p["price"] > 0, "价格 %.3f ≥ %.3f" % (price, p["price"])
    if rtype == "price_below":
        return 0 < price <= p["price"], "价格 %.3f ≤ %.3f" % (price, p["price"])
    if rtype == "pct_above":
        return q["pct"] >= p["pct"], "日涨幅 %.2f%% ≥ %s%%" % (q["pct"], p["pct"])
    if rtype == "pct_below":
        return q["pct"] <= p["pct"], "日涨幅 %.2f%% ≤ %s%%" % (q["pct"], p["pct"])
    if rtype == "vol_ratio_above":
        return q.get("vol_ratio", 0) >= p["value"], "量比 %.2f ≥ %s" % (q.get("vol_ratio", 0), p["value"])
    if rtype == "bid_wall":
        lots = q["bids"][0][1] if p.get("side", "bid") == "bid" else q["asks"][0][1]
        nm = "买一" if p.get("side", "bid") == "bid" else "卖一"
        return lots >= p["lots"], "%s挂单 %d 手 ≥ %d" % (nm, lots, p["lots"])
    if rtype == "rapid_move":
        ticks = ticks_fn(q["symbol"])
        win = [t for t in ticks if q["fetched_at"] - t["fetched_at"] <= p["minutes"] * 60]
        if len(win) < 2:
            return False, ""
        lo = min(t["price"] for t in win if t["price"] > 0)
        hi = max(t["price"] for t in win)
        up = (price / lo - 1) * 100 if lo else 0
        dn = (price / hi - 1) * 100 if hi else 0
        if up >= p["pct"]:
            return True, "%d分钟内急涨 %.2f%%" % (p["minutes"], up)
        if dn <= -p["pct"]:
            return True, "%d分钟内急跌 %.2f%%" % (p["minutes"], dn)
        return False, ""
    if rtype in ("break_high", "break_low", "rsi_above", "rsi_below", "kdj_j_above", "kdj_j_below"):
        kl = kline_fn(q["symbol"])
        if not kl or len(kl) < 22:
            return False, ""
        if rtype in ("break_high", "break_low"):
            hist = kl[-(p["days"] + 1):-1]
            if rtype == "break_high":
                hh = max(b["h"] for b in hist)
                return price > hh, "突破%d日新高 %.3f" % (p["days"], hh)
            ll = min(b["l"] for b in hist)
            return price < ll, "跌破%d日新低 %.3f" % (p["days"], ll)
        if rtype.startswith("rsi"):
            r = indicators.rsi([b["c"] for b in kl], int(p.get("period", 14)))
            if r is None:
                return False, ""
            if rtype == "rsi_above":
                return r >= p["value"], "RSI %.1f ≥ %s" % (r, p["value"])
            return r <= p["value"], "RSI %.1f ≤ %s" % (r, p["value"])
        k = indicators.kdj(kl)
        if not k:
            return False, ""
        if rtype == "kdj_j_above":
            return k["j"] >= p["value"], "KDJ J=%.1f ≥ %s" % (k["j"], p["value"])
        return k["j"] <= p["value"], "KDJ J=%.1f ≤ %s" % (k["j"], p["value"])
    if rtype in ("stop_loss", "take_profit", "trailing_stop") and pos:
        pnl = (price / pos["avg_cost"] - 1) * 100 if pos["avg_cost"] else 0
        if rtype == "stop_loss":
            return pnl <= p["pct"], "浮亏 %.2f%%（成本 %.3f）" % (pnl, pos["avg_cost"])
        if rtype == "take_profit":
            return pnl >= p["pct"], "浮盈 %.2f%%（成本 %.3f）" % (pnl, pos["avg_cost"])
        peak = pos.get("peak_price") or 0
        if peak <= 0:
            return False, ""
        dd = (price / peak - 1) * 100
        return dd <= -abs(p["pct"]) and peak > pos["avg_cost"], "从持仓高点 %.3f 回撤 %.2f%%" % (peak, dd)
    return False, ""


class RuleEngine:
    def evaluate(self, quotes, watch_syms, positions, kline_fn, ticks_fn):
        """返回本轮触发的 alerts 列表（已写库、已推送由调用方决定）。"""
        out = []
        rules = db.all("SELECT * FROM rules WHERE enabled=1")
        by_sym = {}
        for p in positions:
            by_sym.setdefault(p["symbol"], []).append(p)
        now = time.time()
        for r in rules:
            meta = RULE_TYPES.get(r["type"])
            if not meta:
                continue
            params = dict(meta["params"])
            params.update(db.jloads(r["params"], {}) or {})
            tgt = r["symbol"]
            if tgt == "*POS":
                syms = list(by_sym)
            elif tgt == "*WATCH":
                syms = list(watch_syms)
            elif tgt == "*ALL":
                syms = list(dict.fromkeys(list(watch_syms) + list(by_sym)))
            else:
                syms = [tgt]
            state = db.jloads(r["state"], {}) or {}
            changed = False
            for sym in syms:
                q = quotes.get(sym)
                if not q or q.get("suspended"):
                    continue
                targets = by_sym.get(sym, []) if meta.get("position") else [None]
                for pos in targets:
                    key = sym if pos is None else "%s:%s" % (pos["account"], sym)
                    try:
                        hit, msg = _hit(r["type"], params, q, kline_fn, ticks_fn, pos)
                    except Exception as e:
                        log.debug("规则 %s 计算失败 %s: %s", r["id"], sym, e)
                        continue
                    st = state.get(key, {"a": False, "t": 0})
                    if hit and not st["a"] and now - st["t"] >= (r["cooldown_min"] or 0) * 60:
                        st = {"a": True, "t": now}
                        state[key] = st
                        changed = True
                        out.append({"rule": r, "symbol": sym, "name": q["name"], "price": q["price"],
                                    "pct": q["pct"], "message": msg, "position": pos})
                    elif not hit and st["a"]:
                        st["a"] = False
                        state[key] = st
                        changed = True
            if changed:
                fired = any(a["rule"]["id"] == r["id"] for a in out)
                if fired:
                    db.run("UPDATE rules SET state=?,last_triggered=? WHERE id=?", (json.dumps(state), now, r["id"]))
                else:
                    db.run("UPDATE rules SET state=? WHERE id=?", (json.dumps(state), r["id"]))
        return out


def run_action(alert):
    """执行规则动作 auto_sell。返回结果文字。"""
    r, pos = alert["rule"], alert.get("position")
    if r["action"] != "auto_sell" or not pos:
        return ""
    try:
        if pos["account"] == "paper":
            if pos["sellable"] <= 0:
                return "自动卖出失败：今日买入，T+1 不可卖"
            t = broker.paper_execute(pos["symbol"], "sell", pos["sellable"], source="rule",
                                     note="规则#%d %s" % (r["id"], alert["message"]))
            return "模拟盘已自动卖出 %d 股 @ %.3f，盈亏 %.2f" % (t["shares"], t["price"], t["pnl"] or 0)
        o = broker.create_live_order(pos["symbol"], alert["name"], "sell", pos["sellable"] or pos["shares"],
                                     alert["price"], reason="规则#%d %s" % (r["id"], alert["message"]),
                                     source="rule")
        return "实盘已生成卖出待确认订单 #%d" % o["id"]
    except Exception as e:
        return "自动卖出失败：%s" % e


def add_rule(symbol, rtype, params=None, action="notify", cooldown_min=30, note="", created_by="manual"):
    if rtype not in RULE_TYPES:
        raise ValueError("未知规则类型 %s，可选：%s" % (rtype, ", ".join(RULE_TYPES)))
    if action not in ACTIONS:
        raise ValueError("未知动作 %s" % action)
    if not str(symbol).startswith("*"):
        symbol = market.normalize(symbol)
    p = dict(RULE_TYPES[rtype]["params"])
    p.update(params or {})
    return db.run("INSERT INTO rules(symbol,type,params,action,cooldown_min,note,created_by,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (symbol, rtype, json.dumps(p), action, int(cooldown_min), note, created_by, time.time()))
