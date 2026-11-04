"""持仓计划：每个持仓都有一份「为什么买、什么情况下说明错了、什么时候走」。

建仓成交时自动生成：
  - 经过投研委员会的：用组合经理给的 thesis / 止损 / 目标 / 移动止盈 / 最长持有 / 失效条件 / 观察点
  - 手动或未过委员会的：用默认止损和时间止损，标注"未经委员会"
监控每轮检查（engine 调用 check_all）：
  stop_loss 跌破止损价（紧急；模拟盘可自动卖）· target 到达目标价 · trailing 从高点回撤
  time_stop 持有超期仍没涨 · invalidation 失效条件（收盘跌破/盘中跌破/均线破位/主力连续流出/出货特征/利空）
触发后：推送 + 写信号 + 唤醒 AI 复核（除硬止损外不自动卖）。同一检查项每天只触发一次。
"""
import json
import logging
import time

from . import db, indicators, market
from .config import load_config

log = logging.getLogger("plans")

INVALIDATION_TYPES = {
    "close_below": "收盘跌破 price（14:50 后判断）",
    "price_below": "盘中跌破 price",
    "ma_break": "收盘跌破 ma 日均线（14:50 后判断）",
    "main_outflow_days": "主力连续 days 日净流出",
    "distribution": "出现借利好出货特征",
    "news_negative": "出现利空新闻/公告",
}


def get_active(account=None, symbol=None):
    sql, args = "SELECT * FROM plans WHERE status='active'", []
    if account:
        sql += " AND account=?"; args.append(account)
    if symbol:
        sql += " AND symbol=?"; args.append(market.normalize(symbol))
    rows = db.all(sql + " ORDER BY id", args)
    for r in rows:
        r["invalidation"] = db.jloads(r["invalidation"], []) or []
        r["watch_points"] = db.jloads(r["watch_points"], []) or []
        r["checks"] = db.jloads(r["checks"], {}) or {}
    return rows


def _defaults(price):
    pc = load_config()["plans"]
    return {"stop_loss": round(price * (1 + pc["default_stop_pct"] / 100), 3), "target": None, "trailing_pct": 6,
            "max_hold_days": pc["default_max_hold_days"]}


