#!/usr/bin/env python3
"""
Gamma Blast Backtester
======================
Replays the dashboard's gamma-blast logic over REAL 1-minute option + index
candles for a given expiry day, and reports every blast it would have fired.

Usage:
    python backtest_gamma.py                 # NIFTY, nearest expiry
    python backtest_gamma.py BANKNIFTY
    python backtest_gamma.py NIFTY 2026-06-23

Mirrors CONFIG.gammaBlast from app.js. Uses token.json (read-only ok).
"""

import sys
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

BASE = os.path.dirname(os.path.abspath(__file__))
UPSTOX = "https://api.upstox.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SYMBOLS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "SENSEX": "BSE_INDEX|SENSEX",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "RELIANCE": "NSE_EQ|INE002A01018",
    "TATASTEEL": "NSE_EQ|INE081A01020",
}

# ---- mirror of CONFIG.gammaBlast (app.js) ----
G = {
    "windowStartIST": "13:00",
    "windowEndIST": "15:25",
    "minPremium": 7,
    "maxPremium": 60,
    "atmStrikesRange": 3,
    "spotMovePct": 0.0015,
    "spotLookbackSec": 180,
    "premiumSurgePct": 50,
    "premiumLookbackSec": 120,
    "cooldownSec": 60,
    # ---- trade simulation: FIXED stop loss + TRAILING PROFIT (lock gains) ----
    "stopPct": 30,                  # fixed SL = 30% below entry premium (no trail down)
    "trailProfitActivatePct": 30,   # once premium +30% from entry, start locking profit
    "trailProfitGapPct": 20,        # trailing-profit stop sits 20% below the peak
}

# lot size per symbol (for rupee P&L)
LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "RELIANCE": 500, "TATASTEEL": 5500}


def token():
    with open(os.path.join(BASE, "token.json"), encoding="utf-8") as f:
        return json.load(f)["access_token"]


