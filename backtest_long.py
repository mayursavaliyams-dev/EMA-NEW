#!/usr/bin/env python3
"""
FULL multi-year backtest (INDEX, daily) — signal edge over ~3 years.
====================================================================
Upstox has NO expired-option history, so a multi-year OPTION P&L backtest
isn't possible. But daily INDEX candles go back years. This backtests each
strategy's DIRECTIONAL edge on the index (P&L in index POINTS), flipping
long/short on opposite signals — a proxy for whether the signal "works".

Usage:
  python backtest_long.py            # NIFTY, ~1200 days
  python backtest_long.py SENSEX 1200
"""

import sys
import urllib.parse
from datetime import datetime, timedelta

import backtest_gamma as bt
import backtest_multi as bm   # reuse indicators + signal generators


def daily_candles(ikey, from_date, to_date):
    k = urllib.parse.quote(ikey, safe="")
    data = bt.get(f"/v3/historical-candle/{k}/days/1/{to_date}/{from_date}")
    out = []
    for c in data.get("data", {}).get("candles", []):
        out.append({"time": c[0][:10], "open": float(c[1]), "high": float(c[2]),
                    "low": float(c[3]), "close": float(c[4])})
    out.sort(key=lambda x: x["time"])
    return out


def sim_direction(signals, ohlc):
    """Flip long/short on opposite signals. P&L in index points."""
    trades = []
    pos = None  # ('CE'/'PE', entry_price, entry_day)
    for i in sorted(signals):
        side = signals[i]; price = ohlc[i]["close"]; day = ohlc[i]["time"]
        if pos is None:
            pos = (side, price, day)
        elif pos[0] != side:                       # opposite -> close & reverse
            pts = (price - pos[1]) if pos[0] == "CE" else (pos[1] - price)
            trades.append({"in": pos[2], "out": day, "side": pos[0], "pts": pts})
            pos = (side, price, day)
    if pos is not None:                            # close last at final candle
        price = ohlc[-1]["close"]
        pts = (price - pos[1]) if pos[0] == "CE" else (pos[1] - price)
        trades.append({"in": pos[2], "out": ohlc[-1]["time"], "side": pos[0], "pts": pts})
    return trades


def main():
    symbol = (sys.argv[1].upper() if len(sys.argv) > 1 else "NIFTY")
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
    if symbol not in bt.SYMBOLS:
        print("unknown symbol"); return
    ikey = bt.SYMBOLS[symbol]
    to_date = datetime.now(bt.IST).strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    ohlc = daily_candles(ikey, from_date, to_date)
    if len(ohlc) < 60:
        print("not enough data"); return
    closes = [c["close"] for c in ohlc]
    e9 = bm.ema(closes, 9); e21 = bm.ema(closes, 21); e50 = bm.ema(closes, 50)
    print(f"\n=== FULL backtest (INDEX daily, points): {symbol} ===")
    print(f"{len(ohlc)} trading days  ({ohlc[0]['time']} -> {ohlc[-1]['time']})\n")

    results = []
    for name, fn in bm.STRATEGIES:
        if name.startswith("ORB"):
            continue   # ORB is an intraday concept, skip on daily
        try:
            sigs = fn(ohlc, closes, e9, e21, e50)
            trades = sim_direction(sigs, ohlc)
        except Exception as ex:
            print(f"  {name}: ERROR {ex}"); continue
        n = len(trades)
        wins = sum(1 for t in trades if t["pts"] > 0)
        pts = sum(t["pts"] for t in trades)
        wr = (wins / n * 100) if n else 0
        results.append((name, n, wins, wr, pts))

    results.sort(key=lambda x: x[4], reverse=True)
    print(f"{'Strategy':<22}{'Trades':>7}{'Win%':>6}{'Total Points':>14}")
    print("-" * 50)
    for name, n, wins, wr, pts in results:
        print(f"{name:<22}{n:>7}{wr:>5.0f}%{pts:>+14,.0f}")
    print()
    if results:
        print(f"BEST: {results[0][0]}  ({results[0][4]:+,.0f} pts over {len(ohlc)} days)\n")
    print("NOTE: index POINTS (directional proxy), not option Rs. Daily timeframe.")
    print("      Real intraday-option multi-year P&L needs paid data (TrueData/GDFL).\n")


if __name__ == "__main__":
    main()
