#!/usr/bin/env python3
"""AI 股票决策系统 —— 启动：python3 app.py   然后打开 http://127.0.0.1:8686

零第三方依赖（Python 3.9+ 标准库）。
"""
import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import agent, auth, broker, config, db, llm, market, notify, rules  # noqa: E402
from core.engine import Monitor  # noqa: E402

WEB_DIR = os.path.join(config.BASE_DIR, "web")
log = logging.getLogger("app")
MONITOR = None
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def fmt_rows(rows):
    for r in rows:
        for k in ("ts", "created_at", "updated_at", "added_at", "last_triggered"):
            if isinstance(r.get(k), float):
                r[k + "_str"] = market.datetime.fromtimestamp(r[k], market.CN_TZ).strftime("%m-%d %H:%M:%S")
    return rows


# ---------------- API ----------------
@route("GET", "/api/status")
def api_status(q, body):
    st = dict(MONITOR.status) if MONITOR else {"monitor": "未运行"}
    st.update(now=market.now().strftime("%Y-%m-%d %H:%M:%S"), session=market.session_label(),
              llm_enabled=llm.enabled(), wakeup_enabled=bool(config.load_config()["ai"].get("wakeup_command")),
              ai_accounts=config.load_config()["ai"]["accounts"],
              pending=db.one("SELECT COUNT(*) n FROM orders WHERE status='pending_confirm'")["n"])
    return st


@route("GET", "/api/overview")
def api_overview(q, body):
    pos = broker.get_positions()
    idx = [s for s, _ in market.INDEXES]
    quotes = market.quotes_cached([p["symbol"] for p in pos] + idx, max_age=15)
    out = {"accounts": {a: broker.snapshot(a, quotes) for a in ("paper", "live")},
           "indexes": [dict(symbol=s, name=n, price=quotes.get(s, {}).get("price"), pct=quotes.get(s, {}).get("pct"))
                       for s, n in market.INDEXES]}
    return out


@route("GET", "/api/equity")
def api_equity(q, body):
    acct = q.get("account", "paper")
    days = int(q.get("days", 30))
    rows = db.all("SELECT ts,equity,cash,market_value FROM equity WHERE account=? AND ts>=? ORDER BY ts",
                  (acct, time.time() - days * 86400))
    return fmt_rows(rows)


@route("GET", "/api/watchlist")
def api_watch(q, body):
    w = db.all("SELECT * FROM watchlist ORDER BY added_at")
    syms = [x["symbol"] for x in w]
    quotes = market.quotes_cached(syms, max_age=5)
    for x in w:
        qq = quotes.get(x["symbol"])
        if qq:
            x["quote"] = qq
            x["ob"] = agent.indicators.orderbook(qq)
    return fmt_rows(w)


@route("POST", "/api/watchlist")
def api_watch_add(q, body):
    s = market.normalize(body["symbol"])
    qq = market.get_quote(s)
    db.run("INSERT OR REPLACE INTO watchlist VALUES(?,?,?,?,?)", (s, qq["name"], body.get("note", ""), "manual", time.time()))
    return {"ok": True, "symbol": s, "name": qq["name"]}


@route("DELETE", "/api/watchlist/(?P<sym>\\w+)")
def api_watch_del(q, body, sym):
    db.run("DELETE FROM watchlist WHERE symbol=?", (sym,))
    return {"ok": True}


