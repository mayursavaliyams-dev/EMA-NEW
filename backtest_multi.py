#!/usr/bin/env python3
"""
Multi-Strategy Backtester
=========================
Runs several well-known trading strategies through the SAME option-execution
engine (signal on index -> BUY ATM Call/Put -> fixed SL + trailing profit,
intraday, one position at a time, daily cap) and ranks them by net P&L.

Strategies: EMA crossover, Inside-bar breakout, Supertrend, RSI reversal,
ORB (opening range breakout), Bollinger breakout, MACD crossover.

Usage:
  python backtest_multi.py            # NIFTY
  python backtest_multi.py SENSEX 12
"""

import sys
import urllib.parse
from datetime import datetime, timedelta

import backtest_gamma as bt
import backtest_strategy as bs   # reuse fetch_ohlc + ema

EXEC = {"stopPct": 30, "trailProfitActivatePct": 30, "trailProfitGapPct": 20, "maxTradesPerDay": 6}


# ---------- indicators ----------
def ema(v, p): return bs.ema(v, p)

def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period: return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag = gains / period; al = losses / period
    out[period] = 100 - 100 / (1 + (ag / al if al else 999))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100 - 100 / (1 + (ag / al if al else 999))
    return out

def macd(closes):
    e12 = ema(closes, 12); e26 = ema(closes, 26)
    line = [(a - b) if (a is not None and b is not None) else None for a, b in zip(e12, e26)]
    vals = [x for x in line if x is not None]
    sigv = ema(vals, 9)
    sig = [None] * len(line)
    j = 0
    for i in range(len(line)):
        if line[i] is not None:
            sig[i] = sigv[j] if j < len(sigv) else None
            j += 1
    return line, sig

def bollinger(closes, period=20, mult=2):
    up = [None] * len(closes); lo = [None] * len(closes)
    import statistics
    for i in range(period - 1, len(closes)):
        w = closes[i - period + 1:i + 1]
        m = sum(w) / period; sd = statistics.pstdev(w)
        up[i] = m + mult * sd; lo[i] = m - mult * sd
    return up, lo

