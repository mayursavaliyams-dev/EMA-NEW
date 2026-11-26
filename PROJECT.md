# Trading System — Single Source of Truth

**Status: PAPER ONLY. No real-money order path is enabled. Do not go live.**

This file is the authoritative map. If anything elsewhere contradicts it, this wins.

---

## 1. What this project actually is

Two **independent** paper-trading bots that share one Upstox data token but do **not**
coordinate with each other:

| Bot | Port | Language | Thesis | Backtested? |
|-----|------|----------|--------|-------------|
| **EMA** (buying) | 8765 | Browser JS (`app.js`) | Buy ATM Call/Put on EMA signals | **No** |
| **Antigravity-Py** (selling) | 8000 | Python / FastAPI | Sell 1%-OTM NIFTY iron condors | Yes (611 days) |

The selling bot is the one with an evidence trail. The buying bot's exits (+13pt / −8pt)
are **assumptions that have never been backtested** — treat every EMA number as unverified.

---

## 2. Honest validation status of the selling edge

Run in order; each is a harder test than the last.

| Test | Command | Result |
|------|---------|--------|
| In-sample backtest | `py -m backtest.sweep NIFTY` | +52,837 @3% slip, PF 1.92 — **but** best-of-60-configs, no correction |
| Buy & hold benchmark | `py -m backtest.benchmark NIFTY` | Beats B&H (+158% vs +4.9%) — **but** the window was a flat market |
| Anchored walk-forward | `py -m backtest.walkforward NIFTY` | OOS PF 1.91, only 15% decay — **edge survives out-of-sample** |
| Bootstrap + permutation | `py -m backtest.significance NIFTY` | **FAILS**: 95% CI includes 0; p = 0.063 |

**Bottom line:** the direction is promising and out-of-sample-robust, but at **n = 48 it is
statistically unproven** (need ~72+ trades). The one element robust across every walk-forward
fold is **"sell 1.0% OTM NIFTY condors"** — the IV-gate itself is not robust. No crisis period
(2020/2008) is in the data, and short-vol dies in crises. **This is why we are still on paper.**

### The finding that overrides all of the above

`py -m backtest.intraday_engine NIFTY` walks the **real 1-minute index path** (197 days,
Sep-2025→Jun-2026) instead of one price per day, and reprices the position every minute:

| model | trades | win | NET | PF | stops |
|---|---|---|---|---|---|
| DAILY (how every backtest here was run) | 43 | 65% | **+₹5,190** | 1.05 | 23 |
| INTRADAY (1-minute path) | 43 | 26% | **−₹41,171** | 0.51 | 32 |

The daily model overstates by **₹46,361 (+113%)** and misses **9 stop events**. The worst
trades all stop at **09:15** — overnight gaps, which a close-to-close model cannot see.
`… NIFTY sens` shows this is **not** an artefact of the vol-spike assumption: with IV frozen
(leverage 0) the intraday result is still **−₹24,449**. It is the path and the stop timing.

**Read this as: a profitable-looking daily backtest of a short-vol strategy is not evidence.
Until a strategy survives at intraday resolution, its daily P&L is a modelling artefact.**

---

## 3. The real engine (`engines/strangle_engine.py`)

Rewritten after a Red-Team / Decision-Council audit that found the **deployed** strategy was
not the **backtested** one (it lost −₹9,597 over 84 paper trades). What the current engine does:

- **Entry gate** — IV percentile sampled **once per day**, needs ≥ 20 days of history, else it
  does not trade. (The old engine sampled every 30s → a meaningless gate → overtrading.)
- **Real position sizing** — each condor is sized so its **own defined max loss** ≤
  `RISK_PER_TRADE_PCT` of account value; scales with cumulative P&L; capped at `MAX_LOTS`;
  refuses to trade if the account is too small for one lot's risk.
- **Two independent risk halts** — daily loss cap (flattens + halts) and a portfolio
  max-drawdown guard (halts if account value falls `MAX_DRAWDOWN_PCT` below its peak).
- **Fail-closed everywhere** — corrupt state is quarantined and the engine halts; a missing
  price marks the leg STALE and withholds the decision (never fakes it); zero/negative-price or
  negative-credit entries are rejected; atomic state writes (temp → fsync → rename).
