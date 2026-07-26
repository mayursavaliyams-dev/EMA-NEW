"""Config + persisted engine state (config-overrides applied LAST so env can't
flip engine on/off across restarts)."""
import os
import json

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OVERRIDES = os.path.join(DATA, "config-overrides.json")
TOKEN_PATH = os.path.join(BASE, "token.json")

os.makedirs(DATA, exist_ok=True)

UPSTOX = "https://api.upstox.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# index underlyings (Upstox instrument keys) + lot sizes
SYMBOLS = {
    "NIFTY":     {"key": "NSE_INDEX|Nifty 50",   "lot": 65, "step": 50},
    "BANKNIFTY": {"key": "NSE_INDEX|Nifty Bank",  "lot": 30, "step": 100},
    "SENSEX":    {"key": "BSE_INDEX|SENSEX",       "lot": 20, "step": 100},
}

# --- defaults (engine state). config-overrides.json wins over these. ---
DEFAULTS = {
    "TRADE_MODE": "paper",            # paper | live  (NEVER live without explicit ask)
    # FAIL-CLOSED: if config-overrides.json is corrupt/deleted, overrides fall back to
    # these defaults — so the default MUST be OFF, else a corrupt file silently re-arms the engine.
    "STRANGLE_ENGINE_ENABLED": False,
    "DIRECTIONAL_AUTO": False,        # proven loser — OFF
    # only trade what the backtest actually validated (BANKNIFTY = net loser, SENSEX = untested)
    "TRADE_SYMBOLS": ["NIFTY"],
    "minDTE": 2,                      # never enter with fewer days to expiry (avoids 0-DTE gamma)
    "ivMinHistoryDays": 20,           # daily IV samples needed before the percentile gate is valid
    # strangle params
    "legDistancePct": 0.010,          # sell legs ~1.0% OTM (sweep best PF 1.92, +79% vs 1.5%; closer = more intraday gamma risk)
    "wingDistancePct": 0.03,          # condor wing ~3% OTM (tail-safe; ~2%-wide spread, ~17k max loss/lot)
    "legStopMult": 2.0,               # exit a leg if its premium hits 2x entry
    "takeProfitPct": 50,              # exit when combined premium decays this %
    "ivLowPct": 60,                   # below this IV-percentile -> skip (sweep best gate=60: highest profit factor)
    "ivHighPct": 60,                  # at/above this -> iron condor. =gate(60) so we ALWAYS sell condor, never naked (backtest: naked loses)
    "dailyLossCapRs": 15000,          # kill-switch: block new entries once realised loss hits this today
    "squareOffIST": "15:15",
    # ---- REAL position sizing (was: fixed 1 lot). "risk" sizes each trade so its OWN
    #      max loss <= risk budget; "fixed" falls back to lotsPerTrade. ----
    "SIZING_MODE": "risk",            # risk | fixed
    "ACCOUNT_CAPITAL": 500000,        # paper account base (₹). Sizing scales off capital + cumulative P&L.
    "RISK_PER_TRADE_PCT": 6,          # each condor risks at most this % of account value (~1 NIFTY lot @ 5L)
    "MAX_LOTS": 5,                    # hard cap regardless of what the risk budget allows
    "MAX_DRAWDOWN_PCT": 20,           # portfolio guard: HALT if account value falls this % below its peak
    "lotsPerTrade": 1,                # used only when SIZING_MODE = fixed
}


def load_overrides():
    if os.path.exists(OVERRIDES):
        try:
            with open(OVERRIDES, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_overrides(d):
    cur = load_overrides()
    cur.update(d)
    with open(OVERRIDES, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2)
    return cur


def cfg(key):
    """Effective config: env -> overrides -> default (overrides win for engine flags)."""
    ov = load_overrides()
    if key in ov:
        return ov[key]
    if key in os.environ:
        v = os.environ[key]
        if v.lower() in ("true", "false"):
            return v.lower() == "true"
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return v
    return DEFAULTS.get(key)


def access_token():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("access_token")
    except Exception:
        return None
