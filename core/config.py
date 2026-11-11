"""配置管理：data/config.json，缺省值见 DEFAULTS。"""
import copy
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("AI_STOCK_DATA_DIR") or os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    "server": {
        "host": "127.0.0.1",
        "port": 8686,
        # 微信消息里"去确认"链接用的地址；手机要能打开就填局域网/公网地址
        "public_url": "http://127.0.0.1:8686",
        # 设置后所有 /api 请求需带 token（对外开放端口时务必设置）
        "token": "",
    },
    "monitor": {
        # 盘中行情刷新间隔。腾讯/新浪免费源约3秒出一个五档快照，再快也是重复数据
        "poll_interval_sec": 3,
        "record_ticks": True,           # 把秒级快照写入 data/ticks/日期/代码.csv
        "offhours_refresh_sec": 300,    # 非交易时段刷新间隔
        "equity_snapshot_min": 5,       # 净值快照间隔
    },
    "notify": {
        "enabled": True,
        "pushplus_token": "",           # https://www.pushplus.plus
        "serverchan_key": "",           # https://sct.ftqq.com
        "ai_summary": True,             # AI 有交易/待确认时推送摘要
        # 推送降噪：策略信号只推送买/卖且强度≥此值的；"关注"类信号只记录不推送（AI 照样能看到）
        "min_signal_strength": 0.6,
        "push_watch_signals": False,
        # 推送哪些消息。机器人（PushPlus / 微信 ClawBot）两个账户共用；推送内容模拟盘、实盘分开勾选
        # 不分账户的消息：
        "kinds": {
            "premarket": True,          # 盘前外盘简报
            "news_major": True,         # 重大新闻：新浪 7×24「焦点」里的政策/市场级大事（降准降息、证监会、美联储、暴跌…）
            "system": True,             # 系统重启、流程失败
            "alert": False,             # 自选/观察池的盘中异动、策略信号（很多，默认不推；持仓股的异动在分账户里）
            "ai_note": False,           # AI 巡检没交易时的提醒
        },
        # 分账户的消息（实盘的成交/待确认订单不管怎么勾都推送）：
        "accounts": {
            "paper": {"enabled": True,  # 推送模拟盘的动向（没接实盘时靠它看系统在干什么）
                      "kinds": {"trade": True, "committee_pass": True, "committee_reject": False,
                                "watch": True, "daily": True, "weekly": True, "alert_held": True, "news_held": True}},
            "live": {"enabled": True,
                     "kinds": {"trade": True, "committee_pass": True, "committee_reject": False,
                               "watch": True, "daily": True, "weekly": True, "alert_held": True, "news_held": True}},
        },
    },
    "paper": {
        "initial_cash": 10000,
        "allow_offhours": False,        # 允许非交易时间模拟成交（仅调试用）
        "slippage_pct": 0.0005,         # 模拟滑点
    },
    "fees": {
        "commission_rate": 0.00025,     # 佣金 万2.5
        "commission_min": 5.0,          # 最低5元（免五券商改成0）
        "stamp_duty_rate": 0.0005,      # 印花税 卖出万5（ETF免）
        "transfer_fee_rate": 0.00001,   # 过户费 十万分之一
    },
    "risk": {
        "min_confidence": 0.65,         # AI 置信度低于此不执行
        "max_position_pct": 0.4,        # 单票持仓上限（占总资产）
        "max_positions": 3,             # 最多同时持有几只（1万资金分散太多没意义）
        "max_daily_trades": 4,          # 每账户每日最多成交笔数（波段不需要频繁交易）
        "daily_loss_limit_pct": -0.03,  # 当日亏损超过3%熔断：禁止新买入
        "max_order_value": 0,           # 单笔金额上限，0=不限
        "block_st": True,               # 禁止买入ST
        "restrict_to_watchlist": False, # 只允许买自选股
        "max_chase_pct": 5.0,           # 当日涨幅超过此值禁止追买（新手最容易追高）
    },
    "ai": {
        # AI 决策作用的账户：paper=模拟盘自动成交；live=实盘生成待确认订单
        "accounts": ["paper"],
        "pending_expire_min": 15,       # 实盘待确认订单过期时间
        # ---- 可选：内置大模型接口（OpenAI 兼容，DeepSeek/通义/任意）----
        "llm_enabled": False,
        "llm_base_url": "https://api.deepseek.com",
        "llm_model": "deepseek-flash",         # DeepSeek-V4.1-Flash（2026-09）
        "committee_model": "",                 # 委员会单独用的模型，空=同上；可填 deepseek-v4-pro
        "llm_api_key": "",
        "llm_on_alert": True,           # 规则/策略异动时主动调用大模型调研决策
        "llm_signal_min_strength": 0.6, # 策略信号强度≥此值才唤醒大模型（规则异动总是唤醒）
        "llm_timeout_sec": 120,
        "llm_alert_cooldown_min": 10,   # 异动触发调用的最小间隔
        "llm_interval_min": 0,          # 内置定时调用间隔，0=关闭（交给外部定时任务）
        # ---- 可选：异动时唤醒外部 AI（任意命令行 AI）----
        # 规则/强信号触发时在项目目录执行这条 shell 命令，环境变量 AI_TRIGGER=异动描述。例：
        #   claude -p "$(cat prompts/02_异动调研.md)" --allowedTools "Bash,Read,Write"
        "wakeup_command": "",
        "wakeup_cooldown_min": 10,      # 两次唤醒最小间隔
    },
    # 可交易范围（按你的交易权限）：main=沪深主板 gem=创业板 star=科创板 bj=北交所 etf=场内ETF
    "universe": {
        "boards": ["main", "etf"],     # 北交所涨跌幅±30%、流动性差，新手默认不碰（有权限可在设置勾选）
        "min_amount_yi": 2.0,           # 个股当日成交额下限（亿）：太小的票容易被操纵
        "etf_min_amount_yi": 0.5,
        "min_price": 2.0,
        "min_list_days": 250,           # 排除上市不满一年的次新股
        "exclude_st": True,
    },
    "screener": {
        "kline_candidates": 150,        # 初筛后拉日K细算的数量
        "top_n": 20,                    # 候选池大小
        "committee_top": 10,            # 收盘复盘时送委员会审核的数量（分低于 committee_min_score 的不审）
        "committee_min_score": 0.5,
        "committee_parallel": 3,        # 同时开几个委员会（大模型便宜，并行省时间）
        "auto_daily": True,             # 交易日 15:05 自动选股+委员会（不需要外部 AI）
    },
    # 仓位管理
    "sizing": {
        # risk=按单笔风险定仓（股数 = 总资产×单笔风险% ÷ (买入价-止损价)）；percent=按总资产百分比
        "mode": "risk",
        "risk_per_trade_pct": 2.0,      # 每笔最多亏总资产的 2%（1万资金≈200元；1%在小资金下很多票买不了一手）
        # 反马丁金字塔加仓（亏损补仓/马丁格尔始终禁止）
        "pyramid_enabled": True,
        "pyramid_min_profit_pct": 8,    # 浮盈≥8%才允许加仓
        "pyramid_max_adds": 1,          # 最多加仓1次
        "pyramid_add_ratio": 0.5,       # 每次加仓量 = 上次买入量×50%
        "pyramid_raise_stop": True,     # 加仓后止损上移到持仓成本（保本）
    },
    # 大盘情绪择时：不同市场状态下「总仓位上限」（占总资产）
    "regime": {"caps": {"强势": 0.8, "震荡": 0.5, "弱势": 0.2, "冰点": 0.0}},   # 冰点期不开新仓
    "committee": {
        "required_for_buy": True,       # 新建仓/加仓必须先过投研委员会
        "valid_hours": 30,              # 委员会结论有效期（小时）
        "debate_rounds": 2,             # 多空辩论轮数
        "min_confidence": 0.65,         # 组合经理置信度低于此视为不通过
        "auto_run_builtin": True,       # 有内置大模型时，买入被门禁拦下就自动开委员会，通过后自动执行
    },
    "plans": {
        "auto_stop_loss_paper": True,   # 模拟盘跌破计划止损价自动卖出（实盘生成待确认卖单）
        "default_stop_pct": -7,         # 计划未给止损时的默认止损（波段）
        "default_max_hold_days": 20,    # 时间止损：持有超过N天且收益<3% 提醒
    },
    # 每日维护（收盘流程后）：备份 data；清理默认关闭（0 = 永久保留），要清理在这里改天数
    "housekeeping": {"backup_keep": 7, "tick_keep_days": 0, "committee_keep_days": 0},
    # 微信 ClawBot（微信 → 设置 → 插件 → ClawBot）：绑定后提醒优先发到微信对话框，也能在微信里问问题、确认订单
    "wechat_bot": {"enabled": True, "max_push": 9, "also_pushplus": False},
    # 打法：swing=波段（看趋势/基本面，拿几天到几周）；momentum=情绪短线（跟热门题材，卖得比别人快）
    # 两种并存，各账户各用各的，跑一段时间后对比
    "style": {"paper": "momentum", "live": "swing"},
    # 情绪短线参数（只对 style=momentum 的账户生效）
    "momentum": {
        "stop_pct": -4,                 # 买入价下方 4% 硬止损
        "trail_after_pct": 5,           # 浮盈超过 5% 启动回撤止盈
        "trail_pct": 3,                 # 从最高点回撤 3% 就走
        "max_hold_days": 3,             # 拿 3 个交易日还没涨 3% 就走（短线不等）
        "time_stop_min_gain": 3,
        "gap_fail_pct": 2,              # 早盘（9:30~10:00）从开盘价回落 2% 以上 = 高开低走/冲高回落 → 走
        "vwap_break_pct": 1,            # 10 点后跌破分时均价 1% 且板块走弱 → 走
        "news_take_profit_pct": 3,      # 有利好消息且高开 ≥3% → 利好兑现，冲高就卖（真假不重要，卖得快）
        "theme_exit": True,             # 所在题材掉出热门题材前列且板块转跌 → 走
        "theme_top": 5,                 # 只做前 5 名热门题材（选股和委员会用同一个范围）
        "max_chase_pct": 5,             # 情绪短线的追高上限；实际取它和 risk.max_chase_pct 中更小的（风控只收紧不放宽）
        "min_pct": 2,                   # 当日涨幅低于 2% 不算强势
    },
    # 策略开关/参数覆盖：{"macd": {"enabled": true, "params": {...}, "cooldown_min": 240}}
    # 未列出的策略使用其 default_enabled 和默认参数
    "strategies": {},
    "live": {
        # manual=人工确认后在券商App手动下单并回填；qmt=迅投 miniQMT(xtquant，Windows)
        "broker": "manual",
        "qmt_path": "",
        "qmt_account": "",
    },
}

SECRET_KEYS = {("notify", "pushplus_token"), ("notify", "serverchan_key"),
               ("ai", "llm_api_key"), ("server", "token"), ("server", "password")}

_lock = threading.Lock()


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
            return copy.deepcopy(DEFAULTS)
        with open(CONFIG_PATH, encoding="utf-8") as f:
            try:
                user = json.load(f)
            except ValueError:
                user = {}
        return _merge(DEFAULTS, user)


def save_config(patch):
    """patch 为部分配置，合并后保存。值为 '******' 的密钥字段保持不变。"""
    cur = load_config()
    for (sec, key) in SECRET_KEYS:
        if isinstance(patch.get(sec), dict) and patch[sec].get(key) == "******":
            patch[sec].pop(key)
    new = _merge(cur, patch)
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    return new


def masked_config():
    cfg = load_config()
    for (sec, key) in SECRET_KEYS:
        if cfg.get(sec, {}).get(key):
            cfg[sec][key] = "******"
    return cfg


def style(account, cfg=None):
    """账户用哪种打法：swing 波段 / momentum 情绪短线。"""
    cfg = cfg or load_config()
    st = (cfg.get("style") or {}).get(account) or "swing"
    return st if st in ("swing", "momentum") else "swing"
