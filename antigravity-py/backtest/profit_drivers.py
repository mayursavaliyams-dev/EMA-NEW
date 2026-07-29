"""WHAT ACTUALLY DRIVES PROFIT — one-factor-at-a-time sensitivity on the real
611-day bhavcopy backtest. Instead of guessing which knob matters, we move each
knob and measure how much NET moves. Ranked by impact.

Baseline = the deployed config (condor, gate 60, leg 1.0% OTM, DTE 4, stop 2.0, 3% slip).

Usage:  py -m backtest.profit_drivers [NIFTY]
"""
import sys
from backtest import bt_strangle as bt
from backtest.sweep import metrics
import charges as ch

BASE = {"condor": True, "ivGate": 60, "legDistancePct": 0.010, "targetDTE": 4,
        "legStopMult": 2.0, "takeProfitPct": 0, "slipPct": 0.03}


def run(symbol, **over):
    p = dict(BASE); p.update(over)
    bt.P.update(p)
    trades, skipped = bt.simulate(symbol, *DATA)
    m = metrics(trades)
    m["skipped"] = skipped
    return m


def line(label, m, base_net):
    d = m["net"] - base_net
    arrow = "^" if d > 0 else ("v" if d < 0 else "=")
    print(f"  {label:<34}{m['n']:>5}{m['win']:>6.0f}%{m['net']:>+11,}{m['pf']:>7.2f}"
          f"{m['ddrs']:>10,}   {arrow}{d:+,}")


if __name__ == "__main__":
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
    contracts, und, days = bt.load(symbol)
    DATA = (contracts, und, days)
    if not days:
        print("no data"); sys.exit()

    base = run(symbol)
    bn = base["net"]
    print("=" * 88)
    print(f" PROFIT DRIVERS — {symbol} | baseline = deployed config (condor gate60 leg1.0% DTE4 stop2.0 slip3%)")
    print("=" * 88)
    print(f"  {'lever':<34}{'trd':>5}{'win':>7}{'NET':>11}{'PF':>7}{'MaxDD':>10}   vs base")
    line("BASELINE", base, bn)

    print("\n --- 1. COSTS -------------------------------------------------------------")
    for s, lab in ((0.0, "slippage 0% (perfect fills)"), (0.01, "slippage 1%"),
                   (0.02, "slippage 2%"), (0.05, "slippage 5% (illiquid)")):
        line(lab, run(symbol, slipPct=s), bn)
    # charges off: monkey-patch to zero to measure their weight
    _rt = ch.round_trip_charges
    ch.round_trip_charges = lambda *a, **k: 0.0
    line("brokerage/taxes = 0 (hypothetical)", run(symbol), bn)
    ch.round_trip_charges = _rt

    print("\n --- 2. TRADE FREQUENCY (selectivity) -------------------------------------")
    for g, lab in ((0, "no IV gate (trade every week)"), (50, "IV gate 50"),
                   (70, "IV gate 70 (very selective)")):
        line(lab, run(symbol, ivGate=g), bn)

    print("\n --- 3. STRIKE DISTANCE (premium vs risk) ---------------------------------")
    for l in (0.005, 0.015, 0.020):
        line(f"legs {l*100:.1f}% OTM", run(symbol, legDistancePct=l), bn)

    print("\n --- 4. TAIL PROTECTION ---------------------------------------------------")
    line("NAKED (no wings)", run(symbol, condor=False), bn)
    for w in (0.02, 0.04):
        line(f"condor wings {w*100:.0f}% OTM", run(symbol, wingPct=w), bn)

    print("\n --- 5. EXIT RULES --------------------------------------------------------")
    for st in (1.5, 2.5, 3.0):
        line(f"stop {st}x", run(symbol, legStopMult=st), bn)
    for tp in (35, 50, 75):
        line(f"take-profit {tp}%", run(symbol, takeProfitPct=tp), bn)

    print("\n --- 6. TIMING (DTE) ------------------------------------------------------")
    for d in (2, 3, 6):
        line(f"entry DTE {d}", run(symbol, targetDTE=d), bn)

    print("\n" + "=" * 88)
    print(" Read it as: the biggest |vs base| numbers are the levers that decide profit.")
    print(" Daily-resolution paper backtest, charges modelled. n is small — treat as ranking, not truth.")
