"""SQLite 存储。使用回滚日志(TRUNCATE)模式，便于不同进程/虚拟机共享同一个文件。"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from .config import DATA_DIR, load_config

DB_PATH = os.path.join(DATA_DIR, "stock.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY, name TEXT, initial_cash REAL, cash REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS positions(
  account TEXT, symbol TEXT, name TEXT, shares INTEGER, avg_cost REAL,
  today_bought INTEGER DEFAULT 0, last_buy_date TEXT, opened_at REAL, peak_price REAL,
  PRIMARY KEY(account, symbol));
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, account TEXT, symbol TEXT, name TEXT,
  side TEXT, price REAL, shares INTEGER, amount REAL, fee REAL, pnl REAL,
  source TEXT, decision_id INTEGER, order_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, updated_at REAL, account TEXT,
  symbol TEXT, name TEXT, side TEXT, shares INTEGER, price_type TEXT, ref_price REAL,
  limit_price REAL, status TEXT, source TEXT, decision_id INTEGER, reason TEXT,
  confidence REAL, broker_order_id TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS watchlist(
  symbol TEXT PRIMARY KEY, name TEXT, note TEXT, added_by TEXT, added_at REAL);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, type TEXT, params TEXT,
  action TEXT DEFAULT 'notify', enabled INTEGER DEFAULT 1, cooldown_min INTEGER DEFAULT 30,
  note TEXT, created_by TEXT, created_at REAL, state TEXT DEFAULT '{}', last_triggered REAL);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, source TEXT, rule_id INTEGER, strategy TEXT,
  symbol TEXT, name TEXT, type TEXT, side TEXT, strength REAL, message TEXT, price REAL,
  action_result TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT, trigger TEXT,
  market_view TEXT, payload TEXT, results TEXT);
CREATE TABLE IF NOT EXISTS equity(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, ts REAL, equity REAL, cash REAL,
  market_value REAL);
CREATE TABLE IF NOT EXISTS quotes(
  symbol TEXT PRIMARY KEY, name TEXT, data TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, title TEXT, content TEXT,
  channels TEXT, ok INTEGER);
CREATE TABLE IF NOT EXISTS screens(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, regime TEXT, stats TEXT);
CREATE TABLE IF NOT EXISTS candidates(screen_id INTEGER, rank INTEGER, symbol TEXT, name TEXT, board TEXT,
  industry TEXT, score REAL, price REAL, pct REAL, details TEXT, PRIMARY KEY(screen_id, symbol));
CREATE TABLE IF NOT EXISTS committee_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, name TEXT, account TEXT, intent TEXT,
  mode TEXT, trigger TEXT, status TEXT, price REAL, decision TEXT, confidence REAL, final TEXT,
  dossier_path TEXT, error TEXT, finished_at REAL);
CREATE TABLE IF NOT EXISTS committee_opinions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, role TEXT, round INTEGER, ts REAL,
  stance TEXT, score REAL, confidence REAL, summary TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, symbol TEXT, name TEXT, run_id INTEGER,
  created_at REAL, updated_at REAL, status TEXT, entry_price REAL, stop_loss REAL, target REAL,
  trailing_pct REAL, max_hold_days INTEGER, thesis TEXT, invalidation TEXT, watch_points TEXT,
  checks TEXT DEFAULT '{}', closed_at REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS news_pushed(key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS entry_watch(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, symbol TEXT, name TEXT, run_id INTEGER, kind TEXT,
  entry REAL, stop REAL, target REAL, ref_price REAL, created_at REAL, expires_at REAL, status TEXT,
  updated_at REAL, rerun_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS emotion_days(date TEXT PRIMARY KEY, data TEXT, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_runs ON committee_runs(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_plans ON plans(account, symbol, status);
CREATE INDEX IF NOT EXISTS idx_equity ON equity(account, ts);
CREATE INDEX IF NOT EXISTS idx_trades ON trades(account, ts);
CREATE INDEX IF NOT EXISTS idx_alerts ON alerts(ts);
"""

DEFAULT_WATCH = [
    ("sh510300", "沪深300ETF", "大盘基准"),
    ("sz159915", "创业板ETF", "成长风格"),
    ("sz000001", "平安银行", ""),
    ("sh600036", "招商银行", ""),
    ("sh601899", "紫金矿业", ""),
    ("sz000333", "美的集团", ""),
    ("sh600900", "长江电力", ""),
]

DEFAULT_RULES = [
    # symbol, type, params, action, cooldown, note
    ("*POS", "stop_loss", {"pct": -5}, "notify", 60, "持仓浮亏5%提醒"),
    ("*POS", "take_profit", {"pct": 10}, "notify", 120, "持仓浮盈10%提醒"),
    ("*POS", "trailing_stop", {"pct": 6}, "notify", 120, "从持仓最高点回撤6%"),
    ("*WATCH", "rapid_move", {"pct": 2.5, "minutes": 5}, "notify", 60, "5分钟急涨急跌2.5%"),
    ("*WATCH", "pct_above", {"pct": 6}, "notify", 240, "大涨6%"),
    ("*WATCH", "pct_below", {"pct": -5}, "notify", 240, "大跌5%"),
    ("*WATCH", "vol_ratio_above", {"value": 3}, "notify", 120, "量比放大3倍"),
]


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    # TRUNCATE：提交后清空日志文件而不是删除它。兼容不允许删除文件的共享/挂载目录
    c.execute("PRAGMA journal_mode=TRUNCATE")
    return c


def all(sql, args=()):
    c = _connect()
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def one(sql, args=()):
    rows = all(sql, args)
    return rows[0] if rows else None


def run(sql, args=()):
    c = _connect()
    try:
        cur = c.execute(sql, args)
        return cur.lastrowid
    finally:
        c.close()


@contextmanager
def tx():
    """写事务：BEGIN IMMEDIATE 保证并发安全。"""
    c = _connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        c.close()


def init_db():
    c = _connect()
    try:
        c.executescript(SCHEMA)
        # 迁移：选股结果按账户分开（模拟盘 / 实盘各选各的）
        cols = [r[1] for r in c.execute("PRAGMA table_info(screens)").fetchall()]
        if "account" not in cols:
            c.execute("ALTER TABLE screens ADD COLUMN account TEXT DEFAULT 'paper'")
        pcols = [r[1] for r in c.execute("PRAGMA table_info(plans)").fetchall()]
        if "style" not in pcols:      # 迁移：持仓计划记录打法（swing 波段 / momentum 情绪短线）
            c.execute("ALTER TABLE plans ADD COLUMN style TEXT DEFAULT 'swing'")
        cfg = load_config()
        now = time.time()
        first_run = not c.execute("SELECT 1 FROM accounts WHERE id='paper'").fetchone()
        if first_run:
            cash = float(cfg["paper"]["initial_cash"])
            c.execute("INSERT INTO accounts VALUES('paper','模拟盘',?,?,?)", (cash, cash, now))
        if not c.execute("SELECT 1 FROM accounts WHERE id='live'").fetchone():
            c.execute("INSERT INTO accounts VALUES('live','实盘',0,0,?)", (now,))
        if first_run:
            for s, n, note in DEFAULT_WATCH:
                c.execute("INSERT OR IGNORE INTO watchlist VALUES(?,?,?,?,?)", (s, n, note, "system", now))
            for s, t, p, a, cd, note in DEFAULT_RULES:
                c.execute("INSERT INTO rules(symbol,type,params,action,cooldown_min,note,created_by,created_at)"
                          " VALUES(?,?,?,?,?,?,?,?)", (s, t, json.dumps(p), a, cd, note, "system", now))
    finally:
        c.close()


def jloads(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default
