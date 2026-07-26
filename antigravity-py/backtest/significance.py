"""IS THE EDGE STATISTICALLY REAL, OR LUCK? — bootstrap CI + permutation test.

Walk-forward showed OOS NET +45,151 over 48 trades. But 48 is a small sample with a
fat-tailed P&L distribution: a couple of good trades could explain the whole result.
Reporting a point estimate without an interval is exactly what the audits flagged.

Three tests, all on the OUT-OF-SAMPLE trades only:

  1. BOOTSTRAP CI      resample the trade sequence 10,000x with replacement.
                       If the 95% CI for mean P&L includes 0, we cannot claim an edge.

  2. PERMUTATION TEST  the null is "sign of each trade's P&L is random". Flip signs at
                       random 10,000x; p = fraction of null runs beating the real NET.

  3. TAIL DEPENDENCE   drop the single best trade, then the best 3. If the edge dies,
                       it was a lottery ticket, not a process.

Also reports a Sharpe-like per-trade ratio and the Probabilistic Sharpe Ratio idea:
how much of the result survives once we admit the sample is short.

Usage:  py -m backtest.walkforward NIFTY   (to see the trades)
        py -m backtest.significance NIFTY
"""
import sys
import random
import statistics as st

from backtest import bt_strangle as bt
from backtest import walkforward as wf

N_BOOT = 10000
SEED = 12345          # fixed so the result is reproducible by anyone


def oos_trades(symbol):
    """Re-run the walk-forward selection and return ONLY the out-of-sample trades."""
    contracts, und, days = bt.load(symbol)
    if not days:
        return []
    bt.P["condor"] = True
    bt.P["slipPct"] = wf.SLIP
    bt.P["legStopMult"] = 2.0
    bt.P["takeProfitPct"] = 0
    book = {}
    for (g, l, d) in wf.GRID:
        bt.P["ivGate"] = g
        bt.P["legDistancePct"] = l
        bt.P["targetDTE"] = d
        book[(g, l, d)], _ = bt.simulate(symbol, contracts, und, days)

    entries = sorted({t["entry"] for ts in book.values() for t in ts})
    if not entries:
        return []
    start, last = wf._d(entries[0]), wf._d(entries[-1])
    out = []
    t = wf._months_after(start, wf.TRAIN_MONTHS)
    while t < last:
        t_end = wf._months_after(t, wf.TEST_MONTHS)
        tr_lo, tr_hi = start.strftime("%Y-%m-%d"), t.strftime("%Y-%m-%d")
        te_hi = t_end.strftime("%Y-%m-%d")
        best, best_net = None, None
        for cfg, trades in book.items():
            tr = [x for x in trades if tr_lo <= x["entry"] < tr_hi]
            if len(tr) < wf.MIN_TRAIN_TRADES:
                continue
            net = sum(x["net"] for x in tr)
            if best_net is None or net > best_net:
                best, best_net = cfg, net
        if best is None:
            best = wf.DEFAULT_CFG
        out.extend([x for x in book[best] if tr_hi <= x["entry"] < te_hi])
        t = t_end
    return out


def report(symbol):
    rng = random.Random(SEED)
    trades = oos_trades(symbol)
    if len(trades) < 5:
        print("not enough OOS trades"); return
    pnl = [t["net"] for t in trades]
    n = len(pnl)
    total = sum(pnl)
    mean = total / n
    sd = st.stdev(pnl)
    wins = [x for x in pnl if x > 0]

    print("\n" + "=" * 74)
    print(f" STATISTICAL SIGNIFICANCE — {symbol} out-of-sample (walk-forward trades)")
    print("=" * 74)
    print(f" n = {n} trades | NET {total:+,} | mean/trade {mean:+,.0f} | sd {sd:,.0f} "
          f"| win {len(wins)/n*100:.0f}%")
    print(f" per-trade Sharpe-like ratio (mean/sd) = {mean/sd:.3f}")

    # ---- 1. bootstrap CI on mean P&L -------------------------------------
    means = []
    for _ in range(N_BOOT):
        s = [pnl[rng.randrange(n)] for _ in range(n)]
        means.append(sum(s) / n)
    means.sort()
    lo, hi = means[int(.025 * N_BOOT)], means[int(.975 * N_BOOT)]
    p_neg = sum(1 for m in means if m <= 0) / N_BOOT
    print("-" * 74)
    print(f" 1. BOOTSTRAP ({N_BOOT:,} resamples)")
    print(f"    95% CI for mean P&L/trade : [{lo:+,.0f} .. {hi:+,.0f}]")
    print(f"    95% CI for total NET      : [{lo*n:+,.0f} .. {hi*n:+,.0f}]")
    print(f"    P(mean <= 0)              : {p_neg*100:.1f}%")
    print(f"    VERDICT: {'CI EXCLUDES ZERO -> edge survives' if lo > 0 else 'CI INCLUDES ZERO -> cannot claim an edge'}")

    # ---- 2. permutation test --------------------------------------------
    beat = 0
    for _ in range(N_BOOT):
        s = sum(x if rng.random() < .5 else -x for x in pnl)
        if s >= total:
            beat += 1
    p = beat / N_BOOT
    print("-" * 74)
    print(f" 2. PERMUTATION TEST (null = trade signs are random)")
    print(f"    p-value = {p:.4f}   {'(significant at 5%)' if p < .05 else '(NOT significant)'}")

    # ---- 3. tail dependence ---------------------------------------------
    srt = sorted(pnl, reverse=True)
    print("-" * 74)
    print(f" 3. TAIL DEPENDENCE (is it one lucky trade?)")
    print(f"    full sample          NET {total:+,}")
    print(f"    drop best 1 trade    NET {total - srt[0]:+,}")
    print(f"    drop best 3 trades   NET {total - sum(srt[:3]):+,}")
    print(f"    biggest win {srt[0]:+,} = {srt[0]/total*100:.0f}% of total NET")
    surv = total - sum(srt[:3])
    print(f"    VERDICT: {'survives losing its 3 best trades' if surv > 0 else 'DIES without its 3 best trades -> lottery, not process'}")

    print("-" * 74)
    print(" Reproducible: fixed seed 12345. Daily-resolution paper backtest; 3% slippage;")
    print(" charges modelled. No crisis period in the sample window.")


if __name__ == "__main__":
    report((sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper())
