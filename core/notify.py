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


def send(title, content):
    """content 支持 markdown。返回 (ok, 渠道说明)。失败不抛异常。"""
    cfg = load_config()["notify"]
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
