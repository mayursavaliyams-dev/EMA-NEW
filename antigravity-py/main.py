"""Antigravity-Py — FastAPI app: health, strangle engine, options analytics, dashboard.
Run:  uvicorn main:app --port 8000   (or: python main.py)
PAPER mode by default. Never places real orders.
"""
import os
import asyncio
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import config
from connectors import UpstoxConnector
from engines import StrangleEngine
from greeks import greeks
import sizing

BASE = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))

conn = UpstoxConnector()
engine = StrangleEngine(conn)
app = FastAPI(title="Antigravity-Py", version="0.1")


def market_open():
    n = datetime.now(IST)
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 9 * 60 + 15 <= m <= 15 * 60 + 30


def analytics(symbol):
    meta = config.SYMBOLS[symbol]
    exps = [e for e in conn.expiries(meta["key"]) if e >= datetime.now(IST).strftime("%Y-%m-%d")]
    if not exps:
        return {"error": "no expiry"}
    expiry = exps[0]
    chain = conn.option_chain(meta["key"], expiry)
    if not chain:
        return {"error": "no chain"}
    spot = chain[0]["spot"]
    atm = min(chain, key=lambda r: abs(r["strike"] - spot))
    tot_ce = sum(r["ce"]["oi"] or 0 for r in chain)
    tot_pe = sum(r["pe"]["oi"] or 0 for r in chain)
    pcr = round(tot_pe / tot_ce, 2) if tot_ce else 0
    oi_res = max(chain, key=lambda r: r["ce"]["oi"] or 0)["strike"]
    oi_sup = max(chain, key=lambda r: r["pe"]["oi"] or 0)["strike"]
    # max pain
    sks = [r["strike"] for r in chain]
    ceoi = {r["strike"]: r["ce"]["oi"] or 0 for r in chain}
    peoi = {r["strike"]: r["pe"]["oi"] or 0 for r in chain}
    mp, best = None, None
    for K in sks:
        pain = sum((K - s) * ceoi[s] for s in sks if s < K) + sum((s - K) * peoi[s] for s in sks if s > K)
        if best is None or pain < best:
            best, mp = pain, K
    return {"symbol": symbol, "expiry": expiry, "spot": spot, "atm": atm["strike"],
            "pcr": pcr, "max_pain": mp, "oi_support": oi_sup, "oi_resistance": oi_res,
            "total_ce_oi": int(tot_ce), "total_pe_oi": int(tot_pe)}


# ---------------- API ----------------
@app.get("/api/health")
def health():
    return {"status": "ok", "connector": conn.name, "mode": config.cfg("TRADE_MODE"),
            "token_valid": conn.authed(), "market_open": market_open(),
            "strangle_enabled": config.cfg("STRANGLE_ENGINE_ENABLED"),
            "ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/strangle/status")
def strangle_status():
    return engine.status()


@app.post("/api/strangle/enable")
def strangle_enable(on: bool = True):
    config.save_overrides({"STRANGLE_ENGINE_ENABLED": bool(on)})
    return {"status": "ok", "STRANGLE_ENGINE_ENABLED": bool(on)}


@app.get("/api/strangle/trades")
def strangle_trades():
    """Full closed-trade ledger + P&L analytics (gross / charges / net)."""
    return engine.trades()


@app.get("/api/committee/decisions")
def committee_decisions():
    """Decision-committee verdict ledger (Opus-5 debate -> score -> approve/reject)."""
    return engine.decisions_log()


@app.post("/api/strangle/resume")
def strangle_resume():
    """Clear a HALT (kill-switch fired, or state file was quarantined).
    Deliberately a manual, human-only action — the engine never self-resumes."""
    return engine.resume()


@app.get("/api/options/analytics")
def options_analytics(symbol: str = "NIFTY"):
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": "unknown symbol"}, status_code=400)
    return analytics(symbol)


