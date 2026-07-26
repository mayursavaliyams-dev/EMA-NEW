"""Seed the live engine's IV-percentile history from real bhavcopy, so the validated
gate is valid on day 1 of the forward-test instead of waiting ~20 trading days.

This is legitimate: the gate compares today's ATM-IV to a HISTORICAL distribution;
bhavcopy gives us that distribution directly (one ATM-IV per expiry, ~250 samples).
Paper only — writes only the iv_hist array in strangle_state.json.

Run:  py seed_iv_history.py
"""
import os
import json
from datetime import datetime

import config
from backtest import bt_strangle as bt

STATE = os.path.join(config.DATA, "strangle_state.json")
SYMBOL = "NIFTY"


def _dte(xp, d):
    return (datetime.strptime(xp, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days


def atm_iv_series(symbol):
    contracts, und, days = bt.load(symbol)
    ivs = []
    for xp in sorted({k[0] for k in contracts}):
        xdays = sorted({d for k in contracts if k[0] == xp for d in contracts[k] if d <= xp})
        cand = [d for d in xdays if 1 <= _dte(xp, d) <= 10]
        if not cand:
            continue
        entry = min(cand, key=lambda d: abs(_dte(xp, d) - 3))
        spot = und.get(entry)
        if not spot:
            continue
        k = bt.nearest_strike(contracts, xp, "CE", spot)
        T = max(_dte(xp, entry) / 365.0, 1e-6)
        ce = contracts.get((xp, k, "CE"), {}).get(entry, {})
        pe = contracts.get((xp, k, "PE"), {}).get(entry, {})
        iv_ce = bt._iv(spot, k, T, ce.get("c", 0), True)
        iv_pe = bt._iv(spot, k, T, pe.get("c", 0), False)
        iv = (iv_ce + iv_pe) / 2 if (iv_ce and iv_pe) else (iv_ce or iv_pe)
        if iv:
            ivs.append(round(iv, 2))
    return ivs[-250:]


def main():
    ivs = atm_iv_series(SYMBOL)
    if len(ivs) < 20:
        print(f"only {len(ivs)} IV samples — need bhavcopy in data/bhav/. aborting.")
        return
    s = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    s.setdefault("iv_hist", {})[SYMBOL] = ivs
    s.setdefault("iv_day", {})[SYMBOL] = "seed"     # past marker -> today's live sample still added
    for k, v in {"positions": [], "closed": [], "realized_pnl": 0.0, "equity": 0.0,
                 "equity_peak": 0.0, "day": None, "seq": s.get("seq", 0),
                 "halted": False, "halt_reason": ""}.items():
        s.setdefault(k, v)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    lo, hi = min(ivs), max(ivs)
    print(f"seeded {len(ivs)} ATM-IV samples for {SYMBOL}: min {lo}  max {hi}  median {sorted(ivs)[len(ivs)//2]}")
    print("gate is now valid on day 1. engine will paper-sell when IV percentile >= ivLowPct during market hours.")


if __name__ == "__main__":
    main()