@route("GET", "/api/stock/(?P<sym>\\w+)")
def api_stock(q, body, sym):
    sym = market.normalize(sym)
    period = q.get("period", "day")
    count = int(q.get("count", 120 if period == "day" else 240))
    kl = market.get_kline(sym, period, count)
    quote = market.quotes_cached([sym], 5).get(sym)
    if period == "day":
        kl = agent.indicators.merge_live(kl, quote, market.today_str())
    closes = [b["c"] for b in kl]
    ind = agent.indicators
    macd_dif = ind.ema_series(closes, 12)
    slow = ind.ema_series(closes, 26)
    dif = [a - b for a, b in zip(macd_dif, slow)]
    dea = ind.ema_series(dif, 9)
    kdj = ind.kdj_series(kl) if kl else []
    return {"symbol": sym, "quote": quote, "kline": kl,
            "ma5": ind.sma_series(closes, 5), "ma10": ind.sma_series(closes, 10), "ma20": ind.sma_series(closes, 20),
            "macd": {"dif": dif, "dea": dea, "hist": [(a - b) * 2 for a, b in zip(dif, dea)]},
            "kdj": {"k": [x[0] for x in kdj], "d": [x[1] for x in kdj], "j": [x[2] for x in kdj]},
            "rsi": ind.rsi_series(closes, 14) if len(closes) < 400 else [],
            "summary": ind.summarize(kl) if period == "day" else {},
            "ticks": agent.read_ticks(sym, 300)}


@route("GET", "/api/rules")
def api_rules(q, body):
    rows = db.all("SELECT * FROM rules ORDER BY id")
    for r in rows:
        r["params"] = db.jloads(r["params"], {})
        r["label"] = rules.RULE_TYPES.get(r["type"], {}).get("label", r["type"])
        r.pop("state", None)
    return {"rules": fmt_rows(rows), "types": rules.RULE_TYPES, "actions": rules.ACTIONS}


@route("POST", "/api/rules")
def api_rule_add(q, body):
    rid = rules.add_rule(body["symbol"], body["type"], body.get("params"), body.get("action", "notify"),
                         body.get("cooldown_min", 30), body.get("note", ""))
    return {"ok": True, "id": rid}


@route("PUT", "/api/rules/(?P<rid>\\d+)")
def api_rule_upd(q, body, rid):
    if "enabled" in body:
        db.run("UPDATE rules SET enabled=? WHERE id=?", (int(bool(body["enabled"])), int(rid)))
    if "params" in body:
        db.run("UPDATE rules SET params=?,state='{}' WHERE id=?", (json.dumps(body["params"]), int(rid)))
    for k in ("action", "cooldown_min", "note"):
        if k in body:
            db.run("UPDATE rules SET %s=? WHERE id=?" % k, (body[k], int(rid)))
    return {"ok": True}


@route("DELETE", "/api/rules/(?P<rid>\\d+)")
def api_rule_del(q, body, rid):
    db.run("DELETE FROM rules WHERE id=?", (int(rid),))
    return {"ok": True}


@route("GET", "/api/strategies")
def api_strats(q, body):
    from core.engine import StrategyRunner
    return (MONITOR.strats if MONITOR else StrategyRunner()).describe()


@route("POST", "/api/strategies")
def api_strats_save(q, body):
    cur = config.load_config().get("strategies", {})
    cur[body["name"]] = {k: body[k] for k in ("enabled", "params", "cooldown_min") if k in body}
    config.save_config({"strategies": cur})
    if MONITOR:
        MONITOR.strats.reload()
    return {"ok": True}


