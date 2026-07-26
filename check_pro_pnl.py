import json, glob, os
BASE = r"C:\Users\Admin\Downloads\Expiry-Friday-5x"
LOT = {"NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20}

print("===== STRANGLE PAPER TRADES =====")
t = json.load(open(os.path.join(BASE, "data", "strangle-trades.json")))
tot = wins = n = 0
for x in t:
    ppu = x.get("pnlPerUnit")
    if ppu is None:
        print(f"  OPEN  {x.get('inst')} {x.get('structure','')} entry {str(x.get('entryAt'))[:16]} credit {x.get('credit')} (not closed)")
        continue
    lot = LOT.get(x.get("inst"), 1)
    rs = ppu * lot
    tot += rs; n += 1; wins += (rs > 0)
    print(f"  {x.get('inst'):9} {x.get('structure',''):8} {str(x.get('entryAt'))[:10]} credit {x.get('credit')} exitPrem {x.get('exitPrem')} -> perUnit {ppu:+.1f} = Rs {rs:+,.0f}")
if n:
    print(f"  ---- {n} closed | wins {wins} ({wins/n*100:.0f}%) | NET Rs {tot:+,.0f} ----")
else:
    print("  no closed trades")

print("\n===== EQUITY (started ~100000) =====")
for f in ("equity-nifty.json", "equity-sensex.json"):
    p = os.path.join(BASE, "data", f)
    if os.path.exists(p):
        d = json.load(open(p))
        cap = d.get("capital", 0)
        print(f"  {f:20} capital Rs {cap:,}  (P&L Rs {cap-100000:+,})  consecLosses {d.get('consecLosses')}  updated {d.get('updatedAt','')[:10]}")

print("\n===== EOD daily snapshots (last 15) =====")
for f in sorted(glob.glob(os.path.join(BASE, "data", "eod-*.json")))[-15:]:
    try:
        d = json.load(open(f))
        date = os.path.basename(f)[4:-5]
        keys = [k for k in ("totalPnl", "pnl", "netPnl", "dayPnl", "realized", "net", "realizedPnl") if k in d]
        if keys:
            print(f"  {date}: " + ", ".join(f"{k}={d[k]}" for k in keys))
        else:
            print(f"  {date}: keys={list(d)[:8]}")
    except Exception as e:
        print(f"  {f}: {e}")
