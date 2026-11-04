"""11-Agent 投研委员会（建仓/加仓前的门禁）。

流程：
  ① 5 位分析师并行：technical 技术 / capital 资金 / news 消息 / fundamental 基本面 / sentiment 情绪题材
  ② 多空辩论：bull 多头 ⇄ bear 空头，debate_rounds 轮，每轮必须反驳对方
  ③ research_manager 研究主管：裁判
  ④ trader 交易员：入场/止损/目标/仓位
  ⑤ risk 风控官：一票否决
  ⑥ pm 组合经理：最终拍板 + 持仓计划（失效条件/观察点）

两种运行方式，产出完全一样（都写 committee_runs / committee_opinions）：
  内置：run_builtin(symbol) —— Python 直接调用 OpenAI 兼容接口（DeepSeek/通义）
  外部：start_external(symbol) 生成档案 → 外部 AI 依次扮演角色，用 submit_opinion() 提交 → submit_final()
角色提示词在 prompts/committee_roles.md，两种方式共用。
"""
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import agent, broker, db, indicators, market, news
from .config import BASE_DIR, DATA_DIR, load_config

log = logging.getLogger("committee")

ROLES_PATH = os.path.join(BASE_DIR, "prompts", "committee_roles.md")
ANALYSTS = ["technical", "capital", "news", "fundamental", "sentiment"]
ROLE_NAMES = {"technical": "技术分析师", "capital": "资金分析师", "news": "消息分析师", "fundamental": "基本面分析师",
              "sentiment": "情绪题材分析师", "bull": "多头研究员", "bear": "空头研究员",
              "research_manager": "研究主管", "trader": "交易员", "risk": "风控官", "pm": "组合经理"}
ORDER = ANALYSTS + ["bull", "bear", "research_manager", "trader", "risk", "pm"]
DOSSIER_DIR = os.path.join(DATA_DIR, "committee")


def load_roles():
    with open(ROLES_PATH, encoding="utf-8") as f:
        text = f.read()
    parts = re.split(r"^## +(\w+)\s*$", text, flags=re.M)
    roles = {}
    for i in range(1, len(parts) - 1, 2):
        roles[parts[i]] = parts[i + 1].strip()
    return roles


def system_prompt(role, roles=None):
    roles = roles or load_roles()
    return roles.get("_common", "") + "\n\n---\n" + roles[role]


# ---------------- 档案 ----------------
def build_dossier(sym, account="paper", intent="open"):
    from . import screener
    sym = market.normalize(sym)
    q = market.get_quote(sym)
    kl = indicators.merge_live(market.get_kline(sym, "day", 150), q, market.today_str())
    try:
        wk = market.get_kline(sym, "week", 60)
    except Exception:
        wk = []
    it = news.intel(sym, q["name"], q, news_n=12)
    chk = news.distribution_check(q, indicators.summarize(kl), it)
    fund = news.fundamentals(sym) if market.board(sym) != "etf" else {"note": "ETF 无个股财务"}
    try:
        regime = screener.market_regime()
    except Exception as e:
        regime = {"error": str(e)}
    sector = None
    try:
        secs = screener.sectors()
        ind = fund.get("industry")
        if ind and ind in secs:
            sector = dict(secs[ind], name=ind)
    except Exception:
        pass
    snap = broker.snapshot(account)
    cand = None
    try:
        lc = screener.latest_candidates(50, account)
        if lc:
            cand = next((c for c in lc["candidates"] if c["symbol"] == sym), None)
    except Exception:
        pass
    plan = db.one("SELECT * FROM plans WHERE account=? AND symbol=? AND status='active'", (account, sym))
    glob = None
    try:
        from . import global_market
        ov = global_market.overnight()
        if ov:
            glob = {"summary": ov["summary"], "quotes": {v["name"]: v["pct"] for v in ov["quotes"].values()},
                    "this_stock": global_market.link_for(fund.get("industry") or q["name"], ov)}
    except Exception:
        pass
    return {
        "symbol": sym, "name": q["name"], "board": market.BOARD_NAMES[market.board(sym)], "intent": intent,
        "as_of": market.now().strftime("%Y-%m-%d %H:%M"), "session": market.session_label(),
        "quote": agent._compact_quote(q),
        "daily": indicators.summarize(kl),
        "daily_last20": [[b["t"][5:], b["o"], b["c"], b["h"], b["l"], int(b["v"])] for b in kl[-20:]],
        "weekly": indicators.summarize(wk) if wk else None,
        "intel": it,
        "a_share_pattern": {"type": "疑似借利好出货" if chk[0] == "sell" else "疑似利空洗盘", "score": chk[1],
                            "reasons": chk[2]} if chk else None,
        "fundamentals": fund, "sector": sector, "market_regime": regime, "global_overnight": glob,
        "screener": cand,
        "account": {"account": account, "equity": snap["equity"], "cash": snap["cash"],
                    "positions": [{k: p[k] for k in ("symbol", "name", "shares", "avg_cost", "pnl_pct", "weight_pct")}
                                  for p in snap["positions"]],
                    "day_pnl_pct": snap["day_pnl_pct"]},
        "existing_plan": plan,
        "risk_limits": load_config()["risk"], "lot": market.lot_rule(sym)[0],
        "one_lot_cost": round(q["price"] * market.lot_rule(sym)[0], 2),
    }


