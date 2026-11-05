"""AI 决策管道：build_context() 给 AI 看的全部信息 → AI 输出决策 JSON → apply_decision() 风控+执行。

外部定时 AI（每10分钟）和内置大模型（异动触发）走同一套函数。决策 JSON 格式见 AGENT_PROMPT.md。
"""
import csv
import json
import logging
import os
import time

from . import broker, db, indicators, market, notify, rules
from .broker import OrderError
from .config import DATA_DIR, load_config

log = logging.getLogger("agent")
TICK_DIR = os.path.join(DATA_DIR, "ticks")


# ---------------- 秒级 tick 文件 ----------------
TICK_HEADER = ["ts", "time", "price", "volume", "amount"] + \
    ["b%d%s" % (i, k) for i in range(1, 6) for k in ("p", "v")] + \
    ["a%d%s" % (i, k) for i in range(1, 6) for k in ("p", "v")]


def tick_path(sym, day=None):
    day = day or market.now().strftime("%Y%m%d")
    return os.path.join(TICK_DIR, day, sym + ".csv")


def append_tick(q):
    path = tick_path(q["symbol"])
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = [round(q["fetched_at"], 1), q["time"], q["price"], q["volume"], round(q["amount"])]
    for p, v in q["bids"]:
        row += [p, v]
    for p, v in q["asks"]:
        row += [p, v]
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(TICK_HEADER)
        w.writerow(row)


def read_ticks(sym, n=200, day=None):
    path = tick_path(market.normalize(sym), day)
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))[-n:]
    out = []
    for r in rows:
        try:
            out.append({k: (float(v) if k != "time" else v) for k, v in r.items()})
        except ValueError:
            continue
    return out


def tick_stats(sym):
    ticks = read_ticks(sym, 400)
    if len(ticks) < 2:
        return None
    last = ticks[-1]
    res = {"ticks_today_file": tick_path(market.normalize(sym)), "last_time": last["time"]}
    for label, sec in (("1m", 60), ("5m", 300), ("15m", 900)):
        old = [t for t in ticks if last["ts"] - t["ts"] >= sec]
        if old and old[-1]["price"]:
            o = old[-1]
            res["chg_%s_pct" % label] = round((last["price"] / o["price"] - 1) * 100, 2)
            res["vol_%s_lots" % label] = int((last["volume"] - o["volume"]) / 100)
    return res


# ---------------- 上下文 ----------------
def _compact_quote(q):
    ob = indicators.orderbook(q)
    vwap = q["amount"] / q["volume"] if q["volume"] else None
    return {
        "name": q["name"], "price": q["price"], "pct": q["pct"], "open": q["open"], "high": q["high"],
        "low": q["low"], "prev_close": q["prev_close"], "limit_up": q["limit_up"], "limit_down": q["limit_down"],
        "vwap": round(vwap, 3) if vwap else None, "amount_yi": round(q["amount"] / 1e8, 2),
        "turnover_pct": q["turnover"], "vol_ratio": q["vol_ratio"], "pe": q["pe"], "pb": q["pb"],
        "total_mv_yi": q["total_mv"], "bid1": q["bids"][0], "ask1": q["asks"][0],
        "orderbook": ob, "suspended": q["suspended"], "quote_time": q["time"],
    }


_kcache = {}


def daily_kline(sym, count=150):
    key = (sym, market.today_str())
    if key not in _kcache:
        try:
            _kcache[key] = market.get_kline(sym, "day", count)
        except Exception as e:
            log.warning("日K获取失败 %s: %s", sym, e)
            return []
    return _kcache[key]


