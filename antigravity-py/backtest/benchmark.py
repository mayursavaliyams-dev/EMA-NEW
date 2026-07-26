"""BUY & HOLD BENCHMARK — the check we never ran.

Rule (from the TradingView audit playbook): "If someone shows you only win rate,
they are hiding this." A strategy is only interesting if it beats simply holding
the index over the SAME window, on the SAME capital.

Compares: NIFTY buy & hold  vs  our condor+IVgate strategy (1-lot linear and compounded).

Usage:  python -m backtest.benchmark [NIFTY]
"""
import sys
from datetime import datetime

from backtest import bt_strangle as bt
from backtest.compound import compound
import sizing


def cagr(start, final, yrs):
    if start <= 0 or final <= 0 or yrs <= 0:
        return float("nan")
    return ((final / start) ** (1 / yrs) - 1) * 100


def run(symbol):
    contracts, und, days = bt.load(symbol)
    if not days:
        print("no data"); return

    # --- strategy trades over the validated config (condor + gate60 + leg1.0% + DTE4)
    bt.P.update({"condor": True, "ivGate": 60, "legDistancePct": 0.010,
                 "targetDTE": 4, "legStopMult": 2.0, "takeProfitPct": 0})
    for slip, tag in ((0.0, "0% slip"), (0.03, "3% slip")):
        bt.P["slipPct"] = slip
        trades, _ = bt.simulate(symbol, contracts, und, days)
        if not trades:
            print("no trades"); return
        d0, d1 = trades[0]["entry"], trades[-1]["entry"]
        yrs = max((datetime.strptime(d1, "%Y-%m-%d") - datetime.strptime(d0, "%Y-%m-%d")).days / 365.25, 1e-9)
        net1 = sum(t["net"] for t in trades)

        # buy & hold the INDEX over the exact same window
        udays = sorted(d for d in und if d0 <= d <= d1)
        s0, s1 = und[udays[0]], und[udays[-1]]
        bh_ret = (s1 / s0 - 1) * 100
        bh_cagr = cagr(s0, s1, yrs)

        # strategy on Rs 1,00,000 (a) fixed 1 lot  (b) fully compounded
        margin = sizing.MARGIN[symbol]["condor"]
        util = 0.6
        start = 100000
        lots0 = max(1, int((start * util) // margin))
        lin_final = start + net1 * lots0
        _, comp_final, _wd, mdd = compound(trades, start, margin, util)

        print("\n" + "=" * 70)
        print(f" BENCHMARK — {symbol} condor gate60 leg1.0% DTE4 · {tag}")
        print("=" * 70)
        print(f" Window : {d0} -> {d1}  ({yrs:.2f} yrs, {len(trades)} trades)")
        print(f" Index  : {s0:,.0f} -> {s1:,.0f}")
        print("-" * 70)
        print(f"{'':<28}{'Final on Rs 1L':>16}{'Total':>10}{'CAGR':>9}")
        print(f"{'BUY & HOLD ' + symbol:<28}{start*(s1/s0):>16,.0f}{bh_ret:>9.1f}%{bh_cagr:>8.1f}%")
        print(f"{'Strategy (1 lot, linear)':<28}{lin_final:>16,.0f}"
              f"{(lin_final/start-1)*100:>9.1f}%{cagr(start,lin_final,yrs):>8.1f}%")
        print(f"{'Strategy (compounded)':<28}{comp_final:>16,.0f}"
              f"{(comp_final/start-1)*100:>9.1f}%{cagr(start,comp_final,yrs):>8.1f}%")
        print("-" * 70)
        beat_lin = "YES" if cagr(start, lin_final, yrs) > bh_cagr else "NO"
        beat_cmp = "YES" if cagr(start, comp_final, yrs) > bh_cagr else "NO"
        print(f" Beats buy & hold?   linear: {beat_lin}   compounded: {beat_cmp}")
        print(f" Strategy max drawdown (compounded): {mdd:.0f}%")
        print(" NOTE: buy & hold needs NO margin, NO monitoring, NO tail risk of short options.")


if __name__ == "__main__":
    run((sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper())
