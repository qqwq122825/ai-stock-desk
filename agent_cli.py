#!/usr/bin/env python3
"""给 AI 用的命令行接口（不需要网页服务在运行，直接读写 data/stock.db）。

  python3 agent_cli.py context            # 输出完整决策上下文 JSON（账户/持仓/自选/指标/盘口/信号/历史决策/日志）
  python3 agent_cli.py inbox              # 上次决策之后的新信号/订单/成交（异动唤醒时先看这个）
  python3 agent_cli.py decide d.json      # 提交决策 JSON（也可用 - 从标准输入读）→ 风控 → 执行 → 输出结果
  python3 agent_cli.py report [--date 2026-11-04]   # 当日复盘数据
  python3 agent_cli.py journal "今天的总结..."      # 写交易日志（下次的 AI 会在 context 里看到）
  python3 agent_cli.py backtest resonance 600036 000001 --days 500   # 策略回测
  python3 agent_cli.py news 600036           # 个股：公告+新闻(带利好/利空标签)+主力资金流+龙虎榜+出货识别
  python3 agent_cli.py flash --n 30          # 7x24 财经快讯（宏观/政策/板块）
  python3 agent_cli.py flow 600036 --days 20 # 主力资金流向（日级）+ 今日分钟级
  python3 agent_cli.py global [600036 ...]   # 隔夜外盘（美股/港股/半导体/英伟达…）及对指定股票所在板块的影响

  选股 / 大盘：
  python3 agent_cli.py review [--account paper]  # 委员会事后对账 + 模拟盘体检（能不能上实盘）
  python3 agent_cli.py backup                # 立即备份 data（每天收盘后也会自动备份）
  python3 agent_cli.py regime                # 大盘情绪状态 + 总仓位上限
  python3 agent_cli.py emotion               # 情绪周期（冰点/退潮/修复/升温/高潮）+ 热门题材（情绪短线用）
  python3 agent_cli.py screen                # 全市场选股漏斗 → Top20 候选池（约10~20秒）
  python3 agent_cli.py candidates            # 最近一次候选池
  python3 agent_cli.py daily-close           # 每日收盘流程：选股→委员会审前N只→通过的加自选→推送总结（程序15:05会自动跑）

  投研委员会（建仓/加仓前必须通过）：
  python3 agent_cli.py committee start 600900      # 外部模式：建会议+生成档案，输出 run_id 和流程
  python3 agent_cli.py committee opinion <run> <role> f.json [--round 2]   # 提交某角色意见
  python3 agent_cli.py committee final <run> f.json        # 组合经理拍板（自动执行风控否决/置信度门槛）
  python3 agent_cli.py committee run 600900        # 内置模式：程序调用大模型跑完整11角色（需配置 key）
  python3 agent_cli.py committee show <run> | list | stats | roles

  系统维护（给改代码的 AI 用）：
  python3 agent_cli.py health [--hours 24]          # 体检：日志错误、数据源、被拒动作、委员会故障、推送
  python3 agent_cli.py request-restart "改了什么"     # 请求重启加载新代码（只在非交易时段执行）

  持仓计划：
  python3 agent_cli.py plans                       # 所有进行中的持仓计划
  python3 agent_cli.py plan-update 600900 '{"stop_loss": 29.1}' [--account paper]
  python3 agent_cli.py quote 600519 000001
  python3 agent_cli.py kline 600519 --period m5 --count 48
  python3 agent_cli.py ticks 600519 --n 100   # 今日秒级五档快照
  python3 agent_cli.py signals --hours 2      # 最近规则/策略信号
  python3 agent_cli.py status                 # 简要账户状态
  python3 agent_cli.py notify "标题" "内容"     # 给用户推送微信
  python3 agent_cli.py llm --trigger "手动"    # 调用内置大模型决策一次（需配置 key）
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import agent, broker, db, indicators, llm, market, notify  # noqa: E402


def out(x):
    print(json.dumps(x, ensure_ascii=False, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("context")
    p.add_argument("--no-ticks", action="store_true")
    p.add_argument("--no-intel", action="store_true", help="不拉新闻/资金流（更快）")
    p.add_argument("--save", help="同时保存到文件")
    p = sub.add_parser("decide")
    p.add_argument("file", help="决策 JSON 文件路径，或 - 表示 stdin")
    p.add_argument("--agent", default="scheduled-ai")
    p.add_argument("--trigger", default="timer-10min")
    p = sub.add_parser("quote")
    p.add_argument("symbols", nargs="+")
    p = sub.add_parser("kline")
    p.add_argument("symbol")
    p.add_argument("--period", default="day", help="day/week/month/m1/m5/m15/m30/m60")
    p.add_argument("--count", type=int, default=60)
    p = sub.add_parser("ticks")
    p.add_argument("symbol")
    p.add_argument("--n", type=int, default=100)
    p = sub.add_parser("signals")
    p.add_argument("--hours", type=float, default=3)
    sub.add_parser("status")
    p = sub.add_parser("notify")
    p.add_argument("title")
    p.add_argument("content")
    p = sub.add_parser("llm")
    p.add_argument("--trigger", default="命令行手动")
    p.add_argument("--focus")
    sub.add_parser("inbox")
    p = sub.add_parser("report")
    p.add_argument("--date")
    p = sub.add_parser("journal")
    p.add_argument("text", nargs="?", help="不填则显示最近日志")
    p.add_argument("--author", default="ai")
    p = sub.add_parser("backtest")
    p.add_argument("strategy", help="策略名，如 resonance/macd/kdj/rsi/ma_cross/boll；all=全部日线策略")
    p.add_argument("symbols", nargs="*", help="默认=自选股")
    p.add_argument("--days", type=int, default=500)
    p.add_argument("--params", help='JSON 覆盖参数，如 \'{"buy_score":3}\'')
    p.add_argument("--full", action="store_true", help="输出每只股票的成交明细和曲线")
    p = sub.add_parser("news")
    p.add_argument("symbol")
    p.add_argument("--n", type=int, default=15)
    p = sub.add_parser("flash")
    p.add_argument("--n", type=int, default=30)
    p = sub.add_parser("flow")
    p.add_argument("symbol")
    p.add_argument("--days", type=int, default=20)
    p = sub.add_parser("health")
    p.add_argument("--hours", type=float, default=24)
    p = sub.add_parser("request-restart")
    p.add_argument("reason")
    sub.add_parser("regime")
    p = sub.add_parser("review")
    p.add_argument("--account", default="paper")
    sub.add_parser("backup")
    sub.add_parser("emotion")
    p = sub.add_parser("global")
    p.add_argument("symbols", nargs="*")
    p = sub.add_parser("screen")
    p.add_argument("--top", type=int)
    p.add_argument("--account", default="paper", help="paper 模拟盘 / live 实盘（两个账户的选股互不共用）")
    p = sub.add_parser("candidates")
    p.add_argument("--account", default="paper")
    p = sub.add_parser("daily-close")
    p.add_argument("--force", action="store_true", help="非交易日也执行")
    p = sub.add_parser("committee")
    p.add_argument("op", choices=["start", "opinion", "final", "run", "show", "list", "stats", "roles"])
    p.add_argument("args", nargs="*")
    p.add_argument("--round", type=int, default=1)
    p.add_argument("--account", default="paper")
    p.add_argument("--intent", default="open", help="open 新建仓 / add 加仓 / review 持仓复核")
    p.add_argument("--horizon", type=int, default=10)
    sub.add_parser("plans")
    p = sub.add_parser("plan-update")
    p.add_argument("symbol")
    p.add_argument("changes", help="JSON")
    p.add_argument("--account", default="paper")
    a = ap.parse_args()

    db.init_db()
    if a.cmd == "context":
        ctx = agent.build_context(with_ticks=not a.no_ticks, with_intel=not a.no_intel)
        if a.save:
            with open(a.save, "w", encoding="utf-8") as f:
                json.dump(ctx, f, ensure_ascii=False, indent=1, default=str)
        out(ctx)
    elif a.cmd == "decide":
        text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
        out(agent.apply_decision(json.loads(text), agent=a.agent, trigger=a.trigger))
    elif a.cmd == "quote":
        qs = market.get_quotes(a.symbols)
        out({s: dict(agent._compact_quote(q), bids=q["bids"], asks=q["asks"]) for s, q in qs.items()})
    elif a.cmd == "kline":
        kl = market.get_kline(a.symbol, a.period, max(a.count, 80))
        res = {"symbol": market.normalize(a.symbol), "period": a.period, "bars": kl[-a.count:]}
        if a.period == "day":
            q = market.get_quote(a.symbol)
            kl = indicators.merge_live(kl, q, market.today_str())
        res["indicators"] = indicators.summarize(kl)
        out(res)
    elif a.cmd == "ticks":
        t = agent.read_ticks(a.symbol, a.n)
        out({"file": agent.tick_path(market.normalize(a.symbol)), "count": len(t), "ticks": t,
             "stats": agent.tick_stats(a.symbol)})
    elif a.cmd == "signals":
        rows = db.all("SELECT * FROM alerts WHERE ts>=? ORDER BY id DESC", (time.time() - a.hours * 3600,))
        for r in rows:
            r["ts"] = market.datetime.fromtimestamp(r["ts"], market.CN_TZ).strftime("%m-%d %H:%M:%S")
        out(rows)
    elif a.cmd == "status":
        out({"session": market.session_label(), "market_open": market.is_market_open(),
             "accounts": {x: broker.snapshot(x) for x in ("paper", "live")},
             "pending_orders": db.all("SELECT id,symbol,name,side,shares,status FROM orders WHERE status='pending_confirm'")})
    elif a.cmd == "notify":
        ok, ch = notify.send(a.title, a.content)
        out({"ok": ok, "channels": ch})
    elif a.cmd == "llm":
        out(llm.decide(a.trigger, a.focus))
    elif a.cmd == "inbox":
        out(agent.inbox())
    elif a.cmd == "report":
        out(agent.daily_report(a.date))
    elif a.cmd == "journal":
        if a.text:
            out({"saved": agent.journal_append(a.text, a.author)})
        else:
            print(agent.journal_recent(20000, days=7) or "（暂无日志）")
    elif a.cmd == "backtest":
        from core import backtest
        import strategies
        syms = a.symbols or [w["symbol"] for w in db.all("SELECT symbol FROM watchlist")]
        names = [a.strategy]
        if a.strategy == "all":
            names = [n for n, c in strategies.discover().items()
                     if c.on_bar is not strategies.Strategy.on_bar and c.backtestable]
        cache = {}

        def kfn(s):
            if s not in cache:
                cache[s] = market.get_kline(s, "day", a.days + 60)
            return cache[s]
        res = {}
        for n in names:
            r = backtest.run(n, syms, a.days, json.loads(a.params) if a.params else None, kline_fn=kfn)
            if not a.full:
                for x in r["results"]:
                    x.pop("curve", None)
                    x.pop("trade_log", None)
            res[n] = r
        out(res if len(res) > 1 else res[names[0]])
    elif a.cmd == "news":
        from core import news
        q = market.get_quote(a.symbol)
        kl = indicators.merge_live(market.get_kline(q["symbol"], "day", 120), q, market.today_str())
        it = news.intel(q["symbol"], q["name"], q, news_n=a.n)
        chk = news.distribution_check(q, indicators.summarize(kl), it)
        res = {"symbol": q["symbol"], "name": q["name"], "price": q["price"], "pct": q["pct"], "intel": it,
               "a_share_pattern": {"type": "疑似借利好出货" if chk[0] == "sell" else "疑似利空洗盘",
                                   "score": chk[1], "reasons": chk[2]} if chk else None}
        try:
            res["announcements"] = news.announcements(q["symbol"], a.n)
            res["news"] = news.stock_news(q["name"], a.n)
        except Exception as e:
            res["error"] = str(e)
        out(res)
    elif a.cmd == "flash":
        from core import news
        out(news.flash_news(a.n))
    elif a.cmd == "flow":
        from core import news
        f = news.money_flow_today(a.symbol)
        out({"unit": "元（主力=超大单+大单）", "today": f, "daily": news.money_flow_daily(a.symbol, a.days)})
    elif a.cmd == "health":
        from core import health
        out(health.report(a.hours))
    elif a.cmd == "request-restart":
        from core import health
        out(health.request_restart(a.reason))
    elif a.cmd == "global":
        from core import global_market, news
        ov = global_market.overnight(ttl=0)
        res = {"overnight": ov, "regime_adjust": global_market.regime_adjust(ov),
               "note": "美股收盘在北京时间4~5点，主要影响A股当天开盘"}
        syms = a.symbols or [w["symbol"] for w in db.all("SELECT symbol FROM watchlist")] + \
            [p["symbol"] for p in broker.get_positions()]
        qs = market.get_quotes(syms) if syms else {}
        res["stocks"] = {}
        for s, q in qs.items():
            ind = news.industry(s, q["name"])
            res["stocks"][s] = {"name": q["name"], "industry": ind, "link": global_market.link_for(ind, ov)}
        out(res)
    elif a.cmd == "review":
        from core import review
        o = review.outcomes(a.account)
        o.pop("groups", None)
        out({"outcomes": o, "check": review.paper_check(a.account)})
    elif a.cmd == "backup":
        from core import housekeeping
        out(housekeeping.daily())
    elif a.cmd == "regime":
        from core import screener
        out(screener.market_regime())
    elif a.cmd == "emotion":
        from core import emotion
        out(emotion.sentiment())
    elif a.cmd == "screen":
        from core import screener
        out(screener.run(top_n=a.top, progress=lambda m: print(m, file=sys.stderr), account=a.account))
    elif a.cmd == "daily-close":
        from core import daily
        out(daily.run_daily_close(progress=lambda m: print(m, file=sys.stderr), force=a.force))
    elif a.cmd == "candidates":
        from core import screener
        out(screener.latest_candidates(30, a.account) or {"message": "还没有选股结果，先运行 screen"})
    elif a.cmd == "committee":
        from core import committee
        args = a.args

        def read(x):
            return json.loads(sys.stdin.read() if x == "-" else open(x, encoding="utf-8").read())
        if a.op == "start":
            out(committee.start_external(args[0], a.account, a.intent))
        elif a.op == "opinion":
            run = committee.get_run(int(args[0]))
            if not run or run["status"] != "external":
                raise SystemExit("会议 #%s 不存在或不是进行中的外部会议" % args[0])
            committee.save_opinion(int(args[0]), args[1], read(args[2]), a.round)
            done = [o["role"] for o in committee.get_run(int(args[0]))["opinions"]]
            out({"saved": args[1], "round": a.round, "done": done,
                 "remaining": [r for r in committee.ORDER if r not in done]})
        elif a.op == "final":
            out(committee.finalize(int(args[0]), read(args[1])))
        elif a.op == "run":
            out(committee.run_builtin(args[0], a.account, a.intent, trigger="cli",
                                      progress=lambda m: print(m, file=sys.stderr)))
        elif a.op == "show":
            out(committee.get_run(int(args[0])))
        elif a.op == "list":
            rows = db.all("SELECT id,ts,symbol,name,intent,mode,status,decision,confidence FROM committee_runs "
                          "ORDER BY id DESC LIMIT 30")
            for r in rows:
                r["ts"] = market.datetime.fromtimestamp(r["ts"], market.CN_TZ).strftime("%m-%d %H:%M")
            out(rows)
        elif a.op == "stats":
            out(committee.stats(a.horizon))
        elif a.op == "roles":
            print(open(committee.ROLES_PATH, encoding="utf-8").read())
    elif a.cmd == "plans":
        from core import plans
        qs = market.quotes_cached([p["symbol"] for p in plans.get_active()], 10)
        out([plans.brief(p, (qs.get(p["symbol"]) or {}).get("price")) for p in plans.get_active()])
    elif a.cmd == "plan-update":
        from core import plans
        out({"plan_id": plans.update(a.account, a.symbol, json.loads(a.changes), author="cli")})


if __name__ == "__main__":
    main()
