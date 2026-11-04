"""系统体检：给维护 AI 看"哪里坏了、哪里需要改进"。python3 agent_cli.py health"""
import json
import os
import re
import time
from collections import Counter

from . import db, market
from .config import BASE_DIR, DATA_DIR, load_config

LOG_PATH = os.path.join(DATA_DIR, "app.log")
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) \[([\w.]+)\] (.*)$")


def _scan_log(hours=24, max_bytes=3_000_000):
    out = {"errors": Counter(), "warnings": Counter(), "recent_errors": [], "recent_warnings": []}
    if not os.path.exists(LOG_PATH):
        out["note"] = "没有日志文件（程序还没运行过？）"
        return out
    size = os.path.getsize(LOG_PATH)
    with open(LOG_PATH, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        text = f.read().decode("utf-8", errors="replace")
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m or m.group(1) < cutoff:
            continue
        ts, level, name, msg = m.groups()
        # 归一化：去掉数字，便于同类合并
        key = "[%s] %s" % (name, re.sub(r"\d+(\.\d+)?", "N", msg)[:120])
        if level in ("ERROR", "CRITICAL"):
            out["errors"][key] += 1
            out["recent_errors"].append("%s %s" % (ts[5:], line[len(ts) + 4:][:220]))
        elif level == "WARNING":
            out["warnings"][key] += 1
            out["recent_warnings"].append("%s %s" % (ts[5:], line[len(ts) + 4:][:220]))
    out["errors"] = out["errors"].most_common(15)
    out["warnings"] = out["warnings"].most_common(15)
    out["recent_errors"] = out["recent_errors"][-10:]
    out["recent_warnings"] = out["recent_warnings"][-10:]
    return out


def report(hours=24):
    since = time.time() - hours * 3600
    cfg = load_config()
    res = {"now": market.now().strftime("%Y-%m-%d %H:%M"), "session": market.session_label(), "hours": hours}
    res["log"] = _scan_log(hours)
    # 数据新鲜度
    q = db.one("SELECT MAX(updated_at) t FROM quotes")
    res["quotes_age_min"] = round((time.time() - q["t"]) / 60, 1) if q and q["t"] else None
    res["ticks_today"] = os.path.isdir(os.path.join(DATA_DIR, "ticks", market.now().strftime("%Y%m%d")))
    # AI 决策质量
    decs = db.all("SELECT agent,trigger,results FROM decisions WHERE ts>=?", (since,))
    rej = Counter()
    n_act = 0
    for d in decs:
        for a in (db.jloads(d["results"], {}) or {}).get("actions", []):
            n_act += 1
            if a.get("status") == "rejected":
                rej[re.sub(r"\d+(\.\d+)?", "N", str(a.get("error")))[:80]] += 1
    res["decisions"] = {"count": len(decs), "by_agent": Counter(d["agent"] for d in decs).most_common(),
                        "actions": n_act, "rejected_reasons": rej.most_common(10)}
    # 委员会
    runs = db.all("SELECT status,error FROM committee_runs WHERE ts>=?", (since,))
    res["committee"] = {"runs": len(runs), "by_status": Counter(r["status"] for r in runs).most_common(),
                        "errors": [r["error"][:150] for r in runs if r["error"]][:5],
                        "stuck_external": db.one("SELECT COUNT(*) n FROM committee_runs WHERE status IN "
                                                 "('running','external') AND ts<?", (time.time() - 3600,))["n"]}
    # 推送
    msgs = db.all("SELECT ok,channels FROM messages WHERE ts>=?", (since,))
    res["notify"] = {"sent": len(msgs), "failed": sum(1 for m in msgs if not m["ok"]),
                     "configured": bool(cfg["notify"]["pushplus_token"] or cfg["notify"]["serverchan_key"])}
    res["signals"] = db.all("SELECT source,strategy,side,COUNT(*) n FROM alerts WHERE ts>=? "
                            "GROUP BY source,strategy,side ORDER BY n DESC LIMIT 15", (since,))
    # 账户
    from . import broker
    s = broker.snapshot("paper")
    res["paper"] = {k: s[k] for k in ("equity", "cash", "total_pnl_pct", "day_pnl_pct", "today_trades")}
    res["paper"]["positions"] = len(s["positions"])
    res["config"] = {"llm_enabled": cfg["ai"]["llm_enabled"], "llm_model": cfg["ai"]["llm_model"],
                     "llm_interval_min": cfg["ai"]["llm_interval_min"], "ai_accounts": cfg["ai"]["accounts"],
                     "sizing_mode": cfg["sizing"]["mode"], "boards": cfg["universe"]["boards"]}
    res["restart_pending"] = os.path.exists(os.path.join(DATA_DIR, "restart.request"))
    # 问题清单
    issues = []
    if res["log"]["errors"]:
        issues.append("日志里有 %d 类错误，见 log.errors" % len(res["log"]["errors"]))
    if res["quotes_age_min"] and res["quotes_age_min"] > 30 and market.is_market_open():
        issues.append("交易时段行情已 %.0f 分钟没更新：监控可能停了" % res["quotes_age_min"])
    if res["committee"]["errors"]:
        issues.append("委员会有出错的会议")
    if res["committee"]["stuck_external"]:
        issues.append("%d 个委员会会议卡在进行中超过1小时" % res["committee"]["stuck_external"])
    if not res["notify"]["configured"]:
        issues.append("微信推送未配置（PushPlus/Server酱）")
    elif res["notify"]["failed"]:
        issues.append("推送失败 %d 次" % res["notify"]["failed"])
    if n_act and sum(rej.values()) / n_act > 0.6:
        issues.append("AI 动作被风控拒绝比例 %.0f%%，提示词或仓位逻辑可能和规则不匹配" % (sum(rej.values()) / n_act * 100))
    res["issues"] = issues
    return res


def request_restart(reason):
    path = os.path.join(DATA_DIR, "restart.request")
    with open(path, "w", encoding="utf-8") as f:
        f.write("%s %s" % (market.now().strftime("%m-%d %H:%M"), reason))
    return {"requested": True, "note": "程序将在非交易时段（下次监控循环，约15秒内）自动重启；交易时段会等到午休或收盘",
            "flag": os.path.relpath(path, BASE_DIR)}