def build_context(with_ticks=True, with_intel=True):
    from . import news
    cfg = load_config()
    now = market.now()
    watch = db.all("SELECT * FROM watchlist ORDER BY added_at")
    positions = broker.get_positions()
    syms = list(dict.fromkeys([w["symbol"] for w in watch] + [p["symbol"] for p in positions]))
    idx_syms = [s for s, _ in market.INDEXES]
    quotes = market.quotes_cached(syms + idx_syms, max_age=10)
    today = market.today_str()
    since = market.day_start_ts()

    intel = {}
    if with_intel:
        try:
            intel = news.intel_many([s for s in syms if s in quotes],
                                    {s: quotes[s]["name"] for s in syms if s in quotes}, quotes)
        except Exception as e:
            log.warning("消息/资金面获取失败: %s", e)

    stocks = {}
    for s in syms:
        q = quotes.get(s)
        if not q:
            continue
        kl = indicators.merge_live(daily_kline(s), q, today)
        summ = indicators.summarize(kl)
        item = {"quote": _compact_quote(q), "daily": summ,
                "last5_days": [[b["t"], b["o"], b["c"], b["h"], b["l"]] for b in kl[-6:-1]],
                "in_watchlist": any(w["symbol"] == s for w in watch),
                "watch_note": next((w["note"] for w in watch if w["symbol"] == s), None),
                "lot": market.lot_rule(s)[0], "one_lot_cost": round(q["price"] * market.lot_rule(s)[0], 2)}
        if with_ticks:
            ts = tick_stats(s)
            if ts:
                item["intraday_ticks"] = ts
        if with_intel:
            try:
                from . import global_market
                ind = news.industry(s, q["name"])
                if ind:
                    item["industry"] = ind
                lk = global_market.link_for(ind)
                if lk:
                    item["overnight_link"] = lk
            except Exception as e:
                log.debug("外盘联动失败 %s: %s", s, e)
        if intel.get(s):
            item["intel"] = intel[s]
            chk = news.distribution_check(q, summ, intel[s])
            if chk:
                item["a_share_pattern"] = {"type": "疑似借利好出货" if chk[0] == "sell" else "疑似利空洗盘",
                                           "score": chk[1], "reasons": chk[2]}
        sig = db.all("SELECT ts,source,strategy,type,side,strength,message FROM alerts WHERE symbol=? AND ts>=?"
                     " ORDER BY ts DESC LIMIT 6", (s, since))
        if sig:
            item["signals_today"] = [dict(x, ts=market.datetime.fromtimestamp(x["ts"], market.CN_TZ).strftime("%H:%M:%S"))
                                     for x in sig]
        stocks[s] = item

    accounts = {}
    for a in ("paper", "live"):
        snap = broker.snapshot(a, quotes)
        snap["ai_controlled"] = a in cfg["ai"]["accounts"]
        snap["mode"] = "模拟盘：决策立即按盘口成交" if a == "paper" else "实盘：决策生成待确认订单，需人工确认"
        accounts[a] = snap

    def fmt_ts(x):
        return market.datetime.fromtimestamp(x, market.CN_TZ).strftime("%m-%d %H:%M")

    decisions = db.all("SELECT id,ts,agent,trigger,market_view,results FROM decisions ORDER BY id DESC LIMIT 5")
    for d in decisions:
        d["ts"] = fmt_ts(d["ts"])
        res = db.jloads(d.pop("results"), {}) or {}
        d["executed"] = [r.get("summary") for r in res.get("actions", []) if r.get("summary")]
    trades = db.all("SELECT ts,account,symbol,name,side,price,shares,pnl,source FROM trades WHERE ts>=? ORDER BY ts",
                    (since,))
    for t in trades:
        t["ts"] = fmt_ts(t["ts"])
    pending = db.all("SELECT id,created_at,symbol,name,side,shares,ref_price,status,reason FROM orders "
                     "WHERE status IN ('pending_confirm','confirmed','submitted') ORDER BY id DESC LIMIT 10")
    for o in pending:
        o["created_at"] = fmt_ts(o["created_at"])
    alerts = db.all("SELECT ts,source,strategy,symbol,name,type,side,strength,message FROM alerts WHERE ts>=? "
                    "ORDER BY ts DESC LIMIT 30", (time.time() - 3 * 3600,))
    for a in alerts:
        a["ts"] = fmt_ts(a["ts"])
    rl = db.all("SELECT id,symbol,type,params,action,enabled,note,created_by FROM rules ORDER BY id")
    for r in rl:
        r["params"] = db.jloads(r["params"], {})

    flash = []
    regime, cands, glob = None, None, None
    if with_intel:
        try:
            from . import global_market
            ov = global_market.overnight()
            if ov:
                glob = {"summary": ov["summary"], "as_of": ov["fetched"],
                        "quotes": {v["name"]: v["pct"] for v in ov["quotes"].values()},
                        "note": "美股收盘在北京时间4~5点，主要影响A股当天开盘；午后影响减弱"}
        except Exception as e:
            log.warning("外盘获取失败: %s", e)
        try:
            flash = [("%s %s" % (x["time"], x["text"])) for x in news.flash_news(15)]
        except Exception as e:
            log.warning("快讯获取失败: %s", e)
        try:
            from . import screener
            regime = screener.market_regime()
        except Exception as e:
            regime = {"error": str(e)[:100]}
    try:
        from . import screener
        cands = {}
        for ac in cfg["ai"]["accounts"]:      # 每个账户自己的候选池
            lc = screener.latest_candidates(10, ac)
            if lc:
                cands[ac] = {"screen_time": lc["ts"], "items": [
                    {k: c.get(k) for k in ("rank", "symbol", "name", "industry", "score", "price", "reasons")}
                    for c in lc["candidates"]]}
        cands = cands or None
    except Exception:
        pass
    from . import plans as plans_mod
    active_plans = []
    for p in plans_mod.get_active():
        price = (quotes.get(p["symbol"]) or {}).get("price")
        active_plans.append(plans_mod.brief(p, price))
    planned = {(p["account"], p["symbol"]) for p in active_plans}
    no_plan = [{"account": p["account"], "symbol": p["symbol"], "name": p["name"]} for p in positions
               if (p["account"], p["symbol"]) not in planned]
    runs = db.all("SELECT id,ts,account,symbol,name,status,decision,confidence,mode FROM committee_runs "
                  "ORDER BY id DESC LIMIT 8")
    for r in runs:
        r["ts"] = market.datetime.fromtimestamp(r["ts"], market.CN_TZ).strftime("%m-%d %H:%M")

    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S %a"),
        "market_regime": regime,
        "trading_style": {ac: _style_of(ac, cfg) for ac in ("paper", "live")},
        "emotion": _emotion_brief(cfg),
        "global_overnight": glob,
        "position_plans": active_plans,
        "entry_watch": _entry_watch(),
        "positions_without_plan": no_plan,
        "candidates": cands,
        "committee_recent": runs,
        "committee_required_for_buy": cfg["committee"]["required_for_buy"],
        "trading_boards": [market.BOARD_NAMES[b] for b in cfg["universe"]["boards"]],
        "flash_news": flash,
        "session": market.session_label(now),
        "market_open": market.is_market_open(now),
        "indexes": {s: {"name": n, "price": quotes[s]["price"], "pct": quotes[s]["pct"],
                        "amount_yi": round(quotes[s]["amount"] / 1e8)}
                    for s, n in market.INDEXES if s in quotes},
        "accounts": accounts,
        "stocks": stocks,
        "alerts_last_3h": alerts,
        "trades_today": trades,
        "open_orders": pending,
        "recent_decisions": decisions,
        "rules": rl,
        "journal_recent": journal_recent(),
        "risk_limits": cfg["risk"],
        "fees": cfg["fees"],
        "ai_accounts": cfg["ai"]["accounts"],
    }


