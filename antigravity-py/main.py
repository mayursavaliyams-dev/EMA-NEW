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
from trackers.news import NewsHub
from trackers.stock_options import StockOptionsTracker, SETTINGS as TRK_SETTINGS

BASE = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))

conn = UpstoxConnector()
engine = StrangleEngine(conn)
news_hub = NewsHub(store_dir=os.path.join(config.DATA, "news"), keep_days=int(config.cfg("trkKeepDays") or 35))
tracker = StockOptionsTracker(conn, news_hub)   # side tracker: display only, no orders
app = FastAPI(title="Antigravity-Py", version="0.1")
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=2000)     # JSON tables compress ~8x


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


# ---------------- stock-options side tracker (display only) ----------------
@app.get("/api/tracker/status")
def tracker_status():
    return tracker.status()


@app.get("/api/tracker/alerts")
def tracker_alerts(status: str = "", symbol: str = "", limit: int = 200, dir: str = "", max_legs: int = 12, date: str = ""):
    """date=YYYY-MM-DD reads that day's saved alerts (kept trkKeepDays, one monthly expiry cycle)."""
    out = tracker.alerts_for_day(date) if date else list(tracker.alerts)
    if status:
        out = [a for a in out if a["status"] == status.upper()]
    if symbol:
        out = [a for a in out if a["symbol"] == symbol.upper()]
    if dir:
        out = [a for a in out if a.get("dir") == dir.upper()]
    res = []
    for a in out[::-1][:limit]:
        if len(a.get("legs") or []) > max_legs:          # keep payloads small: merged alerts can hold many legs
            a = {**a, "legs": a["legs"][:max_legs], "legs_more": len(a["legs"]) - max_legs}
        res.append(a)
    return {"alerts": res}


@app.get("/api/tracker/board")
def tracker_board(symbol: str = "", limit: int = 60, sort: str = "move", expiry: str = "", sign: str = ""):
    """symbol -> full strike ladder (front expiry, or ?expiry=); else top rows by
    sort = move | volume | oi | unusual | iv, optionally sign = UP | DOWN."""
    return {"symbol": symbol.upper() or None,
            "expiries": tracker.expiries(symbol) if symbol else [],
            "rows": tracker.board(symbol or None, min(limit, 500), sort, expiry or None, sign.upper() or None)}


@app.get("/api/tracker/summary")
def tracker_summary():
    """Per-stock totals: CE/PE OI, PCR, OI change, volume, OI support/resistance, max-volume strike."""
    return {"stocks": tracker.summary(max_age=2.0)}


@app.get("/api/tracker/futures")
def tracker_futures():
    """Front-month stock futures: price, basis (premium/discount), OI change, buildup, rollover %."""
    return {"futures": tracker.futures()}


@app.get("/api/tracker/participants")
def tracker_participants():
    """FII / DII / Pro / Client positions (NSE participant-wise OI): net long, change vs previous day, streak."""
    return tracker.extra.participant_view() if tracker.extra else {"date": None, "rows": []}


@app.get("/api/tracker/sectors")
def tracker_sectors():
    """Sector heatmap: average move, up/down stocks, OI change, futures buildup counts."""
    return {"sectors": tracker.sectors()}


@app.get("/api/tracker/deals")
def tracker_deals(fo_only: bool = True):
    """Today's NSE bulk / block deals (F&O stocks only by default)."""
    if not tracker.extra:
        return {"date": None, "deals": []}
    d = tracker.extra.deals
    rows = d["bulk"] + d["block"]
    if fo_only:
        rows = [x for x in rows if x["symbol"] in tracker.u.stocks]
    return {"date": d["date"], "deals": rows}


@app.get("/api/tracker/stock")
def tracker_stock(symbol: str):
    """Everything for one stock: analytics, IV history, delivery, deals, futures."""
    sym = symbol.upper()
    return {"symbol": sym, "analytics": tracker.anal.get(sym, {}),
            "iv": tracker.hist.iv_stats(sym, tracker.anal.get(sym, {}).get("atm_iv")) if tracker.hist else None,
            "extra": tracker.extra.info(sym) if tracker.extra else None, "futures": tracker.fut_row(sym)}


