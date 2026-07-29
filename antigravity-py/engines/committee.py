"""Decision Committee — the Opus-5 "agents debate -> score -> verdict" as a REAL,
deterministic pre-trade check. No LLM theatre: each 'agent' is an independent,
auditable rule that votes YES/NO with a score and a reason. A candidate trades
ONLY if the committee APPROVES, and the full verdict is logged for review.

Agents:
  PREMIUM   (bull)  — is the option premium rich enough to be worth selling?
  RISK      (bear)  — HARD gate: capital defended (DTE, sizing, daily-loss & drawdown room)
  LIQUIDITY         — reward:risk of the credit vs the position's defined max loss
  STRUCTURE         — defined-risk (condor) preferred over naked
"""

WEIGHTS = {"PREMIUM": 0.40, "RISK": 0.20, "LIQUIDITY": 0.25, "STRUCTURE": 0.15}
APPROVE_SCORE = 50


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def evaluate(ctx):
    checks = []

    # 1) PREMIUM (bull): sell only when premium is genuinely rich
    ivp, gate = ctx["ivp"], ctx["gate"]
    prem_ok = ivp >= gate
    checks.append({"agent": "PREMIUM", "vote": "YES" if prem_ok else "NO",
                   "score": round(_clamp((ivp - 40) / 40), 2),
                   "reason": f"IV pct {ivp} vs gate {gate} — {'rich' if prem_ok else 'too cheap'}"})

    # 2) RISK (bear / defend capital): HARD gate — protect the portfolio (Opus-5 slide 5)
    fails = []
    if ctx["dte"] < ctx["min_dte"]:
        fails.append(f"DTE {ctx['dte']}<{ctx['min_dte']}")
    if ctx["lots"] < 1:
        fails.append("capital too small for 1 lot")
    daily_room = ctx["realized"] > -ctx["cap"] * 0.8
    if not daily_room:
        fails.append("near daily-loss cap (keep 20% buffer)")
    dd_floor = ctx["peak"] * (1 - ctx["max_dd_pct"] / 100 * 0.8)
    dd_room = ctx["account_value"] > dd_floor
    if not dd_room:
        fails.append("near max-drawdown halt")
    risk_ok = not fails
    checks.append({"agent": "RISK", "vote": "YES" if risk_ok else "NO",
                   "score": round(_clamp(1.0 - (0 if daily_room else .5) - (0 if dd_room else .5)), 2),
                   "reason": "capital protected" if risk_ok else "; ".join(fails)})

    # 3) LIQUIDITY / reward:risk — credit vs the position's own defined max loss
    rr = (ctx["credit"] / ctx["max_loss"]) if ctx["max_loss"] else 0.0
    liq_ok = ctx["credit"] > 0
    checks.append({"agent": "LIQUIDITY", "vote": "YES" if liq_ok else "NO",
                   "score": round(_clamp(rr / 0.25), 2),
                   "reason": f"reward:risk {rr:.2f}  (credit {ctx['credit']:.0f} / risk {ctx['max_loss']:.0f})"})

    # 4) STRUCTURE — defined risk preferred
    struct_ok = ctx["structure"] == "condor"
    checks.append({"agent": "STRUCTURE", "vote": "YES" if struct_ok else "NO",
                   "score": 1.0 if struct_ok else 0.3,
                   "reason": f"{ctx['structure']} — {'defined risk' if struct_ok else 'naked / unbounded tail'}"})

    score = round(sum(WEIGHTS[c["agent"]] * c["score"] for c in checks) * 100)
    hard_ok = risk_ok and liq_ok and prem_ok          # RISK+LIQUIDITY+PREMIUM are gates
    verdict = "APPROVE" if (hard_ok and score >= APPROVE_SCORE) else "REJECT"
    return {"verdict": verdict, "score": score, "approve_at": APPROVE_SCORE, "checks": checks}
