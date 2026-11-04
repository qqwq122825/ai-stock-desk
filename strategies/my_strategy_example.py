"""自定义策略模板 —— 复制这个文件改名（如 my_breakout.py），改类名和 name，重启 app.py 即生效。

可用数据：
  q      最新行情快照：price/pct/open/high/low/prev_close/volume/amount/vol_ratio/turnover/
         limit_up/limit_down/bid1/ask1/bids=[[价,手]x5]/asks=[[价,手]x5]/time
  kline  日K列表 [{t,o,c,h,l,v}]（最后一根=今天，已用实时价更新）
  ind    指标摘要：ma5/ma10/ma20/ma60/rsi14/macd{dif,dea,hist,cross}/kdj{k,d,j,cross}/boll{up,mid,low}/
         high20/low20/chg5d_pct/vol_vs_ma5/trend
  ticks  最近约30分钟的秒级快照列表（每个结构同 q，含 fetched_at 时间戳）

工具函数：from core import indicators as I
  I.sma(closes, n)  I.ema_series(closes, n)  I.rsi(closes, n)  I.macd(closes)  I.kdj(kline)
  I.boll(closes)    I.orderbook(q)  -> {imbalance 委比, bid_vol5, ask_vol5, spread_pct, at_limit_up}
"""
from core import indicators as I
from .base import Signal, Strategy


class VolumeBreakout(Strategy):
    name = "volume_breakout"
    label = "放量突破20日高点（示例）"
    description = "价格突破前20日最高价，且量比≥2、买盘委比为正 → 买入信号"
    params = {"days": 20, "vol_ratio": 2.0}
    cooldown_min = 480
    default_enabled = False      # 示例默认关闭，在网页「策略」页打开

    def on_bar(self, sym, q, kline, ind):
        n = self.p["days"]
        if len(kline) < n + 2:
            return None
        prev_high = max(b["h"] for b in kline[-n - 1:-1])
        ob = I.orderbook(q)
        # 回测时没有盘口数据，imbalance=0；实盘时要求买盘不弱于卖盘
        if q["price"] > prev_high and q.get("vol_ratio", 0) >= self.p["vol_ratio"] and ob["imbalance"] >= 0:
            return Signal("buy", "放量突破%d日高点 %.3f，量比 %.1f，委比 %+.0f%%"
                          % (n, prev_high, q["vol_ratio"], ob["imbalance"] * 100), strength=0.7)
        return None
