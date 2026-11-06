"""微信 ClawBot：在你自己的微信里多一个对话框，跟系统聊天、收提醒、确认订单。

用的是微信官方「我 → 设置 → 插件 → ClawBot」的 iLink 接口（https://ilinkai.weixin.qq.com，HTTP + JSON），
协议照着腾讯开源（MIT）的 @tencent-weixin/openclaw-weixin 插件实现，不需要装 OpenClaw，零第三方依赖。

流程：
  1. 网页「设置 → 微信 ClawBot」点「绑定」→ 显示二维码 → 用微信扫码确认（可能要输入手机上显示的数字）
  2. 绑定后后台线程长轮询收消息（getupdates，每次最多挂 35 秒）
  3. 你发消息 → 程序回答（命令或大模型）；系统提醒（notify.send）优先走这里，发不了再退回 PushPlus

微信对机器人的限制（决定了推送策略）：
  - 你 24 小时没说话，机器人就不能再主动发消息（会话过期）
  - 你每说一句话之后，机器人最多主动发 10 条；这里只用 9 条，第 9 条会提醒你"回我一句"
  - 只支持 iPhone 微信 8.0.70+；只发文字
只接受绑定时扫码的那个微信号发来的消息。bot_token 存在 data/wechat_bot.json（不进 git）。
"""
import base64
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request

from . import db, market
from .config import DATA_DIR, load_config

log = logging.getLogger("wechat")
BASE_URL = "https://ilinkai.weixin.qq.com"
PLUGIN_VERSION = "2.4.10"                       # 对齐的官方插件版本
APP_ID = "bot"                                  # 官方插件 package.json 里的 ilink_appid
BOT_AGENT = "AIStockDesk/1.0"
BOT_TYPE = "3"
STATE_PATH = os.path.join(DATA_DIR, "wechat_bot.json")
SESSION_HOURS = 24
_lock = threading.Lock()
_runtime = {"thread": None, "stop": False, "paused_until": 0, "last_poll": 0, "last_error": ""}
LOGIN = {"status": None, "qrcode": None, "url": None, "base": BASE_URL, "started": 0, "verify": None, "message": ""}


# ---------------- 状态（data/wechat_bot.json） ----------------
def _load():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st):
    with _lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, STATE_PATH)


def _update(**kw):
    st = _load()
    st.update(kw)
    _save(st)
    return st


def cfg():
    c = load_config().get("wechat_bot") or {}
    return {"enabled": c.get("enabled", True), "max_push": int(c.get("max_push", 9)),
            "also_pushplus": bool(c.get("also_pushplus", False))}


# ---------------- HTTP ----------------
def _client_version():
    a, b, c = (int(x) for x in PLUGIN_VERSION.split("."))
    return str(((a & 0xff) << 16) | ((b & 0xff) << 8) | (c & 0xff))


def _headers(token=None):
    h = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
         "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode()).decode(),
         "iLink-App-Id": APP_ID, "iLink-App-ClientVersion": _client_version()}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _base_info():
    return {"channel_version": PLUGIN_VERSION, "bot_agent": BOT_AGENT}


def _req(method, base, path, body=None, token=None, timeout=15):
    url = base.rstrip("/") + "/" + path.lstrip("/")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(token))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


# ---------------- 绑定（扫码） ----------------
def login_start():
    r = _req("POST", BASE_URL, "ilink/bot/get_bot_qrcode?bot_type=" + BOT_TYPE, {"local_token_list": []})
    if not r.get("qrcode"):
        raise RuntimeError("获取二维码失败：%s" % r)
    LOGIN.update(status="wait", qrcode=r["qrcode"], url=r.get("qrcode_img_content"), base=BASE_URL,
                 started=time.time(), verify=None, message="请用 iPhone 微信扫码")
    threading.Thread(target=_login_poll, args=(r["qrcode"],), daemon=True, name="wechat-login").start()
    return login_status()


def set_verify_code(code):
    LOGIN["verify"] = str(code).strip()
    LOGIN["message"] = "已提交数字，等待确认…"


