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

- **Providers** (`trackers/providers.py`, setting `trkProvider` = auto | dhan_rest | dhan | upstox):
  - **Dhan REST** (`dhan_rest`, the `auto` choice when `antigravity-py/dhan.json` =
    `{"client_id": "...", "access_token": "..."}` works): all ~25,000 strikes + 218 stocks + 436
    futures via `POST /marketfeed/quote` (1,000 instruments/request, paced `trkDhanRestPerSec` =
    0.8 calls/s under Dhan's 1/s account cap), full refresh about every 33 s. Uses **0 WebSocket
    connections**, so a trading bot on the same Dhan id keeps all 5 sockets (a 6th would
    disconnect it with error 805). 429/RL001 is retried; an auth/plan error stops it and the
    tracker falls back to Upstox.
  - **Dhan WebSocket** (`dhan`, only when chosen explicitly): 5 WebSocket connections x 5000 = **25,000 strikes live** in
    FULL mode — LTP, VWAP (avg price), volume, OI, OI day high/low, prev OI, OHLC, buy/sell qty.
    Packet layouts follow Dhan's official SDK (dhanhq 2.3.0). An expired/invalid token (codes
    806-809) stops Dhan and the tracker falls back to Upstox.
  - **Upstox** (backup): WebSocket ticks for the 5,000 strikes nearest ATM + REST full quotes for
    all 25,000 (OI, OI hi/lo, VWAP, OHLC, volume), paced to <= 40 calls/min. Upstox gives no
    previous-day OI, so "OI chg" is blank on Upstox.
- **Dhan Pro** (`trackers/dhan_pro.py`, needs the ₹999 Data APIs Pro plan + `dhan.json`; setting
  `trkProEnabled`): news with sentiment every `trkProNewsSec` (60 s, merged into the alert news
  so each alert shows 🟢/🔴), 9 market-mover lists every `trkProMoversSec` (120 s: options/futures
  highest OI, OI gainers/losers, top volume; F&O stock price gainers/losers), and RSI/ADX/EMA20/
  EMA50/ATR/MACD for every F&O stock every `trkProTechMin` (5 min, `trkProTechTf` candle) with an
  UP/DOWN/SIDE trend label. Alerts get a "✓ with trend / ✗ vs trend" badge. <= 2 req/s on the
  Data-API budget; a plan error (DH-902) pauses that feature for 30 min. API: `GET /api/tracker/pro`.
- **Universe:** all 218 F&O stocks, front expiry first (Dhan lists ~27k front-expiry legs, Upstox
  ~12.6k — the rest then comes from the next/far expiry), strikes nearest spot, water-filled.
  Re-centred every 3 min (subscription diffed); masters re-downloaded conditionally; Upstox also
  pulls `/v2/option/contract` near the strike-list edge for intraday new strikes.
- **Per strike:** LTP, VWAP, day high/low + range, volume, Vol/OI, OI, OI day hi/lo, OI change,
  live buildup (Long/Short buildup, Short covering, Long unwinding), IV + delta/gamma/theta/vega
  (`trackers/ivcalc.py`, vectorised, same model as `greeks.py`), bid/ask spread, last-trade
  aggressor, buy/sell qty, **all-time high/low** (since listing) and 5/20-day OI build.
- **Multi-day history** (`trackers/history.py`): official NSE F&O bhavcopy, 100-day lookback,
  slim gzip cache in `data/bhav_slim/`. Per stock: CE/PE OI 5d/20d change, consecutive OI-up days,
  the same activity repeated day after day; per strike: OI built over N days; per contract: ATH/ATL
  with dates. Also supplies previous-day OI on Upstox.
- **Stock futures:** front + next month for all 218 stocks are streamed (436 slots reserved): price,
  basis (premium/discount %), OI change vs previous day, futures buildup label, rollover %. History
  (bhavcopy STF rows) adds futures OI 5/20-day change, OI streak and repeated daily activity.
  Alerts: FUT_BUILDUP (price >= 1 % and OI >= 5 %), BASIS flip (premium <-> discount).
- **Market & EOD data** (`trackers/nse_extra.py`, cached in `data/nse_extra/`): FII/DII/Pro/Client
  participant OI (20 days, net + change + streak), delivery % per stock (vs 5/20-day average), today's
  bulk/block deals, sector map (Nifty 500 list) → sector heatmap. Alerts: DELIVERY (accumulation /
  distribution), DEAL, FII (daily, flags an FII flip).
- **Per-stock analytics:** IV Rank / IV Percentile (ATM IV history rebuilt from 100 days of bhavcopy
  closes) + IV term (next − front), max pain (+ MAXPAIN shift alert), GEX ₹cr per 1 % + gamma-flip
  strike (dealer-convention estimate), 25-delta IV skew, expected move from the ATM straddle (+ EXP_MOVE
  alert when spot breaks the day's implied range).
- **Telegram** (`trackers/notify.py`): create `antigravity-py/telegram.json` = `{"bot_token": "...",
  "chat_id": "..."}` (gitignored); kinds pushed = setting `trkTelegramKinds`; test button in the tab.
- **Export:** `/api/tracker/export?what=alerts|trends|summary|history|futures|sectors|participants&fmt=xlsx|csv`
  (buttons in the tab).
- **Performance** (measured with 25,000 live instruments): Dhan packet parsing 39k → 80k packets/s
  (precompiled structs, no copies, detectors only on changed values); top-strike views 0.8–1.2 s →
  0.06–0.15 s (rank on a cheap key, build rows only for the top N); news never blocks a request
  (cache + background refresh); history incremental with a pickled state (restart 3.9 s, 30-min refresh
  0.6 s instead of 25 s); dashboard refreshes heavy tables every 15 s, news once a minute, never
  overlaps calls; gzip on API responses (~8x smaller).
- **Context** (`trackers/nse_context.py`): F&O ban list, MWPL usage %, corporate-event calendar.
- **Trends** (`trackers/trend.py`): per strike and per stock, on 3-min candles — start = EMA5>EMA13
  + above VWAP + 3 green candles + min move from the swing low; end = close below EMA13/VWAP or 50 %
  give-back. Each record keeps where it started, when it was confirmed, why, candles, green/red,
  one-sided %, max pull-back, and why/when it ended — saved to `data/tracker_trends.json`.
- **Alerts** (saved to `data/tracker_alerts.json`, each with a plain-language *reason* and the
  stock's *first alert of the day*): PRICE, OI, VOLUME burst, UNUSUAL Vol/OI, IV spike, ALL-TIME
  HIGH break, TREND start/end, FUT_BUILDUP, BASIS flip, OI wall shift, PCR shift, ATM straddle expansion, stock opening-range
  breakout, F&O BAN / MWPL. Each is matched to NSE filings + Google News (NEWS-MATCH / UNEXPLAINED /
  NEWS-AFTER) in background threads. All thresholds are in the tab's Settings.
- **Test:** `py test_tracker.py` (offline, 134 checks incl. a WebSocket round-trip with a fake Dhan
  feed, history/ATH/trend/IV/context checks and a 25k-strike throughput check).
- Uses its own WebSocket connection(s): Upstox — 1 (the EMA bot uses another); Dhan — up to 5.

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
