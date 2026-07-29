"""GO-LIVE GATE — one command that says, honestly, whether this is ready for
real money. Every gate is a hard requirement from PROJECT.md section 7. It runs
the checks, prints PASS/FAIL per gate, and refuses to say READY unless all pass.

The point is to make the decision mechanical, so a good week of paper P&L cannot
talk anyone into going live early.

Usage:  py golive_gate.py
"""
import json
import os
import subprocess
import sys

import config
from engines import strangle_engine as E

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_TRADES = 30            # forward paper trades
MIN_SIG_TRADES = 72        # needed for 95% confidence at the measured Sharpe
MAX_BACKTEST_DEVIATION = 20  # forward must land within +-20% of backtest expectation

gates = []


def gate(name, ok, detail, blocking=True):
    gates.append({"name": name, "ok": bool(ok), "detail": detail, "blocking": blocking})


# --- 1. paper only -----------------------------------------------------------
mode = config.cfg("TRADE_MODE")
gate("Mode is paper (no live order path)", mode == "paper", f"TRADE_MODE={mode}")

# --- 2. forward paper volume -------------------------------------------------
s = json.load(open(E.STATE, encoding="utf-8")) if os.path.exists(E.STATE) else E._blank()
closed = s.get("closed", [])
gate(f"Forward paper trades >= {MIN_TRADES}", len(closed) >= MIN_TRADES,
     f"{len(closed)} closed paper trades so far")

# --- 3. statistical significance ---------------------------------------------
n_total = len(closed) + 48        # 48 = walk-forward OOS trades already banked
gate(f"Sample >= {MIN_SIG_TRADES} for significance", n_total >= MIN_SIG_TRADES,
     f"{n_total} (48 walk-forward OOS + {len(closed)} forward) — "
     f"bootstrap CI last excluded zero: NO (p=0.063 at n=48)")

# --- 4. forward vs backtest deviation ----------------------------------------
if closed:
    net = sum(c.get("pnl", 0) for c in closed)
    per_trade = net / len(closed)
    expected = 45151 / 48          # OOS mean per trade
    dev = abs(per_trade - expected) / abs(expected) * 100 if expected else 999
    gate(f"Forward within +-{MAX_BACKTEST_DEVIATION}% of backtest", dev <= MAX_BACKTEST_DEVIATION,
         f"forward Rs {per_trade:,.0f}/trade vs expected Rs {expected:,.0f} -> {dev:.0f}% off")
else:
    gate(f"Forward within +-{MAX_BACKTEST_DEVIATION}% of backtest", False,
         "no closed forward trades yet")

# --- 5. risk controls proven -------------------------------------------------
try:
    r = subprocess.run([sys.executable, os.path.join(HERE, "test_risk_controls.py")],
                       capture_output=True, text=True, cwd=HERE, timeout=120)
    ok = "ALL PASS" in r.stdout
    gate("Risk controls fire (kill-switch + drawdown halt)", ok,
         "test_risk_controls.py: " + ("ALL PASS" if ok else "FAILED"))
except Exception as e:
    gate("Risk controls fire (kill-switch + drawdown halt)", False, f"could not run test: {e}")

# --- 6. crash stress test ----------------------------------------------------
try:
    r = subprocess.run([sys.executable, "-m", "backtest.stress_test", "NIFTY"],
                       capture_output=True, text=True, cwd=HERE, timeout=120)
    ok = "Defined risk works" in r.stdout
    worst = [l for l in r.stdout.splitlines() if "GFC-type" in l and "%" in l]
    gate("Crash stress test run (defined risk holds)", ok,
         (worst[0].strip()[:70] if worst else "stress_test.py executed"))
except Exception as e:
    gate("Crash stress test run (defined risk holds)", False, f"{e}")

# --- 7. structure / universe discipline --------------------------------------
gate("Condor only (naked disabled)",
     float(config.cfg("ivHighPct")) <= float(config.cfg("ivLowPct")),
     f"ivHighPct={config.cfg('ivHighPct')} <= ivLowPct={config.cfg('ivLowPct')} -> always condor")
syms = config.cfg("TRADE_SYMBOLS") or []
gate("Only validated symbols", syms == ["NIFTY"], f"TRADE_SYMBOLS={syms}")

# --- 8. engine health --------------------------------------------------------
gate("Engine not halted", not s.get("halted"), s.get("halt_reason") or "no halt")
ivn = len(s.get("iv_hist", {}).get("NIFTY", []))
gate("IV history valid for the gate", ivn >= int(config.cfg("ivMinHistoryDays")),
     f"{ivn} daily samples (need {config.cfg('ivMinHistoryDays')})")

# --- 9. crisis-period data (known gap) ---------------------------------------
gate("Backtest window contains a crisis", False,
     "2024-05..2026-05 has no crash; short-vol dies in crises. Stress test is a model, not data.")

# --- report ------------------------------------------------------------------
print("=" * 84)
print(" GO-LIVE GATE — mechanical readiness check (PROJECT.md section 7)")
print("=" * 84)
passed = 0
for g in gates:
    mark = "PASS" if g["ok"] else "FAIL"
    passed += g["ok"]
    print(f"  [{mark}] {g['name']}")
    print(f"         {g['detail']}")
blockers = [g for g in gates if not g["ok"] and g["blocking"]]
print("-" * 84)
print(f"  {passed}/{len(gates)} gates pass")
if blockers:
    print(f"\n  VERDICT: NOT READY — {len(blockers)} blocking gate(s) open:")
    for b in blockers:
        print(f"    - {b['name']}")
    print("\n  Keep paper-trading. Every P&L number stays a hypothesis until these close.")
else:
    print("\n  VERDICT: all gates pass. Even then: start TINY (1 lot), human presses the button.")
