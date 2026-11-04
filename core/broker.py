"""账户、持仓、撮合。

- 模拟盘（paper）：按实时盘口（买一/卖一）+ 滑点立即成交，A股规则：T+1、一手、涨跌停、费用。
- 实盘（live）：AI/规则只生成"待确认订单"，你在网页确认后交给券商适配器：
    manual：提示你在券商 App 手动下单，成交后在网页回填成交价（持仓同步更新）
    qmt   ：迅投 miniQMT（xtquant，需 Windows + 开通QMT的券商），自动报单
"""
import json
import logging
import time

from . import db, market
from .config import load_config

log = logging.getLogger("broker")


class OrderError(Exception):
    pass


# ---------------- 费用 ----------------
def calc_fees(symbol, side, amount, fees=None):
    fees = fees or load_config()["fees"]
    commission = max(amount * fees["commission_rate"], fees["commission_min"]) if amount > 0 else 0
    stamp = amount * fees["stamp_duty_rate"] if side == "sell" and not market.is_fund(symbol) else 0
    transfer = amount * fees["transfer_fee_rate"] if not market.is_fund(symbol) else 0
    return round(commission + stamp + transfer, 2)


def round_buy_shares(symbol, shares):
    mn, step = market.lot_rule(symbol)
    shares = int(shares)
    if shares < mn:
        return 0
    return mn + (shares - mn) // step * step if step > 1 else shares


# ---------------- 账户查询 ----------------
def get_account(acct):
    a = db.one("SELECT * FROM accounts WHERE id=?", (acct,))
    if not a:
        raise OrderError("账户不存在: %s" % acct)
    return a


def get_positions(acct=None):
    if acct:
        rows = db.all("SELECT * FROM positions WHERE account=? AND shares>0", (acct,))
    else:
        rows = db.all("SELECT * FROM positions WHERE shares>0")
    today = market.today_str()
    for r in rows:
        locked = r["today_bought"] if r["last_buy_date"] == today else 0
        r["sellable"] = max(0, r["shares"] - locked)
    return rows


def equity_prev_close(acct):
    """昨日（今天0点前最后一次）净值快照，用于计算当日盈亏/熔断。"""
    r = db.one("SELECT equity FROM equity WHERE account=? AND ts<? ORDER BY ts DESC LIMIT 1",
               (acct, market.day_start_ts()))
    if r:
        return r["equity"]
    return get_account(acct)["initial_cash"] or None


def snapshot(acct, quotes=None):
    a = get_account(acct)
    pos = get_positions(acct)
    if quotes is None:
        quotes = market.quotes_cached([p["symbol"] for p in pos]) if pos else {}
    mv, items = 0.0, []
    for p in pos:
        q = quotes.get(p["symbol"]) or {}
        price = q.get("price") or p["avg_cost"]
        val = price * p["shares"]
        mv += val
        items.append({
            "symbol": p["symbol"], "name": p["name"], "shares": p["shares"], "sellable": p["sellable"],
            "avg_cost": round(p["avg_cost"], 4), "price": price, "pct_today": q.get("pct"),
            "market_value": round(val, 2),
            "pnl": round((price - p["avg_cost"]) * p["shares"], 2),
            "pnl_pct": round((price / p["avg_cost"] - 1) * 100, 2) if p["avg_cost"] else 0,
            "peak_price": p.get("peak_price"), "opened_at": p["opened_at"],
        })
    equity = a["cash"] + mv
    prev = equity_prev_close(acct)
    today_trades = db.one("SELECT COUNT(*) n FROM trades WHERE account=? AND ts>=?",
                          (acct, market.day_start_ts()))["n"]
    for it in items:
        it["weight_pct"] = round(it["market_value"] / equity * 100, 1) if equity else 0
    return {
        "account": acct, "name": a["name"], "cash": round(a["cash"], 2),
        "market_value": round(mv, 2), "equity": round(equity, 2),
        "initial_cash": a["initial_cash"],
        "total_pnl": round(equity - a["initial_cash"], 2) if a["initial_cash"] else None,
        "total_pnl_pct": round((equity / a["initial_cash"] - 1) * 100, 2) if a["initial_cash"] else None,
        "day_pnl": round(equity - prev, 2) if prev else None,
        "day_pnl_pct": round((equity / prev - 1) * 100, 2) if prev else None,
        "today_trades": today_trades, "positions": items,
    }


