"""Premium-SELLING engine (the edge). PAPER only.

HARDENED after the Red-Team / Decision-Council audit. Fixes, in order of severity:
  F7/D15  all time decisions use IST-aware clock (was naive local datetime.now)
  F1/F2   atomic state writes (temp + os.replace) — no more truncated-file wipes
  F1      corrupt state = FAIL-CLOSED (quarantine + halt), never "start from empty"
  F4      kill-switch now counts OPEN MTM and actually FLATTENS the book (was: only blocked new entries)
  F5/F9   missing LTP no longer silently pretends cur==entry (which disarmed the 2x stop) — legs are
          marked STALE, decisions are withheld, and the condition is surfaced loudly
  F8      entry rejected on zero/negative/absurd prices
  F11     closed[] is capped
  D2      IV percentile sampled ONCE PER DAY (was: every 30s poll -> a meaningless intraday percentile
          that did NOT match the backtested gate). Fail-closed until enough history exists.
  D7      minDTE gate — no more 0-DTE entries
  D6/F12  cumulative equity ledger (realized_pnl still resets daily for the kill-switch)
  id      monotonic seq (ids used to collide)
"""
import os
import json
import tempfile
from datetime import datetime, timezone, timedelta

import config
import charges as ch
from greeks import greeks, breakeven_pop_strangle
from engines import committee

STATE = os.path.join(config.DATA, "strangle_state.json")
IST = timezone(timedelta(hours=5, minutes=30))
MAX_CLOSED = 200


def now_ist():
    return datetime.now(IST)


def _today():
    return now_ist().strftime("%Y-%m-%d")


def _blank():
    return {"positions": [], "closed": [], "iv_hist": {}, "iv_day": {},
            "realized_pnl": 0.0, "equity": 0.0, "equity_peak": 0.0, "day": None, "seq": 0,
            "halted": False, "halt_reason": "", "decisions": []}


def _load():
    if not os.path.exists(STATE):
        return _blank()
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError("state is not an object")
    except Exception as e:
        # FAIL-CLOSED. The old code returned an EMPTY book here, which meant a
        # half-written file caused: duplicate positions + realized P&L wiped +
        # kill-switch amnesia. Never again: quarantine and halt for a human.
        try:
            os.replace(STATE, STATE + ".corrupt")
        except Exception:
            pass
        s = _blank()
        s["halted"] = True
        s["halt_reason"] = f"state file unreadable ({e}) — quarantined to .corrupt, engine HALTED"
        _save(s)
        return s
    for k, v in _blank().items():
        s.setdefault(k, v)
    return s


