"""ANCHORED WALK-FORWARD — the only honest test we had not run.

Every number reported so far was IN-SAMPLE: we ran ~60 configurations over the whole
611-day history and reported the best one. That is textbook backtest overfitting
(Bailey & Lopez de Prado). The best config's in-sample return is biased upward even
if the strategy has NO real edge.

This does it properly:
  * fold k trains on   [start .. T)        <- picks the best config using ONLY the past
  * fold k tests  on   [T .. T+step)       <- applies it to data it has never seen
  * roll T forward, repeat, concatenate ONLY the out-of-sample trades

No look-ahead: each config is simulated chronologically (its IV-percentile history at
any moment contains only past IVs), and the parameter CHOICE for a fold uses train-window
trades only.

The out-of-sample equity is what a live trader would actually have earned.

Usage:  py -m backtest.walkforward NIFTY
"""
import sys
from datetime import datetime

from backtest import bt_strangle as bt
from backtest.sweep import metrics

# realistic: a 4-leg condor does not fill at mid
SLIP = 0.03
TRAIN_MONTHS = 12      # minimum history before the first out-of-sample decision
TEST_MONTHS = 3        # how far ahead we trade on a chosen config before re-choosing
MIN_TRAIN_TRADES = 5   # below this the train window cannot rank configs meaningfully

GRID = [(g, l, d)
        for g in (0, 50, 60, 70)
        for l in (0.010, 0.015, 0.020)
        for d in (2, 3, 4)]

DEFAULT_CFG = (60, 0.010, 4)   # used when a train window is too thin to choose


def _months_after(day, n):
    y, m = day.year, day.month + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    d = min(day.day, 28)
    return datetime(y, m, d)


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d")


def run(symbol):
    contracts, und, days = bt.load(symbol)
    if not days:
        print("no data"); return

    # 1) simulate every config ONCE over the full history (chronological, no leakage)
    print(f"simulating {len(GRID)} configs over {len(days)} days ...")
    bt.P["condor"] = True
    bt.P["slipPct"] = SLIP
    bt.P["legStopMult"] = 2.0
    bt.P["takeProfitPct"] = 0
    book = {}
    for (g, l, d) in GRID:
        bt.P["ivGate"] = g
        bt.P["legDistancePct"] = l
        bt.P["targetDTE"] = d
        trades, _ = bt.simulate(symbol, contracts, und, days)
        book[(g, l, d)] = trades

    all_entries = sorted({t["entry"] for ts in book.values() for t in ts})
    if not all_entries:
        print("no trades"); return
    start, last = _d(all_entries[0]), _d(all_entries[-1])

    # 2) roll the anchored window forward
    folds = []
    oos = []
    t = _months_after(start, TRAIN_MONTHS)
    while t < last:
        t_end = _months_after(t, TEST_MONTHS)
        tr_lo, tr_hi = start.strftime("%Y-%m-%d"), t.strftime("%Y-%m-%d")
        te_lo, te_hi = tr_hi, t_end.strftime("%Y-%m-%d")

        # choose the config that did best on the TRAIN window only
        best, best_net, n_train = None, None, 0
        for cfg, trades in book.items():
            tr = [x for x in trades if tr_lo <= x["entry"] < tr_hi]
            if len(tr) < MIN_TRAIN_TRADES:
                continue
            net = sum(x["net"] for x in tr)
            if best_net is None or net > best_net:
                best, best_net, n_train = cfg, net, len(tr)
        if best is None:
            best, best_net, n_train = DEFAULT_CFG, 0, 0

        te = [x for x in book[best] if te_lo <= x["entry"] < te_hi]
        oos.extend(te)
        folds.append({"train_to": tr_hi, "test": f"{te_lo}..{te_hi}", "cfg": best,
                      "n_train": n_train, "train_net": best_net,
                      "n_test": len(te), "test_net": sum(x["net"] for x in te)})
        t = t_end

    # 3) report
    print("\n" + "=" * 88)
    print(f" ANCHORED WALK-FORWARD — {symbol} condor · {int(SLIP*100)}% slip · "
          f"train>={TRAIN_MONTHS}m, test={TEST_MONTHS}m")
    print("=" * 88)
    print(f"{'Train thru':>11}{'Test window':>24}{'Chosen cfg':>22}"
          f"{'Trn':>5}{'TrnNET':>10}{'Tst':>5}{'TstNET':>10}")
    for f in folds:
        g, l, d = f["cfg"]
        cfg = f"gate{g} leg{l*100:.1f}% dte{d}"
        print(f"{f['train_to']:>11}{f['test']:>24}{cfg:>22}"
              f"{f['n_train']:>5}{f['train_net']:>+10,}{f['n_test']:>5}{f['test_net']:>+10,}")

    m = metrics(oos)
    print("-" * 88)
    print(f" OUT-OF-SAMPLE (concatenated): {m['n']} trades | win {m['win']:.0f}% "
          f"| NET {m['net']:+,} | PF {m['pf']:.2f} | MaxDD {m['ddrs']:,}")

    # 4) the comparison that matters: in-sample best vs what you'd really have got
    bt.P["ivGate"], bt.P["legDistancePct"], bt.P["targetDTE"] = DEFAULT_CFG
    ins, _ = bt.simulate(symbol, contracts, und, days)
    mi = metrics(ins)
    print("-" * 88)
    print(f"{'':<34}{'Trades':>8}{'Win%':>7}{'NET':>12}{'PF':>7}")
    print(f"{'IN-SAMPLE best (what we claimed)':<34}{mi['n']:>8}{mi['win']:>6.0f}%"
          f"{mi['net']:>+12,}{mi['pf']:>7.2f}")
    print(f"{'OUT-OF-SAMPLE (the honest number)':<34}{m['n']:>8}{m['win']:>6.0f}%"
          f"{m['net']:>+12,}{m['pf']:>7.2f}")
    if mi["net"]:
        decay = (1 - m["net"] / mi["net"]) * 100 if mi["net"] > 0 else float("nan")
        print(f"\n Performance decay in-sample -> out-of-sample: {decay:.0f}%")
    cfgs = {f["cfg"] for f in folds}
    print(f" Config stability: {len(cfgs)} different config(s) chosen across {len(folds)} folds"
          f"{'  <- UNSTABLE (overfitting signature)' if len(cfgs) > 1 else '  <- stable'}")
    print(" NOTE: OOS is the only number a live trader could have earned. Daily-res paper backtest.")


if __name__ == "__main__":
    run((sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper())