def on_fill(acct, symbol, name, side, price, decision_id=None):
    """broker.apply_fill 成交后调用。"""
    from . import committee
    symbol = market.normalize(symbol)
    now = time.time()
    act = get_active(acct, symbol)
    if side == "buy":
        run = committee.approved_run(symbol)
        f = (run or {}).get("final") or {}
        d = _defaults(price)
        fields = {
            "run_id": run["id"] if run else None,
            "entry_price": price,
            "stop_loss": f.get("stop_loss") or d["stop_loss"],
            "target": f.get("target") or d["target"],
            "trailing_pct": f.get("trailing_pct") or d["trailing_pct"],
            "max_hold_days": int(f.get("max_hold_days") or d["max_hold_days"]),
            "thesis": f.get("thesis") or "未经投研委员会（手动/规则买入），使用默认止损%s%%和时间止损"
                      % load_config()["plans"]["default_stop_pct"],
            "invalidation": json.dumps(f.get("invalidation") or [], ensure_ascii=False),
            "watch_points": json.dumps(f.get("watch_points") or [], ensure_ascii=False),
        }
        if act:
            p = act[0]
            stop_now = p["stop_loss"] or 0
            if run and run["id"] != p["run_id"]:   # 加仓且有新委员会结论：更新计划（止损只上移不下移）
                stop_now = max(stop_now, float(fields["stop_loss"] or 0))
                db.run("UPDATE plans SET run_id=?,stop_loss=?,target=?,trailing_pct=?,max_hold_days=?,thesis=?,"
                       "invalidation=?,watch_points=?,updated_at=? WHERE id=?",
                       (fields["run_id"], stop_now, fields["target"], fields["trailing_pct"],
                        fields["max_hold_days"], fields["thesis"], fields["invalidation"], fields["watch_points"],
                        now, p["id"]))
            # 金字塔加仓后：止损上移到新的持仓成本（保本）
            if load_config()["sizing"].get("pyramid_raise_stop"):
                pos = db.one("SELECT avg_cost FROM positions WHERE account=? AND symbol=?", (acct, symbol))
                if pos and pos["avg_cost"] > stop_now:
                    db.run("UPDATE plans SET stop_loss=?,updated_at=? WHERE id=?",
                           (round(pos["avg_cost"], 3), now, p["id"]))
                    log.info("计划 #%d 加仓后止损上移到保本 %.3f", p["id"], pos["avg_cost"])
            return p["id"]
        return db.run("INSERT INTO plans(account,symbol,name,run_id,created_at,updated_at,status,entry_price,stop_loss,"
                      "target,trailing_pct,max_hold_days,thesis,invalidation,watch_points) "
                      "VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?)",
                      (acct, symbol, name, fields["run_id"], now, now, price, fields["stop_loss"], fields["target"],
                       fields["trailing_pct"], fields["max_hold_days"], fields["thesis"], fields["invalidation"],
                       fields["watch_points"]))
    # sell：清仓则关闭计划
    left = db.one("SELECT shares FROM positions WHERE account=? AND symbol=?", (acct, symbol))
    if act and (not left or left["shares"] <= 0):
        p = act[0]
        pnl = db.one("SELECT SUM(pnl) s, SUM(CASE WHEN side='buy' THEN amount ELSE 0 END) cost FROM trades "
                     "WHERE account=? AND symbol=? AND ts>=?", (acct, symbol, p["created_at"] - 1))
        held = (now - p["created_at"]) / 86400
        outcome = {"realized_pnl": round(pnl["s"] or 0, 2),
                   "return_pct": round((pnl["s"] or 0) / pnl["cost"] * 100, 2) if pnl["cost"] else None,
                   "held_days": round(held, 1), "exit_price": price}
        db.run("UPDATE plans SET status='closed',closed_at=?,outcome=?,updated_at=? WHERE id=?",
               (now, json.dumps(outcome, ensure_ascii=False), now, p["id"]))
    return None


def update(account, symbol, changes, author="ai"):
    """AI/用户修改计划（如上移止损）。"""
    p = get_active(account, symbol)
    if not p:
        raise ValueError("%s %s 没有进行中的持仓计划" % (account, symbol))
    p = p[0]
    if changes.get("stop_loss") is not None and p["stop_loss"] and float(changes["stop_loss"]) < p["stop_loss"] \
            and author not in ("网页", "cli", "我"):
        raise ValueError("止损只能上移：%s 想把止损从 %.3f 下调到 %.3f（如确需下调请在网页手动修改）"
                         % (author, p["stop_loss"], float(changes["stop_loss"])))
    sets, args = [], []
    for k in ("stop_loss", "target", "trailing_pct", "max_hold_days", "thesis"):
        if k in changes and changes[k] is not None:
            sets.append(k + "=?"); args.append(changes[k])
    for k in ("invalidation", "watch_points"):
        if k in changes and changes[k] is not None:
            sets.append(k + "=?"); args.append(json.dumps(changes[k], ensure_ascii=False))
    if not sets:
        return p["id"]
    sets.append("updated_at=?"); args.append(time.time())
    db.run("UPDATE plans SET %s WHERE id=?" % ",".join(sets), args + [p["id"]])
    log.info("计划 #%d 被 %s 修改：%s", p["id"], author, changes)
    return p["id"]


def _after_1450():
    n = market.now()
    return n.hour * 100 + n.minute >= 1450