# ---------------- 交易日志（AI 跨会话记忆）----------------
JOURNAL_DIR = os.path.join(DATA_DIR, "journal")


def journal_append(text, author="ai"):
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    now = market.now()
    path = os.path.join(JOURNAL_DIR, now.strftime("%Y-%m-%d") + ".md")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n### %s · %s\n%s\n" % (now.strftime("%H:%M"), author, text.strip()))
    return path


def journal_recent(max_chars=3000, days=3):
    """最近几天的日志（新→旧），截断到 max_chars。"""
    if not os.path.isdir(JOURNAL_DIR):
        return ""
    files = sorted(f for f in os.listdir(JOURNAL_DIR) if f.endswith(".md"))[-days:]
    out = ""
    for fn in reversed(files):
        with open(os.path.join(JOURNAL_DIR, fn), encoding="utf-8") as f:
            out += "## %s\n%s\n" % (fn[:-3], f.read()[-max_chars:])
        if len(out) >= max_chars:
            break
    return out[:max_chars]


# ---------------- inbox：上次决策之后的新情况 ----------------
def inbox():
    last = db.one("SELECT id,ts,agent,trigger FROM decisions ORDER BY id DESC LIMIT 1")
    since = last["ts"] if last else market.day_start_ts()
    alerts = db.all("SELECT ts,source,strategy,symbol,name,type,side,strength,message,price,action_result "
                    "FROM alerts WHERE ts>? ORDER BY ts", (since,))
    for a in alerts:
        a["ts"] = market.datetime.fromtimestamp(a["ts"], market.CN_TZ).strftime("%H:%M:%S")
    orders = db.all("SELECT id,symbol,name,side,shares,status,message,updated_at FROM orders WHERE updated_at>?",
                    (since,))
    trades = db.all("SELECT account,symbol,name,side,price,shares,pnl,source FROM trades WHERE ts>?", (since,))
    return {
        "now": market.now().strftime("%Y-%m-%d %H:%M:%S"), "session": market.session_label(),
        "last_decision": dict(last, ts=market.datetime.fromtimestamp(last["ts"], market.CN_TZ)
                              .strftime("%m-%d %H:%M")) if last else None,
        "new_alerts": alerts, "order_updates": orders, "new_trades": trades,
        "wake_reason": os.environ.get("AI_TRIGGER"), "wake_symbols": os.environ.get("AI_SYMBOLS"),
    }