def _login_poll(qrcode):
    deadline = time.time() + 8 * 60
    sent_code = None
    while time.time() < deadline and LOGIN["qrcode"] == qrcode:
        path = "ilink/bot/get_qrcode_status?qrcode=" + qrcode
        if LOGIN["verify"] and LOGIN["verify"] != sent_code:
            path += "&verify_code=" + LOGIN["verify"]
            sent_code = LOGIN["verify"]
        try:
            r = _req("GET", LOGIN["base"], path, timeout=40)
        except Exception as e:
            if "timed out" not in str(e):
                log.warning("查询扫码状态失败: %s", e)
                time.sleep(2)
            continue
        st = r.get("status") or "wait"
        LOGIN["status"] = st
        if st == "scaned":
            LOGIN["message"] = "已扫码，请在手机上确认"
        elif st == "need_verifycode":
            LOGIN["message"] = "请输入手机微信上显示的数字" if not sent_code else "数字不对，请重新输入"
            if LOGIN["verify"] == sent_code:
                LOGIN["verify"] = None
            time.sleep(1)
        elif st == "scaned_but_redirect" and r.get("redirect_host"):
            LOGIN["base"] = "https://" + r["redirect_host"]
        elif st == "expired":
            LOGIN["message"] = "二维码过期了，请重新点「绑定」"
            return
        elif st == "verify_code_blocked":
            LOGIN["message"] = "数字输错太多次，请稍后重新绑定"
            return
        elif st == "binded_redirect":
            LOGIN["message"] = "这个微信已经绑定过了；如果网页显示未绑定，先在微信里删除这个 ClawBot 再绑"
            return
        elif st == "confirmed" and r.get("bot_token"):
            _save({"bot_token": r["bot_token"], "base_url": r.get("baseurl") or LOGIN["base"],
                   "bot_id": r.get("ilink_bot_id"), "user_id": r.get("ilink_user_id"), "bound_at": time.time(),
                   "get_updates_buf": "", "context_token": None, "last_user_ts": 0, "pushes": 0})
            LOGIN.update(message="绑定成功！在微信里给它发「帮助」试试", qrcode=None)
            start()
            return
    if LOGIN["qrcode"] == qrcode:
        LOGIN.update(status="expired", message="等太久了，请重新点「绑定」")


def login_status():
    out = {k: LOGIN.get(k) for k in ("status", "message")}
    if LOGIN.get("url") and LOGIN.get("qrcode"):
        out["qr_svg"] = qr_svg(LOGIN["url"])
    return out


def qr_svg(text, border=3):
    from .qrcodegen import QrCode
    q = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    n = q.get_size() + border * 2
    parts = ["M%d,%dh1v1h-1z" % (x + border, y + border)
             for y in range(q.get_size()) for x in range(q.get_size()) if q.get_module(x, y)]
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges">'
            '<rect width="100%%" height="100%%" fill="#fff"/><path d="%s" fill="#000"/></svg>' % (n, n, " ".join(parts)))


def unbind():
    stop()
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass
    LOGIN.update(status=None, qrcode=None, url=None, message="已解绑")


def status():
    st = _load()
    bound = bool(st.get("bot_token"))
    left = None
    if st.get("last_user_ts"):
        left = max(0, SESSION_HOURS * 3600 - (time.time() - st["last_user_ts"]))
    return {"bound": bound, "running": bool(_runtime["thread"] and _runtime["thread"].is_alive()),
            "bound_at": _fmt(st.get("bound_at")), "last_user_msg": _fmt(st.get("last_user_ts")),
            "session_left_hours": round(left / 3600, 1) if left is not None else None,
            "pushes_used": st.get("pushes", 0), "max_push": cfg()["max_push"],
            "paused": _runtime["paused_until"] > time.time(), "last_error": _runtime["last_error"],
            "login": {k: LOGIN.get(k) for k in ("status", "message")}}


def _fmt(ts):
    return market.datetime.fromtimestamp(ts, market.CN_TZ).strftime("%m-%d %H:%M") if ts else None


# ---------------- 收消息 ----------------
def start():
    st = _load()
    if not st.get("bot_token") or not cfg()["enabled"]:
        return False
    if _runtime["thread"] and _runtime["thread"].is_alive():
        return True
    _runtime["stop"] = False
    t = threading.Thread(target=_poll_loop, daemon=True, name="wechat-bot")
    _runtime["thread"] = t
    t.start()
    log.info("微信 ClawBot 已启动")
    return True


def stop():
    _runtime["stop"] = True


def _poll_loop():
    while not _runtime["stop"]:
        if _runtime["paused_until"] > time.time():
            time.sleep(10)
            continue
        st = _load()
        if not st.get("bot_token"):
            return
        try:
            r = _req("POST", st.get("base_url") or BASE_URL, "ilink/bot/getupdates",
                     {"get_updates_buf": st.get("get_updates_buf") or "", "base_info": _base_info()},
                     token=st["bot_token"], timeout=45)
            _runtime["last_poll"] = time.time()
        except Exception as e:
            if "timed out" not in str(e):
                _runtime["last_error"] = str(e)[:200]
                log.warning("微信收消息失败: %s", e)
                time.sleep(5)
            continue
        if r.get("errcode") == -14 or r.get("ret") == -14:
            _runtime["paused_until"] = time.time() + 3600         # 和官方插件一样：会话失效先停 1 小时
            _runtime["last_error"] = "会话失效（-14），1 小时后重试；一直不行就重新绑定"
            log.warning("微信 ClawBot 会话失效，暂停 1 小时")
            continue
        if r.get("ret") not in (None, 0) or r.get("errcode"):
            _runtime["last_error"] = "getupdates ret=%s errcode=%s %s" % (r.get("ret"), r.get("errcode"), r.get("errmsg"))
            time.sleep(5)
            continue
        if r.get("get_updates_buf") is not None:
            _update(get_updates_buf=r["get_updates_buf"])
        for m in r.get("msgs") or []:
            try:
                _on_message(m)
            except Exception as e:
                log.exception("处理微信消息失败")
                _runtime["last_error"] = str(e)[:200]


