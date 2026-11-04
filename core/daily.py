"""每日收盘自动流程（不需要外部 AI）：

交易日 15:05 之后自动执行一次（engine 调度），也可手动：python3 agent_cli.py daily-close
  1. 全市场选股 → Top 候选
  2. 对前 N 只（跳过已持有、已有有效委员会结论的）用内置大模型开投研委员会
  3. 通过的加入自选（注明入场上限/止损），未通过的记录原因
  4. 推送一条收盘总结到微信，并写交易日志
  → 第二天盘中，每 10 分钟的内置 AI 看到"委员会已通过 + 价格合适"就会在模拟盘买入

需要：config.ai 内置大模型已启用（否则只做选股，不开委员会）。
"""
import logging
import time

from . import broker, committee, db, llm, market, notify, screener
from .config import load_config

log = logging.getLogger("daily")


def run_daily_close(progress=None, force=False, accounts=None):
    """每个交给 AI 的账户各跑一遍（模拟盘、实盘的选股和委员会互不共用）。默认只有模拟盘。"""
    cfg = load_config()
    if not force and not market.is_trading_day(market.now()):
        return {"skipped": "非交易日"}
    accts = accounts or [a for a in ("paper", "live") if a in cfg["ai"]["accounts"]] or ["paper"]
    out = {"accounts": {}, "approved": [], "reviewed": []}
    for a in accts:
        try:
            r = _close_one(a, progress)
        except Exception as e:
            log.exception("收盘流程失败 %s", a)
            r = {"error": str(e), "approved": [], "reviewed": []}
            notify.send("收盘流程失败（%s）" % ("实盘" if a == "live" else "模拟盘"), str(e))
        out["accounts"][a] = r
        out["approved"] += r.get("approved", [])
        out["reviewed"] += r.get("reviewed", [])
    if len(accts) == 1:
        return dict(out["accounts"][accts[0]], account=accts[0])
    return out


def _close_one(account, progress=None):
    say = progress or (lambda m: log.info(m))
    cfg = load_config()
    now = market.now()
    an = "实盘" if account == "live" else "模拟盘"
    t0 = time.time()
    say("每日收盘流程（%s）：选股中…" % an)
    res = screener.run(account=account)
    cands = res["candidates"]
    regime = res["regime"]
    summary = {"screen_id": res["screen_id"], "regime": regime.get("state"), "candidates": len(cands),
               "reviewed": [], "approved": [], "rejected": [], "errors": []}
    n = cfg["screener"].get("committee_top", 3)
    if regime.get("position_cap", 1) <= 0:
        say("大盘冰点，不开新仓，跳过委员会")
        n = 0
    if not llm.enabled():
        say("内置大模型未启用，只选股不开委员会")
        n = 0
    held = {p["symbol"] for p in broker.get_positions(account)}
    picked = []
    for c in cands:
        if len(picked) >= n:
            break
        if c["symbol"] in held or committee.approved_run(c["symbol"], account):
            continue
        picked.append(c)
    for c in picked:
        say("委员会审核 %s %s（选股第%d名 %.1f分）…" % (c["name"], c["symbol"], c["rank"], c["score"]))
        try:
            r = committee.run_builtin(c["symbol"], account, "open", trigger="每日收盘自动")
            item = {"symbol": c["symbol"], "name": c["name"], "run_id": r["run_id"], "decision": r.get("decision"),
                    "summary": r.get("summary", "")[:120]}
            summary["reviewed"].append(item)
            if r["status"] == "approved":
                summary["approved"].append(item)
                note = an + "委员会#%d通过：入场≤%s 止损%s 目标%s" % (r["run_id"], r.get("entry_max"), r.get("stop_loss"),
                                                      r.get("target"))
                db.run("INSERT OR REPLACE INTO watchlist VALUES(?,?,?,?,?)",
                       (c["symbol"], c["name"], note, "daily-close", time.time()))
            else:
                summary["rejected"].append(dict(item, why="；".join(r.get("gate_notes", [])) or r.get("summary", "")[:80]))
        except Exception as e:
            log.exception("委员会失败 %s", c["symbol"])
            summary["errors"].append("%s: %s" % (c["symbol"], e))
    summary["seconds"] = round(time.time() - t0)

    # 推送 + 交易日志
    snap = broker.snapshot(account)
    lines = ["**大盘：%s**（新开仓总仓位上限 %d%%）  " % (regime.get("state"), int(regime.get("position_cap", 0) * 100)),
             an + "：总资产 %.2f，今日 %s%%，持仓 %d 只" % (snap["equity"], snap["day_pnl_pct"], len(snap["positions"])), ""]
    if summary["approved"]:
        lines.append("✅ **委员会通过（%s）：**" % ("明天价格合适会自动买入" if account == "paper" else "AI 会生成建议单等你确认"))
        lines += ["- %s：%s" % (a["name"], a["summary"]) for a in summary["approved"]]
    if summary["rejected"]:
        lines.append("❌ 未通过：" + "；".join("%s（%s）" % (a["name"], a["why"][:40]) for a in summary["rejected"]))
    if not summary["reviewed"]:
        lines.append("今天没有送审（%s）" % ("大盘冰点" if regime.get("position_cap", 1) <= 0 else
                                       "内置大模型未启用" if not llm.enabled() else "候选都已持有或已有结论"))
    lines.append("\n选股前5：" + "、".join("%s(%.1f)" % (c["name"], c["score"]) for c in cands[:5]))
    notify.send(an + "收盘总结：%s，通过%d只" % (regime.get("state"), len(summary["approved"])), "\n".join(lines))
    try:
        from . import agent
        agent.journal_append("【每日收盘自动·" + an + " %s】大盘%s；送审%d只，通过：%s；未通过：%s" % (
            now.strftime("%m-%d"), regime.get("state"), len(summary["reviewed"]),
            "、".join(a["name"] for a in summary["approved"]) or "无",
            "、".join(a["name"] for a in summary["rejected"]) or "无"), author="daily-close")
    except Exception:
        pass
    say("每日收盘流程完成：通过 %d / 送审 %d，用时 %ds" % (len(summary["approved"]), len(summary["reviewed"]),
                                                summary["seconds"]))
    return summary