- **IST-aware clock** for every time decision; monotonic trade ids; capped history.
- Only trades `TRADE_SYMBOLS` (= NIFTY). BANKNIFTY was a net loser; SENSEX untested.

Resume after any halt is **manual and human-only**: `POST /api/strangle/resume`.

---

## 4. What drives decisions vs. what is only shown

**Drives entries/exits:** IV percentile, leg/wing distance, credit, defined max-loss, 2× leg
stop, 50% take-profit, EOD square-off, the two risk halts.

**Display only (NOT used in any decision):** PCR, Max Pain, OI Support/Resistance, PoP. These
are context on the dashboard; they are not alpha. Do not assume the bot acts on them.

---

## 4b. Stock-options side tracker (display only)

`antigravity-py/trackers/` — tab **🔎 Stock Options + News** on :8000. Never places orders and
never feeds the strangle engine.

- **Universe:** all 218 F&O stocks, front expiry, CE+PE nearest spot. Budget 5000 = the Upstox
  v3 `ltpc` per-connection limit, split evenly (~22 legs/stock) with leftovers water-filled.
- **Stays current:** re-centred on spot every 3 min (WS unsub/sub diff); instrument master
  refreshed hourly; when spot nears the last listed strike, `/v2/option/contract` is pulled so
  strikes NSE opens intraday join the same day.
- **Data:** WebSocket ticks (premium) + REST quotes every 30 s (OI/volume, and fallback price).
- **Alerts:** premium ±20 % in 5 min, or OI ±25 % in 15 min (≥ 50 lots) → checked against
  NSE filings + Google News: `NEWS-MATCH` (news in the prior 60 min), `UNEXPLAINED`, or
  `NEWS-AFTER` (news arrived within 60 min after the move). All thresholds editable in the tab.
- **Test:** `py test_tracker.py` (offline, 30 checks).
- Uses a **second** Upstox WebSocket connection (the EMA bot uses the first).

## 5. File map

```
EMA NEW/
├── app.js, index.html, styles.css, server.py   EMA buying bot (:8765)
└── antigravity-py/                              Selling bot (:8000)
    ├── main.py                FastAPI app + 30s engine loop + dashboard
    ├── config.py              all thresholds; config-overrides.json wins; fail-closed defaults
    ├── engines/strangle_engine.py   THE engine (entry, sizing, exits, halts, state)
    ├── connectors/upstox.py   REST data; live order path DISABLED on purpose
    ├── greeks.py  charges.py  sizing.py   BS greeks · Indian F&O charges · (Kelly helper)
    ├── static/index.html      dashboard: TradingView chart, OI heatmap, option chain
    ├── data/strangle_state.json   positions, closed, IV history, equity  (single source)
    └── backtest/              bt_strangle · sweep · sweep_exit · compound · benchmark
                               · walkforward · significance   (+ bhav_fetch, real NSE data)
```

---

## 6. Run book

```bash
# Selling bot
cd antigravity-py && python main.py        # -> http://localhost:8000

# Buying bot
python server.py                            # -> http://localhost:8765/index.html
```

Both die when the launching session ends. For all-day running, launch
`antigravity-py/start_antigravity.bat` yourself in a terminal you keep open.

Key config knobs (`config.py`, or override live in `data/config-overrides.json`):
`ACCOUNT_CAPITAL`, `RISK_PER_TRADE_PCT`, `MAX_LOTS`, `MAX_DRAWDOWN_PCT`, `ivLowPct`,
`legDistancePct`, `TRADE_SYMBOLS`, `STRANGLE_ENGINE_ENABLED`.

---

## 7. The go-live gate (not yet met)

Before a single rupee of real money, ALL of:

1. Significance test passes (95% CI excludes 0) — **currently fails**.
2. ≥ 30 paper trades or 4 weeks forward, within 20% of backtest — **not yet run**.
3. Crisis-period backtest done (2020/2008) — **not yet run**.
4. A human presses the button on every order.

Until then this is a research and paper-trading system, and every P&L number is a
**hypothesis, not a result.**
