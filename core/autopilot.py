"""AI 自动驾驶：给网页"看"的接口 —— 今天 AI 做了什么（时间线）、自动流程每一环的状态。

设计（一个大脑，两只手）：
  大脑（两个账户共用）：选股 → 投研委员会 → AI 决策（每 N 分钟 + 异动唤醒）
  模拟盘这只手：AI 决策直接成交，止损/止盈/时间止损程序自动执行 —— 你只看
  实盘这只手：AI 决策只生成"待确认订单"，你确认后自己去券商 App 下单
"""
import json
import os
import time

from . import db, market
from .config import DATA_DIR, load_config

ACT_CN = {"buy": "买入", "sell": "卖出"}


def _day_start(days):
    now = market.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return start - (days - 1) * 86400


def _fmt(ts):
    return market.datetime.fromtimestamp(ts, market.CN_TZ).strftime("%m-%d %H:%M")


def timeline(account="paper", days=3, limit=200):
    """把 选股 / 委员会 / AI 决策 / 成交 合成一条时间线（新→旧）。"""
    since = _day_start(days)
    ev = []
    for s in db.all("SELECT * FROM screens WHERE ts>=? AND COALESCE(NULLIF(account,''),'paper')=? ORDER BY id DESC",
                    (since, account)):
        st, rg = db.jloads(s["stats"], {}) or {}, db.jloads(s["regime"], {}) or {}
        top = db.all("SELECT name FROM candidates WHERE screen_id=? ORDER BY rank LIMIT 5", (s["id"],))
        ev.append({"ts": s["ts"], "kind": "screen", "icon": "🔍", "title": "选股：全市场 %s → 过滤后 %s → 候选 %s 只" % (
            st.get("universe", "?"), st.get("passed", "?"),
            db.one("SELECT COUNT(*) n FROM candidates WHERE screen_id=?", (s["id"],))["n"]),
            "detail": "大盘：%s（新开仓上限 %s%%）· 前几名：%s" % (
                rg.get("state", "?"), int(float(rg.get("position_cap", 0) or 0) * 100),
                "、".join(t["name"] for t in top) or "-"), "link": "screen"})
    for r in db.all("SELECT * FROM committee_runs WHERE ts>=? AND COALESCE(NULLIF(account,''),'paper')=? ORDER BY id DESC",
                    (since, account)):
        f = db.jloads(r["final"], {}) or {}
        st = {"approved": ("✅", "通过"), "rejected": ("❌", "否决"), "error": ("⚠️", "出错"),
              "running": ("⏳", "开会中"), "external": ("⏳", "外部AI开会中")}.get(r["status"], ("•", r["status"]))
        detail = f.get("thesis") or f.get("summary") or r["error"] or ""
        if r["status"] == "approved":
            detail = "入场≤%s 止损 %s 目标 %s · %s" % (f.get("entry_max"), f.get("stop_loss"), f.get("target"), detail)
        ev.append({"ts": r["ts"], "kind": "committee", "icon": st[0],
                   "title": "委员会#%d %s %s：%s%s" % (r["id"], r["name"] or "", r["symbol"], st[1],
                                                  " · 置信度 %.2f" % r["confidence"] if r["confidence"] is not None else ""),
                   "detail": detail[:220], "link": "committee", "run_id": r["id"], "trigger": r["trigger"]})
    for d in db.all("SELECT * FROM decisions WHERE ts>=? ORDER BY id DESC", (since,)):
        res = db.jloads(d["results"], {}) or {}
        acts = [a for a in res.get("actions", []) if a.get("account") == account]
        lines = []
        for a in acts:
            if a.get("status") == "filled":
                continue            # 成交单独作为事件显示
            nm = a.get("name") or a.get("symbol")
            if a.get("status") == "pending_confirm":
                lines.append("📝 %s" % a.get("summary"))
            elif a.get("status") == "committee_running":
                lines.append("⏳ 想%s %s：先送委员会#%s 审核" % (ACT_CN.get(a.get("action"), ""), nm, a.get("committee_run")))
            else:
                lines.append("🚫 没执行 %s %s：%s" % (ACT_CN.get(a.get("action"), ""), nm, a.get("error")))
        view = (d["market_view"] or "").strip()
        if not view and not lines:
            continue
        ev.append({"ts": d["ts"], "kind": "decision", "icon": "🧠",
                   "title": "AI 巡检（%s）%s" % (d["trigger"] or d["agent"] or "", "" if acts else "：不操作"),
                   "detail": view[:260], "lines": lines, "link": "ai"})
    for t in db.all("SELECT * FROM trades WHERE account=? AND ts>=? ORDER BY id DESC", (account, since)):
        src = {"ai": "AI", "stop": "止损", "plan": "持仓计划", "manual": "手动", "rule": "规则"}.get(t["source"], t["source"])
        pnl = "，盈亏 %+.2f" % t["pnl"] if t["pnl"] is not None else ""
        ev.append({"ts": t["ts"], "kind": "trade", "icon": "🟥" if t["side"] == "buy" else "🟩",
                   "title": "%s %s %s %d股 @%.3f%s" % (src, ACT_CN.get(t["side"], t["side"]), t["name"], t["shares"],
                                                      t["price"], pnl),
                   "detail": (t["note"] or "")[:200], "link": "orders", "side": t["side"], "pnl": t["pnl"]})
    if account == "live":
        for o in db.all("SELECT * FROM orders WHERE account='live' AND created_at>=? ORDER BY id DESC", (since,)):
            ev.append({"ts": o["created_at"], "kind": "order", "icon": "📝",
                       "title": "待确认单#%d %s %s %d股（%s）" % (o["id"], ACT_CN.get(o["side"], o["side"]), o["name"],
                                                          o["shares"], o["status"]),
                       "detail": (o["reason"] or "")[:200], "link": "orders"})
    ev.sort(key=lambda e: e["ts"], reverse=True)
    for e in ev:
        e["time"] = _fmt(e["ts"])
    return ev[:limit]