# ---------------- 存储 ----------------
def _new_run(sym, name, account, intent, mode, trigger, price=None):
    """先建会议记录（网页立刻能看到），档案稍后补上。"""
    return db.run("INSERT INTO committee_runs(ts,symbol,name,account,intent,mode,trigger,status,price)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (time.time(), sym, name, account, intent, mode, trigger, "running", price))


def _save_dossier(rid, dossier):
    os.makedirs(DOSSIER_DIR, exist_ok=True)
    path = os.path.join(DOSSIER_DIR, "run_%d_dossier.json" % rid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dossier, f, ensure_ascii=False, indent=1, default=str)
    db.run("UPDATE committee_runs SET dossier_path=?,name=?,price=? WHERE id=?",
           (path, dossier.get("name"), (dossier.get("quote") or {}).get("price"), rid))


def _create_run(sym, name, account, intent, mode, trigger, price, dossier):
    rid = _new_run(sym, name, account, intent, mode, trigger, price)
    _save_dossier(rid, dossier)
    return rid


# 进行中会议的进度（内存，给网页显示）：run_id -> {stage, step, total, started}
PROGRESS = {}


def _progress(rid, step, total, stage):
    PROGRESS[rid] = {"stage": stage, "step": step, "total": total,
                     "started": PROGRESS.get(rid, {}).get("started", time.time())}
    log.info("委员会#%d [%d/%d] %s", rid, step, total, stage)


def save_opinion(run_id, role, op, rnd=1):
    if role not in ROLE_NAMES:
        raise ValueError("未知角色 %s，可选：%s" % (role, ", ".join(ORDER)))
    if isinstance(op, str):
        op = json.loads(op)
    db.run("INSERT INTO committee_opinions(run_id,role,round,ts,stance,score,confidence,summary,content)"
           " VALUES(?,?,?,?,?,?,?,?,?)",
           (run_id, role, rnd, time.time(), op.get("stance"), _f(op.get("score")), _f(op.get("confidence")),
            op.get("summary", "")[:300], json.dumps(op, ensure_ascii=False)))
    return op


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def finalize(run_id, final):
    """组合经理结论落库。决定是否通过门禁。"""
    if isinstance(final, str):
        final = json.loads(final)
    cfg = load_config()["committee"]
    run = db.one("SELECT * FROM committee_runs WHERE id=?", (run_id,))
    if not run:
        raise ValueError("没有这个委员会会议 #%s" % run_id)
    risk = db.one("SELECT content FROM committee_opinions WHERE run_id=? AND role='risk' ORDER BY id DESC LIMIT 1",
                  (run_id,))
    risk = db.jloads(risk["content"], {}) if risk else {}
    decision = (final.get("decision") or "pass").lower()
    conf = _f(final.get("confidence")) or 0
    notes = []
    if risk.get("veto") and decision in ("buy", "add"):
        decision = "pass"; notes.append("风控官否决：%s" % risk.get("veto_reason"))
    if decision in ("buy", "add") and conf < cfg["min_confidence"]:
        decision = "pass"; notes.append("组合经理置信度 %.2f < %.2f" % (conf, cfg["min_confidence"]))
    if risk.get("max_position_pct") is not None and final.get("position_pct"):
        final["position_pct"] = min(float(final["position_pct"]), float(risk["max_position_pct"]))
    final["decision"] = decision
    if notes:
        final["gate_notes"] = notes
    status = "approved" if decision in ("buy", "add") else "rejected"
    if run["status"] == "running" or run["status"] == "external":
        save_opinion(run_id, "pm", final, 1)
    db.run("UPDATE committee_runs SET status=?,decision=?,confidence=?,final=?,finished_at=? WHERE id=?",
           (status, decision, conf, json.dumps(final, ensure_ascii=False), time.time(), run_id))
    return dict(final, run_id=run_id, status=status)


def approved_run(sym, account="paper"):
    """门禁：该账户有效期内最近一次通过的委员会结论（模拟盘和实盘的委员会互不通用）。"""
    hours = load_config()["committee"]["valid_hours"]
    rows = db.all("SELECT * FROM committee_runs WHERE symbol=? AND COALESCE(NULLIF(account,''),'paper')=? AND status='approved' "
                  "AND finished_at>=? ORDER BY id DESC LIMIT 1",
                  (market.normalize(sym), account or "paper", time.time() - hours * 3600))
    if not rows:
        return None
    r = rows[0]
    r["final"] = db.jloads(r["final"], {})
    return r


def get_run(run_id):
    r = db.one("SELECT * FROM committee_runs WHERE id=?", (run_id,))
    if not r:
        return None
    r["final"] = db.jloads(r["final"], {})
    r["opinions"] = db.all("SELECT role,round,ts,stance,score,confidence,summary,content FROM committee_opinions "
                           "WHERE run_id=? ORDER BY id", (run_id,))
    for o in r["opinions"]:
        o["content"] = db.jloads(o["content"], {})
        o["role_name"] = ROLE_NAMES.get(o["role"], o["role"])
    p = PROGRESS.get(run_id)
    if p:
        r["progress"] = dict(p, elapsed=round(time.time() - p["started"]))
    elif r["status"] == "running" and time.time() - (r["ts"] or 0) > 900:
        r["progress"] = {"stage": "可能已中断（程序重启过？）", "step": 0, "total": 1, "elapsed": 0, "stale": True}
    return r


# ---------------- 内置模式 ----------------
def _ask(role, payload, roles, cfg):
    from . import llm
    msgs = [{"role": "system", "content": system_prompt(role, roles)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]
    last = None
    for _ in range(2):
        try:
            return llm.parse_json(llm.chat(msgs, cfg))
        except Exception as e:
            last = e
            time.sleep(2)
    return {"stance": "中性", "score": 0, "confidence": 0, "summary": "调用失败: %s" % last, "points": [], "error": True}


def _brief(op):
    keep = ("stance", "score", "confidence", "summary", "points", "rebuttals", "verdict", "key_bull", "key_bear",
            "main_force", "catalyst", "risk_events", "red_flags", "theme_stage", "market_fit", "support",
            "resistance", "action", "entry_max", "stop_loss", "target", "hold_days", "position_pct",
            "risk_reward", "veto", "veto_reason", "max_position_pct", "upside_target", "downside_risk")
    return {k: op[k] for k in keep if k in op}


def run_builtin(sym, account="paper", intent="open", trigger="manual", progress=None, rid=None):
    from . import llm
    if not llm.enabled():
        raise RuntimeError("内置大模型未启用，请用外部模式：agent_cli.py committee start %s" % sym)
    ai_cfg = dict(load_config()["ai"])
    if ai_cfg.get("committee_model"):
        ai_cfg["llm_model"] = ai_cfg["committee_model"]
    rounds = load_config()["committee"]["debate_rounds"]
    roles = load_roles()
    sym = market.normalize(sym)
    total = 2 + rounds + 4      # 档案、分析师、N轮辩论、主管、交易员、风控、组合经理
    if rid is None:
        rid = _new_run(sym, _name(sym), account, intent, "builtin", trigger)
    ext = progress or (lambda m: None)

    def say(step, stage):
        _progress(rid, step, total, stage)
        ext("#%d %s" % (rid, stage))
    try:
        say(1, "准备档案：行情、资金、新闻、财务、大盘…")
        dossier = build_dossier(sym, account, intent)
        _save_dossier(rid, dossier)
        say(2, "5 位分析师并行分析中…")
        with ThreadPoolExecutor(max_workers=5) as ex:
            ops = dict(zip(ANALYSTS, ex.map(lambda r: _ask(r, {"dossier": dossier}, roles, ai_cfg), ANALYSTS)))
        for r, op in ops.items():
            save_opinion(rid, r, op)
        reports = {ROLE_NAMES[r]: _brief(op) for r, op in ops.items()}
        debate = []
        for rnd in range(1, rounds + 1):
            say(2 + rnd, "多空辩论 第 %d/%d 轮…" % (rnd, rounds))
            bull = _ask("bull", {"round": rnd, "dossier_core": _core(dossier), "analyst_reports": reports,
                                 "debate_so_far": debate}, roles, ai_cfg)
            save_opinion(rid, "bull", bull, rnd)
            debate.append({"role": "多头", "round": rnd, **_brief(bull)})
            bear = _ask("bear", {"round": rnd, "dossier_core": _core(dossier), "analyst_reports": reports,
                                 "debate_so_far": debate}, roles, ai_cfg)
            save_opinion(rid, "bear", bear, rnd)
            debate.append({"role": "空头", "round": rnd, **_brief(bear)})
        say(3 + rounds, "研究主管裁判中…")
        mgr = _ask("research_manager", {"dossier_core": _core(dossier), "analyst_reports": reports,
                                        "debate": debate}, roles, ai_cfg)
        save_opinion(rid, "research_manager", mgr)
        say(4 + rounds, "交易员制定买入方案…")
        trader = _ask("trader", {"dossier_core": _core(dossier), "research_manager": _brief(mgr),
                                 "account": dossier["account"], "risk_limits": dossier["risk_limits"]}, roles, ai_cfg)
        save_opinion(rid, "trader", trader)
        say(5 + rounds, "风控官审查中…")
        risk = _ask("risk", {"dossier_core": _core(dossier), "trader_plan": _brief(trader),
                             "account": dossier["account"], "market_regime": dossier["market_regime"],
                             "risk_limits": dossier["risk_limits"], "fundamentals": dossier["fundamentals"]},
                    roles, ai_cfg)
        save_opinion(rid, "risk", risk)
        say(6 + rounds, "组合经理拍板中…")
        pm = _ask("pm", {"dossier_core": _core(dossier), "analyst_reports": reports, "debate": debate,
                         "research_manager": _brief(mgr), "trader": _brief(trader), "risk": _brief(risk),
                         "account": dossier["account"], "existing_plan": dossier["existing_plan"]}, roles, ai_cfg)
        res = finalize(rid, pm)
        ext("#%d 结论：%s（%s）" % (rid, res["decision"], res.get("summary", "")))
        PROGRESS.pop(rid, None)
        return res
    except Exception as e:
        PROGRESS.pop(rid, None)
        db.run("UPDATE committee_runs SET status='error',error=?,finished_at=? WHERE id=?", (str(e), time.time(), rid))
        raise


def _core(d):
    """给后面角色的精简档案（避免上下文过长）。"""
    return {k: d.get(k) for k in ("symbol", "name", "board", "intent", "as_of", "quote", "daily", "a_share_pattern",
                                  "sector", "lot", "one_lot_cost")} | {
        "market_state": (d.get("market_regime") or {}).get("state"),
        "overnight_link": (d.get("global_overnight") or {}).get("this_stock"),
        "flow": (d.get("intel") or {}).get("flow_today"), "main_5d_yi": (d.get("intel") or {}).get("main_5d_yi"),
        "news_today": (d.get("intel") or {}).get("news_today")}


_running = {}      # (account, symbol) -> (thread, run_id)


def run_async(sym, account="paper", intent="open", trigger="auto", then_execute=None):
    """后台跑内置委员会，立刻返回 {"started": bool, "run_id": int}。
    同一只股票已在开会时不重复开，返回进行中的会议号。then_execute=dict(决策动作) 时，通过后自动执行。"""
    sym = market.normalize(sym)
    cur = _running.get((account, sym))
    if cur and cur[0].is_alive():
        return {"started": False, "run_id": cur[1]}
    rid = _new_run(sym, _name(sym), account, intent, "builtin", trigger)
    _progress(rid, 0, 1, "排队中…")

    def _go():
        try:
            res = run_builtin(sym, account, intent, trigger, rid=rid)
            if then_execute and res["status"] == "approved":
                act = dict(then_execute, symbol=sym, action="buy",
                           position_pct=min(float(then_execute.get("position_pct") or 1), res.get("position_pct") or 0.2),
                           confidence=res.get("confidence", 0.7),
                           reason="投研委员会#%d通过：%s" % (res["run_id"], res.get("thesis", "")[:120]))
                act.pop("shares", None)
                act.pop("amount", None)
                agent.apply_decision({"market_view": "委员会#%d 自动执行" % res["run_id"], "actions": [act]},
                                     agent="committee#%d" % res["run_id"], trigger=trigger)
            else:
                from . import notify
                notify.send("投研委员会#%d：%s %s" % (res["run_id"], res.get("decision"), _name(sym)),
                            "**%s**  \n%s" % (res.get("summary", ""), "；".join(res.get("gate_notes", []))))
        except Exception as e:
            log.error("委员会失败 %s: %s", sym, e)
    t = threading.Thread(target=_go, daemon=True, name="committee-%d" % rid)
    _running[(account, sym)] = (t, rid)
    t.start()
    return {"started": True, "run_id": rid}


def _name(sym):
    r = db.one("SELECT name FROM quotes WHERE symbol=?", (sym,)) or \
        db.one("SELECT name FROM candidates WHERE symbol=? ORDER BY screen_id DESC LIMIT 1", (sym,)) or \
        db.one("SELECT name FROM watchlist WHERE symbol=?", (sym,))
    return r["name"] if r and r["name"] else sym


# ---------------- 外部模式 ----------------
def start_external(sym, account="paper", intent="open", trigger="external"):
    d = build_dossier(sym, account, intent)
    rid = _create_run(d["symbol"], d["name"], account, intent, "external", trigger, d["quote"]["price"], d)
    db.run("UPDATE committee_runs SET status='external' WHERE id=?", (rid,))
    return {"run_id": rid, "dossier_file": os.path.relpath(os.path.join(DOSSIER_DIR, "run_%d_dossier.json" % rid),
                                                           BASE_DIR),
            "roles_file": os.path.relpath(ROLES_PATH, BASE_DIR), "order": ORDER,
            "debate_rounds": load_config()["committee"]["debate_rounds"],
            "how": ("依次扮演角色（读 roles_file 中对应段落 + _common），每个角色写一个 JSON，用 "
                    "`agent_cli.py committee opinion %d <role> <file|->` 提交；5个分析师可并行（如有子代理）；"
                    "bull/bear 按轮次用 --round；最后组合经理用 `agent_cli.py committee final %d <file|->`" % (rid, rid)),
            "dossier": d}


# ---------------- 统计：哪个 agent 准 ----------------
def stats(horizon_days=10):
    """对 horizon_days 天前完成的会议，比较各角色立场与之后实际涨跌。"""
    runs = db.all("SELECT id,symbol,price,ts FROM committee_runs WHERE status IN ('approved','rejected') AND ts<?",
                  (time.time() - horizon_days * 86400,))
    perf = {}
    kcache = {}
    for r in runs:
        try:
            if r["symbol"] not in kcache:
                kcache[r["symbol"]] = market.get_kline(r["symbol"], "day", 250)
            kl = kcache[r["symbol"]]
            d0 = market.datetime.fromtimestamp(r["ts"], market.CN_TZ).strftime("%Y-%m-%d")
            after = [b for b in kl if b["t"] > d0]
            if len(after) < 5:
                continue
            fut = after[min(len(after), horizon_days) - 1]["c"]
            ret = (fut / r["price"] - 1) * 100
        except Exception:
            continue
        for o in db.all("SELECT role,stance FROM committee_opinions WHERE run_id=?", (r["id"],)):
            p = perf.setdefault(o["role"], {"n": 0, "right": 0, "neutral": 0, "ret_when_bull": []})
            if o["stance"] == "多":
                p["n"] += 1; p["right"] += ret > 0; p["ret_when_bull"].append(ret)
            elif o["stance"] == "空":
                p["n"] += 1; p["right"] += ret < 0
            else:
                p["neutral"] += 1
    out = []
    for role in ORDER:
        p = perf.get(role)
        if not p:
            continue
        out.append({"role": role, "name": ROLE_NAMES[role], "calls": p["n"], "neutral": p["neutral"],
                    "accuracy_pct": round(p["right"] / p["n"] * 100, 1) if p["n"] else None,
                    "avg_ret_when_bull": round(sum(p["ret_when_bull"]) / len(p["ret_when_bull"]), 2)
                    if p["ret_when_bull"] else None})
    return {"horizon_days": horizon_days, "runs_evaluated": len(runs), "roles": out}
