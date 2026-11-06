"""事后对账 + 模拟盘体检：用数据回答「委员会是不是太严」「模拟盘能不能上实盘」。

1) 委员会事后对账（outcomes）
   每次委员会审过的票，记下审核时的价格，之后看 1/3/5 个交易日的收盘涨跌、5 天内最高涨到多少、最低跌到多少。
   按结论分组：通过 / 等回踩 / 没通过；另外看「进了选股前 10 但没送审」的票。
   - 没通过的票后来普遍大涨 → 委员会偏严，错过机会
   - 没通过的票后来普遍下跌 → 委员会在帮你躲坑
2) 模拟盘体检（paper_check）
   跑了多少个交易日、平仓多少笔、胜率、盈亏比、最大回撤、总收益；对照「上实盘前的门槛」逐条打勾。
每周五收盘流程结束后推送一次周报（kind=weekly，分账户），网页「持仓战绩」页也能看。
"""
import logging
import time

from . import db, market

log = logging.getLogger("review")

# 上实盘前的门槛（只是建议，不会自动开实盘）
READY = {"min_days": 20, "min_closed": 20, "min_profit_factor": 1.2, "max_drawdown_pct": 10}


def _day(ts):
    return market.datetime.fromtimestamp(ts, market.CN_TZ).strftime("%Y-%m-%d")


def _after_bars(sym, ts, cache):
    if sym not in cache:
        try:
            cache[sym] = market.get_kline(sym, "day", 60)
        except Exception:
            cache[sym] = []
    d0 = _day(ts)
    # 15:00 前开的会，当天收盘也算「之后」的第 0 天不计；只看之后的交易日
    return [b for b in cache[sym] if b["t"] > d0]


def _track(sym, ts, price, cache):
    bars = _after_bars(sym, ts, cache)
    if not price or not bars:
        return {"days": 0}
    out = {"days": len(bars)}
    for k in (1, 3, 5):
        if len(bars) >= k:
            out["ret%d" % k] = round((bars[k - 1]["c"] / price - 1) * 100, 2)
    first5 = bars[:5]
    out["max_up5"] = round((max(b["h"] for b in first5) / price - 1) * 100, 2)
    out["max_dn5"] = round((min(b["l"] for b in first5) / price - 1) * 100, 2)
    return out