def _save(s):
    """Atomic: write a temp file in the same dir, fsync, then os.replace()."""
    d = os.path.dirname(STATE) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".strangle_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE)          # atomic on Windows and POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _num(key):
    """Numeric config that cannot crash manage() on a bad override/env value."""
    v = config.cfg(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(config.DEFAULTS.get(key, 0) or 0)


def _nearest(chain, target):
    return min(chain, key=lambda r: abs(r["strike"] - target))


def _max_loss_per_lot(structure, credit_ps, ce, pe, cw, pw, lot):
    """Defined-risk max loss (Rs) of ONE lot of this exact position, computed from its
    real strikes and premiums — not a guessed constant."""
    if structure == "condor" and cw and pw:
        call_w = cw["strike"] - ce["strike"]        # short CE -> long CE spread width
        put_w = pe["strike"] - pw["strike"]         # long PE  -> short PE spread width
        loss_ps = max(max(call_w, put_w) - credit_ps, 0.0)
    else:
        # naked has no defined loss; this branch shouldn't run (ivHighPct = gate ⇒ always
        # condor). If it ever does, price it very conservatively so sizing stays tiny.
        loss_ps = (ce["ce"]["ltp"] + pe["pe"]["ltp"]) * 3
    return loss_ps * lot


def _size_lots(s, structure, credit_ps, ce, pe, cw, pw, lot):
    """Risk-based lots: size so the position's own max loss <= RISK_PER_TRADE_PCT of the
    current account value (capital + cumulative realised P&L). Returns (lots, note)."""
    if str(config.cfg("SIZING_MODE") or "risk") == "fixed":
        return max(1, int(_num("lotsPerTrade"))), "fixed"
    acct = _num("ACCOUNT_CAPITAL") + float(s.get("equity", 0))
    budget = acct * _num("RISK_PER_TRADE_PCT") / 100.0
    per_lot = _max_loss_per_lot(structure, credit_ps, ce, pe, cw, pw, lot)
    if per_lot <= 0:
        return 0, "cannot price risk"
    lots = max(0, min(int(budget // per_lot), int(_num("MAX_LOTS"))))
    return lots, f"budget ₹{budget:.0f} / lot-risk ₹{per_lot:.0f} = {lots} lot(s)"


def _dte(expiry, today):
    return (datetime.strptime(expiry, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days


def _iv_percentile(s, symbol, iv, today):
    """ONE sample per DAY. The old version appended on every 30s poll, so the
    'percentile' compared IV to the last ~4 hours of itself — statistically
    meaningless and NOT the gate that was backtested (which used one reading per
    entry decision). Returns None when history is insufficient -> caller must skip.
    """
    hist = s["iv_hist"].setdefault(symbol, [])
    if iv and iv > 0 and s["iv_day"].get(symbol) != today:
        hist.append(round(iv, 2))
        s["iv_day"][symbol] = today
        if len(hist) > 250:                       # ~1 year of daily samples
            del hist[0]
    need = int(_num("ivMinHistoryDays"))
    if not iv or iv <= 0 or len(hist) < need:
        return None                               # FAIL-CLOSED
    below = sum(1 for x in hist if x < iv)
    return round(below / len(hist) * 100, 1)


class StrangleEngine:
    def __init__(self, connector):
        self.c = connector

    # ---------- entry ----------
    def try_enter(self, symbol):
        s = _load()
        today = _today()
        if s.get("day") != today:                 # daily reset (realized only; equity is cumulative)
            s["day"] = today
            s["realized_pnl"] = 0.0
            _save(s)

        if s.get("halted"):
            return {"skipped": f"HALTED: {s.get('halt_reason', '')}"}
        if not config.cfg("STRANGLE_ENGINE_ENABLED"):
            return {"skipped": "engine disabled"}
        allowed = config.cfg("TRADE_SYMBOLS") or []
        if symbol not in allowed:
            return {"skipped": f"{symbol} not in TRADE_SYMBOLS (backtest validated NIFTY only)"}
        cap = abs(_num("dailyLossCapRs"))
        if s["realized_pnl"] <= -cap:
            return {"skipped": "daily loss cap hit (kill-switch)"}
        if any(p["symbol"] == symbol and p["status"] == "OPEN" for p in s["positions"]):
            return {"skipped": "position already open"}

        meta = config.SYMBOLS[symbol]
        exps = sorted(e for e in self.c.expiries(meta["key"]) if e >= today)
        min_dte = int(_num("minDTE"))
        exps = [e for e in exps if _dte(e, today) >= min_dte]      # D7: no 0-DTE
        if not exps:
            return {"skipped": f"no expiry with DTE >= {min_dte}"}
        expiry = exps[0]

        chain = self.c.option_chain(meta["key"], expiry)
        if not chain:
            return {"skipped": "no chain"}
        spot = chain[0].get("spot") or 0
        if spot <= 0:
            return {"skipped": "bad spot"}                          # F8

        atm = _nearest(chain, spot)
        gce = greeks(spot, atm["strike"], expiry, "CE", atm["ce"]["ltp"])
        gpe = greeks(spot, atm["strike"], expiry, "PE", atm["pe"]["ltp"])
        atm_iv = ((gce.get("iv", 0) or 0) + (gpe.get("iv", 0) or 0)) / 2

        ivp = _iv_percentile(s, symbol, atm_iv, today)
        _save(s)                                                   # persist today's IV sample
        if ivp is None:
            n = len(s["iv_hist"].get(symbol, []))
            return {"skipped": f"IV history {n}/{int(_num('ivMinHistoryDays'))} days — gate not valid yet"}
        if ivp < _num("ivLowPct"):
            return {"skipped": f"IV percentile {ivp} < {_num('ivLowPct')} (premium too cheap)"}

        structure = "condor" if ivp >= _num("ivHighPct") else "naked"
        legd = _num("legDistancePct")
        ce = _nearest(chain, spot * (1 + legd))
        pe = _nearest(chain, spot * (1 - legd))
        lot = meta["lot"]                                           # 1-lot contract size

        legs = [
            {"side": "SELL", "opt": "CE", "strike": ce["strike"], "key": ce["ce"]["key"], "entry": ce["ce"]["ltp"], "qty": lot},
            {"side": "SELL", "opt": "PE", "strike": pe["strike"], "key": pe["pe"]["key"], "entry": pe["pe"]["ltp"], "qty": lot},
        ]
        cw = pw = None
        if structure == "condor":
            wd = _num("wingDistancePct")
            cw = _nearest(chain, spot * (1 + wd))
            pw = _nearest(chain, spot * (1 - wd))
            legs += [
                {"side": "BUY", "opt": "CE", "strike": cw["strike"], "key": cw["ce"]["key"], "entry": cw["ce"]["ltp"], "qty": lot},
                {"side": "BUY", "opt": "PE", "strike": pw["strike"], "key": pw["pe"]["key"], "entry": pw["pe"]["ltp"], "qty": lot},
            ]

        # F8/F9: every leg must have a usable price AND a tradable instrument key.
        bad = [l for l in legs if not l["key"] or not l["entry"] or l["entry"] <= 0]
        if bad:
            return {"skipped": f"rejected: {len(bad)} leg(s) with no key / non-positive price"}
        credit_ps = sum((l["entry"] if l["side"] == "SELL" else -l["entry"]) for l in legs)
        if credit_ps <= 0:
            return {"skipped": "rejected: net credit <= 0 (wings cost more than the shorts)"}

        # ---- REAL sizing: size to the position's OWN defined risk (not a fixed 1 lot) ----
        lots, size_note = _size_lots(s, structure, credit_ps, ce, pe, cw, pw, lot)
        if lots < 1:
            return {"skipped": f"capital too small for one lot's risk — {size_note}"}
        for l in legs:
            l["qty"] = lot * lots
        qty = lot * lots
        credit = round(credit_ps * qty, 1)
        max_loss = round(_max_loss_per_lot(structure, credit_ps, ce, pe, cw, pw, lot) * lots)

        # ---- DECISION COMMITTEE (Opus-5: agents debate -> score -> verdict) ----
        acct_val = _num("ACCOUNT_CAPITAL") + s.get("equity", 0)
        verdict = committee.evaluate({
            "ivp": ivp, "gate": _num("ivLowPct"), "structure": structure,
            "dte": _dte(expiry, today), "min_dte": min_dte, "lots": lots,
            "credit": credit, "max_loss": max_loss,
            "realized": s.get("realized_pnl", 0), "cap": cap,
            "account_value": acct_val,
            "peak": max(s.get("equity_peak", acct_val), acct_val),
            "max_dd_pct": _num("MAX_DRAWDOWN_PCT"),
        })
        s.setdefault("decisions", []).append({
            "time": now_ist().isoformat(timespec="seconds"), "symbol": symbol,
            "structure": structure, "ivp": ivp, "credit": credit, "max_loss": max_loss,
            "verdict": verdict["verdict"], "score": verdict["score"], "checks": verdict["checks"],
        })
        s["decisions"] = s["decisions"][-100:]
        if verdict["verdict"] != "APPROVE":
            _save(s)
            nos = [c["agent"] for c in verdict["checks"] if c["vote"] == "NO"]
            return {"skipped": f"committee REJECT (score {verdict['score']}, failed: {', '.join(nos) or 'score'})",
                    "verdict": verdict}

        for l in legs:
            self.c.place_order(l["key"], l["side"], l["qty"])
        pop = breakeven_pop_strangle(spot, ce["strike"], pe["strike"],
                                     legs[0]["entry"] + legs[1]["entry"],
                                     gce.get("iv", 0), gpe.get("iv", 0), expiry)
        s["seq"] = int(s.get("seq", 0)) + 1                          # unique, monotonic id
        pos = {"id": s["seq"], "symbol": symbol, "expiry": expiry, "dte": _dte(expiry, today),
               "structure": structure, "spot_at_entry": spot, "legs": legs, "credit": round(credit, 1),
               "iv_pct": ivp, "pop": pop, "qty": qty, "lots": lots, "max_loss": max_loss,
               "committee_score": verdict["score"],
               "status": "OPEN", "stale": False,
               "entry_time": now_ist().isoformat(timespec="seconds"), "mtm": 0.0}
        s["positions"].append(pos)
        _save(s)
        return {"entered": pos}

    # ---------- management ----------
    def _close(self, s, p, reason):
        cost = 0.0
        for l in p["legs"]:
            cost += ch.round_trip_charges(l["entry"], l.get("cur", l["entry"]), l["qty"], opened=l["side"])
        net = round(p.get("mtm", 0.0) - cost, 1)
        p["status"] = "CLOSED"
        p["exit_reason"] = reason
        p["exit_time"] = now_ist().isoformat(timespec="seconds")
        p["charges"] = round(cost, 1)
        p["pnl"] = net
        s["realized_pnl"] = round(s.get("realized_pnl", 0) + net, 1)
        s["equity"] = round(s.get("equity", 0) + net, 1)             # cumulative, never reset
        s["closed"].append(p)

    def manage(self):
        s = _load()
        if s.get("halted"):
            return s
        n = now_ist()
        nowmin = n.hour * 60 + n.minute
        sq = str(config.cfg("squareOffIST") or "15:15").split(":")
        try:
            sqmin = int(sq[0]) * 60 + int(sq[1])
        except Exception:
            sqmin = 15 * 60 + 15
        tp = _num("takeProfitPct")
        stopmult = _num("legStopMult")
        cap = abs(_num("dailyLossCapRs"))

        for p in list(s["positions"]):
            if p["status"] != "OPEN":
                continue
            try:                                                    # F6: one bad position must not
                keys = [l["key"] for l in p["legs"] if l["key"]]    #     stop the rest of the book
                prices = self.c.ltp(keys) if keys else {}
            except Exception as e:
                p["stale"] = True
                p["stale_reason"] = f"price feed error: {e}"
                continue

            gross = 0.0
            stale = False
            stop_hit = False
            for l in p["legs"]:
                px = prices.get(l["key"])
                if px is None or px <= 0:
                    # F5/F9: DO NOT pretend cur == entry (that silently disarmed the 2x stop
                    # and made MTM look healthy). Mark it and withhold the decision.
                    l["stale"] = True
                    stale = True
                    px = l.get("cur", l["entry"])                   # last known, for display only
                else:
                    l["stale"] = False
                    l["cur"] = px
                if l["side"] == "SELL":
                    gross += (l["entry"] - px) * l["qty"]
                    if not stale and px >= l["entry"] * stopmult:
                        stop_hit = True
                else:
                    gross += (px - l["entry"]) * l["qty"]
            p["mtm"] = round(gross, 1)
            p["stale"] = stale

            reason = None
            if stale:
                # cannot make a price-based decision on stale data; time-based exit still applies
                if nowmin >= sqmin:
                    reason = "EOD SQUARE-OFF (stale prices)"
            elif stop_hit:
                reason = f"LEG STOP ({stopmult:g}x)"
            elif p["credit"] > 0 and gross >= p["credit"] * tp / 100:
                reason = f"TAKE PROFIT ({tp:g}%)"
            elif nowmin >= sqmin:
                reason = "EOD SQUARE-OFF"
            if reason:
                self._close(s, p, reason)

        s["positions"] = [p for p in s["positions"] if p["status"] == "OPEN"]

        # F4: DAILY kill-switch — counts unrealised too, FLATTENS, then halts.
        # Halt on breach even if the book is ALREADY flat: losses that closed themselves
        # (stops/EOD) can blow the cap, and the halt must still be visible and sticky.
        total = s.get("realized_pnl", 0) + sum(p.get("mtm", 0) for p in s["positions"])
        if cap > 0 and total <= -cap and not s.get("halted"):
            for p in list(s["positions"]):
                self._close(s, p, f"KILL-SWITCH (day P&L {total:.0f} <= -{cap:.0f})")
            s["positions"] = []
            s["halted"] = True
            s["halt_reason"] = f"daily loss cap breached (P&L {total:.0f}) — flattened, manual reset required"

        # PORTFOLIO drawdown guard — the "real engine" limit that outlives one day.
        # Track peak account value; halt if it falls MAX_DRAWDOWN_PCT below that peak.
        acct = _num("ACCOUNT_CAPITAL") + s.get("equity", 0)
        peak = max(s.get("equity_peak", acct), acct)
        s["equity_peak"] = round(peak, 1)
        dd_pct = _num("MAX_DRAWDOWN_PCT")
        if peak > 0 and dd_pct > 0 and acct <= peak * (1 - dd_pct / 100) and not s.get("halted"):
            for p in list(s["positions"]):
                self._close(s, p, "MAX-DRAWDOWN HALT")
            s["positions"] = []
            s["halted"] = True
            dd = (1 - acct / peak) * 100
            s["halt_reason"] = f"account drawdown {dd:.1f}% >= {dd_pct:.0f}% — flattened, manual reset required"

        if len(s["closed"]) > MAX_CLOSED:                            # F11
            s["closed"] = s["closed"][-MAX_CLOSED:]
        _save(s)
        return s

    # ---------- ops ----------
    def resume(self):
        """Human clears a halt (kill-switch / corrupt-state)."""
        s = _load()
        s["halted"] = False
        s["halt_reason"] = ""
        _save(s)
        return {"halted": False}

    def decisions_log(self):
        """Full committee verdict ledger — Opus-5 'every idea challenged' audit trail."""
        s = _load()
        d = s.get("decisions", [])
        appr = sum(1 for x in d if x["verdict"] == "APPROVE")
        return {"count": len(d), "approved": appr, "rejected": len(d) - appr,
                "approval_rate": round(appr / len(d) * 100, 1) if d else 0,
                "decisions": d[::-1]}

    def status(self):
        s = _load()
        open_mtm = sum(p.get("mtm", 0) for p in s["positions"])
        stale = [p["id"] for p in s["positions"] if p.get("stale")]
        return {
            "enabled": config.cfg("STRANGLE_ENGINE_ENABLED"),
            "mode": config.cfg("TRADE_MODE"),
            "symbols": config.cfg("TRADE_SYMBOLS"),
            "halted": s.get("halted", False),
            "halt_reason": s.get("halt_reason", ""),
            "data_stale_positions": stale,
            "open_positions": s["positions"],
            "open_mtm": round(open_mtm, 1),
            "realized_pnl_today": s.get("realized_pnl", 0),
            "equity_cumulative": s.get("equity", 0),
            "iv_history_days": {k: len(v) for k, v in s.get("iv_hist", {}).items()},
            "closed_count": len(s["closed"]),
            "recent_closed": s["closed"][-10:],
            "recent_decisions": s.get("decisions", [])[-8:],
            # real-engine risk view
            "sizing_mode": config.cfg("SIZING_MODE"),
            "account_capital": _num("ACCOUNT_CAPITAL"),
            "account_value": round(_num("ACCOUNT_CAPITAL") + s.get("equity", 0), 1),
            "account_peak": s.get("equity_peak", 0),
            "risk_per_trade_pct": _num("RISK_PER_TRADE_PCT"),
            "max_drawdown_pct": _num("MAX_DRAWDOWN_PCT"),
            "open_max_loss": sum(p.get("max_loss", 0) for p in s["positions"]),
            "ist": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def trades(self):
        """Full closed-trade ledger + P&L analytics, with CHARGES explicitly separated
        (gross = P&L before costs, charges = round-trip fees, net = what actually landed)."""
        s = _load()
        closed = s.get("closed", [])
        rows = []
        for p in closed:
            net = p.get("pnl", 0)                        # already net of charges
            charges = p.get("charges", 0)
            gross = round(net + charges, 1)              # P&L before costs
            rows.append({
                "id": p.get("id"), "symbol": p.get("symbol"), "structure": p.get("structure"),
                "lots": p.get("lots"), "qty": p.get("qty"),
                "entry_time": p.get("entry_time"), "exit_time": p.get("exit_time"),
                "expiry": p.get("expiry"), "iv_pct": p.get("iv_pct"), "pop": p.get("pop"),
                "credit": p.get("credit"), "exit_reason": p.get("exit_reason"),
                "legs": [f"{l['side'][0]}{l['strike']}{l['opt']}" for l in p.get("legs", [])],
                "gross": gross, "charges": charges, "net": net,
            })
        wins = [r for r in rows if r["net"] > 0]
        losses = [r for r in rows if r["net"] < 0]
        gross_sum = round(sum(r["gross"] for r in rows), 1)
        charges_sum = round(sum(r["charges"] for r in rows), 1)
        net_sum = round(sum(r["net"] for r in rows), 1)
        gp = sum(r["net"] for r in wins)
        gl = sum(r["net"] for r in losses)
        # equity curve (cumulative net)
        cum = 0.0
        curve = []
        for r in rows:
            cum = round(cum + r["net"], 1)
            curve.append({"t": r["exit_time"], "equity": cum})
        # which EXIT RULE actually makes money (Opus-5 "review -> optimize" loop)
        by_exit = {}
        for r in rows:
            k = (r["exit_reason"] or "?").split("(")[0].strip()
            b = by_exit.setdefault(k, {"reason": k, "n": 0, "net": 0.0, "wins": 0})
            b["n"] += 1
            b["net"] = round(b["net"] + r["net"], 1)
            b["wins"] += 1 if r["net"] > 0 else 0
        for b in by_exit.values():
            b["win_rate"] = round(b["wins"] / b["n"] * 100) if b["n"] else 0
            b["avg"] = round(b["net"] / b["n"]) if b["n"] else 0

        # OPEN positions count too — the tab used to read 0 while a live trade was running
        open_rows = [{
            "id": p.get("id"), "symbol": p.get("symbol"), "structure": p.get("structure"),
            "lots": p.get("lots"), "credit": p.get("credit"), "iv_pct": p.get("iv_pct"),
            "pop": p.get("pop"), "mtm": p.get("mtm", 0), "max_loss": p.get("max_loss"),
            "entry_time": p.get("entry_time"), "expiry": p.get("expiry"),
            "stale": p.get("stale", False),
            "legs": [f"{l['side'][0]}{l['strike']}{l['opt']}" for l in p.get("legs", [])],
        } for p in s.get("positions", [])]
        open_mtm = round(sum(r["mtm"] for r in open_rows), 1)

        acct_cap = _num("ACCOUNT_CAPITAL")
        acct_val = round(acct_cap + s.get("equity", 0), 1)
        peak = max(s.get("equity_peak", acct_val), acct_val)
        cap = abs(_num("dailyLossCapRs"))
        summary = {
            "count": len(rows),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else 0,
            "gross": gross_sum, "charges": charges_sum, "net": net_sum,
            "charges_pct_of_gross": round(charges_sum / gross_sum * 100, 1) if gross_sum else 0,
            "profit_factor": round(gp / abs(gl), 2) if gl else (999.0 if gp else 0),
            "avg_win": round(gp / len(wins)) if wins else 0,
            "avg_loss": round(gl / len(losses)) if losses else 0,
            "best": max((r["net"] for r in rows), default=0),
            "worst": min((r["net"] for r in rows), default=0),
            "realized_today": s.get("realized_pnl", 0),
            "equity_cumulative": s.get("equity", 0),
            # live view
            "open_count": len(open_rows), "open_mtm": open_mtm,
            "total_pnl": round(net_sum + open_mtm, 1),          # closed + unrealised
        }
        risk = {
            "account_value": acct_val, "account_peak": round(peak, 1),
            "drawdown_pct": round((1 - acct_val / peak) * 100, 1) if peak else 0,
            "max_drawdown_pct": _num("MAX_DRAWDOWN_PCT"),
            "daily_cap": cap, "daily_used": abs(min(s.get("realized_pnl", 0), 0)),
            "daily_room": round(cap + min(s.get("realized_pnl", 0), 0), 1),
            "open_risk": sum(r["max_loss"] or 0 for r in open_rows),
            "open_risk_pct": round(sum(r["max_loss"] or 0 for r in open_rows) / acct_val * 100, 1) if acct_val else 0,
            "halted": s.get("halted", False), "halt_reason": s.get("halt_reason", ""),
        }
        return {"summary": summary, "trades": rows[::-1], "equity_curve": curve,
                "open_positions": open_rows, "by_exit": list(by_exit.values()), "risk": risk}
