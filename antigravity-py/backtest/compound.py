"""Compounding analysis for the strangle/condor backtest.

Takes the per-trade NET (for 1 lot) from bt_strangle and simulates REINVESTING:
each trade we size lots = floor(capital * util / margin_per_lot), so as the
account grows the position (and therefore both profit AND drawdown) grows too.

Compares LINEAR (fixed 1 lot) vs COMPOUNDED for a few starting capitals.

Usage:  python -m backtest.compound NIFTY condor ivgate60 [slip3]
"""
import sys
from datetime import datetime

import config
import sizing
from backtest import bt_strangle as bt


def get_trades(symbol, condor, ivgate, slip):
    bt.P["condor"] = condor
    bt.P["ivGate"] = ivgate
    bt.P["slipPct"] = slip
    trades = bt.backtest(symbol) or []
    trades.sort(key=lambda t: t["entry"])
    return trades


def years_span(trades):
    a = datetime.strptime(trades[0]["entry"], "%Y-%m-%d")
    b = datetime.strptime(trades[-1]["entry"], "%Y-%m-%d")
    return max((b - a).days / 365.25, 1e-9)


def compound(trades, start, margin, util, rf=1.0):
    """Sizing scales with WORKING capital. `rf` = fraction of each PROFIT
    reinvested (1.0 = full compound; 0.0 = skim all profit, fixed size; losses
    always hit the trading account). Returns (curve, working_cap, withdrawn, max_dd_pct)."""
    cap = float(start)
    withdrawn = 0.0
    peak = cap
    maxdd = 0.0
    curve = []
    for t in trades:
        lots = int((cap * util) // margin)
        if lots < 1:
            curve.append({**t, "lots": 0, "pnl": 0, "cap": cap})
            continue
        pnl = t["net"] * lots
        if pnl > 0:
            cap += rf * pnl
            withdrawn += (1 - rf) * pnl
        else:
            cap += pnl                      # losses always eat the trading account
        peak = max(peak, cap)
        if peak > 0:
            maxdd = max(maxdd, (peak - cap) / peak)
        curve.append({**t, "lots": lots, "pnl": pnl, "cap": cap})
    return curve, cap, withdrawn, maxdd * 100


def cagr(start, final, yrs):
    if final <= 0 or start <= 0:
        return -100.0
    return ((final / start) ** (1 / yrs) - 1) * 100


def run(symbol, condor, ivgate, slip):
    trades = get_trades(symbol, condor, ivgate, slip)
    if not trades:
        print("no trades"); return
    margin = sizing.MARGIN[symbol]["condor" if condor else "naked"]
    yrs = years_span(trades)
    net1 = sum(t["net"] for t in trades)           # 1-lot total (linear)
    wins = sum(1 for t in trades if t["net"] > 0)
    util = 0.6                                      # use 60% of capital as margin, 40% buffer

    print("\n" + "=" * 74)
    print(f" COMPOUNDING — {symbol}  {'CONDOR' if condor else 'STRANGLE'}"
          f"{f' +IVgate{ivgate}' if ivgate else ''}{f' +{int(slip*100)}%slip' if slip else ''}")
    print("=" * 74)
    print(f" Period: {trades[0]['entry']} -> {trades[-1]['entry']}  ({yrs:.2f} yrs, "
          f"{len(trades)} trades, {wins/len(trades)*100:.0f}% win)")
    print(f" Margin/lot Rs {margin:,} | capital utilisation {util*100:.0f}% (40% kept as buffer)")
    print(f" 1-lot NET (no compounding): Rs {net1:+,}")
    print("-" * 74)
    print(f"{'Start Cap':>12}{'1-Lot Linear':>16}{'COMPOUNDED':>16}{'x':>7}{'CAGR':>8}{'MaxDD':>8}")
    for start in (100000, 300000, 500000, 1000000):
        _, final, _wd, mdd = compound(trades, start, margin, util)
        lin = start + net1 * max(1, int((start * util) // margin))  # linear at the lots that start funds
        x = final / start
        print(f"{start:>12,}{lin:>16,.0f}{final:>16,.0f}{x:>6.1f}x{cagr(start,final,yrs):>7.0f}%{mdd:>7.0f}%")

    # year-by-year for the headline case (start 1L)
    curve, final, _wd, mdd = compound(trades, 100000, margin, util)
    by_yr = {}
    for c in curve:
        y = c["entry"][:4]
        d = by_yr.setdefault(y, {"pnl": 0, "n": 0, "lots": [], "end": 0})
        d["pnl"] += c["pnl"]; d["n"] += 1; d["lots"].append(c["lots"]); d["end"] = c["cap"]
    print("-" * 74)
    print(f" Year-by-year (start Rs 1,00,000, compounded):")
    print(f"{'Year':>8}{'Trades':>8}{'Lots(rng)':>12}{'Year P&L':>14}{'Capital End':>16}")
    for y in sorted(by_yr):
        d = by_yr[y]
        lo, hi = min(d["lots"]), max(d["lots"])
        print(f"{y:>8}{d['n']:>8}{f'{lo}-{hi}':>12}{d['pnl']:>+14,.0f}{d['end']:>16,.0f}")
    print("-" * 74)
    print(f" Final capital Rs {final:,.0f}  ({final/100000:.1f}x in {yrs:.1f} yrs, "
          f"CAGR {cagr(100000,final,yrs):.0f}%, max drawdown {mdd:.0f}%)")

    # partial compounding: skim part of every profit, keep losses in the account
    print("-" * 74)
    print(f" Partial compounding (start Rs 1,00,000): how much PROFIT you reinvest")
    print(f"{'Reinvest':>10}{'WorkingCap':>13}{'Withdrawn':>12}{'TotalWealth':>14}"
          f"{'x':>7}{'CAGR':>8}{'MaxDD':>8}")
    for rf in (1.0, 0.75, 0.5, 0.25, 0.0):
        _, work, wd, mdd_rf = compound(trades, 100000, margin, util, rf)
        total = work + wd
        label = f"{int(rf*100)}%" + (" (full)" if rf == 1.0 else " (none)" if rf == 0.0 else "")
        print(f"{label:>10}{work:>13,.0f}{wd:>12,.0f}{total:>14,.0f}"
              f"{total/100000:>6.1f}x{cagr(100000,total,yrs):>7.0f}%{mdd_rf:>7.0f}%")
    print(" NOTE: compounding scales BOTH profit and drawdown. Daily-resolution paper backtest.")


if __name__ == "__main__":
    sym = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
    condor = "condor" in sys.argv
    slip = 0.03 if "slip3" in sys.argv else 0.0
    ivgate = 0
    for a in sys.argv:
        if a.startswith("ivgate"):
            ivgate = int(a.replace("ivgate", "") or 60)
    run(sym, condor, ivgate, slip)
