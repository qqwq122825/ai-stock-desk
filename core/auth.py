"""后台登录。密码从环境变量 AI_STOCK_PASSWORD 读取，没有就读 data/config.json 的 server.password；都没配置时不允许登录。
（data/ 不进 git，所以密码不会被推到公开仓库。）"""
import hashlib
import hmac
import os
import threading
import time
from http.cookies import SimpleCookie

from .config import DATA_DIR

def _password():
    p = os.environ.get("AI_STOCK_PASSWORD")
    if p:
        return p
    try:
        from .config import load_config
        return str(load_config()["server"].get("password") or "")
    except Exception:
        return ""


PASSWORD = _password()

COOKIE = "aistock_sid"
MAX_AGE = 30 * 86400
FAIL_LIMIT = 5             # 同一 IP 10 分钟内最多输错 5 次
FAIL_WINDOW = 600

_lock = threading.Lock()
_fails = {}
_secret = None


def _key():
    """签名密钥 = 本机随机密钥 + 密码（改密码即让所有旧登录失效）。"""
    global _secret
    if _secret is None:
        path = os.path.join(DATA_DIR, "session.key")
        try:
            with open(path, "rb") as f:
                _secret = f.read()
        except OSError:
            _secret = b""
        if len(_secret) < 32:
            _secret = os.urandom(32)
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(_secret)
    return _secret + PASSWORD.encode("utf-8")


def check_password(pw):
    return bool(PASSWORD) and hmac.compare_digest(str(pw or "").encode("utf-8"), PASSWORD.encode("utf-8"))


def make_session():
    exp = str(int(time.time()) + MAX_AGE)
    sig = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    return "%s.%s" % (exp, sig)


def valid(cookie_header):
    if not PASSWORD or not cookie_header:
        return False
    try:
        c = SimpleCookie()
        c.load(cookie_header)
        val = c[COOKIE].value
        exp, sig = val.split(".", 1)
        if int(exp) < time.time():
            return False
        good = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good)
    except (KeyError, ValueError):
        return False


def set_cookie_header(session):
    return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (COOKIE, session, MAX_AGE)


def clear_cookie_header():
    return "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE


def allowed(ip):
    now = time.time()
    with _lock:
        lst = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = lst
        return len(lst) < FAIL_LIMIT


def record_fail(ip):
    with _lock:
        _fails.setdefault(ip, []).append(time.time())
