"""Parameter sweep to find the NEXT-BEST condor config (loads bhavcopy ONCE).
Varies IV-gate, leg distance (how OTM we sell) and entry DTE. Ranks by NET at
3% slip (realistic for a 4-leg condor). Charges always modelled.

Usage:  python -m backtest.sweep [NIFTY]
"""
import sys
from backtest import bt_strangle as bt


def metrics(trades):
    n = len(trades)
    if not n:
        return dict(n=0, win=0, net=0, worst=0, pf=0, ddrs=0)
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    cum = peak = dd = 0
    for x in nets:
        cum += x; peak = max(peak, cum); dd = max(dd, peak - cum)
    pf = (sum(wins) / abs(sum(losses))) if sum(losses) else 999.0
    return dict(n=n, win=len(wins) / n * 100, net=sum(nets),
                worst=min(nets), pf=pf, ddrs=dd)


def run(symbol):
    contracts, und, days = bt.load(symbol)
    if not days:
        print("no data"); return
    bt.P["condor"] = True
    rows = []
    for gate in (0, 50, 60, 70):
        for leg in (0.010, 0.015, 0.020):
            for dte in (2, 3, 4):
                bt.P["ivGate"] = gate; bt.P["legDistancePct"] = leg; bt.P["targetDTE"] = dte
                bt.P["slipPct"] = 0.0
                t0, _ = bt.simulate(symbol, contracts, und, days); m0 = metrics(t0)
                bt.P["slipPct"] = 0.03
                t3, _ = bt.simulate(symbol, contracts, und, days); m3 = metrics(t3)
                rows.append((gate, leg, dte, m0, m3))
    rows.sort(key=lambda r: r[4]["net"], reverse=True)

    print(f"\n=== NEXT-BEST sweep: {symbol} CONDOR  (ranked by NET @3% slip) ===")
    print(f"{'Gate':>5}{'Leg%':>6}{'DTE':>4}{'Trd':>5}{'Win%':>6}"
          f"{'NET@0%':>10}{'NET@3%':>10}{'Worst':>9}{'PF':>6}{'MaxDD':>9}")
    for gate, leg, dte, m0, m3 in rows[:15]:
        tag = "  <- current" if (gate == 60 and abs(leg - 0.015) < 1e-6 and dte == 3) else ""
        print(f"{gate:>5}{leg*100:>5.1f}{dte:>4}{m3['n']:>5}{m3['win']:>5.0f}%"
              f"{m0['net']:>+10,}{m3['net']:>+10,}{m3['worst']:>+9,}{m3['pf']:>6.2f}{m3['ddrs']:>9,}{tag}")
    print("\n(Win% & NET at 3% slip; PF=profit factor; MaxDD=peak-trough Rs on 1 lot; daily-res paper.)")


if __name__ == "__main__":
    run((sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper())
