#!/usr/bin/env python3
"""
Daily archiver — run once per day (scheduled ~15:40 IST).
Archives every index's option data IF today is that index's expiry day
(archive_expiry skips non-expiry days automatically). Over time this builds
the multi-expiry dataset for `python backtest_gamma.py --archive`.
"""
import os
import csv
from datetime import datetime
import archive_expiry as ae
import backtest_gamma as bt
import backtest_strategy as bs

SYMBOLS = ["NIFTY", "SENSEX", "BANKNIFTY"]
PNL_LOG = os.path.join(bt.BASE, "data", "daily_pnl.csv")

today = datetime.now(bt.IST).strftime("%Y-%m-%d")
print(f"\n===== Daily run: {datetime.now(bt.IST).strftime('%Y-%m-%d %H:%M IST')} =====")

# 1) archive option data (expiry days)
for sym in SYMBOLS:
    try:
        ae.archive(sym)
    except Exception as e:
        print(f"  {sym}: ERROR {e}")

# 2) regular P&L save — log today's strategy P&L per symbol
def log_daily_pnl():
    os.makedirs(os.path.dirname(PNL_LOG), exist_ok=True)
    new = not os.path.exists(PNL_LOG)
    rows = []
    for sym in SYMBOLS:
        try:
            trades, lot = bs.run(sym, None, 3, verbose=False)
        except Exception as e:
            print(f"  PnL {sym}: ERROR {e}"); continue
        tdy = [t for t in trades if t["day"] == today]
        if not tdy:
            continue
        net = round(sum(t["pnl"] for t in tdy))
        wins = sum(1 for t in tdy if t["pnl"] > 0)
        rows.append([today, sym, len(tdy), wins, len(tdy) - wins, net])
        print(f"  P&L {sym}: {len(tdy)} trades, net Rs {net:+}")
    if rows:
        with open(PNL_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "symbol", "trades", "wins", "losses", "net_rs"])
            w.writerows(rows)

log_daily_pnl()
print("===== done =====")
