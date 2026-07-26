# Antigravity-Py — Index Options SELLING bot (paper-first)

Premium-selling (short strangle / tail-safe iron condor) on NIFTY/BANKNIFTY/SENSEX.
**PAPER mode** — never places real orders. FastAPI + Upstox data.

## Run
```
cd antigravity-py
python main.py          # or: uvicorn main:app --port 8000
```
Open http://localhost:8000

## Endpoints
- `GET /api/health` — connector, mode, token, market status
- `GET /api/strangle/status` — open positions + MTM + realized P&L
- `POST /api/strangle/enable?on=true|false`
- `GET /api/options/analytics?symbol=NIFTY` — spot, PCR, Max Pain, OI S/R
- `GET /api/options/chain?symbol=NIFTY` — full chain + greeks
- `GET /api/sizing?symbol=NIFTY&capital=100000&structure=condor&ivp=50`

## How it works
- During market hours the engine, per symbol: IV-percentile gate → skip if cheap,
  sell a naked strangle in normal IV, switch to a **defined-risk iron condor** in
  high IV (caps the tail). Per-leg 2x stop, combined take-profit, EOD square-off,
  daily-loss kill-switch. Charges + slippage netted from P&L.
- State persisted in `data/strangle_state.json`; engine on/off in `data/config-overrides.json`.

## Token
`token.json` holds the Upstox access token (read-only Analytics token is fine for
data; orders are paper). Refresh it when expired.

## Rules
- Never go live without explicit authorization. Selling is the edge; directional
  buying is not. Backtests (TODO, need NSE bhavcopy) are daily-resolution — forward-test first.

## TODO (next)
- `backtest/` on real NSE bhavcopy (leaderboard, cost-stress, regime, tail-safe).
- Live order path (only on explicit ask) with margin pre-check + kill-switch.
- Redis (optional) instead of JSON state.