def record_equity(acct, quotes=None):
    s = snapshot(acct, quotes)
    db.run("INSERT INTO equity(account,ts,equity,cash,market_value) VALUES(?,?,?,?,?)",
           (acct, time.time(), s["equity"], s["cash"], s["market_value"]))
    return s


# ---------------- 成交记账（模拟/实盘回填共用）----------------
def apply_fill(acct, symbol, name, side, price, shares, source="manual", decision_id=None,
               order_id=None, note=""):
    symbol = market.normalize(symbol)
    shares = int(shares)
    if shares <= 0 or price <= 0:
        raise OrderError("成交数量/价格无效")
    amount = round(price * shares, 2)
    fee = calc_fees(symbol, side, amount)
    today = market.today_str()
    now = time.time()
    pnl = None
    with db.tx() as c:
        a = c.execute("SELECT * FROM accounts WHERE id=?", (acct,)).fetchone()
        p = c.execute("SELECT * FROM positions WHERE account=? AND symbol=?", (acct, symbol)).fetchone()
        if side == "buy":
            cost = amount + fee
            if acct == "paper" and a["cash"] + 1e-6 < cost:
                raise OrderError("资金不足：需要 %.2f，可用 %.2f" % (cost, a["cash"]))
            if p and p["shares"] > 0:
                tot = p["shares"] + shares
                avg = (p["shares"] * p["avg_cost"] + cost) / tot
                tb = (p["today_bought"] if p["last_buy_date"] == today else 0) + shares
                c.execute("UPDATE positions SET shares=?,avg_cost=?,today_bought=?,last_buy_date=?,name=?,"
                          "peak_price=MAX(COALESCE(peak_price,0),?) WHERE account=? AND symbol=?",
                          (tot, avg, tb, today, name, price, acct, symbol))
            else:
                c.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?,?,?,?)",
                          (acct, symbol, name, shares, cost / shares, shares, today, now, price))
            c.execute("UPDATE accounts SET cash=cash-?,updated_at=? WHERE id=?", (cost, now, acct))
        else:
            if not p or p["shares"] <= 0:
                raise OrderError("没有持仓 %s" % symbol)
            locked = p["today_bought"] if p["last_buy_date"] == today else 0
            sellable = p["shares"] - locked
            if acct == "paper" and shares > sellable:
                raise OrderError("T+1 限制：可卖 %d 股（今日买入 %d 股不可卖）" % (sellable, locked))
            shares = min(shares, p["shares"])
            amount = round(price * shares, 2)
            fee = calc_fees(symbol, side, amount)
            pnl = round((price - p["avg_cost"]) * shares - fee, 2)
            left = p["shares"] - shares
            if left > 0:
                c.execute("UPDATE positions SET shares=? WHERE account=? AND symbol=?", (left, acct, symbol))
            else:
                c.execute("DELETE FROM positions WHERE account=? AND symbol=?", (acct, symbol))
            c.execute("UPDATE accounts SET cash=cash+?,updated_at=? WHERE id=?", (amount - fee, now, acct))
        tid = c.execute(
            "INSERT INTO trades(ts,account,symbol,name,side,price,shares,amount,fee,pnl,source,decision_id,order_id,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, acct, symbol, name, side, price, shares, amount, fee, pnl, source, decision_id, order_id, note)
        ).lastrowid
    try:
        from . import plans
        plans.on_fill(acct, symbol, name, side, price, decision_id)
    except Exception as e:
        log.warning("持仓计划更新失败 %s: %s", symbol, e)
    return {"trade_id": tid, "account": acct, "symbol": symbol, "name": name, "side": side,
            "price": price, "shares": shares, "amount": amount, "fee": fee, "pnl": pnl}


# ---------------- 模拟盘撮合 ----------------
def check_tradable(quote, side):
    if quote.get("suspended"):
        raise OrderError("%s 停牌" % quote["name"])
    if side == "buy" and quote.get("ask1", 0) <= 0:
        raise OrderError("%s 涨停封板/无卖盘，买不进" % quote["name"])
    if side == "sell" and quote.get("bid1", 0) <= 0:
        raise OrderError("%s 跌停/无买盘，卖不出" % quote["name"])


