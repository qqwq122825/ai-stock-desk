"""微信推送：优先发到微信 ClawBot 对话框（core/wechat_bot.py）；没绑定 / 会话过期 / 额度用完时退回 PushPlus / Server酱。"""
import json
import logging
import time
import urllib.parse
import urllib.request

from . import db
from .config import load_config

log = logging.getLogger("notify")


def _post(url, data, as_json=True, timeout=8):
    if as_json:
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


KIND_NAMES = {"trade": "成交/订单", "committee_pass": "委员会通过", "committee_reject": "委员会没通过",
              "watch": "等回踩", "daily": "收盘总结", "premarket": "盘前简报", "alert": "盘中异动",
              "ai_note": "AI 巡检提醒", "system": "系统"}


def wanted(kind, account=None, cfg=None):
    """按设置判断这类消息要不要推送。实盘的成交/订单永远推送；没分类的（测试、手动）也推。"""
    cfg = cfg or load_config()["notify"]
    if account == "live" and kind == "trade":
        return True
    if account == "paper" and not cfg.get("paper", True):
        return False
    if kind and kind in KIND_NAMES:
        return bool((cfg.get("kinds") or {}).get(kind, True))
    return True


def send(title, content, kind=None, account=None):
    """content 支持 markdown。kind=消息类型（见 KIND_NAMES），account=paper/live。返回 (ok, 渠道说明)。失败不抛异常。
    按设置不推送的消息只记录到消息日志（网页「消息」里能看到）。"""
    cfg = load_config()["notify"]
    if not wanted(kind, account, cfg):
        why = "按设置不推送（%s%s）" % ("模拟盘·" if account == "paper" else "", KIND_NAMES.get(kind, kind))
        try:
            db.run("INSERT INTO messages(ts,title,content,channels,ok) VALUES(?,?,?,?,?)",
                   (time.time(), title, content, why, 0))
        except Exception:
            pass
        return False, why
    channels, ok = [], False
    via_bot = False
    if cfg.get("enabled"):
        try:
            from . import wechat_bot
            via_bot, info = wechat_bot.push(title, content)
            if info != "未绑定":
                channels.append("clawbot:%s" % info)
            ok = ok or via_bot
            if via_bot and not wechat_bot.cfg()["also_pushplus"]:
                cfg = dict(cfg, pushplus_token="", serverchan_key="")       # 已经发到微信对话框了，不再重复
        except Exception as e:
            channels.append("clawbot:err %s" % e)
    if cfg.get("enabled"):
        if cfg.get("pushplus_token"):
            try:
                r = json.loads(_post("https://www.pushplus.plus/send", {
                    "token": cfg["pushplus_token"], "title": title[:100],
                    "content": content, "template": "markdown"}))
                good = r.get("code") == 200
                ok = ok or good
                channels.append("pushplus:%s" % ("ok" if good else r.get("msg")))
            except Exception as e:
                channels.append("pushplus:err %s" % e)
        if cfg.get("serverchan_key"):
            try:
                r = json.loads(_post("https://sctapi.ftqq.com/%s.send" % cfg["serverchan_key"],
                                     {"title": title[:32], "desp": content}, as_json=False))
                good = r.get("code") == 0
                ok = ok or good
                channels.append("serverchan:%s" % ("ok" if good else r.get("message")))
            except Exception as e:
                channels.append("serverchan:err %s" % e)
    if not channels:
        channels.append("未配置推送渠道（仅记录）")
    ch = "; ".join(channels)
    try:
        db.run("INSERT INTO messages(ts,title,content,channels,ok) VALUES(?,?,?,?,?)",
               (time.time(), title, content, ch, int(ok)))
    except Exception as e:
        log.warning("写消息日志失败: %s", e)
    log.info("推送[%s] %s -> %s", "OK" if ok else "--", title, ch)
    return ok, ch
