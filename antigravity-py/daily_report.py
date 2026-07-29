"""DAILY FORWARD-TEST REPORT — append one honest row per trading day.

The forward test only means something if it is recorded. This snapshots the day
(trades, gross, charges, net, committee approval rate, risk headroom), appends it
to data/forward_log.csv, and prints how the live result compares with what the
backtest predicted. Run it after 15:30 IST.

Usage:  py daily_report.py
"""
import csv
import json
import os

import config
from engines import strangle_engine as E

LOG = os.path.join(config.DATA, "forward_log.csv")
EXPECTED_PER_TRADE = 45151 / 48        # walk-forward OOS mean (3% slip)

s = json.load(open(E.STATE, encoding="utf-8")) if os.path.exists(E.STATE) else E._blank()
today = E._today()

closed_today = [c for c in s.get("closed", []) if (c.get("exit_time") or "").startswith(today)]
gross = sum((c.get("pnl", 0) + c.get("charges", 0)) for c in closed_today)
charges = sum(c.get("charges", 0) for c in closed_today)
net = sum(c.get("pnl", 0) for c in closed_today)
wins = sum(1 for c in closed_today if c.get("pnl", 0) > 0)

dec_today = [d for d in s.get("decisions", []) if (d.get("time") or "").startswith(today)]
appr = sum(1 for d in dec_today if d["verdict"] == "APPROVE")

acct = float(config.cfg("ACCOUNT_CAPITAL")) + s.get("equity", 0)
peak = max(s.get("equity_peak", acct), acct)
row = {
    "date": today,
    "trades": len(closed_today),
    "wins": wins,
    "win_rate": round(wins / len(closed_today) * 100, 1) if closed_today else 0,
    "gross": round(gross, 1),
    "charges": round(charges, 1),
    "net": round(net, 1),
    "open_positions": len(s.get("positions", [])),
    "open_mtm": round(sum(p.get("mtm", 0) for p in s.get("positions", [])), 1),
    "decisions": len(dec_today),
    "approved": appr,
    "approval_rate": round(appr / len(dec_today) * 100, 1) if dec_today else 0,
    "equity": round(s.get("equity", 0), 1),
    "account_value": round(acct, 1),
    "drawdown_pct": round((1 - acct / peak) * 100, 1) if peak else 0,
    "halted": s.get("halted", False),
}

new = not os.path.exists(LOG)
rows = []
if not new:
    with open(LOG, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("date") != today]   # replace today's row
with open(LOG, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(row))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    w.writerow(row)

# ---- report ----
tot_closed = s.get("closed", [])
tot_net = sum(c.get("pnl", 0) for c in tot_closed)
tot_chg = sum(c.get("charges", 0) for c in tot_closed)
per_trade = tot_net / len(tot_closed) if tot_closed else 0

print("=" * 74)
print(f" FORWARD-TEST DAILY REPORT — {today}")
print("=" * 74)
print(f"  TODAY   trades {row['trades']}  wins {row['wins']} ({row['win_rate']}%)"
      f"   gross Rs {row['gross']:,.0f}   charges Rs {row['charges']:,.0f}   NET Rs {row['net']:,.0f}")
print(f"          open {row['open_positions']} (MTM Rs {row['open_mtm']:,.0f})"
      f"   committee {row['approved']}/{row['decisions']} approved ({row['approval_rate']}%)")
print(f"  ACCOUNT value Rs {row['account_value']:,.0f}   drawdown {row['drawdown_pct']}%"
      f"   halted={row['halted']}")
print("-" * 74)
print(f"  CUMULATIVE  {len(tot_closed)} trades   NET Rs {tot_net:,.0f}   charges paid Rs {tot_chg:,.0f}")
if tot_closed:
    dev = (per_trade - EXPECTED_PER_TRADE) / abs(EXPECTED_PER_TRADE) * 100
    print(f"  vs BACKTEST  live Rs {per_trade:,.0f}/trade  vs expected Rs {EXPECTED_PER_TRADE:,.0f}/trade"
          f"  -> {dev:+.0f}%")
    print(f"  {'within the +-20% go-live band' if abs(dev) <= 20 else 'OUTSIDE the +-20% band — do not go live'}")
    print(f"  need {max(0, 30 - len(tot_closed))} more paper trades for the 30-trade gate")
else:
    print("  no closed trades yet — nothing to compare")
print(f"\n  logged to {LOG}")
