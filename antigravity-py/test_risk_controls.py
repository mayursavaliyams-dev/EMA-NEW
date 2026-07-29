"""Prove the safety actually fires. Red-Team flagged: an untested kill-switch is
no kill-switch. This drives the engine into loss/drawdown on an ISOLATED temp
state (does NOT touch the live forward-test) and asserts it flattens + halts.

Run:  py test_risk_controls.py
"""
import os
import json
import tempfile

import config
from engines import strangle_engine as E


class FakeConn:
    """Returns each leg's stored entry price -> position MTM ~ 0, so the test is
    driven purely by the realized/equity numbers we inject (deterministic)."""
    name = "fake"
    def ltp(self, keys):
        return {k: PRICES.get(k, 0) for k in keys}
    def place_order(self, *a, **k):
        return {"status": "paper"}


PRICES = {}


def _pos(mtm_entry=100.0):
    key_ce, key_pe = "T|CE", "T|PE"
    PRICES[key_ce] = mtm_entry
    PRICES[key_pe] = mtm_entry
    return {
        "id": 1, "symbol": "NIFTY", "structure": "condor", "status": "OPEN", "stale": False,
        "lots": 1, "qty": 65, "credit": 5000, "mtm": 0.0, "max_loss": 20000,
        "entry_time": "2026-07-26T10:00:00", "expiry": "2026-07-30", "iv_pct": 70, "pop": 80,
        "legs": [
            {"side": "SELL", "opt": "CE", "strike": 24500, "key": key_ce, "entry": mtm_entry, "qty": 65},
            {"side": "SELL", "opt": "PE", "strike": 23500, "key": key_pe, "entry": mtm_entry, "qty": 65},
        ],
    }


def run_scenario(name, state, expect_halt_contains):
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(tmp, "w") as f:
        json.dump(state, f)
    E.STATE = tmp                       # isolate: point engine at temp state
    eng = E.StrangleEngine(FakeConn())
    eng.manage()
    s = json.load(open(tmp))
    os.unlink(tmp)
    halted = s.get("halted")
    reason = s.get("halt_reason", "")
    flat = len(s["positions"]) == 0
    ok = halted and flat and (expect_halt_contains.lower() in reason.lower())
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"         halted={halted}  positions_flattened={flat}  closed={len(s['closed'])}")
    print(f"         reason: {reason}")
    return ok


print("RISK-CONTROL FIRE TEST (isolated temp state — live forward-test untouched)")
print(f"  config: dailyLossCap=Rs {config.cfg('dailyLossCapRs')}  maxDrawdown={config.cfg('MAX_DRAWDOWN_PCT')}%  capital=Rs {config.cfg('ACCOUNT_CAPITAL')}\n")

blank = E._blank()

# --- Scenario 1: DAILY kill-switch (realized loss beyond cap -> flatten + halt) ---
s1 = dict(blank)
s1.update({"positions": [_pos()], "realized_pnl": -16000.0, "day": E._today(),
           "iv_hist": {}, "iv_day": {}, "closed": []})
r1 = run_scenario("Daily kill-switch (realized -16,000 <= -15,000 cap)", s1, "loss cap")

# --- Scenario 2: PORTFOLIO max-drawdown halt (account 26% below peak) ---
s2 = dict(blank)
s2.update({"positions": [_pos()], "realized_pnl": 0.0, "equity": -30000.0,
           "equity_peak": 650000.0, "day": E._today(), "iv_hist": {}, "iv_day": {}, "closed": []})
# account_value = 500000 + (-30000) = 470000 ; peak 650000*(1-0.20)=520000 ; 470000 <= 520000 -> halt
r2 = run_scenario("Max-drawdown halt (account 470k <= 520k threshold)", s2, "drawdown")

# --- Scenario 3: HEALTHY (no loss) -> must NOT halt ---
fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
s3 = dict(blank); s3.update({"positions": [_pos()], "realized_pnl": 500.0, "equity": 1000.0,
                             "equity_peak": 1000.0, "day": E._today(), "iv_hist": {}, "iv_day": {}, "closed": []})
json.dump(s3, open(tmp, "w")); E.STATE = tmp
E.StrangleEngine(FakeConn()).manage()
s3o = json.load(open(tmp)); os.unlink(tmp)
r3 = (not s3o.get("halted")) and len(s3o["positions"]) == 1
print(f"  [{'PASS' if r3 else 'FAIL'}] Healthy book stays OPEN (no false halt)")
print(f"         halted={s3o.get('halted')}  positions_open={len(s3o['positions'])}")

print("\n" + ("ALL PASS ✓ risk controls fire correctly" if (r1 and r2 and r3) else "SOME FAILED ✗"))