def paper_execute(symbol, side, shares, quote=None, price_type="market", limit_price=None,
                  source="manual", decision_id=None, note=""):
    cfg = load_config()
    if not cfg["paper"]["allow_offhours"] and not market.is_market_open():
        raise OrderError("非交易时间（%s），模拟盘不成交" % market.session_label())
    symbol = market.normalize(symbol)
    quote = quote or market.get_quote(symbol)
    check_tradable(quote, side)
    slip = cfg["paper"]["slippage_pct"]
    if side == "buy":
        px = quote["ask1"] or quote["price"]
        px = round(px * (1 + slip), 3)
        if quote.get("limit_up"):
            px = min(px, quote["limit_up"])
        if price_type == "limit" and limit_price and px > limit_price:
            raise OrderError("限价 %.3f 低于卖一 %.3f，未成交" % (limit_price, px))
        shares = round_buy_shares(symbol, shares)
        if shares <= 0:
            raise OrderError("不足一手")
    else:
        px = quote["bid1"] or quote["price"]
        px = round(px * (1 - slip), 3)
        if quote.get("limit_down"):
            px = max(px, quote["limit_down"])
        if price_type == "limit" and limit_price and px < limit_price:
            raise OrderError("限价 %.3f 高于买一 %.3f，未成交" % (limit_price, px))
    return apply_fill("paper", symbol, quote["name"], side, px, shares, source, decision_id, None, note)


def reset_paper(cash=None):
    cfg = load_config()
    cash = float(cash or cfg["paper"]["initial_cash"])
    with db.tx() as c:
        c.execute("DELETE FROM positions WHERE account='paper'")
        c.execute("DELETE FROM trades WHERE account='paper'")
        c.execute("DELETE FROM equity WHERE account='paper'")
        c.execute("UPDATE orders SET status='cancelled' WHERE account='paper' AND status='pending_confirm'")
        c.execute("UPDATE accounts SET cash=?,initial_cash=?,updated_at=? WHERE id='paper'",
                  (cash, cash, time.time()))


# ---------------- 实盘订单 ----------------
def create_live_order(symbol, name, side, shares, ref_price, reason="", confidence=None,
                      source="ai", decision_id=None, price_type="limit", limit_price=None):
    now = time.time()
    oid = db.run(
        "INSERT INTO orders(created_at,updated_at,account,symbol,name,side,shares,price_type,ref_price,"
        "limit_price,status,source,decision_id,reason,confidence,message) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, now, "live", market.normalize(symbol), name, side, int(shares), price_type, ref_price,
         limit_price, "pending_confirm", source, decision_id, reason, confidence, "等待确认"))
    return db.one("SELECT * FROM orders WHERE id=?", (oid,))


def expire_pending():
    mins = load_config()["ai"]["pending_expire_min"]
    db.run("UPDATE orders SET status='expired',updated_at=?,message='超时未确认，已过期' "
           "WHERE status='pending_confirm' AND created_at<?", (time.time(), time.time() - mins * 60))


def _set_order(oid, status, message, broker_order_id=None):
    db.run("UPDATE orders SET status=?,message=?,broker_order_id=COALESCE(?,broker_order_id),updated_at=? "
           "WHERE id=?", (status, message, broker_order_id, time.time(), oid))


def confirm_order(oid):
    expire_pending()
    o = db.one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o:
        raise OrderError("订单不存在")
    if o["status"] != "pending_confirm":
        raise OrderError("订单状态为 %s，不能确认" % o["status"])
    q = market.get_quote(o["symbol"])
    drift = (q["price"] / o["ref_price"] - 1) * 100 if o["ref_price"] else 0
    broker = get_live_broker()
    res = broker.place(o, q)
    _set_order(oid, res["status"], res["message"] + ("（较建议价偏离 %.2f%%）" % drift), res.get("broker_order_id"))
    return db.one("SELECT * FROM orders WHERE id=?", (oid,))


def reject_order(oid, reason="手动拒绝"):
    o = db.one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o or o["status"] not in ("pending_confirm", "confirmed"):
        raise OrderError("该订单不能拒绝/撤销")
    _set_order(oid, "rejected", reason)


def fill_order(oid, price, shares=None):
    """manual 模式：你在券商成交后回填。"""
    o = db.one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o or o["status"] not in ("confirmed", "submitted", "pending_confirm"):
        raise OrderError("订单状态不允许回填")
    t = apply_fill("live", o["symbol"], o["name"], o["side"], float(price), int(shares or o["shares"]),
                   source=o["source"], decision_id=o["decision_id"], order_id=oid, note="券商成交回填")
    _set_order(oid, "filled", "已成交 %d 股 @ %.3f" % (t["shares"], t["price"]))
    return t


