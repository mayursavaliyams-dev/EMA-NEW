"""INTRADAY-RESOLUTION BACKTEST — the fix for the root cause of every false positive.

Every backtest in both repos runs at DAILY resolution: one price per day. For a
short-vol seller that is structurally blind, because the thing that kills you is
the intraday PATH — the gap, the vol spike, and the minute your stop actually
fires. A daily model checks the stop against the day's HIGH but fills at the
day's CLOSE, which is not a trade anyone can make.

This walks the real 1-minute index path and reprices the position every minute:

  * entry   real expiry dates + real ATM IV from NSE bhavcopy (EMA NEW/data/bhav)
  * path    real 1-minute index bars (Antigravity Pro/bt-data/*-1min.json, read-only)
  * pricing Black-Scholes each minute, with the LEVERAGE EFFECT: implied vol rises
            as spot falls, which is exactly the double-hit a seller takes and
            exactly what a daily close-to-close model cannot see
  * stop    fires at the MINUTE a short leg's premium reaches the multiple, and
            fills there — not at some later close

Then it runs the same trades at daily resolution and reports the gap between them.

Usage:  py -m backtest.intraday_engine [NIFTY]
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import config
import charges as ch
from greeks import bs_price
from backtest import bt_strangle as bt

PRO_1MIN = r"C:\Users\Admin\Downloads\Expiry-Friday-5x\bt-data"

P = {
    "targetDTE": 4,
    "legDistancePct": 0.010,
    "wingPct": 0.03,
    "legStopMult": 2.0,
    "takeProfitPct": 0,        # 0 = hold to expiry/EOD
    "slipPct": 0.03,
    "entryMinute": "09:20",
    "squareOff": "15:15",
    "leverage": 3.0,           # IV multiplier per 1.0 of negative return (vol spikes on falls)
    "r": 0.065,
}


def load_1min(symbol):
    """{date: [(hhmm, open, high, low, close)]} from the 1-minute archive."""
    fp = os.path.join(PRO_1MIN, f"{symbol.lower()}-1min.json")
    if not os.path.exists(fp):
        return {}
    with open(fp, encoding="utf-8") as f:
        rows = json.load(f)
    days = defaultdict(list)
    for r in rows:
        ts = str(r[0])
        days[ts[:10]].append((ts[11:16], float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    for d in days:
        days[d].sort(key=lambda x: x[0])
    return dict(days)


def _iv_at(iv0, spot0, spot):
    """Leverage effect: vol expands as the market falls. A daily close model that
    reprices with the entry-day IV understates every stressed minute."""
    ret = (spot - spot0) / spot0
    return iv0 * (1 + P["leverage"] * max(0.0, -ret))


def _px(spot, strike, T, iv, is_call):
    return bs_price(spot, strike, max(T, 1e-6), P["r"], max(iv, 0.005), is_call)


def run(symbol="NIFTY"):
    mins = load_1min(symbol)
    if not mins:
        print(f"no 1-minute data for {symbol} at {PRO_1MIN}"); return
    contracts, und, days = bt.load(symbol)
    if not days:
        print("no bhavcopy — run bhav_fetch first"); return

    meta = config.SYMBOLS[symbol]
    lot, step = meta["lot"], meta["step"]
    have = sorted(set(mins) & set(days))
    if not have:
        print("no overlap between 1-min data and bhavcopy"); return

    def dte(xp, d):
        return (datetime.strptime(xp, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days

    expiries = sorted({k[0] for k in contracts if k[0] <= days[-1]})
    intr, daily = [], []

    for xp in expiries:
        # entry day: closest to target DTE, and it must have 1-minute data
        cand = [d for d in have if 1 <= dte(xp, d) <= 10]
        if not cand:
            continue
        entry_day = min(cand, key=lambda d: abs(dte(xp, d) - P["targetDTE"]))
        bars0 = mins.get(entry_day) or []
        start = next((b for b in bars0 if b[0] >= P["entryMinute"]), None)
        if not start:
            continue
        spot0 = start[4]

        # calibrate IV from the real option chain on the entry day
        k_atm = bt.nearest_strike(contracts, xp, "CE", spot0)
        if k_atm is None:
            continue
        T0 = max(dte(xp, entry_day) / 365.0, 1e-6)
        row_ce = contracts.get((xp, k_atm, "CE"), {}).get(entry_day, {})
        row_pe = contracts.get((xp, k_atm, "PE"), {}).get(entry_day, {})
        iv_c = bt._iv(spot0, k_atm, T0, row_ce.get("c", 0), True) / 100
        iv_p = bt._iv(spot0, k_atm, T0, row_pe.get("c", 0), False) / 100
        iv0 = (iv_c + iv_p) / 2 if (iv_c and iv_p) else (iv_c or iv_p)
        if not iv0 or iv0 <= 0:
            continue

        rnd = lambda x: round(x / step) * step
        ce, pe = rnd(spot0 * (1 + P["legDistancePct"])), rnd(spot0 * (1 - P["legDistancePct"]))
        cw, pw = rnd(spot0 * (1 + P["wingPct"])), rnd(spot0 * (1 - P["wingPct"]))

        e_ce = _px(spot0, ce, T0, iv0, True) * (1 - P["slipPct"])
        e_pe = _px(spot0, pe, T0, iv0, False) * (1 - P["slipPct"])
        e_cw = _px(spot0, cw, T0, iv0, True) * (1 + P["slipPct"])
        e_pw = _px(spot0, pw, T0, iv0, False) * (1 + P["slipPct"])
        credit_ps = (e_ce + e_pe) - (e_cw + e_pw)
        if credit_ps <= 0 or e_ce <= 0 or e_pe <= 0:
            continue

        hold_days = [d for d in have if entry_day <= d <= xp]
        exit_ps, reason, when = None, None, None

        # ---------- INTRADAY WALK ----------
        for d in hold_days:
            for hhmm, o, hi, lo, c in mins[d]:
                if d == entry_day and hhmm < P["entryMinute"]:
                    continue
                elapsed = (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(entry_day, "%Y-%m-%d")).days
                frac = (int(hhmm[:2]) * 60 + int(hhmm[3:]) - 555) / 375.0
                T = max(T0 - (elapsed + max(frac, 0)) / 365.0, 1e-6)
                # adverse extreme inside the bar: call side hurt by the high, put side by the low
                for probe in (hi, lo):
                    iv = _iv_at(iv0, spot0, probe)
                    p_ce = _px(probe, ce, T, iv, True)
                    p_pe = _px(probe, pe, T, iv, False)
                    if p_ce >= e_ce * P["legStopMult"] or p_pe >= e_pe * P["legStopMult"]:
                        w_ce = _px(probe, cw, T, iv, True)
                        w_pe = _px(probe, pw, T, iv, False)
                        exit_ps = ((p_ce + p_pe) - (w_ce + w_pe)) * (1 + P["slipPct"])
                        reason, when = "STOP", f"{d} {hhmm}"
                        break
                if exit_ps is not None:
                    break
                if P["takeProfitPct"]:
                    iv = _iv_at(iv0, spot0, c)
                    cur = ((_px(c, ce, T, iv, True) + _px(c, pe, T, iv, False))
                           - (_px(c, cw, T, iv, True) + _px(c, pw, T, iv, False)))
                    if (credit_ps - cur) >= P["takeProfitPct"] / 100 * credit_ps:
                        exit_ps, reason, when = cur * (1 + P["slipPct"]), "TP", f"{d} {hhmm}"
                        break
            if exit_ps is not None:
                break

        und_exp = und.get(xp) or (mins[hold_days[-1]][-1][4] if hold_days else spot0)
        if exit_ps is None:                                    # settle at intrinsic
            exit_ps = (max(0, und_exp - ce) + max(0, pe - und_exp)
                       - max(0, und_exp - cw) - max(0, pw - und_exp))
            reason, when = "EXPIRY", xp

        gross = (credit_ps - exit_ps) * lot
        cost = (ch.round_trip_charges(e_ce, 0, lot, "SELL") + ch.round_trip_charges(e_pe, 0, lot, "SELL")
                + ch.round_trip_charges(e_cw, 0, lot, "BUY") + ch.round_trip_charges(e_pw, 0, lot, "BUY"))
        intr.append({"xp": xp, "entry": entry_day, "reason": reason, "when": when,
                     "net": round(gross - cost)})

        # ---------- SAME TRADE, DAILY RESOLUTION (what the old backtests did) ----------
        d_exit, d_reason = None, "EXPIRY"
        for d in hold_days[1:]:
            bars = mins[d]
            hi = max(b[2] for b in bars); lo = min(b[3] for b in bars); cl = bars[-1][4]
            elapsed = (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(entry_day, "%Y-%m-%d")).days
            T = max(T0 - elapsed / 365.0, 1e-6)
            # daily model: trigger off the day's extremes, but FILL at the close (the flaw)
            trig = (_px(hi, ce, T, iv0, True) >= e_ce * P["legStopMult"]
                    or _px(lo, pe, T, iv0, False) >= e_pe * P["legStopMult"])
            if trig:
                d_exit = ((_px(cl, ce, T, iv0, True) + _px(cl, pe, T, iv0, False))
                          - (_px(cl, cw, T, iv0, True) + _px(cl, pw, T, iv0, False))) * (1 + P["slipPct"])
                d_reason = "STOP"
                break
        if d_exit is None:
            d_exit = (max(0, und_exp - ce) + max(0, pe - und_exp)
                      - max(0, und_exp - cw) - max(0, pw - und_exp))
        daily.append({"xp": xp, "reason": d_reason, "net": round((credit_ps - d_exit) * lot - cost)})

    if not intr:
        print("no trades"); return

    def summarize(rows, label):
        n = len(rows)
        net = sum(r["net"] for r in rows)
        wins = sum(1 for r in rows if r["net"] > 0)
        w = [r["net"] for r in rows if r["net"] > 0]
        l = [r["net"] for r in rows if r["net"] <= 0]
        pf = (sum(w) / abs(sum(l))) if l else 999
        cum = peak = dd = 0
        for r in rows:
            cum += r["net"]; peak = max(peak, cum); dd = max(dd, peak - cum)
        stops = sum(1 for r in rows if r["reason"] == "STOP")
        print(f"  {label:<26}{n:>5}{wins/n*100:>6.0f}%{net:>+12,}{pf:>7.2f}{dd:>11,}"
              f"{min(r['net'] for r in rows):>+11,}{stops:>7}")
        return net

    print("=" * 92)
    print(f" INTRADAY vs DAILY RESOLUTION — {symbol} condor  legs {P['legDistancePct']*100:.1f}%  "
          f"wings {P['wingPct']*100:.0f}%  stop {P['legStopMult']}x  slip {P['slipPct']*100:.0f}%")
    print(f" 1-min path: {len(have)} days ({have[0]} -> {have[-1]})  |  IV from real bhavcopy, "
          f"leverage {P['leverage']}x on falls")
    print("=" * 92)
    print(f"  {'model':<26}{'trd':>5}{'win':>7}{'NET':>12}{'PF':>7}{'MaxDD':>11}{'worst':>11}{'stops':>7}")
    n_i = summarize(intr, "INTRADAY (1-min path)")
    n_d = summarize(daily, "DAILY (old method)")
    print("-" * 92)
    gap = n_d - n_i
    print(f"  Daily overstates by Rs {gap:+,}  ({gap/abs(n_i)*100:+.0f}% of the intraday result)"
          if n_i else f"  gap Rs {gap:+,}")
    stops_i = sum(1 for r in intr if r["reason"] == "STOP")
    stops_d = sum(1 for r in daily if r["reason"] == "STOP")
    print(f"  Stops seen: intraday {stops_i}  vs  daily {stops_d}"
          f"  -> the daily model misses {stops_i - stops_d} stop event(s)")
    worst = sorted(intr, key=lambda r: r["net"])[:5]
    print("\n  Worst 5 intraday trades (the tail a daily model cannot show):")
    for r in worst:
        print(f"    {r['entry']} -> {r['reason']:<7} {str(r['when'])[:16]:<17} Rs {r['net']:+,}")
    print("\n  CAVEAT: premiums are Black-Scholes, not real quotes (only 9 days of 1-min option")
    print("  data exist). The PATH and the stop timing are real; the premium level is modelled.")


def sensitivity(symbol="NIFTY"):
    """Is the intraday verdict driven by the leverage (vol-spike) assumption?
    leverage=0 means IV never moves — the most conservative possible case."""
    import io, contextlib
    print("  Leverage sensitivity — does the conclusion depend on the vol-spike assumption?")
    print(f"  {'assumption':<28}{'trd':>5}{'win':>7}{'NET':>12}{'PF':>7}{'MaxDD':>11}{'worst':>11}{'stops':>7}")
    keep, last = P["leverage"], ""
    for lev, lab in ((0.0, "lev 0.0  IV never moves"), (1.5, "lev 1.5  mild spike"),
                     (3.0, "lev 3.0  baseline")):
        P["leverage"] = lev
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run(symbol)
        last = buf.getvalue()
        line = next((l for l in last.splitlines() if "INTRADAY (1-min" in l), "")
        print(f"  {lab:<28}" + line.strip()[26:])
    P["leverage"] = keep
    d = next((l for l in last.splitlines() if "DAILY (old" in l), "")
    print(f"  {'DAILY (old method)':<28}" + d.strip()[26:])
    print("\n  If every row above is negative, the daily model's profit was an artefact of")
    print("  resolution, not of the vol assumption.")


if __name__ == "__main__":
    sym = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
    if "sens" in sys.argv:
        sensitivity(sym)
    else:
        run(sym)
