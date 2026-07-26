#!/usr/bin/env python3
"""
Strategy Backtester — EMA inside-bar breakout -> ATM option (Fixed SL + Trail Profit)
====================================================================================
Replays the DASHBOARD strategy on real data:
  - index 1-min candles + EMAs (9/21/50)
  - inside-bar breakout signal (trend-aligned), exactly like the dashboard
  - on each signal: BUY ATM Call (bullish) / Put (bearish) at its real premium
  - exit: fixed SL (-stopPct) + trailing profit (after activatePct, lock gapPct below peak)
  - discipline: one position at a time, max trades/day

Usage:
  python backtest_strategy.py                 # NIFTY, nearest expiry, ~10 days
  python backtest_strategy.py BANKNIFTY
  python backtest_strategy.py NIFTY 2026-06-30 10

NOTE: option premium history only exists for the CURRENT expiry's strikes
(Upstox keeps no expired contracts), so meaningful signals are mostly the expiry week.
"""

import sys
import urllib.parse
from datetime import datetime, timedelta

import backtest_gamma as bt   # reuse get(), series_max(), SYMBOLS, IST, LOT

# ---- strategy params (mirror app.js CONFIG) ----
P = {
    "ema_fast": 9, "ema_mid": 21, "ema_slow": 50,
    "motherBarMinPct": 0.0008,
    "stopPct": 30,
    "trailProfitActivatePct": 30,
    "trailProfitGapPct": 20,
    "maxTradesPerDay": 6,
}


def ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out = [None] * len(values)
    s = sum(values[:period]) / period
    out[period - 1] = s
    for i in range(period, len(values)):
        s = (values[i] - s) * k + s
        out[i] = s
    return out


def run(symbol, expiry=None, days=10, verbose=True):
    symbol = symbol.upper()
    if symbol not in bt.SYMBOLS:
        return [], 1
    ikey = bt.SYMBOLS[symbol]
    today = datetime.now(bt.IST).strftime("%Y-%m-%d")

    if not expiry:
        contracts = bt.get(f"/v2/option/contract?instrument_key={urllib.parse.quote(ikey, safe='')}")
        exps = sorted({c.get("expiry") for c in contracts.get("data", []) if c.get("expiry") and c.get("expiry") >= today})
        expiry = exps[0] if exps else None
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    if verbose:
        print(f"\n=== STRATEGY Backtest: {symbol}  expiry {expiry}  (from {from_date}) ===")

    # index candles (1-min, multi-day)
    spot_map = bt.series_max(ikey, from_date, today)
    epochs = sorted(spot_map)
    closes = [spot_map[e] for e in epochs]
    if len(closes) < 60:
        if verbose: print("not enough index data")
        return [], bt.LOT.get(symbol, 1)
    e9 = ema(closes, P["ema_fast"]); e21 = ema(closes, P["ema_mid"]); e50 = ema(closes, P["ema_slow"])
    ohlc = fetch_ohlc(ikey, from_date, today)
    if verbose:
        print(f"index candles: {len(ohlc)}")

    # option chain -> strike list + key map
    chain = bt.get(f"/v2/option/chain?instrument_key={urllib.parse.quote(ikey, safe='')}&expiry_date={expiry}")
    rows = sorted(chain.get("data", []), key=lambda r: r.get("strike_price", 0))
    strikes = [r["strike_price"] for r in rows]
    keymap = {}
    for r in rows:
        if r.get("call_options"): keymap[(r["strike_price"], "CE")] = r["call_options"]["instrument_key"]
        if r.get("put_options"):  keymap[(r["strike_price"], "PE")] = r["put_options"]["instrument_key"]

    opt_cache = {}
    def opt_series(strike, side):
        k = (strike, side)
        if k in opt_cache:
            return opt_cache[k]
        key = keymap.get(k)
        m = bt.series_max(key, from_date, today) if key else {}
        opt_cache[k] = m
        return m

    def atm(spot):
        return min(strikes, key=lambda s: abs(s - spot)) if strikes else None

    # ---- detect signals + simulate ----
    lot = bt.LOT.get(symbol, 1)
    trades = []
    busy_until = 0
    per_day = {}
    # build OHLC list aligned to epochs order
    o_by_e = {c["time"]: c for c in ohlc}

    for i in range(4, len(epochs)):
        t = epochs[i]
        if t <= busy_until:
            continue
        if e9[i] is None or e21[i] is None or e50[i] is None:
            continue
        cur = o_by_e.get(epochs[i]); inside = o_by_e.get(epochs[i-2]); mother = o_by_e.get(epochs[i-3]); brk = o_by_e.get(epochs[i-1])
        if not (cur and inside and mother and brk):
            continue
        price = cur["close"]
        # inside bar + mother size
        is_inside = inside["high"] <= mother["high"] and inside["low"] >= mother["low"]
        if not is_inside:
            continue
        if (mother["high"] - mother["low"]) / price < P["motherBarMinPct"]:
            continue
        bullish = e9[i] > e21[i] > e50[i] and price > e21[i]
        bearish = e9[i] < e21[i] < e50[i] and price < e21[i]
        side = None
        if bullish and brk["close"] > inside["high"]:
            side = "CE"
        elif bearish and brk["close"] < inside["low"]:
            side = "PE"
        if not side:
            continue
        # daily cap
        day = datetime.fromtimestamp(t, bt.IST).strftime("%Y-%m-%d")
        if per_day.get(day, 0) >= P["maxTradesPerDay"]:
            continue

        strike = atm(price)
        oseries = opt_series(strike, side)
        entry = oseries.get(t)
        if entry is None or entry <= 0:
            continue

        # simulate exit on option premium
        fixed_sl = entry * (1 - P["stopPct"] / 100)
        act = entry * (1 + P["trailProfitActivatePct"] / 100)
        gap = P["trailProfitGapPct"] / 100
        peak = entry; sl = fixed_sl; trail = False
        exit_px = entry; exit_t = t; reason = "EOD"
        day_of_entry = day
        for e in [x for x in epochs if x > t]:
            p = oseries.get(e)
            if p is None:
                continue
            if datetime.fromtimestamp(e, bt.IST).strftime("%Y-%m-%d") != day_of_entry:
                reason = "EOD"; break   # square off at SAME-day close (intraday) — exit_px holds last same-day price
            if p > peak: peak = p
            if peak >= act:
                trail = True
                sl = max(sl, peak * (1 - gap))
            if p <= sl:
                exit_px = p; exit_t = e; reason = "TRAIL" if trail else "SL"; break
            exit_px = p; exit_t = e
        pts = exit_px - entry
        trades.append({"day": day, "time": datetime.fromtimestamp(t, bt.IST).strftime("%H:%M"),
                       "strike": strike, "side": side, "entry": entry, "exit": exit_px,
                       "reason": reason, "pts": pts, "pnl": pts * lot})
        per_day[day] = per_day.get(day, 0) + 1
        busy_until = exit_t

    return trades, lot