def get(path):
    req = urllib.request.Request(UPSTOX + path, headers={
        "Authorization": "Bearer " + token(), "Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _parse_candles(data, out):
    for c in data.get("data", {}).get("candles", []):
        try:
            ts = int(datetime.fromisoformat(c[0]).timestamp())
            out[ts] = float(c[4])     # close
        except Exception:
            pass
    return out


def candles_1m(instrument_key):
    """Today's intraday 1-min candles: dict epoch_sec -> close."""
    k = urllib.parse.quote(instrument_key, safe="")
    return _parse_candles(get(f"/v3/historical-candle/intraday/{k}/minutes/1"), {})


def candles_hist(instrument_key, from_date, to_date):
    """Historical 1-min candles in [from_date, to_date], chunked (1-min API caps
    the range at ~2 weeks), merged: dict epoch_sec -> close."""
    k = urllib.parse.quote(instrument_key, safe="")
    out = {}
    d0 = datetime.strptime(from_date, "%Y-%m-%d").date()
    d1 = datetime.strptime(to_date, "%Y-%m-%d").date()
    chunk = timedelta(days=13)
    a = d0
    while a <= d1:
        b = min(a + chunk, d1)
        try:
            _parse_candles(get(f"/v3/historical-candle/{k}/minutes/1/{b}/{a}"), out)
        except Exception:
            pass
        a = b + timedelta(days=1)
    return out


def series_max(instrument_key, from_date, to_date):
    """Merge all available history (hist range + today's intraday)."""
    out = candles_hist(instrument_key, from_date, to_date)
    try:
        out.update(candles_1m(instrument_key))   # today
    except Exception:
        pass
    return out


def hm_to_min(hm):
    p = hm.split(":")
    return int(p[0]) * 60 + int(p[1])


def ist_minute(epoch):
    d = datetime.fromtimestamp(epoch, IST)
    return d.hour * 60 + d.minute, d.strftime("%H:%M")


def pct_over(series, epochs, i, lookback_sec):
    """% change of series at index i vs lookback_sec earlier."""
    cur = series[i]
    if cur is None:
        return None
    cutoff = epochs[i] - lookback_sec
    base = None
    for j in range(i, -1, -1):
        if epochs[j] <= cutoff and series[j] is not None:
            base = series[j]
            break
    if base is None or base == 0:
        return None
    return (cur - base) / base * 100.0


def run_replay(symbol, epochs, spot, opts):
    """Replay gamma-blast logic + simulate trades. Returns list of trades."""
    def simulate_exit(series, i, entry):
        # FIXED stop loss + TRAILING PROFIT (lock gains once in profit).
        fixed_sl = entry * (1 - G["stopPct"] / 100.0)
        act = entry * (1 + G["trailProfitActivatePct"] / 100.0)
        gap = G["trailProfitGapPct"] / 100.0
        peak = entry
        sl = fixed_sl
        trail_active = False
        last_p, last_j = entry, i
        for j in range(i + 1, len(series)):
            p = series[j]
            if p is None:
                continue
            last_p, last_j = p, j
            if p > peak:
                peak = p
            if peak >= act:                        # profit-trail activated
                trail_active = True
                sl = max(sl, peak * (1 - gap))      # lock profit (ratchet up only)
            if p <= sl:
                return j, sl, ("PROFIT" if trail_active else "SL")
        return last_j, last_p, "EOD"

    lot = LOT.get(symbol, 1)
    trades = []
    position_until = -1
    last_blast = -10**9
    w0, w1 = hm_to_min(G["windowStartIST"]), hm_to_min(G["windowEndIST"])

    for i in range(len(epochs)):
        if epochs[i] <= position_until:
            continue
        mnt, hhmm = ist_minute(epochs[i])
        if mnt < w0 or mnt > w1:
            continue
        if epochs[i] - last_blast < G["cooldownSec"]:
            continue
        smove = pct_over(spot, epochs, i, G["spotLookbackSec"])
        if smove is None or abs(smove) / 100 < G["spotMovePct"]:
            continue
        side = "CE" if smove > 0 else "PE"
        for o in opts:
            if o["side"] != side:
                continue
            entry = o["prem"][i]
            if entry is None or entry < G["minPremium"] or entry > G["maxPremium"]:
                continue
            surge = pct_over(o["prem"], epochs, i, G["premiumLookbackSec"])
            if surge is None or surge < G["premiumSurgePct"]:
                continue
            xj, xprem, reason = simulate_exit(o["prem"], i, entry)
            pts = xprem - entry
            _, xtime = ist_minute(epochs[xj])
            dstr = datetime.fromtimestamp(epochs[i], IST).strftime("%m-%d")
            trades.append({"date": dstr, "in": hhmm, "out": xtime, "strike": o["strike"],
                           "side": side, "entry": entry, "exit": xprem, "reason": reason,
                           "pts": pts, "pnl": pts * lot, "surge": surge, "spot": smove})
            last_blast = epochs[i]
            position_until = epochs[xj]
            break
    return trades


def report(symbol, trades):
    lot = LOT.get(symbol, 1)
    print(f"\n=== {len(trades)} trade(s) | {symbol} | lot size {lot} ===")
    if not trades:
        print("(no trades — try lowering premiumSurgePct or spotMovePct)\n")
        return 0
    print(f"{'Date':>6} {'In':>5} {'Out':>5} {'Strike':>8} {'Sd':>3} {'Entry':>7} {'Exit':>7} "
          f"{'Reason':>7} {'Pts':>7} {'P&L Rs':>10}")
    for t in trades:
        print(f"{t['date']:>6} {t['in']:>5} {t['out']:>5} {t['strike']:>8.0f} {t['side']:>3} "
              f"{t['entry']:>7.2f} {t['exit']:>7.2f} {t['reason']:>7} "
              f"{t['pts']:>+7.2f} {t['pnl']:>+10.0f}")
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gp = sum(t["pnl"] for t in wins); gl = sum(t["pnl"] for t in losses)
    net = gp + gl
    print("-" * 72)
    print(f" Trades: {len(trades)}  Wins: {len(wins)}  Losses: {len(losses)}  "
          f"Win-rate: {len(wins)/len(trades)*100:.0f}%")
    print(f" Gross profit: +{gp:,.0f}   Gross loss: {gl:,.0f}")
    print(f" NET P&L: {net:+,.0f} Rs   (fixed SL -{G['stopPct']}%, trail profit after +{G['trailProfitActivatePct']}%)\n")
    return net


def align(epochs, m):
    """Align a {epoch:val} map to the epochs list (carry-forward last known)."""
    arr, last = [], None
    for e in epochs:
        if e in m:
            last = m[e]
        arr.append(last)
    return arr


def archive_mode(symbol_filter):
    import glob
    adir = os.path.join(BASE, "data", "archive")
    files = sorted(glob.glob(os.path.join(adir, f"{symbol_filter}_*.json"))) if symbol_filter \
        else sorted(glob.glob(os.path.join(adir, "*.json")))
    if not files:
        print(f"\nNo archives in {adir}. Run archive_expiry.py on expiry days first.\n")
        return
    print(f"\n=== MULTI-EXPIRY BACKTEST from {len(files)} archived expiry day(s) ===")
    all_trades = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            a = json.load(f)
        sym = a["symbol"]
        idx = {int(e): float(c) for e, c in a["index"]}
        epochs = sorted(idx)
        spot = [idx[e] for e in epochs]
        opts = []
        for o in a["options"]:
            m = {int(e): float(c) for e, c in o["candles"]}
            opts.append({"strike": o["strike"], "side": o["side"], "prem": align(epochs, m)})
        trades = run_replay(sym, epochs, spot, opts)
        print(f"\n--- {sym} {a['expiry']} ({a['date']}): {len(trades)} trade(s) ---")
        report(sym, trades)
        all_trades += [dict(t, sym=sym) for t in trades]

    # aggregate
    if all_trades:
        gp = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
        gl = sum(t["pnl"] for t in all_trades if t["pnl"] < 0)
        wins = sum(1 for t in all_trades if t["pnl"] > 0)
        print("=" * 72)
        print(f" ALL EXPIRIES: {len(all_trades)} trades  Wins {wins}  "
              f"Win-rate {wins/len(all_trades)*100:.0f}%  NET {gp+gl:+,.0f} Rs")
        print("=" * 72 + "\n")


def main():
    if "--archive" in sys.argv:
        pos = [a for a in sys.argv[1:] if not a.startswith("--")]
        return archive_mode(pos[0].upper() if pos else None)

    symbol = (sys.argv[1].upper() if len(sys.argv) > 1 else "NIFTY")
    if symbol not in SYMBOLS:
        print("unknown symbol", symbol); return
    ikey = SYMBOLS[symbol]

    # expiry
    contracts = get(f"/v2/option/contract?instrument_key={urllib.parse.quote(ikey, safe='')}")
    today = datetime.now().strftime("%Y-%m-%d")
    exps = sorted({c.get("expiry") for c in contracts.get("data", []) if c.get("expiry") and c.get("expiry") >= today})
    expiry = (sys.argv[2] if len(sys.argv) > 2 else (exps[0] if exps else None))
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    print(f"\n=== Gamma Blast Backtest: {symbol}  expiry {expiry}  (history from {from_date}) ===")

    # index candles — full available history
    spot_map = series_max(ikey, from_date, today)
    if not spot_map:
        print("no index candles"); return
    epochs = sorted(spot_map)
    spot = [spot_map[e] for e in epochs]
    day_lo, day_hi = min(spot), max(spot)
    ndays = len({datetime.fromtimestamp(e, IST).date() for e in epochs})
    print(f"index candles: {len(epochs)}  over {ndays} day(s)  range {day_lo:.1f} - {day_hi:.1f}")

    # option chain -> strikes within the FULL range (+/- a little)
    chain = get(f"/v2/option/chain?instrument_key={urllib.parse.quote(ikey, safe='')}&expiry_date={expiry}")
    rows = sorted(chain.get("data", []), key=lambda r: r.get("strike_price", 0))
    band = [r for r in rows if day_lo - 100 <= r.get("strike_price", 0) <= day_hi + 100]
    print(f"strikes in band: {len(band)} (fetching CE+PE history, please wait...)")

    # fetch each option's premium series (full history), aligned to index epochs
    opts = []   # list of dicts: {strike, side, key, prem[]}
    for r in band:
        strike = r.get("strike_price")
        for side, leg in (("CE", r.get("call_options")), ("PE", r.get("put_options"))):
            key = (leg or {}).get("instrument_key")
            if not key:
                continue
            try:
                m = series_max(key, from_date, today)
            except Exception:
                m = {}
            if not m:
                continue
            # align (carry-forward last known)
            arr, last = [], None
            for e in epochs:
                if e in m:
                    last = m[e]
                arr.append(last)
            opts.append({"strike": strike, "side": side, "key": key, "prem": arr})

    print(f"option series loaded: {len(opts)}")

    trades = run_replay(symbol, epochs, spot, opts)
    report(symbol, trades)


if __name__ == "__main__":
    main()