def check_all(quotes, intel_map=None, kline_fn=None, positions=None):
    """返回触发列表 [{plan, key, level, message, auto_sell}]，并记录当日已触发避免重复。"""
    from . import news
    intel_map = intel_map or {}
    today = market.today_str()
    out = []
    pos = {(p["account"], p["symbol"]): p for p in (positions or [])}
    for p in get_active():
        q = quotes.get(p["symbol"])
        ps = pos.get((p["account"], p["symbol"]))
        if not q or q.get("suspended") or not ps:
            continue
        price = q["price"]
        hits = []
        if p["stop_loss"] and price <= p["stop_loss"]:
            hits.append(("stop_loss", "urgent", "跌破止损价 %.3f（现价 %.3f）" % (p["stop_loss"], price), True))
        if p["target"] and price >= p["target"]:
            hits.append(("target", "important", "到达目标价 %.3f，考虑分批止盈" % p["target"], False))
        peak = ps.get("peak_price") or 0
        tp = p["trailing_pct"] or 0
        if tp and peak >= p["entry_price"] * 1.05 and price <= peak * (1 - tp / 100):
            hits.append(("trailing", "important", "从持仓高点 %.3f 回撤 %.1f%%（移动止盈 %s%%）"
                         % (peak, (price / peak - 1) * 100, tp), False))
        held_cal = (time.time() - p["created_at"]) / 86400
        pnl_pct = (price / p["entry_price"] - 1) * 100 if p["entry_price"] else 0
        if p["max_hold_days"] and held_cal >= p["max_hold_days"] * 7 / 5 and pnl_pct < 3:
            hits.append(("time_stop", "important", "已持有约%d个交易日，收益 %.1f%% 未达预期（时间止损）"
                         % (held_cal * 5 / 7, pnl_pct), False))
        it = intel_map.get(p["symbol"]) or {}
        for i, inv in enumerate(p["invalidation"]):
            t = inv.get("type")
            desc = inv.get("desc") or INVALIDATION_TYPES.get(t, t)
            hit = False
            try:
                if t == "close_below" and _after_1450():
                    hit = price < float(inv["price"])
                elif t == "price_below":
                    hit = price < float(inv["price"])
                elif t == "ma_break" and _after_1450() and kline_fn:
                    kl = kline_fn(p["symbol"])
                    ma = indicators.sma([b["c"] for b in kl], int(inv.get("ma", 20)))
                    hit = ma is not None and price < ma
                elif t == "main_outflow_days":
                    days = it.get("flow_days") or []
                    n = int(inv.get("days", 3))
                    hit = len(days) >= n and all((d[1] or 0) < 0 for d in days[-n:])
                elif t == "distribution":
                    hit = bool(it) and (news.distribution_check(q, {}, it) or ("",))[0] == "sell"
                elif t == "news_negative":
                    hit = (it.get("news_today") or {}).get("neg", 0) > 0
            except (KeyError, ValueError, TypeError):
                continue
            if hit:
                hits.append(("inv%d_%s" % (i, t), "important", "失效条件触发：%s" % desc, False))
        changed = False
        for key, level, msg, auto in hits:
            if p["checks"].get(key) == today:
                continue
            p["checks"][key] = today
            changed = True
            out.append({"plan": p, "position": ps, "key": key, "level": level, "message": msg, "auto_sell": auto,
                        "price": price, "name": q["name"]})
        if changed:
            db.run("UPDATE plans SET checks=? WHERE id=?", (json.dumps(p["checks"]), p["id"]))
    return out


def brief(p, price=None):
    d = {k: p[k] for k in ("id", "account", "symbol", "name", "run_id", "entry_price", "stop_loss", "target",
                           "trailing_pct", "max_hold_days", "thesis", "invalidation", "watch_points")}
    d["created"] = market.datetime.fromtimestamp(p["created_at"], market.CN_TZ).strftime("%m-%d")
    d["held_trading_days"] = round((time.time() - p["created_at"]) / 86400 * 5 / 7, 1)
    d["triggered_today"] = [k for k, v in (p.get("checks") or {}).items() if v == market.today_str()]
    if price and p["entry_price"]:
        d["pnl_pct"] = round((price / p["entry_price"] - 1) * 100, 2)
        if p["stop_loss"]:
            d["dist_to_stop_pct"] = round((p["stop_loss"] / price - 1) * 100, 2)
    return d
