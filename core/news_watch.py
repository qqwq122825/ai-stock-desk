"""推送用的新闻筛选：只挑跟你有关的、真正重大的，其余的 AI 照样能看到，但不推送。

  · 持仓股新闻公告（分账户，kind=news_held）：
      - 东方财富公告：持仓股 12 小时内的新公告（减持、问询、业绩、中标…）
      - 新浪 7×24 快讯里点名提到持仓股的（30 分钟内）
  · 重大新闻（通用，kind=news_major）：新浪 7×24 标了「焦点」、并且是政策/市场级大事
    （降准降息、证监会、国务院、美联储、印花税、国家队…），每天最多 5 条
同一条只推一次（news_pushed 表去重）。
"""
import hashlib
import logging
import time

from . import db, market, news

log = logging.getLogger("news_watch")
MAJOR_WORDS = ["降准", "降息", "加息", "印花税", "证监会", "国常会", "国务院常务会议", "政治局", "中国人民银行",
               "美联储", "鲍威尔", "汇金", "国家队", "平准基金", "关税", "暂停交易", "熔断", "救市",
               "停火", "非农", "暴跌", "崩盘", "跌停潮"]
# 命中了也不算：外国的「国务院 / 央行」、例行的报纸版面摘要
NOT_MAJOR = ["美国国务院", "人民日报》头版", "头版的主要内容", "英国央行", "日本央行", "欧洲央行", "韩国央行"]
MAX_MAJOR_PER_DAY = 5


def _seen(key):
    k = hashlib.md5(key.encode("utf-8")).hexdigest()
    if db.one("SELECT 1 FROM news_pushed WHERE key=?", (k,)):
        return True
    db.run("INSERT OR IGNORE INTO news_pushed VALUES(?,?)", (k, time.time()))
    return False


def _ts(s, fmt):
    try:
        return market.datetime.strptime(s, fmt).replace(tzinfo=market.CN_TZ).timestamp()
    except Exception:
        return 0


def scan(positions):
    """返回要推送的 [{title, content, kind, account}]。"""
    out, now = [], time.time()
    held = {}
    for p in positions or []:
        h = held.setdefault(p["symbol"], {"name": p.get("name") or p["symbol"], "accounts": set()})
        h["accounts"].add(p["account"])
    try:
        flash = news.flash_news(50)
    except Exception as e:
        log.debug("快讯失败: %s", e)
        flash = []
    # 1) 持仓股
    for sym, h in held.items():
        acct = "live" if "live" in h["accounts"] else "paper"
        where = "（%s）" % "、".join({"paper": "模拟盘", "live": "实盘"}[a] for a in sorted(h["accounts"]))
        try:
            for a in news.announcements(sym, 5):
                if now - _ts(a["time"], "%Y-%m-%d %H:%M") > 12 * 3600 or _seen("ann|%s|%s" % (sym, a["title"])):
                    continue
                out.append({"kind": "news_held", "account": acct,
                            "title": "持仓公告%s：%s %s" % ("·" + a["tag"] if a["tag"] != "中性" else "", h["name"],
                                                        a["title"][:30]),
                            "content": "**%s %s**%s  \n%s  \n%s %s" % (h["name"], sym, where, a["title"], a["time"],
                                                                       a.get("type") or "")})
        except Exception as e:
            log.debug("公告失败 %s: %s", sym, e)
        for f in flash:
            if h["name"] in f["text"] and now - _ts(f.get("ts_full", ""), "%Y-%m-%d %H:%M:%S") < 1800 \
                    and not _seen("flash|%s" % (f.get("id") or f["text"][:60])):
                t, _ = news.tag(f["text"])
                out.append({"kind": "news_held", "account": acct,
                            "title": "持仓快讯%s：%s" % ("·" + t if t != "中性" else "", h["name"]),
                            "content": "**%s**%s  \n%s  \n%s" % (h["name"], where, f["text"], f["time"])})
    # 2) 重大新闻
    day0 = market.now().replace(hour=0, minute=0, second=0).timestamp()
    sent_today = (db.one("SELECT COUNT(*) n FROM messages WHERE ts>=? AND title LIKE '重大新闻%%'", (day0,)) or {}).get("n", 0)
    for f in flash:
        if sent_today >= MAX_MAJOR_PER_DAY:
            break
        if "焦点" not in (f.get("tags") or []) or now - _ts(f.get("ts_full", ""), "%Y-%m-%d %H:%M:%S") > 1800:
            continue
        hit = [w for w in MAJOR_WORDS if w in f["text"]]
        if any(n in f["text"] for n in NOT_MAJOR):
            hit = [w for w in hit if w in ("降准", "降息", "加息", "熔断", "暴跌", "崩盘")]
        if not hit or _seen("major|%s" % (f.get("id") or f["text"][:60])):
            continue
        sent_today += 1
        out.append({"kind": "news_major", "title": "重大新闻：" + " ".join(f["text"].split())[:40],
                    "content": "%s  \n%s（关键词：%s）" % (f["text"], f["time"], "、".join(hit))})
    return out