# ---------------- 日报 ----------------
def daily_report(day=None):
    day = day or market.today_str()
    t0 = market.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=market.CN_TZ).timestamp()
    t1 = t0 + 86400
    trades = db.all("SELECT * FROM trades WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1))
    decs = db.all("SELECT id,ts,agent,trigger,market_view,results FROM decisions WHERE ts>=? AND ts<? ORDER BY ts",
                  (t0, t1))
    alerts = db.all("SELECT source,strategy,type,side,COUNT(*) n FROM alerts WHERE ts>=? AND ts<? "
                    "GROUP BY source,strategy,type,side ORDER BY n DESC", (t0, t1))
    accts = {}
    for a in ("paper", "live"):
        e = db.all("SELECT equity FROM equity WHERE account=? AND ts<? ORDER BY ts DESC LIMIT 1", (a, t1))
        p = db.all("SELECT equity FROM equity WHERE account=? AND ts<? ORDER BY ts DESC LIMIT 1", (a, t0))
        tr = [t for t in trades if t["account"] == a]
        accts[a] = {"equity_close": e[0]["equity"] if e else None, "equity_prev": p[0]["equity"] if p else None,
                    "trades": len(tr), "fees": round(sum(t["fee"] for t in tr), 2),
                    "realized_pnl": round(sum(t["pnl"] or 0 for t in tr), 2)}
        if accts[a]["equity_close"] and accts[a]["equity_prev"]:
            accts[a]["day_pct"] = round((accts[a]["equity_close"] / accts[a]["equity_prev"] - 1) * 100, 2)
    rejected = 0
    for d in decs:
        res = db.jloads(d.pop("results"), {}) or {}
        d["ts"] = market.datetime.fromtimestamp(d["ts"], market.CN_TZ).strftime("%H:%M")
        d["actions"] = [a.get("summary") or ("拒绝:" + str(a.get("error"))) for a in res.get("actions", [])]
        rejected += sum(1 for a in res.get("actions", []) if a.get("status") == "rejected")
    for t in trades:
        t["ts"] = market.datetime.fromtimestamp(t["ts"], market.CN_TZ).strftime("%H:%M:%S")
    return {"date": day, "accounts": accts, "decisions": len(decs), "rejected_actions": rejected,
            "decision_log": decs, "trades": trades, "signal_counts": alerts,
            "current": {a: broker.snapshot(a) for a in ("paper", "live")}}


# ---------------- 风控 ----------------
def _entry_watch():
    """委员会说「等回踩再买」、程序正在盯的票（到价会自动重开委员会复审）。"""
    try:
        from . import watch
        return [watch.brief(w) for w in watch.active()]
    except Exception:
        return []


def _style_of(acct, cfg):
    from .config import style
    return style(acct, cfg)


def _emotion_brief(cfg):
    """有账户用情绪短线时，给 AI 看情绪周期和热门题材。"""
    if not any(_style_of(a, cfg) == "momentum" for a in ("paper", "live")):
        return None
    try:
        from . import emotion
        e = emotion.sentiment()
        if e.get("error"):
            return e
        return {"stage": e["stage"], "position_cap": e["position_cap"], "tip": e["tip"], "reasons": e["reasons"],
                "hot_themes": [{"name": t["name"], "zt": t["zt"], "max_lb": t["max_lb"],
                                "leaders": [x["name"] for x in t["leaders"]]} for t in e["themes"][:6]]}
    except Exception as ex:
        return {"error": str(ex)[:100]}


def size_and_check(acct, act, q, snap, cfg, watch_syms, regime_cap=None, cap_pct=None, stop=None):
    """计算股数并做风控，返回股数；不通过抛 OrderError。
    regime_cap：大盘状态对应的总仓位上限；cap_pct：委员会给的该票仓位上限；stop：止损价（风险定仓用）。
    定仓说明写回 act["_sizing"]。"""
    risk = cfg["risk"]
    side, sym = act["action"], q["symbol"]
    if snap["today_trades"] >= risk["max_daily_trades"]:
        raise OrderError("今日成交已达上限 %d 笔" % risk["max_daily_trades"])
    pos = next((p for p in snap["positions"] if p["symbol"] == sym), None)
    price = q["ask1"] or q["price"] if side == "buy" else q["bid1"] or q["price"]
    if side == "sell":
        if not pos:
            raise OrderError("没有持仓")
        avail = pos["sellable"] if acct == "paper" else pos["shares"]
        if avail <= 0:
            raise OrderError("T+1：今日买入的不能卖")
        if act.get("shares"):
            n = int(act["shares"])
        elif act.get("position_pct"):
            n = int(avail * float(act["position_pct"]))
        else:
            n = avail
        n = min(n, avail)
        mn, step = market.lot_rule(sym)
        if n < avail:  # 部分卖出需整手；清仓可零股
            n = n // 100 * 100 if step == 100 else n
        if n <= 0:
            raise OrderError("卖出数量不足一手")
        return n
    # ---- buy ----
    if risk["block_st"] and "ST" in q["name"].upper():
        raise OrderError("风控：禁止买入 ST")
    b = market.board(sym)
    if b not in cfg["universe"]["boards"]:
        raise OrderError("无交易权限：%s（已开通：%s）" % (market.BOARD_NAMES[b],
                                                  "/".join(market.BOARD_NAMES[x] for x in cfg["universe"]["boards"])))
    if risk["restrict_to_watchlist"] and sym not in watch_syms:
        raise OrderError("风控：只允许买入自选股")
    from .config import style as _style
    mom = _style(acct, cfg) == "momentum"
    chase = min(cfg["momentum"]["max_chase_pct"], risk["max_chase_pct"]) if mom else risk["max_chase_pct"]
    if q["pct"] >= chase:
        raise OrderError("风控：当日已涨 %.2f%%，超过追高上限 %.1f%%" % (q["pct"], chase))
    if snap["day_pnl_pct"] is not None and snap["day_pnl_pct"] / 100 <= risk["daily_loss_limit_pct"]:
        raise OrderError("风控熔断：当日亏损 %.2f%%，禁止新买入" % snap["day_pnl_pct"])
    held = [p["symbol"] for p in snap["positions"]]
    if sym not in held and len(held) >= risk["max_positions"]:
        raise OrderError("风控：持仓只数已达上限 %d" % risk["max_positions"])
    equity = snap["equity"]
    sz = cfg["sizing"]
    notes = []
    # 1) 调用方请求的数量（作为上限）
    if act.get("shares"):
        n_req = int(act["shares"])
    elif act.get("amount"):
        n_req = int(float(act["amount"]) / price)
    elif act.get("position_pct"):
        n_req = int(equity * float(act["position_pct"]) / price)
    else:
        n_req = None
    # 2) 加仓：只允许反马丁（赚钱才加、越加越少），禁止亏损补仓
    if pos:
        if pos["pnl_pct"] < 0:
            raise OrderError("禁止亏损补仓（马丁格尔）：当前浮亏 %.2f%%" % pos["pnl_pct"])
        if not sz["pyramid_enabled"]:
            raise OrderError("未开启金字塔加仓（设置 sizing.pyramid_enabled）")
        if pos["pnl_pct"] < sz["pyramid_min_profit_pct"]:
            raise OrderError("反马丁：浮盈 %.2f%% 未达 %s%%，暂不加仓" % (pos["pnl_pct"], sz["pyramid_min_profit_pct"]))
        buys = db.all("SELECT shares FROM trades WHERE account=? AND symbol=? AND side='buy' AND ts>=? ORDER BY ts",
                      (acct, sym, (pos.get("opened_at") or 0) - 1))
        adds = max(0, len(buys) - 1)
        if adds >= sz["pyramid_max_adds"]:
            raise OrderError("金字塔已加仓 %d 次，达到上限" % adds)
        last = buys[-1]["shares"] if buys else pos["shares"]
        n_pyr = int(last * sz["pyramid_add_ratio"])
        n_req = min(n_req, n_pyr) if n_req else n_pyr
        notes.append("金字塔第%d次加仓：上次%d股×%.0f%%=%d股" % (adds + 1, last, sz["pyramid_add_ratio"] * 100, n_pyr))
        if sz["pyramid_raise_stop"]:
            stop = max(float(stop or 0), pos["avg_cost"])   # 加仓后止损提到保本，加仓部分的风险按保本价算
    # 3) 定仓方式
    if sz["mode"] == "risk":
        dflt = cfg["momentum"]["stop_pct"] if mom else cfg["plans"]["default_stop_pct"]
        stop = float(stop) if stop else round(price * (1 + dflt / 100), 3)
        per = price - stop
        if per <= 0:
            raise OrderError("止损价 %.3f 不低于买入价 %.3f，无法按风险定仓" % (stop, price))
        budget = equity * sz["risk_per_trade_pct"] / 100
        n_risk = int(budget / per)
        n = min(n_req, n_risk) if n_req is not None else n_risk
        notes.append("单笔风险 %.0f元(%.1f%%)÷每股风险 %.3f(止损%.3f)=%d股" % (budget, sz["risk_per_trade_pct"], per,
                                                                       stop, n_risk))
    else:
        n = n_req if n_req is not None else int(equity * 0.2 / price)
    act["_sizing"] = "；".join(notes)
    cur_val = pos["market_value"] if pos else 0
    max_val = equity * min(risk["max_position_pct"], cap_pct or 1) - cur_val
    if regime_cap is not None:
        room = equity * regime_cap - snap["market_value"]
        if room < max_val:
            max_val = room
            if room <= 0:
                raise OrderError("大盘/情绪总仓位上限 %.0f%%，已用 %.0f%%" % (regime_cap * 100,
                                                                 snap["market_value"] / equity * 100))
    if risk["max_order_value"]:
        max_val = min(max_val, risk["max_order_value"])
    cash = snap["cash"]
    max_val = min(max_val, cash - broker.calc_fees(sym, "buy", cash) - 1) if acct == "paper" else max_val
    n = min(n, int(max_val / price)) if max_val > 0 else 0
    n = broker.round_buy_shares(sym, n)
    if n <= 0:
        lot = market.lot_rule(sym)[0]
        raise OrderError("不足一手（一手约 %.0f 元，单票上限 %.0f%%，可用 %.0f）%s"
                         % (price * lot, risk["max_position_pct"] * 100, cash,
                            ("；" + act["_sizing"] + "。可把止损设近一些，或调高单笔风险") if act.get("_sizing") else ""))
    return n


# ---------------- 执行决策 ----------------
def apply_decision(dec, agent="external", trigger="timer"):
    if isinstance(dec, str):
        dec = json.loads(dec)
    cfg = load_config()
    agent = dec.get("agent") or agent
    trigger = dec.get("trigger") or trigger
    did = db.run("INSERT INTO decisions(ts,agent,trigger,market_view,payload,results) VALUES(?,?,?,?,?,?)",
                 (time.time(), agent, trigger, dec.get("market_view", ""), json.dumps(dec, ensure_ascii=False), "{}"))
    results = {"decision_id": did, "watchlist": [], "rules": [], "actions": []}

    for w in dec.get("watchlist_add") or []:
        try:
            s = market.normalize(w["symbol"] if isinstance(w, dict) else w)
            q = market.get_quote(s)
            db.run("INSERT OR REPLACE INTO watchlist VALUES(?,?,?,?,?)",
                   (s, q["name"], (w.get("note") if isinstance(w, dict) else "") or "", agent, time.time()))
            results["watchlist"].append("加入自选 %s %s" % (s, q["name"]))
        except Exception as e:
            results["watchlist"].append("加入自选失败 %s: %s" % (w, e))
    for s in dec.get("watchlist_remove") or []:
        try:
            db.run("DELETE FROM watchlist WHERE symbol=?", (market.normalize(s),))
            results["watchlist"].append("移出自选 %s" % s)
        except Exception as e:
            results["watchlist"].append("移出失败 %s: %s" % (s, e))
    for r in dec.get("rules_add") or []:
        try:
            rid = rules.add_rule(r["symbol"], r["type"], r.get("params"), r.get("action", "notify"),
                                 r.get("cooldown_min", 30), r.get("note", ""), created_by=agent)
            results["rules"].append("新增规则 #%d %s %s %s" % (rid, r["symbol"], r["type"], r.get("params")))
        except Exception as e:
            results["rules"].append("新增规则失败: %s" % e)
    for rid in dec.get("rules_remove") or []:
        db.run("DELETE FROM rules WHERE id=?", (int(rid),))
        results["rules"].append("删除规则 #%s" % rid)

    for p in dec.get("plans") or []:
        try:
            from . import plans as plans_mod
            pid = plans_mod.update(p.get("account", "paper"), p["symbol"], p, author=agent)
            results.setdefault("plans", []).append("更新持仓计划 #%d %s" % (pid, p["symbol"]))
        except Exception as e:
            results.setdefault("plans", []).append("更新计划失败 %s: %s" % (p.get("symbol"), e))

    watch_syms = {w["symbol"] for w in db.all("SELECT symbol FROM watchlist")}
    caps = {}

    def regime_cap_for(acct):
        """总仓位上限：波段看大盘状态；情绪短线再和情绪周期取小。"""
        if acct not in caps:
            try:
                from . import screener
                caps[acct] = screener.position_cap(acct)["cap"]
            except Exception as e:
                log.warning("大盘/情绪状态获取失败，不做总仓位限制: %s", e)
                caps[acct] = None
        return caps[acct]
    for act in dec.get("actions") or []:
        side = (act.get("action") or "hold").lower()
        if side not in ("buy", "sell"):
            continue
        conf = float(act.get("confidence", 1))
        for acct in act.get("accounts") or cfg["ai"]["accounts"]:
            r = {"account": acct, "symbol": act.get("symbol"), "action": side, "reason": act.get("reason", "")}
            try:
                if acct not in cfg["ai"]["accounts"]:
                    raise OrderError("账户 %s 未授权给 AI（设置 ai.accounts）" % acct)
                if conf < cfg["risk"]["min_confidence"]:
                    raise OrderError("置信度 %.2f 低于阈值 %.2f" % (conf, cfg["risk"]["min_confidence"]))
                q = market.get_quote(act["symbol"])
                r["symbol"], r["name"] = q["symbol"], q["name"]
                snap = broker.snapshot(acct)
                cap_pct = None
                if side == "buy" and cfg["committee"]["required_for_buy"]:
                    from . import committee, llm
                    run = committee.approved_run(q["symbol"], acct)
                    if not run:
                        if cfg["committee"]["auto_run_builtin"] and llm.enabled():
                            started = committee.run_async(q["symbol"], acct, "add" if any(
                                p["symbol"] == q["symbol"] for p in snap["positions"]) else "open",
                                trigger="门禁:" + agent, then_execute={"accounts": [acct],
                                                                     "position_pct": act.get("position_pct")})
                            r.update(status="committee_running", summary=None, committee_run=started["run_id"],
                                     error="未过投研委员会，已%s内置委员会#%d审核，通过后自动执行"
                                           % ("启动" if started["started"] else "在进行", started["run_id"]))
                            results["actions"].append(r)
                            continue
                        raise OrderError("新建仓/加仓需先通过投研委员会：python3 agent_cli.py committee start %s"
                                         % q["symbol"])
                    cap_pct = (run["final"] or {}).get("position_pct")
                    ent = (run["final"] or {}).get("entry_max")
                    px = q["ask1"] or q["price"]
                    if ent and px > float(ent) * 1.01:
                        raise OrderError("现价 %.3f 高于委员会#%d 入场上限 %.3f，不追" % (px, run["id"], float(ent)))
                    r["committee_run"] = run["id"]
                stop = None
                if side == "buy":
                    # 止损价优先级：本次动作指定 > 委员会结论 > 已有持仓计划 > 默认止损%
                    stop = act.get("stop_loss")
                    if not stop and r.get("committee_run"):
                        stop = (run["final"] or {}).get("stop_loss")
                    if not stop:
                        from . import plans as plans_mod
                        ap = plans_mod.get_active(acct, q["symbol"])
                        stop = ap[0]["stop_loss"] if ap else None
                ab = dict(act, action=side)
                n = size_and_check(acct, ab, q, snap, cfg, watch_syms,
                                   regime_cap=regime_cap_for(acct) if side == "buy" else None, cap_pct=cap_pct, stop=stop)
                if ab.get("_sizing"):
                    r["sizing"] = ab["_sizing"]
                if acct == "paper":
                    t = broker.paper_execute(q["symbol"], side, n, quote=q, price_type=act.get("price_type", "market"),
                                             limit_price=act.get("limit_price"), source="ai", decision_id=did,
                                             note=act.get("reason", "")[:200])
                    r.update(status="filled", trade=t,
                             summary="模拟盘%s %s %d股 @%.3f%s" % ("买入" if side == "buy" else "卖出", q["name"],
                                                             t["shares"], t["price"],
                                                             "，盈亏%.2f" % t["pnl"] if t["pnl"] is not None else ""))
                    if side == "buy" and act.get("stop_loss") and not r.get("committee_run"):
                        from . import plans as plans_mod
                        try:
                            plans_mod.update(acct, q["symbol"], {"stop_loss": float(act["stop_loss"])}, author=agent)
                        except ValueError as e:      # 计划默认止损更紧（如情绪短线 -4%）：保留更紧的，不影响已成交
                            r["plan_note"] = str(e)
                else:
                    ref = q["ask1"] if side == "buy" else q["bid1"]
                    o = broker.create_live_order(q["symbol"], q["name"], side, n, ref or q["price"],
                                                 reason=act.get("reason", ""), confidence=conf, source="ai",
                                                 decision_id=did, limit_price=act.get("limit_price"))
                    r.update(status="pending_confirm", order_id=o["id"],
                             summary="实盘待确认#%d %s %s %d股 参考价%.3f" % (o["id"], "买入" if side == "buy" else "卖出",
                                                                    q["name"], n, o["ref_price"]))
            except Exception as e:
                r.update(status="rejected", error=str(e), summary=None)
            results["actions"].append(r)

    # 推送
    done = [a for a in results["actions"] if a["status"] in ("filled", "pending_confirm")]
    rejected = [a for a in results["actions"] if a["status"] == "rejected"]
    if (done and cfg["notify"]["ai_summary"]) or dec.get("notify"):
        url = cfg["server"]["public_url"].rstrip("/")
        tok = cfg["server"]["token"]
        lines = ["**%s**" % dec.get("market_view", "")[:300], ""]
        for a in done:
            lines.append("- %s  \n  理由：%s" % (a["summary"], a["reason"][:150]))
        for a in rejected:
            lines.append("- ❌ %s %s 被拒：%s" % (a["action"], a.get("name") or a["symbol"], a["error"]))
        if any(a["status"] == "pending_confirm" for a in done):
            lines.append("\n👉 [去确认实盘订单](%s/?%s#orders)" % (url, "token=" + tok if tok else ""))
        if dec.get("notify"):
            lines.append("\n" + str(dec["notify"]))
        title = "AI决策(%s)：%s" % (trigger[:20], "，".join(a["summary"] for a in done)[:60] or "提醒")
        notify.send(title, "\n".join(lines))

    if dec.get("journal"):
        try:
            results["journal"] = journal_append(str(dec["journal"]), agent)
        except OSError as e:
            results["journal"] = "写日志失败: %s" % e
    db.run("UPDATE decisions SET results=? WHERE id=?", (json.dumps(results, ensure_ascii=False), did))
    return results