def _text_of(m):
    out = []
    for it in m.get("item_list") or []:
        if it.get("type") == 1:
            out.append((it.get("text_item") or {}).get("text") or "")
        elif it.get("type") == 3:                         # 语音：微信自带转文字
            out.append((it.get("voice_item") or {}).get("text") or "")
    return "".join(out).strip()


def _on_message(m):
    if m.get("message_type") != 1:                        # 只处理用户发来的
        return
    st = _load()
    sender = m.get("from_user_id")
    if st.get("user_id") and sender != st["user_id"]:
        log.warning("忽略非绑定用户的消息")
        return
    _update(context_token=m.get("context_token"), last_user_ts=time.time(), pushes=0,
            user_id=st.get("user_id") or sender)
    text = _text_of(m)
    if not text:
        return
    log.info("微信收到：%s", text[:60])
    try:
        reply = respond(text)
    except Exception as e:
        log.exception("回复失败")
        reply = "出错了：%s" % e
    send_text(reply, to=sender, context_token=m.get("context_token"))


# ---------------- 发消息 ----------------
def send_text(text, to=None, context_token=None):
    st = _load()
    if not st.get("bot_token"):
        raise RuntimeError("还没绑定微信 ClawBot")
    to = to or st.get("user_id")
    context_token = context_token or st.get("context_token")
    chunks = [text[i:i + 1800] for i in range(0, len(text), 1800)] or [""]
    for ch in chunks:
        body = {"msg": {"from_user_id": "", "to_user_id": to,
                        "client_id": "aistock-%d-%06d" % (time.time() * 1000, random.randint(0, 999999)),
                        "message_type": 2, "message_state": 2,
                        "item_list": [{"type": 1, "text_item": {"text": ch}}],
                        "context_token": context_token},
                "base_info": _base_info()}
        r = _req("POST", st.get("base_url") or BASE_URL, "ilink/bot/sendmessage", body, token=st["bot_token"])
        if r.get("ret") not in (None, 0) or r.get("errcode"):
            raise RuntimeError("sendmessage ret=%s errcode=%s %s" % (r.get("ret"), r.get("errcode"), r.get("errmsg")))
    return True


