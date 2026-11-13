"""行情代理：服务器机房 IP 被行情源封控（东方财富 push2 持续 502）时，行情请求改走代理。

设置页「行情代理」填一个地址（存在 data/config.json，不进代码仓库，接口返回时打码）：
  - 代理提取 API：访问后返回一批 `ip:port`（每行一个，或 JSON）。程序定时提取、轮换、剔除坏的。
  - 或固定代理：`http://host:port` / `http://user:pass@host:port`
只有**公开行情数据**（东方财富）会走代理；大模型密钥、微信、推送、登录都不走代理。
HTTPS 证书照常校验，代理看不到也改不了内容。
"""
import json
import logging
import random
import re
import threading
import time
import urllib.request

from .config import load_config

log = logging.getLogger("proxy")
_lock = threading.Lock()
_st = {"list": [], "ts": 0, "bad": {}, "good": None, "err": "", "down_until": 0}
_PAIR = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9][A-Za-z0-9.-]+\.[A-Za-z]{2,}):(\d{2,5})(?!\d)")
BAD_SECONDS = 600


def conf():
    return load_config().get("proxy") or {}


def enabled():
    c = conf()
    return bool(c.get("enabled") and (c.get("api_url") or "").strip())


def _fixed(url):
    """`http://host:port`（没有路径和参数）当作固定代理，返回 'host:port' 或 'user:pass@host:port'。"""
    m = re.match(r"^(?:https?|socks\w*)://([^/?#]+)/?$", url.strip())
    return m.group(1) if m and ":" in m.group(1).split("@")[-1] else None


def parse(text):
    """从提取 API 的返回里找出所有 ip:port（纯文本、JSON 都行）。"""
    out = []
    try:
        d = json.loads(text)
    except ValueError:
        d = None

    def walk(x):
        if isinstance(x, dict):
            ip, port = x.get("ip") or x.get("host") or x.get("IP"), x.get("port") or x.get("Port")
            if ip and port:
                out.append("%s:%s" % (ip, port))
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            out.extend("%s:%s" % m for m in _PAIR.findall(x))
    if d is not None:
        walk(d)
    if not out:
        out = ["%s:%s" % m for m in _PAIR.findall(text)]
    return list(dict.fromkeys(out))


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-stock/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return parse(r.read(200000).decode("utf-8", errors="replace"))


def pool(force=False):
    c = conf()
    url = (c.get("api_url") or "").strip()
    if not url:
        return []
    fx = _fixed(url)
    if fx:
        return [fx]
    ttl = max(1, float(c.get("refresh_min") or 5)) * 60
    with _lock:
        if not force and _st["list"] and time.time() - _st["ts"] < ttl:
            return list(_st["list"])
        if not force and not _st["list"] and time.time() - _st["ts"] < 30:
            return []                       # 刚提取失败过：30 秒内不反复打提取接口
        _st["ts"] = time.time()
        try:
            lst = _fetch(url)
            _st.update(list=lst, err="" if lst else "提取接口没有返回 ip:port")
        except Exception as e:
            _st["err"] = "提取代理失败：%s" % type(e).__name__      # 不带地址，免得把密码写进日志
            log.warning(_st["err"])
        return list(_st["list"])


def _candidates(n):
    lst, now = pool(), time.time()
    live = [p for p in lst if _st["bad"].get(p, 0) < now]
    random.shuffle(live)
    g = _st["good"]
    if g in live:
        live.remove(g)
        live.insert(0, g)
    return live[:n]


def _open_via(p, url, headers, timeout, encoding):
    h = urllib.request.ProxyHandler({"http": "http://" + p, "https": "http://" + p})
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(h).open(req, timeout=timeout) as r:
        return r.read().decode(encoding, errors="replace")


def get(url, headers, timeout=8, encoding="utf-8", tries=3):
    """经代理取一个地址；换着试几个代理，都不行就抛最后一个错误。"""
    if _st["down_until"] > time.time():
        raise OSError("代理暂时都不可用（1 分钟后再试）")
    cands = _candidates(tries)
    if not cands and len(pool()) > 0:        # 都被标记成坏的了：重新提取一批
        with _lock:
            _st["bad"].clear()
        pool(force=True)
        cands = _candidates(tries)
    if not cands:
        raise OSError(_st["err"] or "没有可用的代理")
    err = None
    for p in cands:
        try:
            text = _open_via(p, url, headers, timeout, encoding)
            _st["good"] = p
            return text
        except Exception as e:
            err = e
            _st["bad"][p] = time.time() + BAD_SECONDS
            if _st["good"] == p:
                _st["good"] = None
    _st["down_until"] = time.time() + 60
    raise OSError("代理都失败：%s" % type(err).__name__)


def status():
    now = time.time()
    return {"enabled": enabled(), "count": len(_st["list"]) or (1 if _fixed((conf().get("api_url") or "")) else 0),
            "bad": sum(1 for v in _st["bad"].values() if v > now), "using": bool(_st["good"]),
            "fetched_ago": int(now - _st["ts"]) if _st["ts"] else None, "error": _st["err"]}


TEST_URL = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f6"
            "&fs=m:1+t:2&fields=f12,f14")


def test():
    """设置页「测试」：重新提取一批，挨个试到第一个能拿到东方财富行情的为止（最多试 8 个）。"""
    if not (conf().get("api_url") or "").strip():
        return {"ok": False, "msg": "还没填代理地址"}
    with _lock:
        _st["bad"].clear()
        _st["down_until"] = 0
    lst = pool(force=True)
    if not lst:
        return {"ok": False, "count": 0, "msg": _st["err"] or "没有提取到代理"}
    hdr = {"User-Agent": "Mozilla/5.0 (Macintosh) ai-stock/1.0", "Referer": "https://quote.eastmoney.com/"}
    tried, last = 0, ""
    for p in random.sample(lst, min(8, len(lst))):
        tried += 1
        t0 = time.time()
        try:
            n = json.loads(_open_via(p, TEST_URL, hdr, 8, "utf-8"))["data"]["total"]
            _st["good"] = p
            return {"ok": True, "count": len(lst), "tried": tried, "ms": int((time.time() - t0) * 1000),
                    "msg": "提取到 %d 个代理，第 %d 个就能拿到东方财富行情（%d 只，%d 毫秒）" % (
                        len(lst), tried, n, (time.time() - t0) * 1000)}
        except Exception as e:
            last = type(e).__name__
            _st["bad"][p] = time.time() + BAD_SECONDS
    return {"ok": False, "count": len(lst), "tried": tried,
            "msg": "提取到 %d 个代理，试了 %d 个都拿不到东方财富行情（%s）。可能是代理要先把服务器 IP 加白名单，或地区不对" % (
                len(lst), tried, last)}
