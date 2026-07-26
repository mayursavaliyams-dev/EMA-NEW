"""Backtest the SHORT STRANGLE on real NSE bhavcopy (daily resolution).
Per expiry: enter ~target-DTE day (sell CE & PE ~1.5% OTM at that day's close),
hold to expiry settlement; per-leg 2x stop checked vs daily HIGH; net charges.

Usage:  python -m backtest.bt_strangle NIFTY
"""
import sys
import os
import csv
import glob
from collections import defaultdict
from datetime import datetime

import config
import charges as ch
from greeks import bs_price

BHAV = os.path.join(config.DATA, "bhav")


def _iv(S, K, T, price, is_call):
    if price <= 0 or T <= 0 or S <= 0:
        return 0.0
    lo, hi = 0.001, 5.0
    for _ in range(50):
        m = (lo + hi) / 2
        if bs_price(S, K, T, 0.065, m, is_call) > price:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2 * 100

P = {
    "legDistancePct": 0.015,
    "legStopMult": 2.0,
    "targetDTE": 3,        # enter when days-to-expiry is closest to this
    "slipPct": 0.0,        # per-fill slippage stress (set 0.03 for 3%)
    "condor": False,       # True = iron condor (buy wings to cap tail)
    "wingPct": 0.03,       # wing distance from spot
    "ivGate": 0,           # only sell if ATM-IV percentile >= this (0 = no gate)
    "takeProfitPct": 0,    # exit early when net premium decays this % (0 = hold to expiry)
}


_LOADED = {}


def load(symbol):
    """contracts[(expiry,strike,opt)][date] = row ; und[date]=spot ; days sorted."""
    if symbol in _LOADED:
        return _LOADED[symbol]
    contracts = defaultdict(dict)
    und = {}
    days = set()
    for fp in sorted(glob.glob(os.path.join(BHAV, "*.csv"))):
        with open(fp, encoding="utf-8", errors="ignore") as f:
            for r in csv.DictReader(f):
                if r.get("FinInstrmTp") != "IDO" or r.get("TckrSymb") != symbol:
                    continue
                opt = r.get("OptnTp")
                if opt not in ("CE", "PE"):
                    continue
                try:
                    d = r["TradDt"][:10]; xp = r["XpryDt"][:10]
                    strike = float(r["StrkPric"])
                    row = {"o": float(r["OpnPric"] or 0), "h": float(r["HghPric"] or 0),
                           "l": float(r["LwPric"] or 0), "c": float(r["ClsPric"] or 0),
                           "settle": float(r["SttlmPric"] or 0), "und": float(r["UndrlygPric"] or 0)}
                except Exception:
                    continue
                contracts[(xp, strike, opt)][d] = row
                days.add(d)
                if row["und"]:
                    und[d] = row["und"]
    _LOADED[symbol] = (contracts, und, sorted(days))
    return _LOADED[symbol]


def nearest_strike(contracts, expiry, opt, target):
    strikes = [k[1] for k in contracts if k[0] == expiry and k[2] == opt]
    return min(strikes, key=lambda s: abs(s - target)) if strikes else None