def supertrend(ohlc, period=10, mult=3):
    n = len(ohlc); st = [None] * n; dir_ = [None] * n
    if n < period + 1: return st, dir_
    trs = [0.0] * n
    for i in range(1, n):
        h, l, pc = ohlc[i]["high"], ohlc[i]["low"], ohlc[i - 1]["close"]
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr = [None] * n
    atr[period] = sum(trs[1:period + 1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
    ub = [None] * n; lb = [None] * n
    for i in range(period, n):
        hl2 = (ohlc[i]["high"] + ohlc[i]["low"]) / 2
        ub[i] = hl2 + mult * atr[i]; lb[i] = hl2 - mult * atr[i]
    trend = 1
    for i in range(period + 1, n):
        c = ohlc[i]["close"]
        if c > (ub[i - 1] or 0): trend = 1
        elif c < (lb[i - 1] or 0): trend = -1
        dir_[i] = trend
    return atr, dir_


# ---------- strategy signal generators -> dict {i: 'CE'/'PE'} ----------
def sig_ema_cross(o, c, e9, e21, e50):
    s = {}
    for i in range(1, len(c)):
        if None in (e9[i], e21[i], e9[i-1], e21[i-1]): continue
        if e9[i-1] <= e21[i-1] and e9[i] > e21[i]: s[i] = "CE"
        elif e9[i-1] >= e21[i-1] and e9[i] < e21[i]: s[i] = "PE"
    return s

def sig_inside_bar(o, c, e9, e21, e50):
    s = {}
    for i in range(4, len(c)):
        if None in (e9[i], e21[i], e50[i]): continue
        cur, inside, mother, brk = o[i], o[i-2], o[i-3], o[i-1]
        price = cur["close"]
        if not (inside["high"] <= mother["high"] and inside["low"] >= mother["low"]): continue
        if (mother["high"] - mother["low"]) / price < 0.0008: continue
        if e9[i] > e21[i] > e50[i] and price > e21[i] and brk["close"] > inside["high"]: s[i] = "CE"
        elif e9[i] < e21[i] < e50[i] and price < e21[i] and brk["close"] < inside["low"]: s[i] = "PE"
    return s

def sig_supertrend(o, c, e9, e21, e50):
    _, d = supertrend(o)
    s = {}
    for i in range(1, len(c)):
        if d[i] is None or d[i-1] is None: continue
        if d[i-1] == -1 and d[i] == 1: s[i] = "CE"
        elif d[i-1] == 1 and d[i] == -1: s[i] = "PE"
    return s

def sig_rsi(o, c, e9, e21, e50):
    r = rsi(c); s = {}
    for i in range(1, len(c)):
        if r[i] is None or r[i-1] is None: continue
        if r[i-1] <= 30 and r[i] > 30: s[i] = "CE"      # oversold bounce
        elif r[i-1] >= 70 and r[i] < 70: s[i] = "PE"    # overbought drop
    return s

def sig_orb(o, c, e9, e21, e50):
    # opening range = first 15 one-min candles of each day; break = signal (once/day)
    s = {}; day_hi = {}; day_lo = {}; count = {}; done = {}
    for i in range(len(c)):
        day = datetime.fromtimestamp(o[i]["time"], bt.IST).strftime("%Y-%m-%d")
        count[day] = count.get(day, 0) + 1
        if count[day] <= 15:
            day_hi[day] = max(day_hi.get(day, -1e9), o[i]["high"])
            day_lo[day] = min(day_lo.get(day, 1e9), o[i]["low"])
            continue
        if done.get(day): continue
        if o[i]["close"] > day_hi[day]: s[i] = "CE"; done[day] = True
        elif o[i]["close"] < day_lo[day]: s[i] = "PE"; done[day] = True
    return s

def sig_bollinger(o, c, e9, e21, e50):
    up, lo = bollinger(c); s = {}
    for i in range(1, len(c)):
        if None in (up[i], up[i-1], lo[i], lo[i-1]): continue
        if c[i-1] <= up[i-1] and c[i] > up[i]: s[i] = "CE"
        elif c[i-1] >= lo[i-1] and c[i] < lo[i]: s[i] = "PE"
    return s

def sig_macd(o, c, e9, e21, e50):
    line, sg = macd(c); s = {}
    for i in range(1, len(c)):
        if None in (line[i], sg[i], line[i-1], sg[i-1]): continue
        if line[i-1] <= sg[i-1] and line[i] > sg[i]: s[i] = "CE"
        elif line[i-1] >= sg[i-1] and line[i] < sg[i]: s[i] = "PE"
    return s

STRATEGIES = [
    ("EMA 9/21 Cross", sig_ema_cross),
    ("Inside-Bar Breakout", sig_inside_bar),
    ("Supertrend", sig_supertrend),
    ("RSI Reversal", sig_rsi),
    ("ORB (15m)", sig_orb),
    ("Bollinger Breakout", sig_bollinger),
    ("MACD Cross", sig_macd),
]


def execute(signals, epochs, o_by_i, opt_series, atm, lot):
    trades = []; busy = 0; per_day = {}
    for i in sorted(signals):
        t = epochs[i]
        if t <= busy: continue
        side = signals[i]; price = o_by_i[i]["close"]
        day = datetime.fromtimestamp(t, bt.IST).strftime("%Y-%m-%d")
        if per_day.get(day, 0) >= EXEC["maxTradesPerDay"]: continue
        strike = atm(price)
        os_ = opt_series(strike, side)
        entry = os_.get(t)
        if entry is None or entry <= 0: continue
        fixed_sl = entry * (1 - EXEC["stopPct"] / 100)
        act = entry * (1 + EXEC["trailProfitActivatePct"] / 100)
        gap = EXEC["trailProfitGapPct"] / 100
        peak = entry; sl = fixed_sl; exit_px = entry; exit_t = t
        for e in epochs:
            if e <= t: continue
            p = os_.get(e)
            if p is None: continue
            if datetime.fromtimestamp(e, bt.IST).strftime("%Y-%m-%d") != day: break
            if p > peak: peak = p
            if peak >= act: sl = max(sl, peak * (1 - gap))
            if p <= sl: exit_px = p; exit_t = e; break
            exit_px = p; exit_t = e
        trades.append({"pnl": (exit_px - entry) * lot})
        per_day[day] = per_day.get(day, 0) + 1
        busy = exit_t
    return trades


def main():
    symbol = (sys.argv[1].upper() if len(sys.argv) > 1 else "NIFTY")
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    if symbol not in bt.SYMBOLS:
        print("unknown symbol"); return
    ikey = bt.SYMBOLS[symbol]
    today = datetime.now(bt.IST).strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    lot = bt.LOT.get(symbol, 1)
    print(f"\n=== MULTI-STRATEGY backtest: {symbol} (from {from_date}, lot {lot}) ===")

    contracts = bt.get(f"/v2/option/contract?instrument_key={urllib.parse.quote(ikey, safe='')}")
    exps = sorted({x.get("expiry") for x in contracts.get("data", []) if x.get("expiry") and x.get("expiry") >= today})
    expiry = exps[0] if exps else None

    ohlc = bs.fetch_ohlc(ikey, from_date, today)
    if len(ohlc) < 60:
        print("not enough data"); return
    epochs = [c["time"] for c in ohlc]
    closes = [c["close"] for c in ohlc]
    e9 = ema(closes, 9); e21 = ema(closes, 21); e50 = ema(closes, 50)
    print(f"index candles: {len(ohlc)}")

    chain = bt.get(f"/v2/option/chain?instrument_key={urllib.parse.quote(ikey, safe='')}&expiry_date={expiry}")
    rows = sorted(chain.get("data", []), key=lambda r: r.get("strike_price", 0))
    strikes = [r["strike_price"] for r in rows]
    keymap = {}
    for r in rows:
        if r.get("call_options"): keymap[(r["strike_price"], "CE")] = r["call_options"]["instrument_key"]
        if r.get("put_options"):  keymap[(r["strike_price"], "PE")] = r["put_options"]["instrument_key"]
    cache = {}
    def opt_series(strike, side):
        k = (strike, side)
        if k not in cache:
            key = keymap.get(k)
            cache[k] = bt.series_max(key, from_date, today) if key else {}
        return cache[k]
    def atm(spot): return min(strikes, key=lambda s: abs(s - spot)) if strikes else None

    print("running strategies (fetching option data)...\n")
    results = []
    for name, fn in STRATEGIES:
        try:
            sigs = fn(ohlc, closes, e9, e21, e50)
            trades = execute(sigs, epochs, ohlc, opt_series, atm, lot)
        except Exception as ex:
            print(f"  {name}: ERROR {ex}"); continue
        n = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        net = sum(t["pnl"] for t in trades)
        wr = (wins / n * 100) if n else 0
        results.append((name, n, wins, wr, net))

    results.sort(key=lambda x: x[4], reverse=True)
    print(f"{'Strategy':<22}{'Trades':>7}{'Wins':>6}{'Win%':>6}{'NET Rs':>10}")
    print("-" * 52)
    for name, n, wins, wr, net in results:
        print(f"{name:<22}{n:>7}{wins:>6}{wr:>5.0f}%{net:>+10,.0f}")
    print()
    if results:
        print(f"BEST: {results[0][0]}  (NET {results[0][4]:+,.0f} Rs)\n")


if __name__ == "__main__":
    main()
