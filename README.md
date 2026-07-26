# EMA + Antigravity-Py — Options Trading Research (paper)

Two **paper-only** trading bots and a validation-first backtest suite.

- **EMA** (`server.py`, `app.js`, `index.html`) — buying dashboard, ATM CE/PE on EMA signals, port 8765.
- **Antigravity-Py** (`antigravity-py/`) — condor **selling** engine (IV-gate, real risk sizing, kill-switch), FastAPI on port 8000.
- **Backtests** (`antigravity-py/backtest/`) — `bt_strangle`, `sweep`, `walkforward`, `significance`, `benchmark` on real NSE bhavcopy.

See [PROJECT.md](PROJECT.md) for the full map, honest validation status, and the go-live gate.

> Paper only. No real-money order path is enabled. Every P&L number is a hypothesis, not a result.
> Secrets (`token.json`, `config.json`), data and logs are gitignored.
