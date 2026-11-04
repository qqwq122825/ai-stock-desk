"""策略日线回测：用历史日K逐日调用策略的 on_bar，信号次日开盘价成交。

规则：
- 每只股票独立回测，初始资金 cash，买入信号满仓（position_pct）买入，卖出信号全部卖出
- 信号在第 i 天收盘产生 → 第 i+1 天开盘价成交（避免未来函数）
- 一手取整、A股费用（佣金/印花税/过户费）、T+1 天然满足
- 次日开盘涨停（开盘=涨停价）买不进；开盘跌停卖不出
- 秒级盘口策略（只有 on_tick）无法用日K回测

用法：python3 agent_cli.py backtest resonance 600036 000001 --days 500
"""
import strategies as strat_pkg
from . import broker, indicators, market
from .config import load_config


def _pseudo_quote(sym, kl, i):
    b = kl[i]
    prev = kl[i - 1]["c"] if i > 0 else b["o"]
    vols = [x["v"] for x in kl[max(0, i - 5):i]]
    vr = b["v"] / (sum(vols) / len(vols)) if vols and sum(vols) else 1.0
    lu, ld = round(prev * 1.1, 2), round(prev * 0.9, 2)
    return {"symbol": sym, "name": sym, "price": b["c"], "prev_close": prev, "open": b["o"], "high": b["h"],
            "low": b["l"], "volume": b["v"] * 100, "amount": b["v"] * 100 * b["c"], "pct": (b["c"] / prev - 1) * 100,
            "change": b["c"] - prev, "vol_ratio": vr, "turnover": 0, "limit_up": lu, "limit_down": ld,
            "bids": [[b["c"], 0]] * 5, "asks": [[b["c"], 0]] * 5, "bid1": b["c"], "ask1": b["c"],
            "suspended": False, "time": b["t"].replace("-", "") + "150000", "fetched_at": 0}


def run_one(inst, sym, kl, cash=10000.0, position_pct=0.95, warmup=60, fees=None):
    fees = fees or load_config()["fees"]
    sym = market.normalize(sym)
    cash0 = cash
    shares, cost = 0, 0.0
    trades, curve = [], []
    entry_i = None
    pending = None
    for i in range(warmup, len(kl)):
        bar = kl[i]
        # 1) 先处理前一天的信号：今天开盘成交
        if pending:
            side, msg = pending
            px = bar["o"]
            prev_c = kl[i - 1]["c"]
            if side == "buy" and shares == 0 and px < round(prev_c * 1.1, 2) - 1e-6:
                n = broker.round_buy_shares(sym, int(cash * position_pct / px))
                if n > 0:
                    amt = px * n
                    fee = broker.calc_fees(sym, "buy", amt, fees)
                    if amt + fee <= cash:
                        cash -= amt + fee
                        shares, cost, entry_i = n, (amt + fee) / n, i
                        trades.append({"date": bar["t"], "side": "buy", "price": px, "shares": n, "fee": fee, "why": msg})
            elif side == "sell" and shares > 0 and px > round(prev_c * 0.9, 2) + 1e-6:
                amt = px * shares
                fee = broker.calc_fees(sym, "sell", amt, fees)
                cash += amt - fee
                pnl = (px - cost) * shares - fee
                trades.append({"date": bar["t"], "side": "sell", "price": px, "shares": shares, "fee": fee,
                               "pnl": round(pnl, 2), "pnl_pct": round((px / cost - 1) * 100, 2),
                               "hold_days": i - entry_i, "why": msg})
                shares = 0
        pending = None
        # 2) 今天收盘跑策略
        window = kl[:i + 1]
        q = _pseudo_quote(sym, kl, i)
        try:
            sig = inst.on_bar(sym, q, window, indicators.summarize(window))
        except Exception:
            sig = None
        for s in (sig if isinstance(sig, list) else [sig]):
            if s and s["side"] in ("buy", "sell"):
                pending = (s["side"], s["message"])
        curve.append({"t": bar["t"], "equity": round(cash + shares * bar["c"], 2)})

    if not curve:
        return {"symbol": sym, "error": "K线数据不足"}
    final = curve[-1]["equity"]
    peak, mdd = 0, 0
    for p in curve:
        peak = max(peak, p["equity"])
        mdd = min(mdd, p["equity"] / peak - 1)
    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sells if t["pnl"] > 0]
    bh = kl[-1]["c"] / kl[warmup]["c"] - 1
    return {
        "symbol": sym, "start": kl[warmup]["t"], "end": kl[-1]["t"], "days": len(curve),
        "return_pct": round((final / cash0 - 1) * 100, 2),
        "buy_hold_pct": round(bh * 100, 2),
        "excess_pct": round((final / cash0 - 1 - bh) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "trades": len(sells), "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "avg_pnl_pct": round(sum(t["pnl_pct"] for t in sells) / len(sells), 2) if sells else None,
        "avg_hold_days": round(sum(t["hold_days"] for t in sells) / len(sells), 1) if sells else None,
        "fees": round(sum(t["fee"] for t in trades), 2),
        "holding_at_end": shares > 0,
        "trade_log": trades[-20:],
        "curve": curve[::max(1, len(curve) // 120)],
    }


def run(strategy, symbols, days=500, params=None, cash=10000.0, position_pct=0.95, kline_fn=None):
    classes = strat_pkg.discover()
    if strategy not in classes:
        raise ValueError("没有策略 %s，可选：%s" % (strategy, ", ".join(classes)))
    cls = classes[strategy]
    if cls.on_bar is strat_pkg.Strategy.on_bar or not cls.backtestable:
        raise ValueError("%s 依赖实时盘口/资金流/新闻或只发关注提醒，无法用历史日K回测" % strategy)
    inst = cls(**(params or {}))
    kline_fn = kline_fn or (lambda s: market.get_kline(s, "day", days + 60))
    results = []
    for s in symbols:
        try:
            results.append(run_one(inst, s, kline_fn(s), cash, position_pct))
        except Exception as e:
            results.append({"symbol": s, "error": str(e)})
    ok = [r for r in results if "error" not in r]
    summary = None
    if ok:
        n = len(ok)
        summary = {
            "strategy": strategy, "label": cls.label, "params": inst.p, "symbols": n,
            "avg_return_pct": round(sum(r["return_pct"] for r in ok) / n, 2),
            "avg_buy_hold_pct": round(sum(r["buy_hold_pct"] for r in ok) / n, 2),
            "avg_excess_pct": round(sum(r["excess_pct"] for r in ok) / n, 2),
            "avg_max_drawdown_pct": round(sum(r["max_drawdown_pct"] for r in ok) / n, 2),
            "total_trades": sum(r["trades"] for r in ok),
            "beat_buy_hold": sum(1 for r in ok if r["excess_pct"] > 0),
        }
    return {"summary": summary, "results": results}
