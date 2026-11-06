"""实时监控主循环（后台线程）。

每 poll_interval_sec（默认3秒）：
  1. 拉自选+持仓的五档盘口快照 → 写 quotes 缓存 + data/ticks/*.csv
  2. 跑预设规则（rules）和指标策略（strategies）→ 信号写 alerts 表 → 合并推送微信
  3. 异动时可选唤醒内置大模型调研决策；规则动作 auto_sell 自动卖出
  4. 定期记录净值、过期实盘待确认订单
"""
import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict, deque

import strategies as strat_pkg
from . import agent, broker, db, global_market, indicators, llm, market, news, notify, plans, rules, wakeup
from .config import load_config

log = logging.getLogger("engine")
from .config import DATA_DIR  # noqa: E402
RESTART_FLAG = os.path.join(DATA_DIR, "restart.request")


class StrategyRunner:
    def __init__(self):
        self.reload()
        self.last = {}

    def reload(self):
        self.classes = strat_pkg.discover()
        self.instances = {}
        conf = load_config().get("strategies", {})
        for name, cls in self.classes.items():
            c = conf.get(name, {})
            if c.get("enabled", cls.default_enabled):
                self.instances[name] = (cls(**(c.get("params") or {})), c)
        log.info("已加载策略: %s（启用 %s）", list(self.classes), list(self.instances))

    def describe(self):
        conf = load_config().get("strategies", {})
        out = []
        for name, cls in self.classes.items():
            c = conf.get(name, {})
            p = dict(cls.params)
            p.update(c.get("params") or {})
            out.append({"name": name, "label": cls.label, "description": cls.description,
                        "enabled": c.get("enabled", cls.default_enabled), "params": p,
                        "cooldown_min": c.get("cooldown_min", cls.cooldown_min),
                        "kind": "tick" if cls.on_tick is not strat_pkg.Strategy.on_tick else "bar",
                        "backtestable": cls.backtestable and cls.on_bar is not strat_pkg.Strategy.on_bar})
        return out

    def _cool(self, name, sym, side, cooldown):
        key = (name, sym, side)
        if key not in self.last:
            r = db.one("SELECT MAX(ts) t FROM alerts WHERE strategy=? AND symbol=? AND side=?", key)
            self.last[key] = (r and r["t"]) or 0
        if time.time() - self.last[key] < cooldown * 60:
            return False
        self.last[key] = time.time()
        return True

    def run(self, sym, q, kline, ind, ticks):
        out = []
        for name, (inst, c) in self.instances.items():
            try:
                sigs = []
                if kline:
                    sigs.append(inst.on_bar(sym, q, kline, ind))
                if ticks:
                    sigs.append(inst.on_tick(sym, q, ticks))
                for s in sigs:
                    for sig in (s if isinstance(s, list) else [s]):
                        if sig and self._cool(name, sym, sig["side"], c.get("cooldown_min", inst.cooldown_min)):
                            out.append(dict(sig, strategy=name, label=inst.label))
            except Exception as e:
                log.debug("策略 %s 在 %s 出错: %s", name, sym, e)
        return out


class IntelThread(threading.Thread):
    """后台刷新消息面+资金面：盘中每60秒（资金流），非交易时段每30分钟。不阻塞3秒行情循环。"""

    def __init__(self, mon):
        super().__init__(daemon=True, name="intel")
        self.mon = mon
        self.last_error = None

    def run(self):
        while not self.mon.stop_flag.is_set():
            is_open = False
            try:
                is_open = market.is_market_open()
                _, _, syms = self.mon.symbols()
                rows = db.all("SELECT symbol,name FROM quotes")
                names = {r["symbol"]: r["name"] for r in rows}
                quotes = {s: (self.mon.ticks[s][-1] if self.mon.ticks[s] else None) for s in syms}
                self.mon.intel.update(news.intel_many(syms, names, quotes))
                self.mon.status["intel_updated"] = time.time()
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
                log.warning("消息/资金面刷新失败: %s", e)
            self.mon.stop_flag.wait(60 if is_open else 1800)


class Monitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="monitor")
        self.ticks = defaultdict(lambda: deque(maxlen=600))   # 内存里每只保留约30分钟快照
        self.rule_engine = rules.RuleEngine()
        self.strats = StrategyRunner()
        self.stop_flag = threading.Event()
        self.status = {"started_at": time.time(), "loops": 0, "last_loop": None, "last_error": None,
                       "symbols": 0, "market_open": False, "session": ""}
        self._last_refresh = 0
        self._last_equity = 0
        self._close_snap_day = None
        self._last_llm_timer = 0
        self.intel = {}            # symbol -> news.intel()，由 IntelThread 每分钟刷新
        self.intel_thread = IntelThread(self)

    def start(self):
        super().start()
        self.intel_thread.start()

    def symbols(self):
        watch = [w["symbol"] for w in db.all("SELECT symbol FROM watchlist")]
        pos = broker.get_positions()
        explicit = [r["symbol"] for r in db.all("SELECT DISTINCT symbol FROM rules WHERE enabled=1")
                    if not r["symbol"].startswith("*")]
        from . import watch as entry_watch
        try:
            ew = entry_watch.symbols()
        except Exception:
            ew = []
        return watch, pos, list(dict.fromkeys(watch + [p["symbol"] for p in pos] + ew + explicit))

    def kline(self, sym):
        q = self.ticks[sym][-1] if self.ticks[sym] else None
        return indicators.merge_live(agent.daily_kline(sym), q, market.today_str())

    def run(self):
        log.info("监控线程启动")
        while not self.stop_flag.is_set():
            cfg = load_config()
            mcfg = cfg["monitor"]
            is_open = False
            try:
                now = market.now()
                is_open = market.is_market_open(now)
                self.status.update(market_open=is_open, session=market.session_label(now))
                if is_open or time.time() - self._last_refresh >= mcfg["offhours_refresh_sec"]:
                    self.tick(cfg, is_open)
                    self._last_refresh = time.time()
                broker.expire_pending()
                self.premarket_brief(now)
                self.daily_close(now, cfg)
                self.check_restart(now)
                self.status["last_error"] = None
            except Exception as e:
                log.exception("监控循环出错")
                self.status["last_error"] = "%s %s" % (time.strftime("%H:%M:%S"), e)
            self.status["loops"] += 1
            self.status["last_loop"] = time.time()
            self.stop_flag.wait(mcfg["poll_interval_sec"] if is_open else 15)

    def daily_close(self, now, cfg):
        """交易日 15:05~17:00 自动跑一次每日收盘流程（后台线程，不阻塞盯盘）。"""
        if not cfg["screener"].get("auto_daily", True):
            return
        t, d = now.hour * 100 + now.minute, now.strftime("%Y%m%d")
        if not (1505 <= t < 1700) or getattr(self, "_daily_day", None) == d:
            return
        if not market.is_trading_day(now):
            return
        # 重启后不重复跑：标记文件记录今天已执行
        marker = os.path.join(DATA_DIR, "daily_close.done")
        try:
            with open(marker, encoding="utf-8") as f:
                if f.read().strip() == d:
                    self._daily_day = d
                    return
        except OSError:
            pass
        self._daily_day = d
        with open(marker, "w", encoding="utf-8") as f:
            f.write(d)

        def go():
            from . import daily
            try:
                self.status["daily"] = "运行中"
                r = daily.run_daily_close()   # 按 ai.accounts 分别跑
                self.status["daily"] = "完成：通过%d/送审%d" % (len(r.get("approved", [])), len(r.get("reviewed", [])))
            except Exception as e:
                log.exception("每日收盘流程失败")
                self.status["daily"] = "失败：%s" % e
                notify.send("每日收盘流程失败", str(e), kind="system")
        threading.Thread(target=go, daemon=True, name="daily-close").start()

    def check_restart(self, now):
        """维护 AI 改完代码后写 data/restart.request，程序在非交易时段自己重启加载新代码（同一个窗口内）。"""
        path = RESTART_FLAG
        if not os.path.exists(path) or market.is_trading_session(now):
            return
        try:
            with open(path, encoding="utf-8") as f:
                reason = f.read().strip()[:200]
        except OSError:
            reason = ""
        try:
            os.remove(path)
        except OSError:
            try:
                os.replace(path, path + ".done")
            except OSError:
                log.error("无法清除重启标记，放弃重启以免循环")
                return
        log.warning("收到重启请求（%s），正在重启加载新代码…", reason or "无说明")
        notify.send("系统重启加载新代码", reason or "维护 AI 请求重启", kind="system")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def premarket_brief(self, now):
        """交易日 9:00~9:30 推送一次：隔夜外盘 + 受影响的持仓/自选。"""
        t, d = now.hour * 100 + now.minute, now.strftime("%Y%m%d")
        if not (900 <= t < 930) or getattr(self, "_premarket_day", None) == d or not market.is_trading_day(now):
            return
        self._premarket_day = d
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        # A successful send is recorded by notify.send; failed attempts must not block a retry after restart.
        if db.one("SELECT 1 FROM messages WHERE title LIKE '盘前外盘%' AND ok=1 AND ts>=? AND ts<? LIMIT 1",
                  (t0, t0 + 86400)):
            return
        ov = global_market.overnight(ttl=60)
        if not ov:
            return
        s = ov["summary"]
        lines = ["**%s**（纳指/半导体/英伟达均值 %+.2f%%）" % (s["mood"], s["tech_avg_pct"]), "",
                 " · ".join("%s %+.2f%%" % (v["name"], v["pct"]) for v in ov["quotes"].values())]
        lines += ["- " + n for n in s["notes"]]
        watch, positions, syms = self.symbols()
        names = {r["symbol"]: r["name"] for r in db.all("SELECT symbol,name FROM quotes")}
        held = {p["symbol"] for p in positions}
        linked = []
        for sym in syms:
            lk = global_market.link_for(news.industry(sym, names.get(sym, "")), ov)
            if lk and abs(lk["avg_pct"]) >= 1:
                linked.append("- %s%s（%s）：%s → %s" % ("【持仓】" if sym in held else "", names.get(sym, sym),
                                                       lk["chain"], "、".join("%s%+.1f%%" % (x["name"], x["pct"])
                                                                            for x in lk["drivers"]), lk["hint"]))
        if linked:
            lines += ["", "**相关持仓/自选：**"] + linked
        lines.append("\n美股影响主要在开盘，注意高开低走/低开高走，不要开盘第一分钟追。")
        notify.send("盘前外盘：%s" % s["mood"], "\n".join(lines), kind="premarket")

    def check_entry_watch(self, cfg, quotes):
        """委员会说「等回踩到 X 再买」的，到价站稳就自动重开委员会复审。"""
        from . import watch as entry_watch
        try:
            hits = entry_watch.check(quotes, cfg)
        except Exception as e:
            log.warning("等回踩检查失败: %s", e)
            return
        for h in hits:
            w = h["watch"]
            notify.send("等回踩%s：%s" % ("到价复审" if h["status"] == "hit" else "作废", h["name"]),
                        "**%s %s** 现价 %.3f（%s，委员会#%d 说等回踩 %.3f）  \n%s"
                        % (h["name"], w["symbol"], h["price"], "模拟盘" if w["account"] == "paper" else "实盘",
                           w["run_id"], w["entry"], h["msg"]), kind="watch", account=w["account"])

    def tick(self, cfg, is_open):
        watch, positions, syms = self.symbols()
        self.status["symbols"] = len(syms)
        if not syms:
            return
        quotes = market.get_quotes(syms + [s for s, _ in market.INDEXES])
        market.save_quotes(quotes)
        # 记录秒级快照（去重：行情时间不变就不记）
        for s in syms:
            q = quotes.get(s)
            if not q:
                continue
            dq = self.ticks[s]
            if not dq or dq[-1]["time"] != q["time"]:
                dq.append(q)
                if is_open and cfg["monitor"]["record_ticks"]:
                    try:
                        agent.append_tick(q)
                    except OSError as e:
                        log.warning("写 tick 失败: %s", e)
        # 持仓最高价（移动止盈用）
        if positions:
            with db.tx() as c:
                for p in positions:
                    q = quotes.get(p["symbol"])
                    if q and q["high"] > (p["peak_price"] or 0):
                        c.execute("UPDATE positions SET peak_price=? WHERE account=? AND symbol=?",
                                  (q["high"], p["account"], p["symbol"]))
        if is_open:
            self.evaluate(cfg, quotes, watch, positions, syms)
            self.check_entry_watch(cfg, quotes)
            if time.time() - self._last_equity >= cfg["monitor"]["equity_snapshot_min"] * 60:
                for a in ("paper", "live"):
                    broker.record_equity(a, quotes)
                self._last_equity = time.time()
            iv = cfg["ai"].get("llm_interval_min") or 0
            if iv and time.time() - self._last_llm_timer >= iv * 60 and llm.enabled():
                self._last_llm_timer = time.time()
                llm.decide_async("内置定时(%d分钟)" % iv, force=True)
        else:
            now = market.now()
            d = now.strftime("%Y%m%d")
            if market.is_trading_day(now) and now.hour * 100 + now.minute >= 1500 and self._close_snap_day != d:
                for a in ("paper", "live"):
                    broker.record_equity(a, quotes)
                self._close_snap_day = d

    def evaluate(self, cfg, quotes, watch, positions, syms):
        alerts = []
        # 1) 预设规则
        for a in self.rule_engine.evaluate(quotes, watch, positions, self.kline, lambda s: list(self.ticks[s])):
            res = rules.run_action(a)
            r = a["rule"]
            db.run("INSERT INTO alerts(ts,source,rule_id,symbol,name,type,side,strength,message,price,action_result)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), "rule", r["id"], a["symbol"], a["name"], r["type"], "watch", 1.0,
                    a["message"], a["price"], res))
            alerts.append({"src": "规则#%d %s" % (r["id"], r["note"] or rules.RULE_TYPES[r["type"]]["label"]),
                           "name": a["name"], "symbol": a["symbol"], "price": a["price"], "pct": a["pct"],
                           "msg": a["message"] + (("；" + res) if res else ""),
                           "wake": r["action"] == "notify_ai" or cfg["ai"]["llm_on_alert"]})
        # 2) 指标/盘口策略
        for s in syms:
            q = quotes.get(s)
            if not q or q.get("suspended"):
                continue
            kl = self.kline(s)
            ind = indicators.summarize(kl) if kl else {}
            qx = dict(q, intel=self.intel.get(s))
            for sig in self.strats.run(s, qx, kl, ind, list(self.ticks[s])):
                db.run("INSERT INTO alerts(ts,source,strategy,symbol,name,type,side,strength,message,price)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), "strategy", sig["strategy"], s, q["name"], sig["strategy"], sig["side"],
                        sig["strength"], sig["message"], q["price"]))
                tag = {"buy": "🔴买入信号", "sell": "🟢卖出信号", "watch": "👀关注"}[sig["side"]]
                ncfg = cfg["notify"]
                push = (sig["side"] != "watch" and sig["strength"] >= ncfg.get("min_signal_strength", 0)) or \
                    (sig["side"] == "watch" and ncfg.get("push_watch_signals", True))
                if not push:
                    continue   # 已写入信号历史，不推送
                alerts.append({"src": "%s·%s" % (tag, sig["label"]), "name": q["name"], "symbol": s,
                               "price": q["price"], "pct": q["pct"], "msg": sig["message"],
                               "wake": cfg["ai"]["llm_on_alert"] and sig["side"] != "watch"
                               and sig["strength"] >= cfg["ai"]["llm_signal_min_strength"]})
        # 3) 持仓计划检查（止损/目标/移动止盈/时间止损/失效条件）
        for t in plans.check_all(quotes, self.intel, self.kline, positions):
            p, ps, res = t["plan"], t["position"], ""
            if t["auto_sell"]:
                try:
                    if p["account"] == "paper" and cfg["plans"]["auto_stop_loss_paper"]:
                        if ps["sellable"] > 0:
                            tr = broker.paper_execute(p["symbol"], "sell", ps["sellable"], source="plan",
                                                      note="计划#%d %s" % (p["id"], t["message"]))
                            res = "模拟盘已自动卖出 %d 股 @ %.3f，盈亏 %.2f" % (tr["shares"], tr["price"], tr["pnl"] or 0)
                        else:
                            res = "今日买入 T+1 不可卖"
                    elif p["account"] == "live":
                        o = broker.create_live_order(p["symbol"], t["name"], "sell", ps["shares"], t["price"],
                                                     reason="计划#%d %s" % (p["id"], t["message"]), source="plan")
                        res = "已生成实盘卖单 #%d 待确认" % o["id"]
                except Exception as e:
                    res = "自动卖出失败：%s" % e
            side = "sell" if t["auto_sell"] or t["key"] in ("stop_loss", "trailing") or t["key"].startswith("inv") else "watch"
            db.run("INSERT INTO alerts(ts,source,strategy,symbol,name,type,side,strength,message,price,action_result)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), "plan", "plan#%d" % p["id"], p["symbol"], t["name"], t["key"], side, 1.0,
                    t["message"], t["price"], res))
            alerts.append({"src": "📋持仓计划#%d(%s)" % (p["id"], "模拟" if p["account"] == "paper" else "实盘"),
                           "name": t["name"], "symbol": p["symbol"], "price": t["price"],
                           "pct": (quotes.get(p["symbol"]) or {}).get("pct", 0),
                           "msg": t["message"] + (("；" + res) if res else ""), "wake": True})
        if not alerts:
            return
        lines = []
        for a in alerts:
            lines.append("**%s %s** %.3f（%+.2f%%）  \n%s：%s" % (a["name"], a["symbol"], a["price"], a["pct"],
                                                              a["src"], a["msg"]))
        title = "异动：" + "，".join("%s %s" % (a["name"], a["msg"][:16]) for a in alerts[:3])
        wake = [a for a in alerts if a["wake"]]
        if wake:
            reason = "；".join("%s %s" % (a["name"], a["msg"]) for a in wake[:5])
            syms = list(dict.fromkeys(a["symbol"] for a in wake))
            if llm.enabled() and llm.decide_async("异动:" + reason, focus=",".join(syms)):
                lines.append("\n🤖 已唤醒内置AI调研，决策结果稍后推送")
            if wakeup.trigger(reason, syms):
                lines.append("\n🤖 已唤醒外部AI调研，决策结果稍后推送")
        plan = [a for a in alerts if a["src"].startswith("📋")]
        kind = "trade" if plan else "alert"
        acct = None
        if plan:
            acct = "live" if any("实盘" in a["src"] for a in plan) else "paper"
        notify.send(title[:90], "\n\n".join(lines), kind=kind, account=acct)