def strip_md(s):
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s or "")
    s = re.sub(r"^#+\s*", "", s, flags=re.M)
    s = s.replace("  \n", "\n").replace("`", "")
    s = re.sub(r"\[([^\]]+)\]\((http[^)]+)\)", r"\1 \2", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def push(title, content):
    """系统提醒走微信对话框。返回 (ok, 说明)；发不了（没绑定/会话过期/额度用完）返回 False 让调用方退回 PushPlus。"""
    c = cfg()
    st = _load()
    if not c["enabled"] or not st.get("bot_token"):
        return False, "未绑定"
    if not st.get("context_token"):
        return False, "还没收到你的消息：先在微信里给它发一句「帮助」（微信规定你先说话，机器人才能主动发）"
    if time.time() - (st.get("last_user_ts") or 0) > (SESSION_HOURS - 0.5) * 3600:
        return False, "会话过期（24 小时没说话）"
    used = st.get("pushes", 0)
    if used >= c["max_push"]:
        return False, "主动消息额度用完（%d 条）" % c["max_push"]
    text = "【%s】\n%s" % (title, strip_md(content))
    if used == c["max_push"] - 1:
        text += "\n\n⚠️ 这是最后一条主动提醒，回我任意一句就能继续收到（不然后面的提醒会走 PushPlus）"
    try:
        send_text(text[:3500])
    except Exception as e:
        return False, "发送失败 %s" % e
    _update(pushes=used + 1)
    return True, "ok(%d/%d)" % (used + 1, c["max_push"])


# ---------------- 回答 ----------------
HELP = """我是你的 A 股 AI 交易系统，可以这样用：
· 持仓 / 状态 —— 账户、持仓、大盘和情绪
· 订单 —— 实盘待确认订单
· 确认 12 / 拒绝 12 —— 处理实盘待确认订单
· 审 600519 —— 让委员会审一只股票（只审不买）
· 其他任何问题直接问，比如「今天为什么没买」「隆基怎么看」
系统的提醒也会发到这里。24 小时内回我一句，提醒才能继续发过来。"""


def respond(text):
    t = text.strip()
    low = t.lower()
    if low in ("帮助", "help", "?", "？", "菜单"):
        return HELP
    if t in ("持仓", "状态", "账户", "仓位"):
        return _status_text()
    if t in ("订单", "待确认"):
        return _orders_text()
    m = re.match(r"^(确认|拒绝)\s*#?(\d+)$", t)
    if m:
        from . import broker
        oid = int(m.group(2))
        try:
            if m.group(1) == "确认":
                o = broker.confirm_order(oid)
                return "✅ 已确认订单 #%d\n%s" % (oid, o.get("message") or "")
            broker.reject_order(oid, "微信拒绝")
            return "已拒绝订单 #%d" % oid
        except Exception as e:
            return "订单 #%d 处理失败：%s" % (oid, e)
    m = re.match(r"^审\s*(\S+)$", t)
    if m:
        from . import committee
        try:
            r = committee.run_async(m.group(1), "paper", "open", trigger="微信")
            return "委员会#%d %s，大约 5~8 分钟出结果，结论会发到这里（只审不买）" % (r["run_id"], "已开始" if r["started"] else "已在进行")
        except Exception as e:
            return "开委员会失败：%s" % e
    return _ask_llm(t)


def _status_text():
    from . import broker, screener
    lines = []
    try:
        reg = screener.market_regime()
        lines.append("大盘：%s（仓位上限 %d%%）" % (reg["state"], reg["position_cap"] * 100))
    except Exception:
        pass
    try:
        from . import emotion
        e = emotion.sentiment()
        if not e.get("error"):
            lines.append("情绪：%s（短线上限 %d%%）%s" % (e["stage"], e["position_cap"] * 100, e.get("tip", "")))
    except Exception:
        pass
    for acct, nm in (("paper", "模拟盘"), ("live", "实盘")):
        try:
            s = broker.snapshot(acct)
        except Exception:
            continue
        if not s.get("equity"):
            continue
        lines.append("\n%s：总资产 %.0f，今日 %+.2f%%，现金 %.0f" % (nm, s["equity"], s.get("day_pnl_pct") or 0, s["cash"]))
        for p in s["positions"]:
            lines.append("· %s %d股 成本%.2f 现价%.2f %+.1f%%" % (p["name"], p["shares"], p["avg_cost"], p["price"], p["pnl_pct"]))
        if not s["positions"]:
            lines.append("· 空仓")
    return "\n".join(lines) or "暂时取不到数据"


def _orders_text():
    rows = db.all("SELECT * FROM orders WHERE status='pending_confirm' ORDER BY id DESC LIMIT 10")
    if not rows:
        return "没有待确认的实盘订单"
    return "\n".join("#%d %s %s %d股 参考价%.2f\n  %s" % (o["id"], "买入" if o["side"] == "buy" else "卖出", o["name"],
                                                     o["shares"], o["ref_price"] or 0, (o.get("reason") or "")[:60])
                     for o in rows) + "\n\n回「确认 编号」或「拒绝 编号」"


def _ask_llm(question):
    from . import agent, llm
    if not llm.enabled():
        return "内置大模型没开，只能用命令。发「帮助」看看能做什么"
    try:
        ctx = agent.build_context()
    except Exception as e:
        ctx = {"error": str(e)}
    keep = {k: ctx.get(k) for k in ("now", "session", "market_regime", "emotion", "trading_style", "accounts",
                                    "position_plans", "entry_watch", "candidates", "committee_recent",
                                    "recent_decisions", "indexes", "global_overnight", "open_orders", "trades_today")}
    ctx_txt = json.dumps(keep, ensure_ascii=False, default=str)[:24000]
    sys_prompt = ("你是用户自己的 A 股 AI 交易系统的助手，在微信对话框里回答他。用户是新手。\n"
                  "只根据下面的系统数据回答，数据里没有的就说不知道，不要编。用中文，口语化，简短（一般 300 字以内），"
                  "不要用 markdown 表格和 ** 加粗。你不能下单、不能改设置：买卖由系统的 AI 和投研委员会按规则执行，"
                  "实盘订单要用户回「确认 编号」。涉及要不要买卖的，说清楚系统的判断和理由，提醒这不是投资建议。")
    cfgai = load_config()["ai"]
    text = llm.chat([{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": "系统数据：\n%s\n\n用户问：%s" % (ctx_txt, question)}],
                    cfgai, json_mode=False)
    return strip_md(text)[:3000]