@app.post("/api/tracker/telegram/test")
def tracker_telegram_test():
    if not tracker.tg.enabled():
        return JSONResponse({"error": "telegram.json missing (bot_token + chat_id)"}, status_code=400)
    try:
        tracker.tg._send("✅ Antigravity tracker: Telegram alerts are working.")
        return {"status": "sent"}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


@app.get("/api/tracker/export")
def tracker_export(what: str = "alerts", fmt: str = "csv"):
    """Download alerts | trends | summary | history | futures | sectors | participants as CSV (or xlsx)."""
    import io, csv as _csv, json as _json
    from fastapi.responses import Response
    src = {"alerts": lambda: list(tracker.alerts)[::-1], "trends": lambda: tracker.trend_list(limit=5000),
           "summary": tracker.summary, "futures": tracker.futures, "sectors": tracker.sectors,
           "history": lambda: tracker.hist.stock_table() if tracker.hist else [],
           "participants": lambda: (tracker.extra.participant_view()["rows"] if tracker.extra else [])}
    if what not in src:
        return JSONResponse({"error": f"what must be one of {sorted(src)}"}, status_code=400)
    rows = src[what]() or []
    flat = [{k: (_json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict, set, tuple)) else v)
             for k, v in r.items() if k not in ("news",) or what == "alerts"} for r in rows]
    cols = []
    for r in flat:
        for k in r:
            if k not in cols:
                cols.append(k)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M")
    if fmt == "xlsx":
        try:
            from openpyxl import Workbook
        except ImportError:
            return JSONResponse({"error": "pip install openpyxl for xlsx (CSV works without it)"}, status_code=400)
        wb = Workbook()
        ws = wb.active
        ws.title = what[:30]
        ws.append(cols)
        for r in flat:
            ws.append([r.get(c) for c in cols])
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="tracker_{what}_{stamp}.xlsx"'})
    buf = io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(flat)
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="tracker_{what}_{stamp}.csv"'})


@app.get("/api/tracker/trends")
def tracker_trends(status: str = "", symbol: str = "", limit: int = 300, date: str = ""):
    """Saved trend records: where each trend started, when it was confirmed, why, and how/why it ended."""
    if date:
        out = [t for t in reversed(tracker.trends_for_day(date))
               if (not status or t.get("status") == status.upper()) and (not symbol or t.get("symbol") == symbol.upper())]
        return {"trends": out[:limit]}
    return {"trends": tracker.trend_list(status or None, symbol or None, limit)}


@app.get("/api/tracker/days")
def tracker_days():
    """Days with saved alerts / trends (newest first) and how long they are kept."""
    return {"days": tracker.history_days(), "keep_days": config.cfg("trkKeepDays") or 35}


@app.get("/api/tracker/history")
def tracker_history(window: int = 5):
    """Multi-day (NSE bhavcopy) OI build per stock: 5/20-day OI change per side, streaks, repeated activity."""
    h = tracker.hist
    return {"status": h.status if h else "off", "dates": (h.dates[-1:] if h else []),
            "stocks": h.stock_table(max(2, min(window, 20))) if h else []}


@app.get("/api/tracker/history/stock")
def tracker_history_stock(symbol: str, days: int = 30, n: int = 5):
    """Day-by-day record for one stock + strikes where OI was built most over the last n days."""
    h = tracker.hist
    if not h:
        return JSONResponse({"error": "history off"}, status_code=404)
    sym = symbol.upper()
    return {"symbol": sym, "days": h.stock_record(sym, max(1, min(days, 120))),
            "strikes": h.strike_build(sym, max(1, min(n, 20)), 30)}


@app.get("/api/tracker/candles")
def tracker_candles(key: str = "", symbol: str = "", interval: str = "1"):
    """Candles + session VWAP + volume + OI for one strike (key) or the underlying (symbol)."""
    if interval not in ("1", "3", "5", "15", "25", "30", "60"):
        return JSONResponse({"error": "bad interval"}, status_code=400)
    if symbol and not key:
        s = tracker.u.stocks.get(symbol.upper())
        if not s:
            return JSONResponse({"error": "unknown symbol"}, status_code=404)
        key = s["underlying_key"]
    elif key not in tracker.sel:
        return JSONResponse({"error": "not a tracked strike"}, status_code=404)
    try:
        c = tracker.candles(key, interval)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"[:300]}, status_code=502)
    m = tracker.sel.get(key, {})
    return {"key": key, "meta": m, "live": tracker.latest.get(key, {}), "interval": interval, "candles": c}


