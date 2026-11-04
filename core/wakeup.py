"""异动时唤醒外部 AI：执行 config.ai.wakeup_command（任意命令行 AI），带冷却，后台运行不阻塞监控。

被唤醒的 AI 会拿到环境变量：
  AI_TRIGGER   异动描述（如 "长江电力 5分钟内急涨 2.3%；招商银行 共振买入信号"）
  AI_SYMBOLS   相关股票代码，逗号分隔
  AI_STOCK_DIR 项目目录
然后按 prompts/02_异动调研.md 的流程：agent_cli.py inbox → 调研 → decide。
"""
import logging
import os
import subprocess
import time

from .config import BASE_DIR, DATA_DIR, load_config

log = logging.getLogger("wakeup")
_state = {"last": 0, "proc": None}


def enabled():
    return bool(load_config()["ai"].get("wakeup_command", "").strip())


def trigger(reason, symbols=(), force=False):
    cfg = load_config()["ai"]
    cmd = cfg.get("wakeup_command", "").strip()
    if not cmd:
        return False
    if not force and time.time() - _state["last"] < cfg.get("wakeup_cooldown_min", 10) * 60:
        return False
    p = _state["proc"]
    if p is not None and p.poll() is None:
        log.info("上一个被唤醒的 AI 还在运行，跳过")
        return False
    _state["last"] = time.time()
    env = dict(os.environ, AI_TRIGGER=reason[:1000], AI_SYMBOLS=",".join(symbols), AI_STOCK_DIR=BASE_DIR)
    logf = open(os.path.join(DATA_DIR, "wakeup.log"), "a", encoding="utf-8")
    logf.write("\n===== %s 唤醒：%s\n$ %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), reason, cmd))
    logf.flush()
    try:
        _state["proc"] = subprocess.Popen(cmd, shell=True, cwd=BASE_DIR, env=env, stdout=logf,
                                          stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        log.info("已唤醒外部 AI：%s", reason[:80])
        return True
    except Exception as e:
        logf.write("启动失败: %s\n" % e)
        log.error("唤醒外部 AI 失败: %s", e)
        return False
    finally:
        logf.close()   # 子进程已持有自己的文件描述符