def simulate(symbol, contracts, und, days):
    lot = config.SYMBOLS[symbol]["lot"]
    last_data = days[-1]
    # only expiries that actually COMPLETE within the fetched data window
    expiries = sorted({k[0] for k in contracts if k[0] <= last_data})
    trades = []

    def dte(xp, d):
        return (datetime.strptime(xp, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days

    iv_hist = []          # rolling ATM-IV history for percentile gate
    skipped = 0
    for xp in expiries:
        xdays = sorted({d for k in contracts if k[0] == xp for d in contracts[k] if d <= xp})
        # need a sane entry (DTE 1..10) and the actual expiry day present
        cand = [d for d in xdays if 1 <= dte(xp, d) <= 10]
        if not cand or xp not in xdays:
            continue
        entry_day = min(cand, key=lambda d: abs(dte(xp, d) - P["targetDTE"]))
        spot = und.get(entry_day)
        if not spot:
            continue

        # ---- IV-percentile gate (sell only when premium is genuinely rich) ----
        atm_k = nearest_strike(contracts, xp, "CE", spot)
        T = max(dte(xp, entry_day) / 365.0, 1e-6)
        atm_ce = contracts.get((xp, atm_k, "CE"), {}).get(entry_day, {})
        atm_pe = contracts.get((xp, atm_k, "PE"), {}).get(entry_day, {})
        iv_ce = _iv(spot, atm_k, T, atm_ce.get("c", 0), True)
        iv_pe = _iv(spot, atm_k, T, atm_pe.get("c", 0), False)
        atm_iv = (iv_ce + iv_pe) / 2 if (iv_ce and iv_pe) else (iv_ce or iv_pe)
        ivp = 50.0
        if len(iv_hist) >= 10 and atm_iv:
            ivp = sum(1 for x in iv_hist if x < atm_iv) / len(iv_hist) * 100
        if atm_iv:
            iv_hist.append(atm_iv)
        if P["ivGate"] and ivp < P["ivGate"]:
            skipped += 1
            continue

        ce_k = nearest_strike(contracts, xp, "CE", spot * (1 + P["legDistancePct"]))
        pe_k = nearest_strike(contracts, xp, "PE", spot * (1 - P["legDistancePct"]))
        if ce_k is None or pe_k is None:
            continue
        ce = contracts[(xp, ce_k, "CE")]; pe = contracts[(xp, pe_k, "PE")]
        if entry_day not in ce or entry_day not in pe:
            continue
        ce_in = ce[entry_day]["c"] * (1 - P["slipPct"])
        pe_in = pe[entry_day]["c"] * (1 - P["slipPct"])
        if ce_in <= 0 or pe_in <= 0:
            continue

        # optional iron-condor wings (BUY further OTM to cap the tail)
        cw_k = pw_k = None; cw_in = pw_in = 0.0; cw = pw = {}
        if P["condor"]:
            cw_k = nearest_strike(contracts, xp, "CE", spot * (1 + P["wingPct"]))
            pw_k = nearest_strike(contracts, xp, "PE", spot * (1 - P["wingPct"]))
            cw = contracts.get((xp, cw_k, "CE"), {}); pw = contracts.get((xp, pw_k, "PE"), {})
            cw_in = (cw.get(entry_day, {}).get("c") or 0) * (1 + P["slipPct"])
            pw_in = (pw.get(entry_day, {}).get("c") or 0) * (1 + P["slipPct"])

        hold = [d for d in xdays if d >= entry_day]
        entry_credit = (ce_in + pe_in) - (cw_in + pw_in)   # net credit per share
        tp = P["takeProfitPct"]
        reason, exit_day = "EXPIRY", None
        for d in hold[1:]:   # skip entry day
            cr, pr = ce.get(d), pe.get(d)
            # stop: a SHORT leg's premium hits the stop multiple (vs daily HIGH)
            if (cr and cr["h"] >= ce_in * P["legStopMult"]) or (pr and pr["h"] >= pe_in * P["legStopMult"]):
                reason, exit_day = "STOP", d
                break
            # take-profit: net premium has decayed enough (vs daily CLOSE)
            if tp and entry_credit > 0 and cr and pr:
                cur = cr["c"] + pr["c"]
                if P["condor"]:
                    cur -= (cw.get(d, {}).get("c") or 0) + (pw.get(d, {}).get("c") or 0)
                if (entry_credit - cur) >= tp / 100.0 * entry_credit:
                    reason, exit_day = "TP", d
                    break
        und_exp = und.get(xp) or spot

        def px(series, strike, opt):
            if reason in ("STOP", "TP"):
                return series.get(exit_day, {}).get("c") or 0.0
            return max(0.0, (und_exp - strike) if opt == "CE" else (strike - und_exp))

        ce_out = px(ce, ce_k, "CE") * (1 + P["slipPct"])
        pe_out = px(pe, pe_k, "PE") * (1 + P["slipPct"])
        gross = ((ce_in - ce_out) + (pe_in - pe_out)) * lot
        cost = (ch.round_trip_charges(ce_in, ce_out, lot, "SELL") +
                ch.round_trip_charges(pe_in, pe_out, lot, "SELL"))
        if P["condor"]:
            cw_out = px(cw, cw_k, "CE") * (1 - P["slipPct"])
            pw_out = px(pw, pw_k, "PE") * (1 - P["slipPct"])
            gross += ((cw_out - cw_in) + (pw_out - pw_in)) * lot   # long wings
            cost += (ch.round_trip_charges(cw_in, cw_out, lot, "BUY") +
                     ch.round_trip_charges(pw_in, pw_out, lot, "BUY"))
        net = gross - cost
        credit = ((ce_in + pe_in) - (cw_in + pw_in)) * lot
        trades.append({"expiry": xp, "entry": entry_day, "spot": spot, "ce": ce_k, "pe": pe_k,
                       "credit": round(credit), "ce_r": reason, "pe_r": reason, "net": round(net)})

    return trades, skipped


def backtest(symbol):
    contracts, und, days = load(symbol)
    if not days:
        print("No bhavcopy data — run: python -m backtest.bhav_fetch <from> <to>")
        return
    lot = config.SYMBOLS[symbol]["lot"]
    trades, skipped = simulate(symbol, contracts, und, days)
    # report
    strat = "IRON CONDOR" if P["condor"] else "SHORT STRANGLE"
    print(f"\n=== {strat} backtest: {symbol}  ({len(trades)} expiries, lot {lot}) ===")
    if not trades:
        print("no trades"); return
    print(f"{'Expiry':>11}{'Entry':>11}{'Spot':>9}{'CE':>8}{'PE':>8}{'Credit':>9}{'CEx':>8}{'PEx':>8}{'Net Rs':>9}")
    for t in trades:
        print(f"{t['expiry']:>11}{t['entry']:>11}{t['spot']:>9.0f}{t['ce']:>8.0f}{t['pe']:>8.0f}"
              f"{t['credit']:>9}{t['ce_r']:>8}{t['pe_r']:>8}{t['net']:>+9}")
    wins = [t for t in trades if t["net"] > 0]
    net = sum(t["net"] for t in trades)
    print("-" * 80)
    print(f" Trades {len(trades)} | Wins {len(wins)} | Win-rate {len(wins)/len(trades)*100:.0f}% "
          f"| NET {net:+,} Rs | slippage {P['slipPct']*100:.0f}% | IV-gate {P['ivGate']} (skipped {skipped})")
    return trades


if __name__ == "__main__":
    sym = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
    if "condor" in sys.argv:
        P["condor"] = True
    if "slip3" in sys.argv:
        P["slipPct"] = 0.03
    for a in sys.argv:
        if a.startswith("ivgate"):
            P["ivGate"] = int(a.replace("ivgate", "") or 60)
    backtest(sym)