@app.get("/api/tracker/symbols")
def tracker_symbols():
    return {"symbols": tracker.symbols()}


_news_busy = set()


def _news_refresh(sym, name):
    import threading
    if sym in _news_busy:
        return
    _news_busy.add(sym)

    def run():
        try:
            news_hub.news_for(sym, name)
        finally:
            _news_busy.discard(sym)
    threading.Thread(target=run, daemon=True).start()


@app.get("/api/tracker/news")
def tracker_news(symbol: str = "", hours: float = 24):
    since = datetime.now(IST).timestamp() - hours * 3600
    if symbol:
        sym = symbol.upper()
        s = tracker.u.stocks.get(sym, {})
        # never block the request on the network: serve the cache, refresh it in the background
        _news_refresh(sym, s.get("name", sym))
        return {"items": news_hub.news_for(sym, s.get("name", sym), since_ts=since, fetch=False)}
    fo = set(tracker.u.stocks) or None
    return {"items": news_hub.latest_nse(fo, limit=300, since_ts=since)}


@app.get("/api/tracker/news/dhan-raw")
def tracker_news_dhan_raw():
    """Last raw Dhan news reply + how it was parsed (to confirm Dhan's real field names)."""
    if not news_hub.dhan_enabled():
        return {"enabled": False, "note": "Dhan news is used only when the tracker runs on Dhan (trkProvider=dhan)"}
    if news_hub.dhan_raw is None:
        news_hub.poll_dhan(force=True)
    raw = news_hub.dhan_raw or {}
    from trackers.news import dhan_news_items, parse_dhan_news
    items = dhan_news_items(raw.get("reply"))
    return {"enabled": True, "polls": news_hub.dhan_polls, "error": news_hub.errors.get("Dhan"),
            "fields_seen": sorted({k for it in items for k in it})[:60], "raw_count": len(items),
            "parsed_sample": parse_dhan_news(raw.get("reply"), tracker._sid_map())[:5],
            "raw_sample": items[:3], "request": raw.get("request"), "at": raw.get("at")}


