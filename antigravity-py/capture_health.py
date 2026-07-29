"""1-MINUTE OPTION CAPTURE HEALTH — is the data we are collecting actually usable?

The intraday engine currently prices options with Black-Scholes because only a
handful of days of real 1-minute option quotes exist. Those quotes are being
captured by the Antigravity Pro server into data/opt-candles/, but the capture
only runs while that server is up — so days are silently partial or missing, and
a partial day is worse than no day: it looks like data and quietly biases the test.

This audits the archive (READ-ONLY) and answers three things:
  1. which trading days are missing entirely,
  2. which days are PARTIAL (capture started after 09:15 or stopped before 15:30),
  3. whether the strikes we actually trade (1% OTM shorts, 3% OTM wings) are covered.

Usage:  py capture_health.py
"""
import json
import os
from datetime import datetime, timedelta

PRO = r"C:\Users\Admin\Downloads\Expiry-Friday-5x\data\opt-candles"
FULL_BARS = 375                 # 09:15 -> 15:29 inclusive
OK_BARS = 350                   # tolerate a few gaps
SYMBOL = "NIFTY"
LEG_PCT, WING_PCT = 0.010, 0.03

if not os.path.isdir(PRO):
    print(f"archive not found: {PRO}"); raise SystemExit

files = sorted(f for f in os.listdir(PRO) if f.endswith(".json"))
if not files:
    print("no capture files yet"); raise SystemExit

rows, all_days = [], []
for fn in files:
    day = fn[:-5]
    all_days.append(day)
    try:
        d = json.load(open(os.path.join(PRO, fn), encoding="utf-8"))
    except Exception as e:
        rows.append({"day": day, "err": str(e)[:40]}); continue
    ser = d.get("series", {})
    keys = [k for k in ser if k.startswith(SYMBOL + "|")]
    if not keys:
        rows.append({"day": day, "bars": 0, "strikes": 0, "first": "-", "last": "-", "cover": None})
        continue
    longest = max((ser[k] for k in keys), key=len)
    t0 = datetime.fromtimestamp(longest[0][0] / 1000).strftime("%H:%M")
    t1 = datetime.fromtimestamp(longest[-1][0] / 1000).strftime("%H:%M")
    strikes = sorted({float(k.split("|")[1]) for k in keys})
    # would the strikes we trade have been captured?  (spot ~ mid of the captured range)
    spot = (strikes[0] + strikes[-1]) / 2
    need = [spot * (1 + LEG_PCT), spot * (1 - LEG_PCT), spot * (1 + WING_PCT), spot * (1 - WING_PCT)]
    cover = all(min(strikes) <= n <= max(strikes) for n in need)
    rows.append({"day": day, "bars": len(longest), "strikes": len(strikes),
                 "first": t0, "last": t1, "cover": cover,
                 "lo": strikes[0], "hi": strikes[-1]})

# missing weekdays inside the captured range
s, e = datetime.strptime(all_days[0], "%Y-%m-%d"), datetime.strptime(all_days[-1], "%Y-%m-%d")
have = set(all_days)
missing = []
d = s
while d <= e:
    if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in have:
        missing.append(d.strftime("%Y-%m-%d"))
    d += timedelta(days=1)

full = [r for r in rows if r.get("bars", 0) >= OK_BARS]
partial = [r for r in rows if 0 < r.get("bars", 0) < OK_BARS]

print("=" * 78)
print(f" 1-MIN OPTION CAPTURE HEALTH — {SYMBOL}   ({len(files)} files, {all_days[0]} -> {all_days[-1]})")
print("=" * 78)
print(f"  {'day':<12}{'bars':>6}{'first':>8}{'last':>8}{'strikes':>9}{'trade-strikes':>15}   status")
for r in rows:
    if "err" in r:
        print(f"  {r['day']:<12}   unreadable: {r['err']}"); continue
    b = r.get("bars", 0)
    st = "FULL" if b >= OK_BARS else ("PARTIAL" if b else "EMPTY")
    cov = "yes" if r.get("cover") else ("no" if r.get("cover") is not None else "-")
    print(f"  {r['day']:<12}{b:>6}{r['first']:>8}{r['last']:>8}{r['strikes']:>9}{cov:>15}   {st}")

print("-" * 78)
print(f"  FULL days    : {len(full)}")
print(f"  PARTIAL days : {len(partial)}   (capture started late or server stopped)")
print(f"  MISSING days : {len(missing)}" + (f"   -> {', '.join(missing)}" if missing else ""))
bad_cov = [r for r in rows if r.get("cover") is False]
print(f"  strike gaps  : {len(bad_cov)} day(s) missing the strikes we would trade")

print("-" * 78)
NEED_FULL = 60          # ~3 months of clean days before real-premium intraday is worth running
print(f"  Real-premium intraday backtest needs roughly {NEED_FULL} FULL days.")
print(f"  Have {len(full)} -> about {max(0, NEED_FULL - len(full))} more trading days"
      f" (~{max(0, NEED_FULL - len(full)) / 21:.1f} months) at full capture.")
print("  Until then the intraday engine prices with Black-Scholes: the PATH is real,")
print("  the premium LEVEL is modelled.")
if partial or missing:
    print("\n  Capture only runs while the Antigravity Pro server is up. The auto-start task")
    print("  (Antigravity-Bot-Auto-Start, 08:50 weekdays) must stay Ready or days go missing.")