@app.get("/api/options/chain")
def options_chain(symbol: str = "NIFTY"):
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": "unknown symbol"}, status_code=400)
    meta = config.SYMBOLS[symbol]
    exps = [e for e in conn.expiries(meta["key"]) if e >= datetime.now(IST).strftime("%Y-%m-%d")]
    expiry = exps[0]
    chain = conn.option_chain(meta["key"], expiry)
    spot = chain[0]["spot"]
    for r in chain:
        r["ce"]["greeks"] = greeks(spot, r["strike"], expiry, "CE", r["ce"]["ltp"])
        r["pe"]["greeks"] = greeks(spot, r["strike"], expiry, "PE", r["pe"]["ltp"])
    return {"symbol": symbol, "expiry": expiry, "spot": spot, "strikes": chain}


@app.get("/api/candles")
def api_candles(symbol: str = "NIFTY", interval: str = "5"):
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": "unknown symbol"}, status_code=400)
    return {"symbol": symbol, "interval": interval,
            "candles": conn.candles(config.SYMBOLS[symbol]["key"], interval)}


@app.get("/api/option_candles")
def api_option_candles(symbol: str = "NIFTY", strike: float = 0, opt: str = "CE", interval: str = "5"):
    """Candlestick history for ONE option contract (a strike's CE or PE) — lets the
    chart show that option's own High/Low, not the index's."""
    symbol = symbol.upper()
    opt = opt.upper()
    if symbol not in config.SYMBOLS or opt not in ("CE", "PE"):
        return JSONResponse({"error": "bad symbol/opt"}, status_code=400)
    meta = config.SYMBOLS[symbol]
    exps = [e for e in conn.expiries(meta["key"]) if e >= datetime.now(IST).strftime("%Y-%m-%d")]
    if not exps:
        return JSONResponse({"error": "no expiry"}, status_code=404)
    expiry = exps[0]
    chain = conn.option_chain(meta["key"], expiry)
    row = min(chain, key=lambda r: abs(r["strike"] - strike)) if chain else None
    if not row:
        return JSONResponse({"error": "no chain"}, status_code=404)
    side = row["ce"] if opt == "CE" else row["pe"]
    key = side.get("key")
    candles = conn.candles(key, interval) if key else []
    hi = max((c["high"] for c in candles), default=None)
    lo = min((c["low"] for c in candles), default=None)
    return {"symbol": symbol, "expiry": expiry, "strike": row["strike"], "opt": opt,
            "instrument_key": key, "ltp": side.get("ltp"), "day_high": hi, "day_low": lo,
            "candles": candles}


@app.get("/api/sizing")
def api_sizing(symbol: str = "NIFTY", capital: float = 100000, structure: str = "condor", ivp: float = 50):
    lots, f, scale = sizing.sized_lots(symbol.upper(), structure, capital, iv_percentile=ivp)
    return {"symbol": symbol.upper(), "capital": capital, "structure": structure,
            "lots": lots, "kelly_fraction": f, "iv_scale": scale,
            "margin_per_lot": sizing.MARGIN.get(symbol.upper(), {}).get(structure)}


# ---------------- engine loop ----------------
async def engine_loop():
    while True:
        try:
            if market_open():
                # only the symbols the backtest actually validated (NIFTY);
                # BANKNIFTY was a net loser and SENSEX was never tested.
                for sym in (config.cfg("TRADE_SYMBOLS") or []):
                    if sym in config.SYMBOLS:
                        await asyncio.to_thread(engine.try_enter, sym)
                await asyncio.to_thread(engine.manage)
        except Exception as e:
            print("[engine] error:", e)
        await asyncio.sleep(30)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(engine_loop())
    print(f"[antigravity-py] up — mode={config.cfg('TRADE_MODE')} "
          f"strangle={config.cfg('STRANGLE_ENGINE_ENABLED')}")


# ---------------- static dashboard ----------------
STATIC = os.path.join(BASE, "static")
os.makedirs(STATIC, exist_ok=True)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