class ManualBroker:
    name = "manual"

    def place(self, order, quote):
        side = "买入" if order["side"] == "buy" else "卖出"
        return {"status": "confirmed",
                "message": "已确认：请在券商App%s %s %d 股（参考价 %.3f），成交后点「已成交」回填"
                           % (side, order["name"], order["shares"], quote["price"])}

    def sync(self):
        return {"ok": False, "message": "manual 模式不支持自动同步，请在网页手动设置资金/持仓"}


class QMTBroker:
    """迅投 miniQMT 适配器（需要 Windows + 券商开通 QMT 权限 + pip 安装 xtquant）。
    未在你的券商环境实测，首次使用请用 1 手小单验证。"""
    name = "qmt"

    def __init__(self, path, account):
        from xtquant.xttrader import XtQuantTrader  # noqa
        from xtquant.xttype import StockAccount
        self.trader = XtQuantTrader(path, int(time.time()) % 100000)
        self.trader.start()
        if self.trader.connect() != 0:
            raise OrderError("连接 miniQMT 失败，请确认客户端已登录（极简模式）")
        self.acc = StockAccount(account)
        self.trader.subscribe(self.acc)

    @staticmethod
    def _code(sym):
        s = market.normalize(sym)
        return s[2:] + "." + s[:2].upper()

    def place(self, order, quote):
        from xtquant import xtconstant
        side = xtconstant.STOCK_BUY if order["side"] == "buy" else xtconstant.STOCK_SELL
        # 用对手价限价单，避免市价单滑点失控
        px = order.get("limit_price") or (quote["ask1"] if order["side"] == "buy" else quote["bid1"]) or quote["price"]
        oid = self.trader.order_stock(self.acc, self._code(order["symbol"]), side, int(order["shares"]),
                                      xtconstant.FIX_PRICE, float(px), "ai-stock", "order-%s" % order["id"])
        if oid is None or oid < 0:
            return {"status": "rejected", "message": "QMT 报单失败 (%s)" % oid}
        return {"status": "submitted", "message": "已报单 QMT#%s 限价 %.3f" % (oid, px), "broker_order_id": str(oid)}

    def sync(self):
        asset = self.trader.query_stock_asset(self.acc)
        pos = self.trader.query_stock_positions(self.acc) or []
        with db.tx() as c:
            c.execute("UPDATE accounts SET cash=?,updated_at=? WHERE id='live'", (asset.cash, time.time()))
            c.execute("DELETE FROM positions WHERE account='live'")
            for p in pos:
                if p.volume <= 0:
                    continue
                code, ex = p.stock_code.split(".")
                sym = ex.lower() + code
                c.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?)",
                          ("live", sym, sym, p.volume, getattr(p, "avg_price", 0) or p.open_price,
                           p.volume - p.can_use_volume, market.today_str(), time.time(), None))
        return {"ok": True, "message": "已同步 QMT 资金 %.2f，持仓 %d 只" % (asset.cash, len(pos))}


_live = None


def get_live_broker():
    global _live
    cfg = load_config()["live"]
    if cfg["broker"] == "qmt":
        if _live is None or _live.name != "qmt":
            _live = QMTBroker(cfg["qmt_path"], cfg["qmt_account"])
        return _live
    return ManualBroker()


def set_live_account(cash=None, positions=None):
    """manual 模式下手工设置实盘资金/持仓。positions: [{symbol,shares,avg_cost}]"""
    with db.tx() as c:
        if cash is not None:
            c.execute("UPDATE accounts SET cash=?,updated_at=? WHERE id='live'", (float(cash), time.time()))
            a = c.execute("SELECT initial_cash FROM accounts WHERE id='live'").fetchone()
            if not a["initial_cash"]:
                c.execute("UPDATE accounts SET initial_cash=? WHERE id='live'", (float(cash),))
        for p in positions or []:
            sym = market.normalize(p["symbol"])
            if int(p.get("shares", 0)) <= 0:
                c.execute("DELETE FROM positions WHERE account='live' AND symbol=?", (sym,))
                continue
            c.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?,?,?,?)",
                      ("live", sym, p.get("name") or sym, int(p["shares"]), float(p["avg_cost"]), 0,
                       None, time.time(), None))