def report(symbol, trades, lot):
    print(f"\n=== {len(trades)} trades | {symbol} | lot {lot} ===")
    if not trades:
        print("(no signals in available option-history window)\n"); return
    print(f"{'Day':>10} {'Time':>5} {'Strike':>8} {'Sd':>3} {'Entry':>7} {'Exit':>7} {'Why':>6} {'P&L Rs':>9}")
    for t in trades:
        print(f"{t['day']:>10} {t['time']:>5} {t['strike']:>8.0f} {t['side']:>3} "
              f"{t['entry']:>7.2f} {t['exit']:>7.2f} {t['reason']:>6} {t['pnl']:>+9.0f}")
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gp = sum(t["pnl"] for t in wins); gl = sum(t["pnl"] for t in losses)
    print("-" * 64)
    print(f" Trades {len(trades)} | Wins {len(wins)} | Losses {len(losses)} | "
          f"Win-rate {len(wins)/len(trades)*100:.0f}%")
    print(f" Gross +{gp:,.0f} / {gl:,.0f}  |  NET {gp+gl:+,.0f} Rs")
    byday = {}
    for t in trades:
        byday[t["day"]] = byday.get(t["day"], 0) + t["pnl"]
    print(" Per-day:", ", ".join(f"{d}:{round(p):+}" for d, p in sorted(byday.items())))
    print()


def main():
    symbol = (sys.argv[1].upper() if len(sys.argv) > 1 else "NIFTY")
    expiry = sys.argv[2] if len(sys.argv) > 2 else None
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    trades, lot = run(symbol, expiry, days)
    report(symbol, trades, lot)


def fetch_ohlc(instrument_key, from_date, to_date):
    """Full OHLC 1-min candles (chunked) — dict-list with time/open/high/low/close."""
    k = urllib.parse.quote(instrument_key, safe="")
    out = {}
    d0 = datetime.strptime(from_date, "%Y-%m-%d").date()
    d1 = datetime.strptime(to_date, "%Y-%m-%d").date()
    a = d0
    while a <= d1:
        b = min(a + timedelta(days=13), d1)
        try:
            data = bt.get(f"/v3/historical-candle/{k}/minutes/1/{b}/{a}")
            for c in data.get("data", {}).get("candles", []):
                ts = int(datetime.fromisoformat(c[0]).timestamp())
                out[ts] = {"time": ts, "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
        except Exception:
            pass
        a = b + timedelta(days=1)
    # also today's intraday
    try:
        data = bt.get(f"/v3/historical-candle/intraday/{k}/minutes/1")
        for c in data.get("data", {}).get("candles", []):
            ts = int(datetime.fromisoformat(c[0]).timestamp())
            out[ts] = {"time": ts, "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
    except Exception:
        pass
    return sorted(out.values(), key=lambda x: x["time"])


if __name__ == "__main__":
    main()
