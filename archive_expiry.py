#!/usr/bin/env python3
"""
Expiry-day Archiver
===================
Saves a full day's 1-minute option data (ATM band CE+PE) + index for an expiry,
permanently to data/archive/{symbol}_{expiry}.json — so a real multi-expiry
dataset accumulates over time for backtesting (Upstox does NOT keep expired
contracts, so we must collect them ourselves on each expiry day).

Run on (or right after) an expiry day:
    python archive_expiry.py                 # NIFTY, today's expiry
    python archive_expiry.py BANKNIFTY
    python archive_expiry.py NIFTY --force   # archive today even if not expiry

Best automated: schedule daily ~15:40 IST (it only archives on expiry days
unless --force).
"""

import sys
import os
import json
import urllib.parse
from datetime import datetime

import backtest_gamma as bt   # reuse fetch helpers

ARCHIVE_DIR = os.path.join(bt.BASE, "data", "archive")


def archive(symbol, force=False):
    symbol = symbol.upper()
    if symbol not in bt.SYMBOLS:
        print("unknown symbol", symbol); return
    ikey = bt.SYMBOLS[symbol]

    today = datetime.now(bt.IST).strftime("%Y-%m-%d")
    contracts = bt.get(f"/v2/option/contract?instrument_key={urllib.parse.quote(ikey, safe='')}")
    exps = sorted({c.get("expiry") for c in contracts.get("data", []) if c.get("expiry") and c.get("expiry") >= today})
    if not exps:
        print("no expiries"); return
    expiry = exps[0]

    if expiry != today and not force:
        print(f"Today ({today}) is NOT an expiry day (nearest {expiry}). "
              f"Use --force to archive anyway.")
        return

    print(f"Archiving {symbol} expiry {expiry} (date {today}) ...")

    spot_map = bt.candles_1m(ikey)
    if not spot_map:
        print("no index candles (market may not have data yet)"); return
    epochs = sorted(spot_map)
    lo, hi = min(spot_map.values()), max(spot_map.values())

    chain = bt.get(f"/v2/option/chain?instrument_key={urllib.parse.quote(ikey, safe='')}&expiry_date={expiry}")
    rows = sorted(chain.get("data", []), key=lambda r: r.get("strike_price", 0))
    band = [r for r in rows if lo - 150 <= r.get("strike_price", 0) <= hi + 150]
    print(f"  index candles {len(epochs)}  range {lo:.1f}-{hi:.1f}  strikes {len(band)} (fetching...)")

    options = []
    for r in band:
        strike = r.get("strike_price")
        for side, leg in (("CE", r.get("call_options")), ("PE", r.get("put_options"))):
            key = (leg or {}).get("instrument_key")
            if not key:
                continue
            try:
                m = bt.candles_1m(key)
            except Exception:
                m = {}
            if not m:
                continue
            options.append({"strike": strike, "side": side,
                            "candles": [[e, m[e]] for e in sorted(m)]})

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    out = {
        "symbol": symbol, "expiry": expiry, "date": today,
        "index": [[e, spot_map[e]] for e in epochs],
        "options": options,
    }
    path = os.path.join(ARCHIVE_DIR, f"{symbol}_{expiry}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    sz = round(os.path.getsize(path) / 1024, 1)
    print(f"  [OK] saved {len(options)} option series -> {path} ({sz} KB)")


def main():
    args = sys.argv[1:]
    force = "--force" in args
    pos = [a for a in args if not a.startswith("--")]
    archive(pos[0] if pos else "NIFTY", force=force)


if __name__ == "__main__":
    main()