def status(monitor=None, account="paper"):
    """自动流程每一环的状态 + 今日统计，给"AI 自动驾驶"卡片用。"""
    cfg = load_config()
    from . import llm
    now = market.now()
    today = now.strftime("%Y%m%d")
    t0 = _day_start(1)
    trading_day = market.is_trading_day(now)
    hm = now.hour * 100 + now.minute
    last_dec = db.one("SELECT ts,trigger FROM decisions ORDER BY id DESC LIMIT 1")
    last_screen = db.one("SELECT ts FROM screens WHERE COALESCE(NULLIF(account,''),'paper')=? ORDER BY id DESC LIMIT 1", (account,))
    try:
        with open(os.path.join(DATA_DIR, "daily_close.done"), encoding="utf-8") as f:
            daily_done = f.read().strip() == today
    except OSError:
        daily_done = False
    mon = monitor.status if monitor is not None else {}
    iv = cfg["ai"].get("llm_interval_min") or 0
    llm_on = llm.enabled()

    def step(name, when, state, note=""):
        return {"name": name, "when": when, "state": state, "note": note}
    is_open = trading_day and bool(mon.get("market_open"))
    if not trading_day:
        phase, phase_note = "off", "今天休市"
    elif hm < 930:
        phase, phase_note = "wait", "9:30 开盘后开始"
    elif 1130 <= hm < 1300:
        phase, phase_note = "wait", "午休，13:00 继续"
    elif hm >= 1500:
        phase, phase_note = "done", "今天已收盘"
    elif is_open:
        phase, phase_note = "run", ""
    else:
        phase, phase_note = "wait", "等待监控更新"

    pre = db.one("SELECT ts FROM messages WHERE title LIKE '盘前外盘%' AND ok=1 AND ts>=? AND ts<? "
                 "ORDER BY ts DESC LIMIT 1", (t0, t0 + 86400))
    steps = [
        step("盘前简报", "09:00", "done" if pre else ("wait" if trading_day and hm < 930 else "off"),
             ("已推送 %s" % _fmt(pre["ts"])[6:]) if pre else
             ("未记录成功推送" if trading_day and hm >= 930 else "推送隔夜外盘、今日计划")),
        step("AI 巡检决策", "交易时段每 %d 分钟 + 异动唤醒" % iv if iv else "未开启定时",
             phase if llm_on and iv else "off",
             (phase_note if llm_on and iv else "") or ("上次：%s" % _fmt(last_dec["ts"]) if last_dec else "还没有决策")),
        step("盯盘 · 止损止盈", "交易时段每 %s 秒" % cfg["monitor"]["poll_interval_sec"], phase,
             phase_note or ("模拟盘自动执行" if cfg["plans"].get("auto_stop_loss_paper", True) else "只推送提醒")),
        step("收盘选股 + 委员会", "15:05", "done" if daily_done else ("wait" if trading_day else "off"),
             mon.get("daily") or ("上次选股：%s" % _fmt(last_screen["ts"]) if last_screen else "还没选过")),
    ]
    today_cnt = {
        "decisions": db.one("SELECT COUNT(*) n FROM decisions WHERE ts>=?", (t0,))["n"],
        "committee": db.one("SELECT COUNT(*) n FROM committee_runs WHERE ts>=? AND COALESCE(NULLIF(account,''),'paper')=?", (t0, account))["n"],
        "approved": db.one("SELECT COUNT(*) n FROM committee_runs WHERE ts>=? AND COALESCE(NULLIF(account,''),'paper')=? AND status='approved'",
                           (t0, account))["n"],
    }
    for a in ("paper", "live"):
        today_cnt["trades_" + a] = db.one("SELECT COUNT(*) n FROM trades WHERE account=? AND ts>=?", (a, t0))["n"]
    today_cnt["pending"] = db.one("SELECT COUNT(*) n FROM orders WHERE account='live' AND status='pending_confirm'")["n"]
    return {"ai_accounts": cfg["ai"]["accounts"], "llm_enabled": llm_on, "llm_model": cfg["ai"].get("llm_model"),
            "phase_note": phase_note, "trading_day": trading_day, "market_open": phase == "run",
            "session": mon.get("session"), "steps": steps, "today": today_cnt,
            "committee_required": cfg["committee"]["required_for_buy"]}
