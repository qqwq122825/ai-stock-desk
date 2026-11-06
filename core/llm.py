"""可选：内置大模型（OpenAI 兼容接口：DeepSeek / 通义千问 / Kimi / 本地 Ollama 等）。

用途：盘中规则/策略异动时，程序主动把上下文发给大模型调研并决策（不用等10分钟定时器）。
不配置 key 则完全不启用，只走外部定时 AI。

  DeepSeek:  base_url=https://api.deepseek.com                          model=deepseek-flash（V4.1-Flash）/ deepseek-v4-pro
  通义千问:   base_url=https://dashscope.aliyuncs.com/compatible-mode/v1  model=qwen-plus
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request

from . import agent
from .config import BASE_DIR, load_config

log = logging.getLogger("llm")
_lock = threading.Lock()
_last_call = {"ts": 0}


def _prompt():
    path = os.path.join(BASE_DIR, "AGENT_PROMPT.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "你是A股交易决策助手，只输出 JSON 决策。"


def chat(messages, cfg, json_mode=True):
    body = {"model": cfg["llm_model"], "messages": messages, "temperature": 0.3}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(cfg["llm_base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + cfg["llm_api_key"]})
    with urllib.request.urlopen(req, timeout=cfg.get("llm_timeout_sec", 120)) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def parse_json(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def enabled():
    c = load_config()["ai"]
    return bool(c.get("llm_enabled") and c.get("llm_api_key"))


def decide(trigger="manual", focus=None):
    """同步调用一次大模型并执行决策。返回 results。"""
    cfg = load_config()["ai"]
    if not enabled():
        raise RuntimeError("内置大模型未启用（设置里填 llm_api_key 并打开 llm_enabled）")
    with _lock:
        _last_call["ts"] = time.time()
        ctx = agent.build_context()
        user = ("【你是程序内置模型：不能运行命令，所有数据都在下面的上下文里。买入未经委员会的股票时，"
                "系统会自动为它召开内置投研委员会，通过后才执行，所以你只需在 actions 里表达买入意图。】\n"
                "大盘状态、情绪阶段、仓位上限一律以本次上下文里的 market_regime / emotion 为准，"
                "不要沿用 recent_decisions 里你以前写的结论（行情会变）。\n"
                "触发原因：%s\n%s\n\n当前上下文 JSON：\n%s\n\n请只输出一个决策 JSON 对象。"
                % (trigger, ("重点关注：%s" % focus) if focus else "",
                   json.dumps(ctx, ensure_ascii=False, default=str)))
        text = chat([{"role": "system", "content": _prompt()}, {"role": "user", "content": user}], cfg)
        dec = parse_json(text)
        return agent.apply_decision(dec, agent="llm:" + cfg["llm_model"], trigger=trigger)


def decide_async(trigger, focus=None, force=False):
    """异动触发：有冷却，后台线程执行，不阻塞行情循环。"""
    if not enabled():
        return False
    cfg = load_config()["ai"]
    if not force and time.time() - _last_call["ts"] < cfg.get("llm_alert_cooldown_min", 10) * 60:
        return False
    if _lock.locked():
        return False
    _last_call["ts"] = time.time()

    def _run():
        try:
            res = decide(trigger, focus)
            log.info("大模型决策完成 #%s", res.get("decision_id"))
        except Exception as e:
            log.error("大模型决策失败: %s", e)
    threading.Thread(target=_run, daemon=True).start()
    return True
