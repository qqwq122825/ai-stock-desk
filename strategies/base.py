"""策略基类。

写自己的策略：在 strategies/ 目录下新建 my_xxx.py，继承 Strategy，实现 on_bar 和/或 on_tick，
返回 Signal（或 Signal 列表 / None）。重启 app.py 后自动加载，在网页「策略」页启用和调参。

  on_bar(sym, quote, kline, ind)
      每次行情刷新（盘中约3秒）调用。kline = 日K（今天这根已用实时价更新），
      ind = indicators.summarize(kline) 的结果（ma5/ma20/rsi14/macd/kdj/boll...）。
  quote["intel"]（实盘监控中才有，约每分钟更新）：消息面+资金面，见 core/news.py intel()
      flow_today{main_yi 主力净额亿, small_yi 小单, main_last30m_yi ...}、flow_days、main_5d_yi、
      news_today{count,pos,neg}、news_3d[...]、dragon_tiger_30d[...]
  on_tick(sym, quote, ticks)
      秒级盘口策略。ticks = 最近的行情快照列表（旧→新，每个含 price/volume/bids/asks/time），
      quote = 最新一个快照。

Signal 字段：
  side      "buy" | "sell" | "watch"（只提醒）
  strength  0~1，信号强度（会展示给 AI 参考，也用于是否唤醒大模型）
  message   人能看懂的说明
"""


class Signal(dict):
    def __init__(self, side, message, strength=0.5, **extra):
        super().__init__(side=side, message=message, strength=round(float(strength), 2), **extra)


class Strategy:
    name = "base"          # 唯一英文名（配置里用）
    label = "基础策略"      # 中文名
    description = ""
    params = {}            # 默认参数，可在网页/配置覆盖
    cooldown_min = 240     # 同一只股票同方向信号的冷却时间（分钟）
    default_enabled = True
    backtestable = True    # 依赖实时资金流/新闻/盘口的策略设为 False（历史日K回测不了）

    def __init__(self, **params):
        self.p = dict(self.params)
        self.p.update({k: v for k, v in params.items() if k in self.params})

    def on_bar(self, sym, quote, kline, ind):
        return None

    def on_tick(self, sym, quote, ticks):
        return None
