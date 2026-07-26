"""Read-only significance/robustness check on Antigravity Pro's 20-year 0-DTE
sell backtest results. Does NOT touch that repo. Uses the yearly breakdown
(no per-trade P&L is stored) to answer the one question that matters for
short-vol: does it survive the crisis years, or is it pennies-before-a-steamroller?
"""
import json
import os
import random

BASE = r"C:\Users\Admin\Downloads\Expiry-Friday-5x"
random.seed(12345)


def net_of(v):
    if isinstance(v, dict):
        for k in ("netRs", "net", "pnl", "rs"):
            if k in v:
                return v[k]
    return v if isinstance(v, (int, float)) else None


def boot_ci(vals, n=10000):
    m = []
    k = len(vals)
    for _ in range(n):
        s = sum(vals[random.randrange(k)] for _ in range(k)) / k
        m.append(s)
    m.sort()
    return m[int(.025 * n)], m[int(.975 * n)], sum(1 for x in m if x <= 0) / n


for inst in ("nifty", "banknifty", "sensex"):
    d = json.load(open(os.path.join(BASE, f"backtest-tv-sell-results-{inst}.json")))
    print("=" * 78)
    print(f" {inst.upper()}  |  {d['traded']}/{d['expiries']} expiries traded  |  {d['dataSource']}")
    print("=" * 78)
    for it in d["results"]:
        by = it["byYear"]
        yrs = sorted((y, net_of(v)) for y, v in by.items())
        yrs = [(y, v) for y, v in yrs if v is not None]
        neg = [(y, v) for y, v in yrs if v < 0]
        vals = [v for _, v in yrs]
        lo, hi, pneg = boot_ci(vals)
        srt = sorted(vals)
        without_worst = sum(vals) - srt[0]
        print(f"\n  {it['kind']}  | win {it['winRate']}%  PF {it['pf']}  net Rs {it['netRs']:,}"
              f"  avg/trade {it['avgRs']:,}  worst-trade {it['worstRs']:,}")
        print(f"    {len(yrs)} years | NEGATIVE years: {len(neg)}"
              + (("  -> " + ", ".join(f"{y}:{v:,}" for y, v in neg)) if neg else "  (none)"))
        w = sorted(yrs, key=lambda x: x[1])[:3]
        print(f"    worst 3 years: " + ", ".join(f"{y} {v:,}" for y, v in w))
        print(f"    yearly-mean 95% CI: [{lo:,.0f} .. {hi:,.0f}]  P(mean<=0)={pneg*100:.1f}%"
              f"  {'CI EXCLUDES 0' if lo > 0 else 'CI INCLUDES 0'}")
        print(f"    drop best year -> total still Rs {without_worst:,}"
              f"  (best year = {srt[-1]/sum(vals)*100:.0f}% of total)")
    print()

print("NOTE: 0-DTE selling on DAILY-resolution BS settlement structurally UNDER-estimates")
print("intraday gamma/gap tail — the exact risk that kills 0-DTE sellers. Their own note")
print("says 'selling tail risk is real; forward-test.' High win% + high PF is the classic")
print("short-vol signature, NOT proof of safety.")