@route("GET", "/api/alerts")
def api_alerts(q, body):
    n = int(q.get("limit", 100))
    return fmt_rows(db.all("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (n,)))


@route("GET", "/api/messages")
def api_msgs(q, body):
    return fmt_rows(db.all("SELECT * FROM messages ORDER BY id DESC LIMIT 50"))


@route("GET", "/api/decisions")
def api_decisions(q, body):
    rows = db.all("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (int(q.get("limit", 50)),))
    for r in rows:
        r["payload"] = db.jloads(r["payload"], {})
        r["results"] = db.jloads(r["results"], {})
    return fmt_rows(rows)


@route("GET", "/api/trades")
def api_trades(q, body):
    acct = q.get("account")
    if acct:
        rows = db.all("SELECT * FROM trades WHERE account=? ORDER BY id DESC LIMIT 200", (acct,))
    else:
        rows = db.all("SELECT * FROM trades ORDER BY id DESC LIMIT 200")
    return fmt_rows(rows)


@route("GET", "/api/orders")
def api_orders(q, body):
    broker.expire_pending()
    return fmt_rows(db.all("SELECT * FROM orders ORDER BY id DESC LIMIT 100"))


@route("POST", "/api/orders/(?P<oid>\\d+)/(?P<op>confirm|reject|fill)")
def api_order_op(q, body, oid, op):
    oid = int(oid)
    if op == "confirm":
        return broker.confirm_order(oid)
    if op == "reject":
        broker.reject_order(oid, body.get("reason", "手动拒绝"))
        return {"ok": True}
    return broker.fill_order(oid, float(body["price"]), body.get("shares"))


@route("POST", "/api/trade")
def api_trade(q, body):
    """手动下单：模拟盘立即撮合；实盘=记录你在券商的真实成交。"""
    acct, side = body.get("account", "paper"), body["side"]
    if acct == "paper":
        return broker.paper_execute(body["symbol"], side, int(body["shares"]), source="manual",
                                    note=body.get("note", ""))
    qq = market.get_quote(body["symbol"])
    return broker.apply_fill("live", qq["symbol"], qq["name"], side, float(body.get("price") or qq["price"]),
                             int(body["shares"]), source="manual", note=body.get("note", "手工记录"))


@route("POST", "/api/accounts/paper/reset")
def api_reset(q, body):
    broker.reset_paper(body.get("cash"))
    return {"ok": True}


@route("POST", "/api/accounts/live/set")
def api_live_set(q, body):
    broker.set_live_account(body.get("cash"), body.get("positions"))
    return {"ok": True}


@route("POST", "/api/accounts/live/sync")
def api_live_sync(q, body):
    return broker.get_live_broker().sync()


@route("GET", "/api/config")
def api_cfg(q, body):
    return config.masked_config()


@route("POST", "/api/config")
def api_cfg_save(q, body):
    config.save_config(body)
    return config.masked_config()


@route("POST", "/api/notify/test")
def api_notify_test(q, body):
    ok, ch = notify.send("测试推送：AI股票系统", "如果你在微信看到这条，推送配置成功 ✅  \n%s" % market.now())
    return {"ok": ok, "channels": ch}


@route("GET", "/api/agent/context")
def api_ctx(q, body):
    return agent.build_context()


@route("POST", "/api/agent/decide")
def api_decide(q, body):
    return agent.apply_decision(body, agent=body.get("agent", "http"), trigger=body.get("trigger", "http"))


@route("POST", "/api/agent/llm")
def api_llm(q, body):
    return llm.decide(trigger=body.get("trigger", "网页手动触发"), focus=body.get("focus"))


@route("POST", "/api/backtest")
def api_backtest(q, body):
    from core import backtest
    syms = body.get("symbols") or [w["symbol"] for w in db.all("SELECT symbol FROM watchlist")]
    return backtest.run(body["strategy"], syms, int(body.get("days", 500)), body.get("params"))


@route("GET", "/api/report")
def api_report(q, body):
    return agent.daily_report(q.get("date"))


@route("GET", "/api/inbox")
def api_inbox(q, body):
    return agent.inbox()


@route("GET", "/api/journal")
def api_journal(q, body):
    return {"text": agent.journal_recent(30000, days=int(q.get("days", 7)))}


@route("POST", "/api/journal")
def api_journal_add(q, body):
    return {"saved": agent.journal_append(body["text"], body.get("author", "我"))}


@route("POST", "/api/wakeup/test")
def api_wakeup_test(q, body):
    from core import wakeup
    if not wakeup.enabled():
        raise ValueError("未设置 ai.wakeup_command")
    return {"ok": wakeup.trigger("网页手动测试唤醒", force=True)}


@route("GET", "/api/intel/(?P<sym>\\w+)")
def api_intel(q, body, sym):
    from core import news
    sym = market.normalize(sym)
    if MONITOR and MONITOR.intel.get(sym) and not q.get("fresh"):
        it = MONITOR.intel[sym]
    else:
        qq = market.quotes_cached([sym], 10).get(sym) or {}
        it = news.intel(sym, qq.get("name"), qq)
    qq = market.quotes_cached([sym], 10).get(sym)
    kl = agent.indicators.merge_live(agent.daily_kline(sym), qq, market.today_str())
    chk = news.distribution_check(qq, agent.indicators.summarize(kl), it) if qq else None
    link = None
    try:
        from core import global_market
        ind = news.industry(sym, (qq or {}).get("name", ""))
        link = dict(global_market.link_for(ind) or {}, industry=ind) if ind else None
    except Exception:
        pass
    return {"intel": it, "overnight": link, "pattern": {"type": "疑似借利好出货" if chk[0] == "sell" else "疑似利空洗盘",
                                     "score": chk[1], "reasons": chk[2]} if chk else None}


@route("GET", "/api/global")
def api_global(q, body):
    from core import global_market
    return global_market.overnight() or {"error": "外盘行情获取失败"}


@route("GET", "/api/flash")
def api_flash(q, body):
    from core import news
    return news.flash_news(int(q.get("n", 20)))


_screen_state = {"running": False, "msg": "", "result": None}


@route("POST", "/api/screen")
def api_screen(q, body):
    import threading
    from core import screener
    if _screen_state["running"]:
        return {"running": True, "msg": _screen_state["msg"]}

    def go():
        _screen_state.update(running=True, msg="选股中…")
        try:
            _screen_state["result"] = screener.run(progress=lambda m: _screen_state.update(msg=m))
            _screen_state["msg"] = "完成"
        except Exception as e:
            _screen_state["msg"] = "失败：%s" % e
        finally:
            _screen_state["running"] = False
    threading.Thread(target=go, daemon=True).start()
    return {"running": True, "msg": "已开始"}


@route("GET", "/api/screen")
def api_screen_get(q, body):
    from core import screener
    return {"running": _screen_state["running"], "msg": _screen_state["msg"],
            "latest": screener.latest_candidates(30)}


@route("GET", "/api/regime")
def api_regime(q, body):
    from core import screener
    try:
        return screener.market_regime()
    except Exception as e:
        lc = screener.latest_candidates(1)
        if lc and lc.get("regime"):
            return dict(lc["regime"], as_of="%s（实时获取失败，显示上次选股时的状态）" % lc["ts"])
        return {"error": "大盘状态获取失败：%s" % e}


@route("GET", "/api/committee")
def api_committee_list(q, body):
    rows = db.all("SELECT id,ts,symbol,name,account,intent,mode,trigger,status,decision,confidence,price,error "
                  "FROM committee_runs ORDER BY id DESC LIMIT 50")
    return fmt_rows(rows)


@route("GET", "/api/committee/(?P<rid>\\d+)")
def api_committee_get(q, body, rid):
    from core import committee
    r = committee.get_run(int(rid))
    if not r:
        raise ApiError("没有这个会议", 404)
    return fmt_rows([r])[0]


@route("POST", "/api/committee/run")
def api_committee_run(q, body):
    from core import committee
    if not llm.enabled():
        raise ValueError("内置大模型未启用。可让外部 AI 运行：python3 agent_cli.py committee start %s" % body["symbol"])
    return committee.run_async(body["symbol"], body.get("account", "paper"), body.get("intent", "open"),
                               trigger="网页手动")


@route("GET", "/api/plans")
def api_plans(q, body):
    from core import plans
    act = plans.get_active()
    qs = market.quotes_cached([p["symbol"] for p in act], 15)
    closed = db.all("SELECT * FROM plans WHERE status='closed' ORDER BY closed_at DESC LIMIT 30")
    for c in closed:
        c["outcome"] = db.jloads(c["outcome"], {})
    return {"active": [plans.brief(p, (qs.get(p["symbol"]) or {}).get("price")) for p in act],
            "closed": fmt_rows(closed)}


@route("PUT", "/api/plans")
def api_plan_update(q, body):
    from core import plans
    return {"plan_id": plans.update(body.get("account", "paper"), body["symbol"], body, author="网页")}


@route("GET", "/api/search")
def api_search(q, body):
    """按代码查名称（简易）。"""
    try:
        qq = market.get_quote(q.get("q", ""))
        return [{"symbol": qq["symbol"], "name": qq["name"], "price": qq["price"]}]
    except Exception:
        return []


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ai-stock/1.0"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, data, ctype="application/json; charset=utf-8", headers=None):
        if not isinstance(data, (bytes, bytearray)):
            data = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _client_ip(self):
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):   # 只信任本机反向代理（nginx）转发的真实 IP
            ip = (self.headers.get("X-Forwarded-For") or ip).split(",")[0].strip()
        return ip

    def _auth(self, method, u):
        """返回 True 表示已处理（登录相关或拦截），False 表示放行。"""
        if u.path == "/login":
            with open(os.path.join(WEB_DIR, "login.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return True
        if u.path == "/api/login" and method == "POST":
            ip = self._client_ip()
            if not auth.allowed(ip):
                self._send(429, {"error": "密码错误次数太多，请 10 分钟后再试"})
                return True
            try:
                pw = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}").get("password")
            except ValueError:
                pw = None
            if auth.check_password(pw):
                log.info("后台登录成功 %s", ip)
                self._send(200, {"ok": True}, headers={"Set-Cookie": auth.set_cookie_header(auth.make_session())})
            else:
                auth.record_fail(ip)
                log.warning("后台密码错误 %s", ip)
                self._send(401, {"error": "密码错误"})
            return True
        if u.path == "/api/logout":
            self._send(200, {"ok": True}, headers={"Set-Cookie": auth.clear_cookie_header()})
            return True
        if u.path.endswith(".js") and os.path.isfile(os.path.join(WEB_DIR, os.path.basename(u.path))):
            return False                    # 静态库（echarts）不需要登录
        if auth.valid(self.headers.get("Cookie")):
            return False
        if u.path.startswith("/api/"):
            self._send(401, {"error": "请先登录", "login": True})
        else:
            self._send(302, b"", "text/plain", headers={"Location": "/login"})
        return True

    def _handle(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if self._auth(method, u):
            return
        if not u.path.startswith("/api/"):
            name = os.path.basename(u.path)
            static = os.path.join(WEB_DIR, name)
            if name and name != "index.html" and os.path.isfile(static):
                ctype = {"js": "application/javascript", "css": "text/css", "svg": "image/svg+xml",
                         "png": "image/png"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
                with open(static, "rb") as f:
                    return self._send(200, f.read(), ctype)
            path = os.path.join(WEB_DIR, "index.html")
            with open(path, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        tok = config.load_config()["server"]["token"]
        if tok and self.headers.get("X-Token") != tok and q.get("token") != tok:
            return self._send(401, {"error": "需要 token"})
        body = {}
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, {"error": "JSON 格式错误"})
        for m, rx, fn in ROUTES:
            mm = rx.match(u.path)
            if m == method and mm:
                try:
                    return self._send(200, fn(q, body, **mm.groupdict()))
                except (ApiError,) as e:
                    return self._send(e.code, {"error": str(e)})
                except (broker.OrderError, ValueError, KeyError, RuntimeError) as e:
                    return self._send(400, {"error": str(e) if not isinstance(e, KeyError) else "缺少参数 %s" % e})
                except Exception as e:
                    log.error("API 错误 %s\n%s", u.path, traceback.format_exc())
                    return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def setup_logging():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(os.path.join(config.DATA_DIR, "app.log"), encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)


def main():
    global MONITOR
    ap = argparse.ArgumentParser()
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-monitor", action="store_true", help="只开网页，不跑实时监控")
    args = ap.parse_args()
    setup_logging()
    db.init_db()
    cfg = config.load_config()
    host = args.host or cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]
    if not args.no_monitor:
        MONITOR = Monitor()
        MONITOR.start()
    srv = ThreadingHTTPServer((host, port), Handler)
    log.info("网页控制台: http://%s:%d", "127.0.0.1" if host == "0.0.0.0" else host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("退出")


if __name__ == "__main__":
    main()
