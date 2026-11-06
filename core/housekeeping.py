"""每日收盘后的维护：备份数据；按设置清理旧的秒级行情和会议资料（默认不清理）。

备份：data/backups/stock-YYYYMMDD.zip（数据库用 SQLite 在线备份，保证一致；外加 config、委员会资料、交易日志），
     保留最近 housekeeping.backup_keep 份（默认 7；删的只是更早的备份副本，原数据不动）。
     注意：备份和原数据在同一块硬盘上，防误删/数据库损坏，不防硬盘坏。想更保险，宝塔「计划任务」里把 data/backups 同步到别处。
清理：housekeeping.tick_keep_days / committee_keep_days 设为大于 0 才会删（默认 0 = 永久保留）。
日志：app.log 超过 10MB 自动换新文件，最多留 3 个（在 app.py 里设置）。
"""
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile

from .config import DATA_DIR

log = logging.getLogger("housekeeping")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")


def _hk():
    from .config import load_config
    h = load_config().get("housekeeping") or {}
    return int(h.get("backup_keep", 7) or 7), int(h.get("tick_keep_days", 0) or 0), int(h.get("committee_keep_days", 0) or 0)


def backup(now=None):
    now = now or time.time()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    day = time.strftime("%Y%m%d", time.localtime(now))
    path = os.path.join(BACKUP_DIR, "stock-%s.zip" % day)
    tmpdir = tempfile.mkdtemp(prefix="bk", dir=BACKUP_DIR)
    try:
        dbp = os.path.join(DATA_DIR, "stock.db")
        snap = os.path.join(tmpdir, "stock.db")
        if os.path.exists(dbp):
            src, dst = sqlite3.connect(dbp), sqlite3.connect(snap)
            with dst:
                src.backup(dst)
            src.close(); dst.close()
        with zipfile.ZipFile(path + ".part", "w", zipfile.ZIP_DEFLATED) as z:
            if os.path.exists(snap):
                z.write(snap, "stock.db")
            for name in ("config.json", "wechat_bot.json"):
                p = os.path.join(DATA_DIR, name)
                if os.path.exists(p):
                    z.write(p, name)
            for sub in ("committee", "journal"):
                root = os.path.join(DATA_DIR, sub)
                for dp, _, files in os.walk(root):
                    for f in files:
                        full = os.path.join(dp, f)
                        z.write(full, os.path.relpath(full, DATA_DIR))
        os.replace(path + ".part", path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    size = round(os.path.getsize(path) / 1e6, 2)
    keep = _hk()[0]
    olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("stock-") and f.endswith(".zip"))
    for f in olds[:-keep]:
        os.remove(os.path.join(BACKUP_DIR, f))
    return {"path": path, "size_mb": size, "kept": min(len(olds), keep)}


def cleanup(now=None):
    now = now or time.time()
    _, tick_days, committee_days = _hk()
    removed = {"tick_days": 0, "committee_files": 0, "note": "清理已关闭（永久保留）" if not (tick_days or committee_days) else ""}
    tick_root = os.path.join(DATA_DIR, "ticks")
    cutoff = time.strftime("%Y%m%d", time.localtime(now - tick_days * 86400))
    if tick_days and os.path.isdir(tick_root):
        for d in os.listdir(tick_root):
            if d.isdigit() and len(d) == 8 and d < cutoff:
                shutil.rmtree(os.path.join(tick_root, d), ignore_errors=True)
                removed["tick_days"] += 1
    croot = os.path.join(DATA_DIR, "committee")
    if committee_days and os.path.isdir(croot):
        for f in os.listdir(croot):
            p = os.path.join(croot, f)
            if os.path.isfile(p) and now - os.path.getmtime(p) > committee_days * 86400:
                os.remove(p)
                removed["committee_files"] += 1
    return removed


def daily(now=None):
    out = {}
    try:
        out["backup"] = backup(now)
    except Exception as e:
        log.exception("备份失败")
        out["backup_error"] = str(e)
    try:
        out["cleanup"] = cleanup(now)
    except Exception as e:
        log.exception("清理失败")
        out["cleanup_error"] = str(e)
    log.info("每日维护：%s", out)
    return out