@app.get("/api/tracker/selfcheck")
def tracker_selfcheck():
    """One-click recheck of everything the tracker depends on. Each row: ok | warn | fail + plain reason."""
    import time as _t
    out = []

    def add(name, state, detail=""):
        out.append({"check": name, "state": state, "detail": str(detail)[:240]})
    st = tracker.status()
    active = st.get("active")
    add("Paper mode (no orders)", "ok" if config.cfg("TRADE_MODE") == "paper" else "fail", config.cfg("TRADE_MODE"))
    add("Tracker on", "ok" if st.get("enabled") else "fail")
    want = str(config.cfg("trkProvider") or "auto")
    add("Data source = Dhan", "ok" if want == "dhan" else "warn", f"setting trkProvider={want}, using {st.get('provider') or '--'}")
    d = tracker.providers.get("dhan")
    if d is not None:
        age = d.token_age_h() if hasattr(d, "token_age_h") else None
        add("Dhan token present", "ok" if d.available() else "fail",
            "token loaded" if d.available() else (st.get("dhan_error") or "no token: run DHAN_TOKEN.bat"))
        if age is not None:
            add("Dhan token age", "ok" if age < 20 else "warn" if age < 24 else "fail",
                f"{age:.1f} h old (renewed automatically at ~20 h; expires ~24 h)")
        add("Dhan feed error", "fail" if st.get("dhan_error") else "ok", st.get("dhan_error") or "none")
    if not active:
        add("Market hours", "warn", "market closed - live checks skipped (TRACKER_TEST_NOW.bat runs them now)")
    else:
        feed = st.get("feed") or ""
        import re as _re
        m = _re.search(r"(\d+)/(\d+) connections", feed)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            add("Live feed connected", "ok" if a == b else "warn" if a else "fail",
                feed + ("" if a == b else " (some connections still connecting / dropped)"))
        else:
            add("Live feed connected", "ok" if "connected" in feed.lower() or "ws" in feed.lower() else "fail", feed)
        t0 = st.get("ticks") or 0
        _t.sleep(3)
        t1 = tracker.status().get("ticks") or 0
        add("Prices arriving (3 s)", "ok" if t1 > t0 else "fail", f"+{t1 - t0} updates in 3 s")
        add("Strikes tracked / live", "ok" if st.get("instruments_live") else "fail",
            f"{st.get('instruments_tracked')} tracked, {st.get('instruments_live')} live, {st.get('stocks_tracked')} stocks")
        add("IV / greeks", "ok" if st.get("greeks") else "warn", f"{st.get('greeks')} strikes")
    h = str(st.get("history") or "")
    add("History (NSE bhavcopy)", "ok" if "trading days" in h else "warn", h or "off")
    ctx = st.get("context") or {}
    add("F&O ban list", "ok" if ctx.get("ban_date") and not ctx.get("errors") else "warn",
        f"{ctx.get('ban_date') or '--'}: {', '.join(ctx.get('ban') or []) or 'none'} {ctx.get('errors') or ''}")
    add("FII/DII, deals, delivery (NSE)", "fail" if st.get("extra_errors") else "ok", st.get("extra_errors") or "no errors")
    ne = st.get("news_errors") or {}
    for src in ("NSE", "Google News", "Dhan"):
        if src == "Dhan" and not news_hub.dhan_enabled():
            add("News: Dhan", "warn", "used only when the tracker runs on Dhan")
            continue
        add(f"News: {src}", "fail" if src in ne else "ok", ne.get(src) or "no errors")
    if news_hub.dhan_enabled():
        add("Dhan news reply", "ok" if news_hub.dhan_polls else "warn", f"{news_hub.dhan_polls} replies so far")
    try:
        probe = os.path.join(config.DATA, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        days = tracker.history_days()
        add("Saving history (35-day files)", "ok", f"{len(days)} day(s) saved, kept {config.cfg('trkKeepDays')} days")
    except Exception as e:
        add("Saving history (35-day files)", "fail", e)
    errs = st.get("errors") or {}
    add("Tracker errors", "fail" if errs else "ok", errs or "none")
    n = {k: sum(1 for x in out if x["state"] == k) for k in ("ok", "warn", "fail")}
    return {"time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), "summary": n, "checks": out}


@app.get("/api/tracker/settings")
def tracker_settings_get():
    return {k: config.cfg(k) for k in TRK_SETTINGS}


@app.post("/api/tracker/settings")
def tracker_settings_set(body: dict):
    bad = [k for k in body if k not in TRK_SETTINGS]
    if bad:
        return JSONResponse({"error": f"unknown settings: {bad}"}, status_code=400)
    clean = {}
    for k, v in body.items():
        d = TRK_SETTINGS[k]
        if k == "trkProvider":
            if str(v).lower() not in ("auto", "dhan", "upstox"):
                return JSONResponse({"error": "trkProvider must be auto | dhan | upstox"}, status_code=400)
            clean[k] = str(v).lower()
            continue
        try:
            clean[k] = bool(v) if isinstance(d, bool) else type(d)(v)
        except (TypeError, ValueError):
            return JSONResponse({"error": f"bad value for {k}"}, status_code=400)
        if not isinstance(d, bool) and clean[k] < 0:
            return JSONResponse({"error": f"{k} must be >= 0"}, status_code=400)
    if "trkBudget" in clean:
        clean["trkBudget"] = min(clean["trkBudget"], 25000)
    if "trkDhanConnections" in clean:
        clean["trkDhanConnections"] = max(1, min(clean["trkDhanConnections"], 5))
    config.save_overrides(clean)
    tracker._t_rebal = 0          # apply budget / window changes on the next pass
    return {"status": "ok", **{k: config.cfg(k) for k in TRK_SETTINGS}}


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
    tracker.start()
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
    # access_log off: the dashboard polls every few seconds; logging each request grows tracker.log by
    # hundreds of MB a week when the tracker runs unattended for days
    uvicorn.run("main:app", host="127.0.0.1", port=8000, access_log=False)
