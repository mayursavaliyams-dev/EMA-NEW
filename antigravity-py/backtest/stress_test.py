"""CRASH STRESS TEST — the validation our data cannot give us.

The 611-day window (2024-05 -> 2026-05) contains no crisis. Short-vol dies in
crises, so a backtest without one is not evidence of safety. This prices the
CURRENT deployed structure (condor, legs 1% OTM, wings 3% OTM) under crash gaps
using Black-Scholes with a VOL SPIKE — the two things that kill a seller at once:
the underlying gaps AND implied vol explodes.

Scenarios are historical Indian-market single-day index moves.

Usage:  py -m backtest.stress_test [NIFTY]
"""
import sys

import config
from greeks import bs_price

SCENARIOS = [
    ("Calm drift",            +0.002, 1.0),
    ("Normal down day",       -0.010, 1.2),
    ("Sharp fall",            -0.025, 1.8),
    ("Budget/event shock",    -0.040, 2.5),
    ("2020 COVID-type day",   -0.100, 4.0),
    ("2008 GFC-type day",     -0.110, 4.5),
    ("Melt-up (call side)",   +0.060, 2.5),
]

R = 0.065


def leg_price(spot, strike, T, iv, is_call):
    return bs_price(spot, strike, T, R, max(iv, 0.01), is_call)


def run(symbol="NIFTY"):
    meta = config.SYMBOLS[symbol]
    lot = meta["lot"]
    spot = 24200.0                      # representative
    base_iv = 0.12                      # 12% ATM IV (current regime)
    T = 4 / 365.0                       # DTE 4, as deployed
    legd = float(config.cfg("legDistancePct"))
    wingd = float(config.cfg("wingDistancePct"))
    step = meta["step"]

    def rnd(x):
        return round(x / step) * step

    ce, pe = rnd(spot * (1 + legd)), rnd(spot * (1 - legd))
    cw, pw = rnd(spot * (1 + wingd)), rnd(spot * (1 - wingd))

    # entry credit at t0
    entry = (leg_price(spot, ce, T, base_iv, True) + leg_price(spot, pe, T, base_iv, False)
             - leg_price(spot, cw, T, base_iv, True) - leg_price(spot, pw, T, base_iv, False))
    credit = entry * lot
    width = max(cw - ce, pe - pw)
    max_loss = (width - entry) * lot

    cap = float(config.cfg("ACCOUNT_CAPITAL"))
    daily_cap = abs(float(config.cfg("dailyLossCapRs")))

    print("=" * 92)
    print(f" CRASH STRESS TEST — {symbol} condor  spot {spot:.0f}  DTE 4  ATM IV {base_iv*100:.0f}%")
    print(f" structure: SELL {ce:.0f}CE / {pe:.0f}PE   BUY {cw:.0f}CE / {pw:.0f}PE   lot {lot}")
    print(f" credit Rs {credit:,.0f}   defined max loss Rs {max_loss:,.0f}   account Rs {cap:,.0f}")
    print("=" * 92)
    print(f"  {'scenario':<24}{'move':>8}{'IV x':>6}{'spot':>9}{'position P&L':>15}{'% acct':>9}   verdict")

    for name, move, ivmul in SCENARIOS:
        s1 = spot * (1 + move)
        iv1 = base_iv * ivmul
        T1 = max(T - 1 / 365.0, 1e-6)          # one day later
        now = (leg_price(s1, ce, T1, iv1, True) + leg_price(s1, pe, T1, iv1, False)
               - leg_price(s1, cw, T1, iv1, True) - leg_price(s1, pw, T1, iv1, False))
        pnl = (entry - now) * lot
        pnl = max(pnl, -max_loss)               # wings cap the loss — that is the point
        pct = pnl / cap * 100
        if pnl <= -max_loss * 0.99:
            v = "MAX LOSS (wings held)"
        elif pnl <= -daily_cap:
            v = "kill-switch fires"
        elif pnl < 0:
            v = "loss, within limits"
        else:
            v = "profit"
        print(f"  {name:<24}{move*100:>+7.1f}%{ivmul:>6.1f}{s1:>9.0f}{pnl:>+15,.0f}{pct:>8.1f}%   {v}")

    print("-" * 92)
    # the same shock WITHOUT wings — what naked selling would have done
    print("  Same shocks on a NAKED strangle (no wings) — what the tail really looks like:")
    for name, move, ivmul in SCENARIOS[-3:]:
        s1 = spot * (1 + move)
        iv1 = base_iv * ivmul
        T1 = max(T - 1 / 365.0, 1e-6)
        n_entry = leg_price(spot, ce, T, base_iv, True) + leg_price(spot, pe, T, base_iv, False)
        n_now = leg_price(s1, ce, T1, iv1, True) + leg_price(s1, pe, T1, iv1, False)
        npnl = (n_entry - n_now) * lot
        print(f"    {name:<26}{npnl:>+14,.0f}  ({npnl/cap*100:+.1f}% of account)  <- UNCAPPED")

    print("-" * 92)
    print(f"  Defined risk works: worst condor outcome is Rs {max_loss:,.0f} ({max_loss/cap*100:.1f}% of account),")
    print(f"  which is why sizing caps lots at {config.cfg('RISK_PER_TRADE_PCT')}% risk per trade.")
    print("  CAVEAT: this assumes wings actually FILL in a crash. In a real gap the spread")
    print("  widens and the long wings may be unfillable — treat max-loss as a floor, not a promise.")


if __name__ == "__main__":
    run((sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper())
