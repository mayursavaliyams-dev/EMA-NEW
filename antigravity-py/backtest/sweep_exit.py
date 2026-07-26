"""Exit-logic sweep: at the best ENTRY (gate60 · leg1.0% · DTE4 · condor),
vary take-profit % and stop multiple to find the best EXIT combo.
Ranked by NET @3% slip. Charges modelled. Daily-resolution paper backtest.

Usage:  python -m backtest.sweep_exit [NIFTY]
"""
import sys
from backtest import bt_strangle as bt
from backtest.sweep import metrics


def run(symbol):
    contracts, und, days = bt.load(symbol)
    if not days:
        print("no data"); return
    # fixed best entry
    bt.P["condor"] = True
    bt.P["ivGate"] = 60
    bt.P["legDistancePct"] = 0.010
    bt.P["targetDTE"] = 4

    rows = []
    for tp in (0, 25, 40, 50, 65, 75):
        for stop in (1.5, 2.0, 2.5, 3.0):
            bt.P["takeProfitPct"] = tp
            bt.P["legStopMult"] = stop
            bt.P["slipPct"] = 0.0
            t0, _ = bt.simulate(symbol, contracts, und, days); m0 = metrics(t0)
            bt.P["slipPct"] = 0.03
            t3, _ = bt.simulate(symbol, contracts, und, days); m3 = metrics(t3)
            rows.append((tp, stop, m0, m3))
    rows.sort(key=lambda r: r[3]["net"], reverse=True)

    print(f"\n=== EXIT sweep: {symbol} condor gate60 leg1.0% DTE4  (ranked NET@3%) ===")
    print(f"{'TP%':>5}{'Stop':>6}{'Trd':>5}{'Win%':>6}{'NET@0%':>10}{'NET@3%':>10}"
          f"{'Worst':>9}{'PF':>6}{'MaxDD':>9}")
    for tp, stop, m0, m3 in rows:
        tag = "  <- hold-to-expiry" if (tp == 0 and stop == 2.0) else ""
        tps = "OFF" if tp == 0 else f"{tp}"
        print(f"{tps:>5}{stop:>6.1f}{m3['n']:>5}{m3['win']:>5.0f}%"
              f"{m0['net']:>+10,}{m3['net']:>+10,}{m3['worst']:>+9,}{m3['pf']:>6.2f}{m3['ddrs']:>9,}{tag}")
    print("\n(TP=take-profit on net-premium decay; Stop=short-leg premium x; daily-res paper.)")


if __name__ == "__main__":
    run((sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper())
