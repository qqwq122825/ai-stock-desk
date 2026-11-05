"""离线单元测试（不联网，用假行情）。运行：python3 -m unittest discover tests -v"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

TMP = tempfile.mkdtemp(prefix="aistock_test_")
os.environ["AI_STOCK_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent, broker, config, db, indicators, market, rules  # noqa: E402

PRICES = {}


def fake_quote(sym, price=10.0, pct=1.0, name=None, prev=None, vol_ratio=1.0, bid1=None, ask1=None):
    sym = market.normalize(sym)
    prev = prev or round(price / (1 + pct / 100), 2)
    b1 = price - 0.01 if bid1 is None else bid1
    a1 = price + 0.01 if ask1 is None else ask1
    return {"symbol": sym, "code": sym[2:], "name": name or ("测试" + sym[2:]), "price": price,
            "prev_close": prev, "open": prev, "volume": 1000000, "time": market.now().strftime("%Y%m%d%H%M%S"),
            "change": price - prev, "pct": pct, "high": price, "low": prev, "amount": price * 1e6,
            "turnover": 1, "pe": 10, "circ_mv": 100, "total_mv": 100, "pb": 1,
            "limit_up": round(prev * 1.1, 2), "limit_down": round(prev * 0.9, 2), "vol_ratio": vol_ratio,
            "bids": [[b1, 500]] + [[b1 - 0.01 * i, 100] for i in range(1, 5)],
            "asks": [[a1, 500]] + [[a1 + 0.01 * i, 100] for i in range(1, 5)],
            "bid1": b1, "ask1": a1, "suspended": False, "fetched_at": time.time(),
            "date": market.today_str()}


def fake_get_quotes(symbols):
    out = {}
    for s in symbols:
        s = market.normalize(s)
        out[s] = PRICES.get(s) or fake_quote(s)
    return out


def fake_kline(symbol, period="day", count=120):
    base = 10.0
    return [{"t": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28), "o": base + i * 0.01, "c": base + i * 0.01 + 0.005,
             "h": base + i * 0.01 + 0.05, "l": base + i * 0.01 - 0.05, "v": 10000} for i in range(count)]


market.get_quotes = fake_get_quotes
REAL_GET_KLINE = market.get_kline
market.get_kline = fake_kline
market.is_trading_day = lambda dt=None: True
market.is_market_open = lambda dt=None: True
import core.news as news  # noqa: E402
news.intel_many = lambda syms, names=None, quotes=None, workers=6: {}
news.flash_news = lambda n=20: [{"time": "11-04 10:00", "text": "测试快讯"}]
news.fundamentals = lambda sym: {"industry": "测试行业", "financials": []}
news.intel = lambda sym, name=None, quote=None, news_n=8: {"news_today": {"count": 0, "pos": 0, "neg": 0}}
import core.screener as screener  # noqa: E402
REGIME = {"state": "强势", "position_cap": 0.9, "score": 3}
screener.market_regime = lambda ttl=120: dict(REGIME)
screener.sectors = lambda ttl=120: {}
news.industry = lambda sym, name="": "半导体" if sym.endswith("000001") else ""
import core.emotion as emotion  # noqa: E402
REAL_POOL = emotion.pool
POOLS = {"zt": [], "zb": [], "dt": [], "yzt": []}
POOLS_BY_DATE = {}
emotion.pool = lambda kind, date=None: list((POOLS_BY_DATE.get(date) or POOLS)[kind])
emotion._last_trade_date = lambda: market.now().strftime("%Y%m%d")
import core.global_market as gm  # noqa: E402
REAL_OVERNIGHT = gm.overnight


def fake_ov(**pcts):
    base = {"usDJI": 0.2, "usIXIC": 1.85, "usINX": 0.5, "usSOXX": -3.1, "usNVDA": -1.7, "usAAPL": 6.5,
            "usTSLA": -3.5, "usHXC": 4.1, "hkHSI": 1.7, "hkHSTECH": 1.8}
    base.update(pcts)
    q = {k: {"name": gm.NAMES[k], "price": 1, "prev": 1, "pct": v, "time": ""} for k, v in base.items()}
    return {"quotes": q, "summary": gm._summary(q), "fetched": "test"}


gm.overnight = lambda ttl=600: fake_ov()
import core.notify as notify  # noqa: E402
SENT = []
notify.send = lambda t, c: (SENT.append((t, c)) or (True, "test"))
agent.notify.send = notify.send


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        config.save_config({"paper": {"slippage_pct": 0, "initial_cash": 10000},
                            "committee": {"required_for_buy": False},
                            "style": {"paper": "swing", "live": "swing"}})   # 老用例按波段；情绪短线见 TestMomentum
        REGIME.update(state="强势", position_cap=0.9)
        db.init_db()
        PRICES.clear()
        SENT.clear()
        agent._kcache.clear()


class TestFeesAndLots(Base):
    def test_fees(self):
        # 买 2000 元：佣金最低5 + 过户费0.02
        self.assertAlmostEqual(broker.calc_fees("sz000001", "buy", 2000), 5.02, 2)
        # 卖 100000：佣金25 + 印花税50 + 过户1
        self.assertAlmostEqual(broker.calc_fees("sz000001", "sell", 100000), 76.0, 2)
        # ETF 免印花税和过户费
        self.assertAlmostEqual(broker.calc_fees("sh510300", "sell", 100000), 25.0, 2)

    def test_lots(self):
        self.assertEqual(broker.round_buy_shares("sz000001", 250), 200)
        self.assertEqual(broker.round_buy_shares("sz000001", 99), 0)
        self.assertEqual(broker.round_buy_shares("sh688001", 199), 0)
        self.assertEqual(broker.round_buy_shares("sh688001", 253), 253)

    def test_normalize(self):
        self.assertEqual(market.normalize("600519"), "sh600519")
        self.assertEqual(market.normalize("000001"), "sz000001")
        self.assertEqual(market.normalize("300750"), "sz300750")
        self.assertEqual(market.normalize("510300"), "sh510300")
        self.assertEqual(market.normalize("600519.SH"), "sh600519")
        self.assertEqual(market.normalize("920001"), "bj920001")


class TestPaper(Base):
    def test_buy_sell_t1(self):
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        t = broker.paper_execute("000001", "buy", 300)
        self.assertEqual(t["shares"], 300)
        self.assertAlmostEqual(t["price"], 10.01)
        s = broker.snapshot("paper")
        self.assertAlmostEqual(s["cash"], 10000 - 3003 - 5.03, 2)
        with self.assertRaises(broker.OrderError):     # T+1
            broker.paper_execute("000001", "sell", 100)
        # 模拟到了第二天
        db.run("UPDATE positions SET last_buy_date='2000-01-01'")
        t2 = broker.paper_execute("000001", "sell", 300)
        self.assertEqual(t2["shares"], 300)
        self.assertLess(t2["pnl"], 0)   # 买卖价差+费用
        self.assertEqual(broker.get_positions("paper"), [])

    def test_insufficient_cash_and_limit_up(self):
        PRICES["sh600519"] = fake_quote("600519", 1446.0)
        with self.assertRaises(broker.OrderError):
            broker.paper_execute("600519", "buy", 100)
        PRICES["sz000002"] = fake_quote("000002", 11.0, pct=10, ask1=0)
        with self.assertRaises(broker.OrderError) as e:
            broker.paper_execute("000002", "buy", 100)
        self.assertIn("涨停", str(e.exception))


class TestIndicators(Base):
    def test_basic(self):
        kl = fake_kline("x", count=120)
        s = indicators.summarize(kl)
        self.assertEqual(s["trend"], "多头排列")
        self.assertGreater(s["rsi14"], 50)
        self.assertIn("kdj", s)
        self.assertIsNotNone(s["macd"])

    def test_kdj_cross(self):
        kl = [{"t": str(i), "o": 10, "c": 10 - i * 0.1, "h": 10.1, "l": 9 - i * 0.1, "v": 1} for i in range(20)]
        kl.append({"t": "x", "o": 8, "c": 10.5, "h": 10.6, "l": 8, "v": 1})
        k = indicators.kdj(kl)
        self.assertEqual(k["cross"], "金叉")


class TestRules(Base):
    def test_edge_and_cooldown(self):
        rules.add_rule("000001", "price_above", {"price": 10.5}, cooldown_min=0)
        eng = rules.RuleEngine()
        q = fake_quote("000001", 10.6)
        args = ({"sz000001": q}, ["sz000001"], [], lambda s: [], lambda s: [])
        n0 = len([a for a in eng.evaluate(*args) if a["rule"]["type"] == "price_above"])
        n1 = len([a for a in eng.evaluate(*args) if a["rule"]["type"] == "price_above"])
        self.assertEqual((n0, n1), (1, 0))  # 第二次不重复触发
        q2 = fake_quote("000001", 10.0)
        eng.evaluate({"sz000001": q2}, ["sz000001"], [], lambda s: [], lambda s: [])
        n2 = len([a for a in eng.evaluate(*args) if a["rule"]["type"] == "price_above"])
        self.assertEqual(n2, 1)  # 回落后再突破再次触发

    def test_stop_loss_auto_sell(self):
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        broker.paper_execute("000001", "buy", 500)
        db.run("UPDATE positions SET last_buy_date='2000-01-01'")
        db.run("DELETE FROM rules")
        rules.add_rule("*POS", "stop_loss", {"pct": -5}, action="auto_sell", cooldown_min=0)
        PRICES["sz000001"] = fake_quote("000001", 9.3, pct=-7)
        al = rules.RuleEngine().evaluate({"sz000001": PRICES["sz000001"]}, [], broker.get_positions(),
                                         lambda s: [], lambda s: [])
        self.assertEqual(len(al), 1)
        res = rules.run_action(al[0])
        self.assertIn("自动卖出", res)
        self.assertEqual(broker.get_positions("paper"), [])


class TestAgent(Base):
    def test_context_and_decision_paper(self):
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        ctx = agent.build_context(with_ticks=False)
        self.assertIn("sz000001", ctx["stocks"])
        self.assertIn("paper", ctx["accounts"])
        json.dumps(ctx, ensure_ascii=False, default=str)
        res = agent.apply_decision({
            "market_view": "测试", "actions": [
                {"symbol": "000001", "action": "buy", "position_pct": 0.3, "confidence": 0.8, "reason": "测试买"},
                {"symbol": "600036", "action": "buy", "shares": 100, "confidence": 0.3, "reason": "低置信"},
                {"symbol": "000333", "action": "sell", "confidence": 0.9, "reason": "无持仓"}],
            "rules_add": [{"symbol": "000001", "type": "price_below", "params": {"price": 9.5}}],
            "watchlist_add": [{"symbol": "601318", "note": "AI加"}]})
        st = [a["status"] for a in res["actions"]]
        self.assertEqual(st, ["filled", "rejected", "rejected"])
        self.assertEqual(res["actions"][0]["trade"]["shares"], 200)  # 3000/10.01 → 299 → 200
        self.assertTrue(db.one("SELECT 1 FROM watchlist WHERE symbol='sh601318'"))
        self.assertTrue(SENT)

    def test_position_cap(self):
        config.save_config({"risk": {"max_position_pct": 0.2}})
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        res = agent.apply_decision({"actions": [{"symbol": "000001", "action": "buy", "position_pct": 0.9,
                                                 "confidence": 0.9}]})
        self.assertEqual(res["actions"][0]["trade"]["shares"], 100)  # 上限2000元 → 199股 → 100
        config.save_config({"risk": {"max_position_pct": 0.4}})

    def test_live_pending_confirm_fill(self):
        config.save_config({"ai": {"accounts": ["paper", "live"]}})
        broker.set_live_account(cash=50000)
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        res = agent.apply_decision({"actions": [{"symbol": "000001", "action": "buy", "amount": 5000,
                                                 "confidence": 0.9, "accounts": ["live"]}]})
        a = res["actions"][0]
        self.assertEqual(a["status"], "pending_confirm")
        o = broker.confirm_order(a["order_id"])
        self.assertEqual(o["status"], "confirmed")
        t = broker.fill_order(o["id"], 10.02)
        self.assertEqual(t["shares"], 400)
        self.assertEqual(broker.get_positions("live")[0]["shares"], 400)
        config.save_config({"ai": {"accounts": ["paper"]}})

    def test_ticks_file(self):
        q = fake_quote("000001", 10.0)
        agent.append_tick(q)
        q2 = dict(q, price=10.2, fetched_at=q["fetched_at"] + 120, volume=q["volume"] + 50000)
        agent.append_tick(q2)
        t = agent.read_ticks("000001")
        self.assertEqual(len(t), 2)
        self.assertEqual(agent.tick_stats("000001")["chg_1m_pct"], 2.0)


class TestStrategies(Base):
    def test_discover_and_run(self):
        from core.engine import StrategyRunner
        sr = StrategyRunner()
        names = {d["name"] for d in sr.describe()}
        self.assertTrue({"macd", "kdj", "rsi", "resonance", "orderbook", "tick_momentum"} <= names)
        q = fake_quote("000001", 10.0)
        kl = fake_kline("x", count=120)
        sigs = sr.run("sz000001", q, kl, indicators.summarize(kl), [q])
        self.assertIsInstance(sigs, list)


class TestSizing(Base):
    def _buy(self, **act):
        db.run("DELETE FROM quotes")   # 清掉报价缓存，用最新假行情
        a = dict(symbol="000001", action="buy", confidence=0.9)
        a.update(act)
        return agent.apply_decision({"actions": [a]})["actions"][0]

    def test_risk_based(self):
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        a = self._buy(stop_loss=9.5)               # 200元 ÷ (10.01-9.5) = 392 → 300股
        self.assertEqual(a["status"], "filled", a)
        self.assertEqual(a["trade"]["shares"], 300)
        self.assertIn("单笔风险 200元", a["sizing"])
        from core import plans
        self.assertEqual(plans.get_active("paper", "000001")[0]["stop_loss"], 9.5)

    def test_far_stop_rejected(self):
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        a = self._buy(stop_loss=5.0)               # 200 ÷ 5.01 = 39股 < 一手
        self.assertEqual(a["status"], "rejected")
        self.assertIn("不足一手", a["error"])
        self.assertIn("止损设近", a["error"])

    def test_no_martingale_and_pyramid(self):
        from core import plans
        config.save_config({"risk": {"max_position_pct": 0.6}})   # 默认40%上限下，1万资金加不了一手
        self.addCleanup(config.save_config, {"risk": {"max_position_pct": 0.4}})
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        self.assertEqual(self._buy(stop_loss=9.5)["trade"]["shares"], 300)
        PRICES["sz000001"] = fake_quote("000001", 9.6, pct=-4)
        a = self._buy()
        self.assertIn("禁止亏损补仓", a["error"])
        PRICES["sz000001"] = fake_quote("000001", 10.4, pct=4)     # 浮盈 ~3.5% < 8%
        self.assertIn("浮盈", self._buy()["error"])
        PRICES["sz000001"] = fake_quote("000001", 11.0, pct=3)     # 浮盈 ~9.7%
        a = self._buy(position_pct=0.4)
        self.assertEqual(a["status"], "filled", a)
        self.assertEqual(a["trade"]["shares"], 100)               # 300×50%=150 → 100
        self.assertIn("金字塔第1次加仓", a["sizing"])
        p = plans.get_active("paper", "000001")[0]
        pos = broker.get_positions("paper")[0]
        self.assertAlmostEqual(p["stop_loss"], round(pos["avg_cost"], 3))   # 止损上移到保本
        config.save_config({"sizing": {"pyramid_max_adds": 1}})
        PRICES["sz000001"] = fake_quote("000001", 12.5, pct=3)
        self.assertIn("达到上限", self._buy()["error"])
        config.save_config({"sizing": {"pyramid_max_adds": 2}})

    def test_ai_cannot_lower_stop(self):
        from core import plans
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        self._buy(stop_loss=9.5)
        res = agent.apply_decision({"plans": [{"symbol": "000001", "stop_loss": 9.0}]}, agent="ai")
        self.assertIn("止损只能上移", res["plans"][0])
        plans.update("paper", "000001", {"stop_loss": 9.0}, author="网页")
        self.assertEqual(plans.get_active("paper", "000001")[0]["stop_loss"], 9.0)

    def test_percent_mode(self):
        config.save_config({"sizing": {"mode": "percent"}})
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        a = self._buy(position_pct=0.3, stop_loss=5.0)           # 百分比模式不受止损距离影响
        self.assertEqual(a["trade"]["shares"], 200)
        config.save_config({"sizing": {"mode": "risk"}})


class TestGlobal(Base):
    def test_links_and_divergence(self):
        ov = fake_ov()
        chip = gm.link_for("半导体", ov)
        self.assertEqual(chip["chain"], "半导体链")
        self.assertLess(chip["avg_pct"], -2)
        self.assertIn("承压", chip["hint"])
        self.assertEqual(gm.link_for("消费电子", ov)["chain"], "消费电子/苹果链")
        self.assertIsNone(gm.link_for("银行", ov))
        self.assertTrue(any("分化" in n for n in ov["summary"]["notes"]))   # 纳指涨、半导体跌

    def test_regime_adjust_morning_only(self):
        ov = fake_ov(usINX=-3.0)
        real_now = market.now
        try:
            market.now = lambda: real_now().replace(hour=9, minute=40)
            d, reason = gm.regime_adjust(ov)
            self.assertEqual(d, -1)
            self.assertIn("外盘大跌", reason)
            market.now = lambda: real_now().replace(hour=14, minute=0)
            self.assertEqual(gm.regime_adjust(ov), (0, None))   # 午后不再修正
        finally:
            market.now = real_now

    def test_context_has_overnight(self):
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        ctx = agent.build_context(with_ticks=False)
        self.assertIn("费城半导体ETF", ctx["global_overnight"]["quotes"])
        self.assertEqual(ctx["stocks"]["sz000001"]["overnight_link"]["chain"], "半导体链")
        self.assertNotIn("overnight_link", ctx["stocks"]["sh600036"])

    def test_signal_push_filter(self):
        from core.engine import Monitor
        m = Monitor()
        m.strats.instances = {}
        sent = []
        old_send = notify.send
        self.addCleanup(setattr, notify, "send", old_send)
        notify.send = lambda t, c: (sent.append(c) or (True, "t"))

        class Fake:
            label = "测试"
            cooldown_min = 0

            def on_bar(self, *a):
                from strategies import Signal
                return [Signal("watch", "只关注不推送", 0.9), Signal("buy", "强买入要推送", 0.7)]

            def on_tick(self, *a):
                return None
        m.strats.instances = {"fake": (Fake(), {})}
        q = fake_quote("000001", 10.0)
        m.evaluate(config.load_config(), {"sz000001": q}, [], [], ["sz000001"])
        self.assertEqual(len(sent), 1)
        self.assertIn("强买入要推送", sent[0])
        self.assertNotIn("只关注不推送", sent[0])
        self.assertEqual(db.one("SELECT COUNT(*) n FROM alerts WHERE strategy='fake'")["n"], 2)  # 都记录了


class TestMaintenance(Base):
    def test_health_report(self):
        from core import health
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        agent.apply_decision({"actions": [{"symbol": "300750", "action": "buy", "confidence": 0.9}]})
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(health.LOG_PATH, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write("%s,001 ERROR [engine] 监控循环出错 boom 123\n%s,002 ERROR [engine] 监控循环出错 boom 456\n" % (ts, ts))
        r = health.report(24)
        self.assertEqual(r["log"]["errors"][0][1], 2)          # 两条同类错误合并
        self.assertEqual(r["decisions"]["rejected_reasons"][0][1], 1)
        self.assertTrue(any("推送未配置" in i for i in r["issues"]))
        json.dumps(r, ensure_ascii=False, default=str)

    def test_restart_only_offhours(self):
        from core import engine, health
        health.request_restart("测试")
        self.assertTrue(os.path.exists(engine.RESTART_FLAG))
        calls = []
        old = os.execv
        os.execv = lambda *a: calls.append(a)
        old_sess = market.is_trading_session
        try:
            m = engine.Monitor()
            market.is_trading_session = lambda dt=None: True
            m.check_restart(market.now())
            self.assertEqual(calls, [])                             # 交易时段不重启
            self.assertTrue(os.path.exists(engine.RESTART_FLAG))
            market.is_trading_session = lambda dt=None: False
            m.check_restart(market.now())
            self.assertEqual(len(calls), 1)                         # 非交易时段重启
            self.assertFalse(os.path.exists(engine.RESTART_FLAG))   # 标记清掉，不会循环重启
        finally:
            os.execv = old
            market.is_trading_session = old_sess


class TestLogin(Base):
    def setUp(self):
        super().setUp()
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        import app as app_mod
        from core import auth
        auth._fails.clear()
        self.password = "fixture-login-value"
        original_password = auth.PASSWORD
        auth.PASSWORD = self.password
        self.addCleanup(setattr, auth, "PASSWORD", original_password)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app_mod.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.ur = urllib.request

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        self.opener = urllib.request.build_opener(NoRedirect)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, path, data=None, cookie=None, method=None):
        r = self.ur.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                            headers=dict({"Content-Type": "application/json"}, **({"Cookie": cookie} if cookie else {})),
                            method=method)
        try:
            resp = self.opener.open(r, timeout=5)
            return resp.status, resp.read(), resp.headers
        except self.ur.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_login_flow(self):
        code, body, _ = self.req("/api/status")
        self.assertEqual(code, 401)
        self.assertTrue(json.loads(body)["login"])
        code, _, h = self.req("/")
        self.assertEqual((code, h["Location"]), (302, "/login"))
        code, body, _ = self.req("/login")
        self.assertEqual(code, 200)
        self.assertIn("后台密码".encode(), body)
        code, _, _ = self.req("/api/login", {"password": "wrong"})
        self.assertEqual(code, 401)
        code, body, h = self.req("/api/login", {"password": self.password})
        self.assertEqual(code, 200)
        cookie = h["Set-Cookie"].split(";")[0]
        code, body, _ = self.req("/api/config", cookie=cookie)
        self.assertEqual(code, 200)
        code, _, _ = self.req("/", cookie=cookie)
        self.assertEqual(code, 200)
        code, _, _ = self.req("/api/config", cookie=cookie[:-3] + "abc")   # 篡改签名
        self.assertEqual(code, 401)

    def test_missing_password_fails_closed(self):
        from core import auth
        auth.PASSWORD = ""
        self.assertEqual(self.req("/api/login", {"password": ""})[0], 401)
        self.assertEqual(self.req("/api/login", {"password": self.password})[0], 401)

    def test_bruteforce_lockout(self):
        for _ in range(5):
            self.assertEqual(self.req("/api/login", {"password": "x"})[0], 401)
        self.assertEqual(self.req("/api/login", {"password": self.password})[0], 429)   # 锁定期间正确密码也不行


class TestDailyClose(Base):
    def test_daily_flow(self):
        from core import daily, committee, llm
        cands = [{"rank": i + 1, "symbol": s, "name": n, "score": 9 - i, "price": 10, "pct": 1, "reasons": []}
                 for i, (s, n) in enumerate([("sz000001", "平安"), ("sh600036", "招行"), ("sh600900", "长电"),
                                             ("sz000333", "美的")])]
        old = (screener.run, committee.run_builtin, llm.enabled)
        reviewed = []

        def fake_run(sym, acct, intent, trigger=""):
            reviewed.append(sym)
            ok = sym == "sz000001"
            rid = db.run("INSERT INTO committee_runs(ts,symbol,status,finished_at,final) VALUES(?,?,?,?,?)",
                         (time.time(), sym, "approved" if ok else "rejected", time.time(), "{}"))
            return {"run_id": rid, "status": "approved" if ok else "rejected", "decision": "buy" if ok else "pass",
                    "summary": "测试", "entry_max": 10.2, "stop_loss": 9.4, "target": 11.5,
                    "gate_notes": [] if ok else ["风控官否决：测试"]}
        screener.run = lambda **k: {"screen_id": 1, "candidates": cands, "regime": {"state": "震荡", "position_cap": 0.5}}
        committee.run_builtin = fake_run
        llm.enabled = lambda: True
        try:
            PRICES["sh600900"] = fake_quote("600900", 10.0)
            broker.paper_execute("600900", "buy", 100)          # 已持有的跳过
            r = daily.run_daily_close(force=True)
        finally:
            screener.run, committee.run_builtin, llm.enabled = old
        self.assertEqual(reviewed, ["sz000001", "sh600036", "sz000333"])   # 前3只（跳过持仓的长电）
        self.assertEqual([a["symbol"] for a in r["approved"]], ["sz000001"])
        w = db.one("SELECT note FROM watchlist WHERE symbol='sz000001'")
        self.assertIn("委员会", w["note"])
        self.assertIn("入场≤10.2", w["note"])
        self.assertTrue(any("收盘总结" in t or "通过" in c for t, c in [(x[0], x[1]) for x in SENT]))

    def test_accounts_are_separate(self):
        """模拟盘和实盘：选股结果、委员会结论、收盘流程互不共用。"""
        from core import daily, committee, llm
        db.init_db()
        sid = db.run("INSERT INTO screens(ts,regime,stats,account) VALUES(?,?,?,?)", (time.time(), "{}", "{}", "paper"))
        db.run("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)", (sid, 1, "sz000001", "平安", "main", "", 9, 10, 1, "{}"))
        self.assertEqual(len(screener.latest_candidates(10, "paper")["candidates"]), 1)
        self.assertIsNone(screener.latest_candidates(10, "live"))
        db.run("INSERT INTO committee_runs(ts,symbol,account,status,finished_at,final) VALUES(?,?,?,?,?,?)",
               (time.time(), "sz000001", "paper", "approved", time.time(), "{}"))
        self.assertIsNotNone(committee.approved_run("sz000001", "paper"))
        self.assertIsNone(committee.approved_run("sz000001", "live"))     # 模拟盘的通过不解锁实盘
        # 收盘流程只跑交给 AI 的账户
        seen = []
        old = (screener.run, committee.run_builtin, llm.enabled)
        screener.run = lambda **k: seen.append(k.get("account")) or {
            "screen_id": 1, "candidates": [], "regime": {"state": "震荡", "position_cap": 0.5}}
        llm.enabled = lambda: True
        try:
            daily.run_daily_close(force=True)
            self.assertEqual(seen, ["paper"])
            config.save_config({"ai": {"accounts": ["paper", "live"]}})
            daily.run_daily_close(force=True)
            self.assertEqual(seen, ["paper", "paper", "live"])
        finally:
            screener.run, committee.run_builtin, llm.enabled = old
            config.save_config({"ai": {"accounts": ["paper"]}})

    def test_screen_live_without_cash_and_budget(self):
        """实盘没录资金时给清楚的提示；日K超出时间预算时跳过剩下的，不会一直卡住。"""
        with self.assertRaises(ValueError) as cm:
            screener.run(account="live")
        self.assertIn("录入", str(cm.exception))
        self.assertFalse(screener.STATE["running"])
        universe = [{"symbol": "sz%06d" % i, "name": "股%d" % i, "price": 10, "pct": 1, "amount": 5e8, "turnover": 2,
                     "pe": 10, "vol_ratio": 1, "high": 10, "low": 9.5, "open": 9.8, "prev_close": 9.9, "total_mv": 1e10,
                     "pb": 1, "list_date": "20100101", "main_net": 0, "main_pct": 0, "industry": "x", "board": "main"}
                    for i in range(1, 31)]
        old = (screener.snapshot_all, screener.KLINE_BUDGET_SEC, screener._kline_cached, news.money_flow_daily,
               news.announcements)
        calls = []

        def slow(sym):
            calls.append(sym)
            time.sleep(0.3)
            return fake_kline(sym, "day", 130)
        screener.snapshot_all = lambda ttl=60: {"stocks": universe, "etfs": []}
        screener.KLINE_BUDGET_SEC = 0.5
        screener._kline_cached = slow
        news.money_flow_daily = lambda s, n=5: []
        news.announcements = lambda s, n=8: []
        try:
            t0 = time.time()
            res = screener.run(account="paper")
        finally:
            (screener.snapshot_all, screener.KLINE_BUDGET_SEC, screener._kline_cached, news.money_flow_daily,
             news.announcements) = old
        self.assertLess(time.time() - t0, 3)
        self.assertGreater(res["stats"]["kline_skipped"], 0)
        self.assertLess(len(calls), 30)
        self.assertEqual(res["account"], "paper")
        self.assertEqual(screener.STATE["stage"], "完成")

    def test_icepoint_skips_committee(self):
        from core import daily, committee, llm
        old = (screener.run, committee.run_builtin, llm.enabled)
        screener.run = lambda **k: {"screen_id": 1, "candidates": [{"rank": 1, "symbol": "sz000001", "name": "x",
                                                                      "score": 9, "price": 1, "pct": 0, "reasons": []}],
                                    "regime": {"state": "冰点", "position_cap": 0.0}}
        committee.run_builtin = lambda *a, **k: self.fail("冰点不应开委员会")
        llm.enabled = lambda: True
        try:
            r = daily.run_daily_close(force=True)
        finally:
            screener.run, committee.run_builtin, llm.enabled = old
        self.assertEqual(r["reviewed"], [])


class TestKlineFallback(Base):
    def test_fallback_and_breaker(self):
        calls = []

        def bad(*a):
            calls.append("t")
            raise OSError("HTTP Error 501: Not Implemented")

        def good(*a):
            calls.append("e")
            return [{"t": "2026-11-04", "o": 1, "c": 1, "h": 1, "l": 1, "v": 1}]
        old = (market._kline_tencent, market._kline_eastmoney)
        market._kline_tencent, market._kline_eastmoney = bad, good
        market._down.clear()
        try:
            self.assertEqual(len(REAL_GET_KLINE("600900")), 1)
            self.assertEqual(len(REAL_GET_KLINE("600900")), 1)
            self.assertEqual(calls, ["t", "e", "e"])   # 腾讯熔断后第二次直接走东财
        finally:
            market._kline_tencent, market._kline_eastmoney = old
            market._down.clear()


class TestNewFeatures(Base):
    def test_backtest(self):
        from core import backtest
        import math
        # 正弦波走势：均线策略应该有若干笔交易
        kl = []
        for i in range(400):
            c = 10 + 2 * math.sin(i / 15.0) + i * 0.002
            kl.append({"t": "D%04d" % i, "o": c - 0.02, "c": c, "h": c + 0.1, "l": c - 0.1, "v": 10000 + (i % 7) * 500})
        r = backtest.run("ma_cross", ["000001"], 400, kline_fn=lambda s: kl)
        x = r["results"][0]
        self.assertNotIn("error", x)
        self.assertGreaterEqual(x["trades"], 3)
        self.assertLessEqual(x["max_drawdown_pct"], 0)
        self.assertEqual(r["summary"]["symbols"], 1)
        buys = [t for t in x["trade_log"] if t["side"] == "buy"]
        self.assertTrue(all(t["shares"] % 100 == 0 for t in buys))
        with self.assertRaises(ValueError):
            backtest.run("orderbook", ["000001"], kline_fn=lambda s: kl)

    def test_inbox_journal_report(self):
        db.run("INSERT INTO alerts(ts,source,symbol,name,type,side,strength,message,price) VALUES(?,?,?,?,?,?,?,?,?)",
               (time.time(), "rule", "sz000001", "平安", "pct_above", "watch", 1, "大涨", 10))
        ib = agent.inbox()
        self.assertEqual(len(ib["new_alerts"]), 1)
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        agent.apply_decision({"market_view": "x", "actions": [], "journal": "今天观望，等回调"}, agent="t")
        self.assertEqual(len(agent.inbox()["new_alerts"]), 0)   # 决策之后 inbox 清空
        self.assertIn("等回调", agent.journal_recent())
        self.assertIn("等回调", agent.build_context(with_ticks=False)["journal_recent"])
        rep = agent.daily_report()
        self.assertEqual(rep["decisions"], 1)

    def test_wakeup(self):
        from core import wakeup
        out = os.path.join(TMP, "woke.txt")
        config.save_config({"ai": {"wakeup_command": 'echo "$AI_TRIGGER|$AI_SYMBOLS" > "%s"' % out}})
        self.assertTrue(wakeup.trigger("测试异动", ["sz000001"]))
        self.assertFalse(wakeup.trigger("冷却中", ["sz000001"]))   # 冷却
        wakeup._state["proc"].wait(5)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "测试异动|sz000001")
        config.save_config({"ai": {"wakeup_command": ""}})


class TestAShare(Base):
    def _q(self, **kw):
        q = fake_quote("000001", 11.0, pct=4.0, prev=10.58, vol_ratio=2.6)
        q.update(open=10.95, high=11.4, low=10.9, amount=11.15 * 3e6, volume=3e6)   # vwap≈11.15
        q.update(kw)
        return q

    def test_distribution_detected(self):
        it = {"news_today": {"count": 2, "pos": 2, "neg": 0},
              "flow_today": {"main_yi": -1.2, "small_yi": 0.9, "main_last30m_yi": -0.3}}
        r = news.distribution_check(self._q(), {"chg20d_pct": 32}, it)
        self.assertIsNotNone(r)
        self.assertEqual(r[0], "sell")
        self.assertTrue(any("主力净流出" in x for x in r[2]))
        self.assertTrue(any("散户接盘" in x for x in r[2]))

    def test_healthy_rally_not_flagged(self):
        # 利好 + 上涨 + 主力净流入 + 收在均价上方 → 不报出货
        it = {"news_today": {"count": 1, "pos": 1, "neg": 0}, "flow_today": {"main_yi": 2.0, "small_yi": -0.8}}
        q = self._q(price=11.38, high=11.4)
        self.assertIsNone(news.distribution_check(q, {"chg20d_pct": 5}, it))

    def test_washout(self):
        it = {"news_today": {"count": 1, "pos": 0, "neg": 1}, "flow_today": {"main_yi": 0.6, "small_yi": -0.4}}
        q = fake_quote("000001", 10.55, pct=-0.3, prev=10.58)
        q.update(open=10.2, high=10.6, low=10.1, amount=10.4 * 1e6, volume=1e6)
        r = news.distribution_check(q, {}, it)
        self.assertEqual(r[0], "watch")

    def test_tags_and_strategy(self):
        self.assertEqual(news.tag("关于控股股东减持股份的预披露公告")[0], "利空")
        self.assertEqual(news.tag("公司中标5亿元订单")[0], "利好")
        from core.engine import StrategyRunner
        sr = StrategyRunner()
        it = {"news_today": {"count": 2, "pos": 2, "neg": 0},
              "flow_today": {"main_yi": -1.2, "small_yi": 0.9, "main_last30m_yi": -0.3}}
        q = dict(self._q(), intel=it)
        kl = fake_kline("x", count=120)
        sigs = [s for s in sr.run("sz000001", q, kl, {"chg20d_pct": 32}, []) if s["strategy"] == "distribution"]
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["side"], "sell")
        self.assertIn("出货", sigs[0]["message"])

    def test_context_has_flash(self):
        ctx = agent.build_context(with_ticks=False)
        self.assertEqual(ctx["flash_news"][0], "11-04 10:00 测试快讯")


class TestCommitteeAndPlans(Base):
    OPS = {"stance": "多", "score": 3, "confidence": 0.7, "summary": "s", "points": ["p"]}
    FINAL = {"stance": "多", "score": 3, "confidence": 0.75, "summary": "可以买", "points": [], "decision": "buy",
             "position_pct": 0.3, "entry_max": 10.2, "stop_loss": 9.4, "target": 11.5, "trailing_pct": 6,
             "max_hold_days": 15, "thesis": "放量突破10元平台",
             "invalidation": [{"type": "price_below", "price": 9.8, "desc": "跌回平台下方"}],
             "watch_points": ["关注能否站稳10元"]}

    def _external_run(self, final=None, veto=False):
        from core import committee
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        r = committee.start_external("000001")
        rid = r["run_id"]
        self.assertTrue(os.path.exists(os.path.join(config.BASE_DIR, r["dossier_file"])) or
                        os.path.exists(r["dossier"] and committee.get_run(rid)["dossier_path"]))
        for role in committee.ORDER[:-1]:
            op = dict(self.OPS)
            if role == "risk":
                op.update(veto=veto, veto_reason="测试否决" if veto else "", max_position_pct=0.25)
            committee.save_opinion(rid, role, op)
        return rid, committee.finalize(rid, final or dict(self.FINAL))

    def test_roles_file(self):
        from core import committee
        roles = committee.load_roles()
        for r in committee.ORDER + ["_common"]:
            self.assertIn(r, roles, r)
        self.assertEqual(len(committee.ORDER), 11)

    def test_gate_blocks_then_allows(self):
        config.save_config({"committee": {"required_for_buy": True}})
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        res = agent.apply_decision({"actions": [{"symbol": "000001", "action": "buy", "position_pct": 0.3,
                                                 "confidence": 0.9}]})
        self.assertEqual(res["actions"][0]["status"], "rejected")
        self.assertIn("投研委员会", res["actions"][0]["error"])
        rid, fin = self._external_run()
        self.assertEqual(fin["status"], "approved")
        self.assertEqual(fin["position_pct"], 0.25)       # 风控官上限压低仓位
        res = agent.apply_decision({"actions": [{"symbol": "000001", "action": "buy", "position_pct": 0.4,
                                                 "confidence": 0.9}]})
        a = res["actions"][0]
        self.assertEqual(a["status"], "filled", a)
        self.assertEqual(a["committee_run"], rid)
        self.assertEqual(a["trade"]["shares"], 200)      # 25% × 10000 / 10.01 → 249 → 200
        from core import plans
        p = plans.get_active("paper", "000001")[0]
        self.assertEqual(p["run_id"], rid)
        self.assertEqual(p["stop_loss"], 9.4)
        self.assertEqual(p["watch_points"], ["关注能否站稳10元"])

    def test_veto_and_entry_max(self):
        config.save_config({"committee": {"required_for_buy": True}})
        _, fin = self._external_run(veto=True)
        self.assertEqual(fin["status"], "rejected")
        self.assertIn("风控官否决", fin["gate_notes"][0])
        _, fin = self._external_run()
        PRICES["sz000001"] = fake_quote("000001", 10.6)   # 超过入场上限 10.2
        res = agent.apply_decision({"actions": [{"symbol": "000001", "action": "buy", "confidence": 0.9}]})
        self.assertIn("入场上限", res["actions"][0]["error"])

    def test_permission_and_regime(self):
        PRICES["sz300750"] = fake_quote("300750", 20.0)
        res = agent.apply_decision({"actions": [{"symbol": "300750", "action": "buy", "confidence": 0.9}]})
        self.assertIn("无交易权限", res["actions"][0]["error"])
        REGIME.update(state="冰点", position_cap=0.1)
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        res = agent.apply_decision({"actions": [{"symbol": "000001", "action": "buy", "position_pct": 0.3,
                                                 "confidence": 0.9}]})
        self.assertEqual(res["actions"][0]["status"], "rejected")   # 冰点10% = 1000元 < 一手

    def test_plan_triggers_and_close(self):
        from core import plans
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        broker.paper_execute("000001", "buy", 300)
        p = plans.get_active("paper", "000001")[0]
        self.assertIn("未经投研委员会", p["thesis"])
        self.assertAlmostEqual(p["stop_loss"], round(10.01 * 0.93, 3))
        plans.update("paper", "000001", {"invalidation": [{"type": "price_below", "price": 9.8}]})
        q = fake_quote("000001", 9.7, pct=-3)
        hits = plans.check_all({"sz000001": q}, {}, None, broker.get_positions())
        self.assertEqual([h["key"] for h in hits], ["inv0_price_below"])
        self.assertEqual(plans.check_all({"sz000001": q}, {}, None, broker.get_positions()), [])  # 当天不重复
        q2 = fake_quote("000001", 9.2, pct=-8)
        hits = plans.check_all({"sz000001": q2}, {}, None, broker.get_positions())
        self.assertTrue(any(h["key"] == "stop_loss" and h["auto_sell"] for h in hits))
        db.run("UPDATE positions SET last_buy_date='2000-01-01'")
        PRICES["sz000001"] = q2
        broker.paper_execute("000001", "sell", 300)
        self.assertEqual(plans.get_active("paper", "000001"), [])
        closed = db.one("SELECT * FROM plans WHERE symbol='sz000001'")
        self.assertEqual(closed["status"], "closed")
        self.assertLess(json.loads(closed["outcome"])["realized_pnl"], 0)

    def test_builtin_pipeline_fake_llm(self):
        from core import committee, llm
        seen = []

        def fake_chat(msgs, cfg):
            sysmsg = msgs[0]["content"]
            role = next(r for r in committee.ORDER[::-1] if committee.load_roles()[r] in sysmsg)
            seen.append(role)
            payload = json.loads(msgs[1]["content"])
            if role in ("bull", "bear") and payload["round"] == 2:
                assert payload["debate_so_far"], "第2轮必须看到第1轮辩论"
            if role == "pm":
                return json.dumps(self.FINAL, ensure_ascii=False)
            if role == "risk":
                return json.dumps(dict(self.OPS, veto=False, max_position_pct=0.3))
            return json.dumps(self.OPS, ensure_ascii=False)
        old = (llm.enabled, llm.chat)
        llm.enabled, llm.chat = (lambda: True), fake_chat
        try:
            PRICES["sz000001"] = fake_quote("000001", 10.0)
            res = committee.run_builtin("000001")
        finally:
            llm.enabled, llm.chat = old
        self.assertEqual(res["status"], "approved")
        self.assertEqual(sorted(set(seen)), sorted(committee.ORDER))
        self.assertEqual(len(seen), 13)
        run = committee.get_run(res["run_id"])
        self.assertEqual(len(run["opinions"]), 13)
        self.assertEqual([o["round"] for o in run["opinions"] if o["role"] == "bear"], [1, 2])

    def test_autopilot_timeline_and_status(self):
        """AI 动态按账户区分；查询不改变账户授权。"""
        from core import autopilot
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        config.save_config({"committee": {"required_for_buy": False}})
        agent.apply_decision({"market_view": "测试巡检", "actions": [
            {"symbol": "000001", "action": "buy", "position_pct": 0.2, "confidence": 0.8, "reason": "测试买"}]},
            agent="test", trigger="测试")
        ev = autopilot.timeline("paper")
        kinds = {e["kind"] for e in ev}
        self.assertIn("trade", kinds)
        self.assertIn("decision", kinds)
        self.assertTrue(all("time" in e for e in ev))
        self.assertFalse([e for e in autopilot.timeline("live") if e["kind"] == "trade"])
        st = autopilot.status()
        self.assertEqual(len(st["steps"]), 4)
        self.assertEqual(st["today"]["trades_paper"], 1)
        self.assertEqual(st["ai_accounts"], ["paper"])
        self.assertEqual(config.load_config()["ai"]["accounts"], ["paper"])

    def test_run_async_returns_immediately_with_progress(self):
        """送委员会：立刻拿到会议号（不等档案/大模型），重复点击不重开，进行中能看到进度。"""
        import threading as th
        from core import committee, llm, notify
        gate = th.Event()

        def slow_chat(msgs, cfg):
            gate.wait(5)
            sysmsg = msgs[0]["content"]
            role = next(r for r in committee.ORDER[::-1] if committee.load_roles()[r] in sysmsg)
            if role == "pm":
                return json.dumps(self.FINAL, ensure_ascii=False)
            return json.dumps(dict(self.OPS, veto=False), ensure_ascii=False)
        old = (llm.enabled, llm.chat, notify.send)
        llm.enabled, llm.chat, notify.send = (lambda: True), slow_chat, (lambda *a, **k: None)
        try:
            PRICES["sz000001"] = fake_quote("000001", 10.0)
            t0 = time.time()
            r1 = committee.run_async("000001", trigger="网页")
            self.assertLess(time.time() - t0, 1.0)
            self.assertTrue(r1["started"])
            r2 = committee.run_async("sz000001")
            self.assertEqual(r2, {"started": False, "run_id": r1["run_id"]})
            run = committee.get_run(r1["run_id"])
            self.assertEqual(run["status"], "running")
            self.assertIn("stage", run["progress"])
            gate.set()
            committee._running[("paper", "sz000001")][0].join(10)
        finally:
            gate.set()
            llm.enabled, llm.chat, notify.send = old
        run = committee.get_run(r1["run_id"])
        self.assertEqual(run["status"], "approved")
        self.assertNotIn("progress", run)

    def test_context_has_plans_and_regime(self):
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        broker.paper_execute("000001", "buy", 100)
        ctx = agent.build_context(with_ticks=False)
        self.assertEqual(ctx["market_regime"]["state"], "强势")
        self.assertEqual(ctx["position_plans"][0]["symbol"], "sz000001")
        self.assertEqual(ctx["positions_without_plan"], [])


def _zt(code, ind, lbc=1, fbt=93000, m=0, pct=10.0):
    return {"symbol": ("sh" if m else "sz") + code, "name": "涨停" + code, "price": 10, "pct": pct, "industry": ind,
            "lbc": lbc, "first_seal": fbt, "open_times": 0, "amount": 1e8, "float_mv": 5e9, "turnover": 5,
            "seal_fund": 1e7}


def set_pools(n_zt=84, n_zb=48, n_dt=0, yzt_pct=4.5, inds=("光伏设备",) * 11 + ("半导体",) * 4, max_lb=4):
    zt = [_zt("%06d" % (600000 + i), inds[i] if i < len(inds) else "其他%d" % i, lbc=max_lb if i == 0 else 1,
              fbt=93000 + i) for i in range(n_zt)]
    POOLS.update(zt=zt, zb=[_zt("%06d" % (1000 + i), "x") for i in range(n_zb)],
                 dt=[_zt("%06d" % (2000 + i), "y", pct=-10) for i in range(n_dt)],
                 yzt=[_zt("%06d" % (3000 + i), "z", pct=yzt_pct) for i in range(20)])


class TestMomentum(Base):
    """情绪短线：情绪周期、热门题材、短线计划、快速离场。"""

    def setUp(self):
        super().setUp()
        config.save_config({"style": {"paper": "momentum", "live": "swing"}})
        set_pools()
        from core import plans
        self._hm = plans._hm
        self.addCleanup(setattr, plans, "_hm", self._hm)

    def test_sentiment_stages(self):
        e = emotion.sentiment()
        self.assertEqual(e["zt"], 84)
        self.assertAlmostEqual(e["zb_rate"], 36.4, 1)
        self.assertEqual(e["stage"], "修复")
        self.assertEqual(e["themes"][0]["name"], "光伏设备")
        self.assertEqual(e["themes"][0]["leaders"][0]["lbc"], 4)
        set_pools(n_zt=20)
        self.assertEqual(emotion.sentiment(date="20991231")["stage"], "冰点")
        set_pools(n_zt=80, n_zb=20, yzt_pct=5)
        self.assertEqual(emotion.sentiment(date="20991230")["stage"], "升温")
        set_pools(n_zt=80, n_zb=80, yzt_pct=1)
        self.assertEqual(emotion.sentiment(date="20991229")["stage"], "退潮")
        set_pools(n_zt=120, n_zb=20, yzt_pct=5, max_lb=7)
        self.assertEqual(emotion.sentiment(date="20991228")["stage"], "高潮")

    def test_cap_never_looser_than_regime(self):
        REGIME.update(state="弱势", position_cap=0.2)              # 情绪「修复」50%，大盘弱势 20% → 取 20%
        self.assertEqual(screener.position_cap("paper")["cap"], 0.2)
        REGIME.update(state="强势", position_cap=0.9)              # 大盘强势 90%，情绪修复 50% → 取 50%
        self.assertEqual(screener.position_cap("paper")["cap"], 0.5)

    def _at(self, hhmm, day="2026-11-05"):
        import datetime as _dt
        real = emotion.market.now
        t = _dt.datetime.strptime(day + " " + hhmm, "%Y-%m-%d %H:%M").replace(tzinfo=market.CN_TZ)
        emotion.market.now = lambda: t
        self.addCleanup(setattr, emotion.market, "now", real)

    def _day_pools(self, date, **kw):
        set_pools(**kw)
        POOLS_BY_DATE[date] = {k: list(v) for k, v in POOLS.items()}
        self.addCleanup(POOLS_BY_DATE.clear)

    def test_intraday_uses_yesterday_close(self):
        self._day_pools("20261104")                                   # 昨天收盘：修复
        self._day_pools("20261105", n_zt=10, n_zb=2)                  # 今天 9:40 才 10 家涨停
        self._at("09:40")
        e = emotion.sentiment()
        self.assertEqual((e["stage"], e["prev_stage"], e["intraday"]), ("修复", "修复", True))   # 不会误判冰点
        self.assertIsNone(db.one("SELECT 1 FROM emotion_days WHERE date='20261105'"))         # 盘中不保存
        self.assertTrue(db.one("SELECT 1 FROM emotion_days WHERE date='20261104'"))           # 昨天收盘存下来了
        self._day_pools("20261105", n_zt=120, n_zb=10, yzt_pct=6, max_lb=7)                   # 盘中很热也不上调
        emotion._cache.clear()
        self._at("11:00")
        self.assertEqual(emotion.sentiment()["stage"], "修复")
        self._day_pools("20261105", n_zt=40, n_zb=10, yzt_pct=-3)                            # 昨天涨停今天被砸
        self._at("10:15")
        self.assertEqual(emotion.sentiment()["stage"], "冰点")
        self._at("09:50")
        self.assertEqual(emotion.sentiment()["stage"], "修复")                                # 10 点前只看跌停
        self._at("15:10")
        e = emotion.sentiment()
        self.assertEqual((e["stage"], e["final"]), ("冰点", True))                             # 收盘后用完整数据
        self.assertTrue(db.one("SELECT 1 FROM emotion_days WHERE date='20261105'"))

    def test_style_per_account(self):
        self.assertEqual(config.style("paper"), "momentum")
        self.assertEqual(config.style("live"), "swing")

    def test_momentum_plan_defaults(self):
        from core import plans
        PRICES["sz000001"] = fake_quote("000001", 10.0)
        broker.paper_execute("000001", "buy", 300)
        p = plans.get_active("paper", "000001")[0]
        self.assertEqual(p["style"], "momentum")
        self.assertAlmostEqual(p["stop_loss"], round(10.01 * 0.96, 3))
        self.assertEqual(p["trailing_pct"], 3)
        self.assertEqual(p["max_hold_days"], 3)

    def test_chase_limit_and_emotion_cap(self):
        PRICES["sz000003"] = fake_quote("000003", 10.6, pct=6)    # 风控只收紧：短线追高上限不超过 risk.max_chase_pct(5%)
        res = agent.apply_decision({"actions": [{"symbol": "000003", "action": "buy", "confidence": 0.9}]})
        self.assertIn("追高上限 5.0%", res["actions"][0]["error"])
        config.save_config({"momentum": {"max_chase_pct": 8}})     # 短线参数调大也放不宽
        res = agent.apply_decision({"actions": [{"symbol": "000003", "action": "buy", "confidence": 0.9}]})
        self.assertIn("追高上限 5.0%", res["actions"][0]["error"])
        PRICES["sz000001"] = fake_quote("000001", 10.4, pct=4)
        res = agent.apply_decision({"actions": [{"symbol": "000001", "action": "buy", "confidence": 0.9}]})
        self.assertEqual(res["actions"][0]["status"], "filled", res)
        set_pools(n_zt=20)                                          # 冰点 → 情绪仓位 0
        PRICES["sz000002"] = fake_quote("000002", 10.0, pct=4)
        res = agent.apply_decision({"actions": [{"symbol": "000002", "action": "buy", "confidence": 0.9}]})
        self.assertIn("总仓位上限", res["actions"][0]["error"])

    def _hold(self, price=10.0):
        from core import plans
        PRICES["sz000001"] = fake_quote("000001", price)
        broker.paper_execute("000001", "buy", 300)
        return plans

    def test_gap_fail_waits_for_t1_then_sells(self):
        plans = self._hold()
        plans._hm = lambda: 945
        q = fake_quote("000001", 10.1, prev=10.0)
        q.update(open=10.3, high=10.5)                              # 高开冲高后跌破开盘价
        self.assertEqual(plans.check_all({"sz000001": q}, {}, None, broker.get_positions()), [])  # T+1 不触发也不消耗
        db.run("UPDATE positions SET last_buy_date='2000-01-01'")
        hits = plans.check_all({"sz000001": q}, {}, None, broker.get_positions())
        self.assertEqual([(h["key"], h["auto_sell"]) for h in hits], [("gap_fail", True)])

    def test_news_take_profit_and_sealed_hold(self):
        plans = self._hold()
        db.run("UPDATE positions SET last_buy_date='2000-01-01'")
        plans._hm = lambda: 1030
        q = fake_quote("000001", 10.5, prev=10.0)
        q.update(open=10.45, high=10.6)
        hits = plans.check_all({"sz000001": q}, {"sz000001": {"news_today": {"pos": 2}}}, None,
                               broker.get_positions())
        self.assertEqual([h["key"] for h in hits], ["news_take_profit"])
        self.assertTrue(hits[0]["auto_sell"])
        # 封死涨停：同样的利好也不卖
        db.run("UPDATE plans SET checks='{}'")
        q = fake_quote("000001", 11.0, prev=10.0, ask1=0)
        q.update(open=10.5, high=11.0, limit_up=11.0, asks=[[0, 0]] * 5)
        self.assertEqual(plans.check_all({"sz000001": q}, {"sz000001": {"news_today": {"pos": 2}}}, None,
                                         broker.get_positions()), [])

    def test_only_one_auto_sell_per_round(self):
        plans = self._hold()
        db.run("UPDATE positions SET last_buy_date='2000-01-01', peak_price=11")
        plans._hm = lambda: 945
        q = fake_quote("000001", 9.5, prev=10.0)                    # 同时触发 止损 + 回撤 + 冲高回落
        q.update(open=10.0, high=10.4)
        hits = plans.check_all({"sz000001": q}, {}, None, broker.get_positions())
        self.assertEqual(sum(1 for h in hits if h["auto_sell"]), 1)
        self.assertEqual(hits[0]["key"], "stop_loss")

    def test_swing_plan_unchanged(self):
        config.save_config({"style": {"paper": "swing"}})
        plans = self._hold()
        db.run("UPDATE positions SET last_buy_date='2000-01-01'")
        plans._hm = lambda: 945
        q = fake_quote("000001", 10.1, prev=10.0)
        q.update(open=10.3, high=10.5)
        self.assertEqual(plans.check_all({"sz000001": q}, {}, None, broker.get_positions()), [])

    def test_momentum_score(self):
        s = {"symbol": "sz000001", "name": "x", "board": "main", "industry": "光伏设备", "pct": 4.5, "vol_ratio": 2.5,
             "main_pct": 6, "prev_close": 10, "high": 10.55, "price": 10.5, "turnover": 8, "total_mv": 1e10,
             "amount": 5e8}
        hot = {"光伏设备": 1}
        mc = config.load_config()["momentum"]
        self.assertIsNotNone(screener.momentum_pre(s, {}, hot, mc))
        self.assertIsNone(screener.momentum_pre(dict(s, industry="银行"), {}, hot, mc))   # 不在热门题材
        self.assertIsNone(screener.momentum_pre(dict(s, pct=9.8), {}, hot, mc))          # 快涨停不追
        self.assertIsNone(screener.momentum_pre(dict(s, pct=6), {}, hot, mc))            # 超过风控追高上限 5%
        sc, why, m = screener.momentum_score(s, fake_kline("sz000001"), {}, hot, mc)
        self.assertGreater(sc, 5)
        self.assertTrue(any("热门题材第1" in w for w in why))
        bad = dict(s, high=11.0, main_pct=-8)                       # 冲高回落 + 主力流出
        self.assertLess(screener.momentum_score(bad, fake_kline("sz000001"), {}, hot, mc)[0], sc - 3)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