def _group_stats(rows):
    def avg(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return round(sum(v) / len(v), 2) if v else None
    return {"n": len(rows), "with_3d": sum(1 for r in rows if r.get("ret3") is not None),
            "avg_ret1": avg("ret1"), "avg_ret3": avg("ret3"), "avg_ret5": avg("ret5"),
            "hit_up8": sum(1 for r in rows if (r.get("max_up5") or 0) >= 8),
            "hit_dn5": sum(1 for r in rows if (r.get("ret5") if r.get("ret5") is not None else r.get("ret3") or 0) <= -5)}


def outcomes(account="paper", lookback_days=30):
    since = time.time() - lookback_days * 86400
    cache, groups = {}, {"approved": [], "wait": [], "rejected": [], "unreviewed": []}
    seen = set()
    for r in db.all("SELECT id,ts,symbol,name,status,decision,price FROM committee_runs WHERE ts>=? "
                    "AND COALESCE(account,'paper')=? AND status IN ('approved','rejected') ORDER BY id", (since, account)):
        t = _track(r["symbol"], r["ts"], r["price"], cache)
        row = dict(t, id=r["id"], symbol=r["symbol"], name=r["name"], date=_day(r["ts"])[5:], price=r["price"])
        g = "approved" if r["status"] == "approved" else ("wait" if r["decision"] == "wait" else "rejected")
        groups[g].append(row)
        seen.add((r["symbol"], _day(r["ts"])))
    for s in db.all("SELECT id,ts FROM screens WHERE ts>=? AND COALESCE(account,'paper')=? ORDER BY id",
                    (since, account)):
        for c in db.all("SELECT symbol,name,price FROM candidates WHERE screen_id=? AND rank<=10", (s["id"],)):
            if (c["symbol"], _day(s["ts"])) in seen:
                continue
            seen.add((c["symbol"], _day(s["ts"])))
            t = _track(c["symbol"], s["ts"], c["price"], cache)
            groups["unreviewed"].append(dict(t, symbol=c["symbol"], name=c["name"], date=_day(s["ts"])[5:], price=c["price"]))
    stats = {k: _group_stats(v) for k, v in groups.items()}
    rej = groups["rejected"] + groups["wait"]
    missed = sorted([r for r in rej if (r.get("max_up5") or 0) >= 8], key=lambda r: -r["max_up5"])[:5]
    dodged = sorted([r for r in rej if (r.get("ret5") if r.get("ret5") is not None else r.get("ret3") or 0) <= -5],
                    key=lambda r: r.get("ret5") if r.get("ret5") is not None else r.get("ret3"))[:5]
    verdict = None
    s_rej = _group_stats(rej)
    if s_rej["with_3d"] >= 8 and s_rej["avg_ret3"] is not None:
        if s_rej["avg_ret3"] >= 2 or s_rej["hit_up8"] >= max(3, s_rej["n"] * 0.25):
            verdict = "偏严：没通过的票之后平均涨 %.1f%%，%d 只 5 天内涨超 8%%，可能错过了机会" % (s_rej["avg_ret3"], s_rej["hit_up8"])
        elif s_rej["avg_ret3"] <= -1:
            verdict = "合理：没通过的票之后平均跌 %.1f%%，委员会帮你躲开了" % s_rej["avg_ret3"]
        else:
            verdict = "说不清：没通过的票之后平均 %+.1f%%，和不买差不多" % s_rej["avg_ret3"]
    return {"account": account, "lookback_days": lookback_days, "stats": stats, "missed": missed, "dodged": dodged,
            "verdict": verdict or "样本还不够（被否且已过 3 个交易日的少于 8 只），再跑一段时间", "groups": groups,
            "watch": _watch_stats(account, since)}


def _watch_stats(account, since):
    rows = db.all("SELECT status, COUNT(*) n FROM entry_watch WHERE account=? AND created_at>=? GROUP BY status",
                  (account, since))
    d = {r["status"]: r["n"] for r in rows}
    reruns = db.all("SELECT r.status FROM entry_watch w JOIN committee_runs r ON r.id=w.rerun_id "
                    "WHERE w.account=? AND w.created_at>=?", (account, since))
    d["rerun_approved"] = sum(1 for r in reruns if r["status"] == "approved")
    return d


def paper_check(account="paper"):
    a = db.one("SELECT * FROM accounts WHERE id=?", (account,)) or {}
    eq = db.all("SELECT ts, equity FROM equity WHERE account=? ORDER BY ts", (account,))
    days = len({_day(r["ts"]) for r in eq})
    peak, mdd = 0, 0.0
    for r in eq:
        peak = max(peak, r["equity"])
        if peak:
            mdd = max(mdd, (1 - r["equity"] / peak) * 100)
    closed = db.all("SELECT pnl, amount FROM trades WHERE account=? AND side='sell' AND pnl IS NOT NULL", (account,))
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [t["pnl"] for t in closed if t["pnl"] <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) else (None if not wins else float("inf"))
    init = a.get("initial_cash") or 0
    cur = eq[-1]["equity"] if eq else init
    ret = (cur / init - 1) * 100 if init else None
    checks = [
        ("跑满 %d 个交易日" % READY["min_days"], days >= READY["min_days"], "%d 天" % days),
        ("平仓满 %d 笔" % READY["min_closed"], len(closed) >= READY["min_closed"], "%d 笔" % len(closed)),
        ("盈亏比 ≥ %.1f（赚的总额 ÷ 亏的总额）" % READY["min_profit_factor"],
         pf is not None and pf >= READY["min_profit_factor"], "—" if pf is None else ("∞" if pf == float("inf") else "%.2f" % pf)),
        ("最大回撤 ≤ %d%%" % READY["max_drawdown_pct"], mdd <= READY["max_drawdown_pct"] and days > 0, "%.1f%%" % mdd),
        ("总收益为正", ret is not None and ret > 0, "—" if ret is None else "%+.2f%%" % ret),
    ]
    ok = all(c[1] for c in checks)
    return {"account": account, "days": days, "closed": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "avg_pnl": round(sum(t["pnl"] for t in closed) / len(closed), 2) if closed else None,
            "profit_factor": None if pf is None else (999 if pf == float("inf") else round(pf, 2)),
            "max_drawdown_pct": round(mdd, 2), "total_return_pct": None if ret is None else round(ret, 2),
            "checks": [{"name": c[0], "ok": c[1], "value": c[2]} for c in checks], "ready": ok,
            "advice": "各项都达标，可以考虑小资金实盘（每单仍需你确认）" if ok else "继续模拟：还有没达标的项"}


def weekly_text(account="paper"):
    o, p = outcomes(account, 30), paper_check(account)
    nm = "模拟盘" if account == "paper" else "实盘"
    st = o["stats"]

    def line(k, label):
        s = st[k]
        if not s["n"]:
            return None
        return "%s %d 只：之后 1/3/5 天平均 %s / %s / %s，5 天内涨超 8%% 的 %d 只" % (
            label, s["n"], _p(s["avg_ret1"]), _p(s["avg_ret3"]), _p(s["avg_ret5"]), s["hit_up8"])
    lines = ["**委员会事后对账（近 30 天）**"]
    lines += [x for x in (line("approved", "✅ 通过"), line("wait", "⏳ 等回踩"), line("rejected", "❌ 没通过"),
                          line("unreviewed", "· 前10没送审")) if x] or ["还没有可对账的会议"]
    lines.append("结论：" + o["verdict"])
    if o["missed"]:
        lines.append("错过的：" + "、".join("%s(%s 最高%+.0f%%)" % (r["name"], r["date"], r["max_up5"]) for r in o["missed"][:3]))
    if o["dodged"]:
        lines.append("躲过的：" + "、".join("%s(%s %+.0f%%)" % (r["name"], r["date"], r.get("ret5") if r.get("ret5") is not None else r.get("ret3"))
                                         for r in o["dodged"][:3]))
    w = o["watch"]
    if w:
        lines.append("等回踩：登记 %d、到价复审 %d（复审通过 %d）、作废 %d、过期 %d" % (
            sum(v for k, v in w.items() if k != "rerun_approved"), w.get("triggered", 0), w.get("rerun_approved", 0),
            w.get("invalid", 0), w.get("expired", 0)))
    lines += ["", "**%s体检**：%s" % (nm, p["advice"])]
    lines += ["%s %s：%s" % ("✅" if c["ok"] else "⬜", c["name"], c["value"]) for c in p["checks"]]
    if p["closed"]:
        lines.append("胜率 %.0f%%，平均每笔 %+.2f" % (p["win_rate"], p["avg_pnl"]))
    return "%s周报：%s" % (nm, o["verdict"][:20]), "\n".join(lines)


def _p(x):
    return "--" if x is None else "%+.1f%%" % x


def send_weekly(account="paper"):
    from . import notify
    title, content = weekly_text(account)
    return notify.send(title, content, kind="weekly", account=account)
