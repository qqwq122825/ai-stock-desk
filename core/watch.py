"""等回踩：委员会说「现在别追，等回踩到 X 站稳再买」时，程序替你盯着。

委员会结论为 wait、并且给了入场价（entry_max）和止损（stop_loss）时，自动登记一条「等回踩」：
  - 有效期 3 个交易日
  - 回踩型（入场价低于当时价格）：价格回到入场价附近（≤ 入场价 × 1.01），且从当天最低点拉回 ≥0.5%（站稳，不是还在往下掉），
    10:00 以后才判断（开盘半小时波动大）
  - 突破型（入场价高于当时价格，例如"放量站上 4.25"）：价格站上入场价，且不超过入场价 × 1.01（不追）
  - 跌破止损价 → 作废；过期 → 作废
到价后**不会直接买**，而是自动再开一次投研委员会，用最新数据复审；复审通过才按正常风控买入（实盘只生成待确认订单）。
所以它不绕过任何门禁，只是把委员会说的"到价再看"真的执行出来。
"""
import logging
import time

from . import db, market
from .config import load_config

log = logging.getLogger("watch")
VALID_TRADING_DAYS = 3


def _expires(now_ts):
    d = market.datetime.fromtimestamp(now_ts, market.CN_TZ)
    n = 0
    while n < VALID_TRADING_DAYS:
        d += market.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return d.replace(hour=15, minute=0, second=0).timestamp()


def on_finalize(run, final):
    """委员会结论落库后调用：wait + 入场价 + 止损 → 登记等回踩。返回 watch id 或 None。"""
    try:
        if (final.get("decision") or "").lower() != "wait":
            return None
        if (run.get("trigger") or "").startswith("等回踩"):
            return None     # 到价复审的结论又是"再等等"：不再登记，避免一直循环开会
        entry, stop = final.get("entry_max"), final.get("stop_loss")
        entry, stop = float(entry or 0), float(stop or 0)
        price = float(run.get("price") or 0)
        if not price:
            q = market.get_quote(run["symbol"])
            price = q["price"]
        if entry <= 0 or stop <= 0 or stop >= entry:
            return None
        kind = "breakout" if entry > price * 1.005 else "pullback"
        acct = run.get("account") or "paper"
        now = time.time()
        db.run("UPDATE entry_watch SET status='replaced',updated_at=? WHERE account=? AND symbol=? AND status='active'",
               (now, acct, run["symbol"]))
        return db.run("INSERT INTO entry_watch(account,symbol,name,run_id,kind,entry,stop,target,ref_price,created_at,"
                      "expires_at,status,updated_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
                      (acct, run["symbol"], run.get("name"), run["id"], kind, entry, stop, final.get("target"), price,
                       now, _expires(now), now, (final.get("summary") or "")[:200]))
    except Exception as e:
        log.warning("登记等回踩失败 %s: %s", run.get("symbol"), e)
        return None


def active(account=None):
    sql, args = "SELECT * FROM entry_watch WHERE status='active'", []
    if account:
        sql += " AND account=?"; args.append(account)
    return db.all(sql + " ORDER BY id", args)


def symbols():
    return [r["symbol"] for r in active()]


def _hm():
    n = market.now()
    return n.hour * 100 + n.minute


def triggered(w, q, hm=None):
    """返回 (状态, 说明)：('hit', ...) 到价站稳 / ('invalid', ...) 跌破止损 / (None, '')。"""
    hm = _hm() if hm is None else hm
    px, low = q.get("price") or 0, q.get("low") or 0
    if px <= 0:
        return None, ""
    if px <= w["stop"]:
        return "invalid", "跌破止损价 %.3f，等回踩作废" % w["stop"]
    if hm < 1000:
        return None, ""
    if w["kind"] == "pullback":
        if px <= w["entry"] * 1.01 and low > 0 and px >= low * 1.005:
            return "hit", "回踩到 %.3f（入场价 %.3f），从今天低点 %.3f 拉回 %.1f%%" % (px, w["entry"], low, (px / low - 1) * 100)
    else:
        if w["entry"] <= px <= w["entry"] * 1.01:
            return "hit", "站上突破价 %.3f（现价 %.3f）" % (w["entry"], px)
    return None, ""


def check(quotes, cfg=None):
    """盯盘每轮调用（交易时段）。到价 → 自动复审；返回提醒列表。"""
    cfg = cfg or load_config()
    now = time.time()
    out = []
    db.run("UPDATE entry_watch SET status='expired',updated_at=? WHERE status='active' AND expires_at<?", (now, now))
    for w in active():
        q = quotes.get(w["symbol"])
        if not q or q.get("suspended"):
            continue
        st, msg = triggered(w, q)
        if st == "invalid":
            db.run("UPDATE entry_watch SET status='invalid',updated_at=?,note=? WHERE id=?", (now, msg, w["id"]))
            out.append({"watch": w, "status": "invalid", "msg": msg, "price": q["price"], "name": q["name"],
                        "rerun": None})
        elif st == "hit":
            rerun = None
            if w["account"] in cfg["ai"]["accounts"]:
                try:
                    from . import committee, llm
                    if llm.enabled():
                        rerun = committee.run_async(w["symbol"], w["account"], "open", trigger="等回踩到价复审",
                                                    then_execute={"accounts": [w["account"]]})["run_id"]
                except Exception as e:
                    msg += "；复审启动失败：%s" % e
            db.run("UPDATE entry_watch SET status='triggered',updated_at=?,rerun_id=?,note=? WHERE id=?",
                   (now, rerun, msg, w["id"]))
            out.append({"watch": w, "status": "hit", "msg": msg + ("；已自动开委员会#%d复审，通过才会买" % rerun if rerun else
                                                   "；没开内置大模型/账户没授权给 AI，请自己决定"),
                        "price": q["price"], "name": q["name"], "rerun": rerun})
    return out


def brief(w):
    return {k: w.get(k) for k in ("id", "account", "symbol", "name", "run_id", "kind", "entry", "stop", "target",
                                  "ref_price", "status", "note")} | {
        "expires": market.datetime.fromtimestamp(w["expires_at"], market.CN_TZ).strftime("%m-%d %H:%M")}
