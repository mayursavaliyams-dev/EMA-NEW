#!/usr/bin/env python3
"""
EMA Strategy Dashboard - Local Proxy + OAuth + Data Store
=========================================================
Single local server that:
  - Serves the dashboard (index.html / app.js / styles.css)
  - Keeps the Upstox access token SERVER-SIDE (browser never sees it)
  - Handles the Upstox OAuth login flow (/login -> /callback)
  - Proxies real market data:  /api/intraday , /api/ltp
  - Stores every fetched candle snapshot + LTP tick to ./data/

Uses ONLY the Python standard library. Run:  python server.py
Open: http://localhost:5000
"""

import json
import os
import csv
import math
import queue
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from market_feed import MarketFeed
    FEED_AVAILABLE = True
except Exception as _e:
    print("[warn] live feed unavailable:", _e)
    FEED_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")
SESSION_PATH = os.path.join(BASE_DIR, "data", "session.json")
TRADING_TOKEN_PATH = os.path.join(BASE_DIR, "data", "trading_token.json")
REAL_ORDER_LOG = os.path.join(BASE_DIR, "data", "orders_real.csv")

UPSTOX = "https://api.upstox.com"

# Cloudflare on api.upstox.com bans the default "Python-urllib" user-agent
# (error 1010). Send a normal browser-like UA.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

# Symbol -> Upstox instrument key
SYMBOLS = {
    "NIFTY":     {"name": "NIFTY 50",   "key": "NSE_INDEX|Nifty 50"},
    "SENSEX":    {"name": "SENSEX",     "key": "BSE_INDEX|SENSEX"},
    "BANKNIFTY": {"name": "BANK NIFTY", "key": "NSE_INDEX|Nifty Bank"},
    "RELIANCE":  {"name": "RELIANCE",   "key": "NSE_EQ|INE002A01018"},
    "TATASTEEL": {"name": "TATA STEEL", "key": "NSE_EQ|INE081A01020"},
}
# reverse: instrument key -> symbol
KEY_TO_SYMBOL = {v["key"]: s for s, v in SYMBOLS.items()}

# Live WebSocket feed (lazy-started on first SSE subscriber)
FEED = MarketFeed(lambda: access_token(), SYMBOLS) if FEED_AVAILABLE else None

# Files we must never serve to the browser
PROTECTED = {"config.json", "token.json", "server.py"}

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico":  "image/x-icon",
}


# ----------------------------------------------------------------------------
# Config / token helpers
# ----------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_token():
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_token(access_token, expires_at):
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"access_token": access_token, "expires_at": expires_at}, f, indent=2)


def now_epoch():
    return int(datetime.now(timezone.utc).timestamp())


def token_valid():
    t = load_token()
    if not t or not t.get("access_token"):
        return False
    exp = t.get("expires_at", 0)
    return exp == 0 or exp > now_epoch() + 30


def access_token():
    t = load_token()
    return t.get("access_token") if t else None


# ----------------------------------------------------------------------------
# Upstox API calls (server-side, token injected here)
# ----------------------------------------------------------------------------
def upstox_get(path):
    url = UPSTOX + path
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + (access_token() or ""),
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upstox_token_exchange(code):
    cfg = load_config()
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": cfg["api_key"],
        "client_secret": cfg["api_secret"],
        "redirect_uri": cfg["redirect_uri"],
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(
        UPSTOX + "/v2/login/authorization/token",
        data=body,
        headers={
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ----------------------------------------------------------------------------
# Data storage
# ----------------------------------------------------------------------------
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def store_candles(symbol, interval, candles):
    ensure_data_dir()
    path = os.path.join(DATA_DIR, f"{symbol}_{interval}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"symbol": symbol, "interval": interval,
                   "saved_at": now_epoch(), "candles": candles}, f)


_expiry_cache = {}   # symbol -> (day_str, [expiries])

def get_expiries(symbol):
    """Sorted list of upcoming expiry dates for a symbol (cached per day)."""
    today = datetime.now().strftime("%Y-%m-%d")
    cached = _expiry_cache.get(symbol)
    if cached and cached[0] == today:
        return cached[1]
    key = urllib.parse.quote(SYMBOLS[symbol]["key"], safe="")
    data = upstox_get(f"/v2/option/contract?instrument_key={key}")
    exps = sorted({c.get("expiry") for c in data.get("data", []) if c.get("expiry")})
    # keep only today/future
    exps = [e for e in exps if e >= today]
    _expiry_cache[symbol] = (today, exps)
    return exps


def store_option_snapshot(symbol, expiry, snapshot):
    """Append an ATM option snapshot to a per-expiry file (expiry-to-expiry log)."""
    ensure_data_dir()
    odir = os.path.join(DATA_DIR, "options")
    os.makedirs(odir, exist_ok=True)
    path = os.path.join(odir, f"{symbol}_{expiry}.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["epoch", "iso", "symbol", "expiry", "side",
                        "spot", "strike", "instrument_key", "ltp"])
        w.writerow([now_epoch(), datetime.now().isoformat(timespec="seconds"),
                    symbol, expiry, snapshot.get("side"), snapshot.get("spot"),
                    snapshot.get("strike"), snapshot.get("instrument_key"),
                    snapshot.get("ltp")])


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price(S, K, T, r, sigma, is_call):
    if sigma <= 0 or T <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def compute_greeks(spot, strike, expiry_date, side, price, r=0.065):
    """Black-Scholes IV + greeks from market price (Upstox returns greeks=0)."""
    try:
        S = float(spot); K = float(strike); mkt = float(price)
        if S <= 0 or K <= 0 or mkt <= 0:
            return {}
        # time to expiry (years), expiry at 15:30 IST
        exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(hour=15, minute=30)
        now = datetime.now()
        T = max((exp_dt - now).total_seconds() / (365.0 * 24 * 3600), 1e-6)
        is_call = (side == "CE")
        # implied vol via bisection
        lo, hi = 0.001, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if _bs_price(S, K, T, r, mid, is_call) > mkt:
                hi = mid
            else:
                lo = mid
        iv = (lo + hi) / 2
        d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1
        gamma = _norm_pdf(d1) / (S * iv * math.sqrt(T))
        vega = S * _norm_pdf(d1) * math.sqrt(T) / 100.0          # per 1% vol
        theta_y = (-(S * _norm_pdf(d1) * iv) / (2 * math.sqrt(T))
                   - (r * K * math.exp(-r * T) * (_norm_cdf(d2) if is_call else _norm_cdf(-d2)))
                   * (1 if is_call else -1))
        theta = theta_y / 365.0                                   # per day
        return {"iv": round(iv * 100, 2), "delta": round(delta, 3),
                "gamma": round(gamma, 5), "theta": round(theta, 2), "vega": round(vega, 2)}
    except Exception:
        return {}


def load_trading_token():
    """Separate TRADING token (full order permission) — used only for placing orders."""
    if not os.path.exists(TRADING_TOKEN_PATH):
        return None
    try:
        with open(TRADING_TOKEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("access_token")
    except Exception:
        return None


def log_real_order(txn, instrument, qty, status, order_id):
    ensure_data_dir()
    new = not os.path.exists(REAL_ORDER_LOG)
    with open(REAL_ORDER_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["epoch", "iso", "txn", "instrument", "qty", "status", "order_id"])
        w.writerow([now_epoch(), datetime.now().isoformat(timespec="seconds"),
                    txn, instrument, qty, status, order_id])


def store_optionwatch(symbol, expiry, result):
    """Append the ATM CE/PE snapshot each poll -> expiry-to-expiry option log."""
    ensure_data_dir()
    odir = os.path.join(DATA_DIR, "options")
    os.makedirs(odir, exist_ok=True)
    path = os.path.join(odir, f"{symbol}_{expiry}_watch.csv")
    # pick the ATM strike row
    atm = result.get("atm_strike")
    row = next((s for s in result.get("strikes", []) if s["strike"] == atm), None)
    if not row:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["epoch", "iso", "spot", "atm_strike",
                        "ce_ltp", "ce_oi", "ce_vol", "pe_ltp", "pe_oi", "pe_vol"])
        w.writerow([now_epoch(), datetime.now().isoformat(timespec="seconds"),
                    result.get("spot"), atm,
                    row["ce"]["ltp"], row["ce"]["oi"], row["ce"]["volume"],
                    row["pe"]["ltp"], row["pe"]["oi"], row["pe"]["volume"]])


def store_tick(symbol, price):
    ensure_data_dir()
    path = os.path.join(DATA_DIR, "ticks.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["epoch", "iso", "symbol", "price"])
        ts = now_epoch()
        w.writerow([ts, datetime.now().isoformat(timespec="seconds"), symbol, price])


# ----------------------------------------------------------------------------
# Candle normalization
# Upstox candle = [iso_ts, open, high, low, close, volume, oi]  (newest first)
# -> {time(epoch sec), open, high, low, close, volume} ascending
# ----------------------------------------------------------------------------
def intraday_or_history(key_quoted, unit, interval, days=7):
    """Today's intraday candles; if empty (market closed/weekend), fall back to
    the last `days` of historical candles so the chart always has data."""
    data = upstox_get(f"/v3/historical-candle/intraday/{key_quoted}/{unit}/{interval}")
    candles = normalize_candles(data.get("data", {}).get("candles", []))
    if candles:
        return candles
    to_d = datetime.now().strftime("%Y-%m-%d")
    from_d = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    data = upstox_get(f"/v3/historical-candle/{key_quoted}/{unit}/{interval}/{to_d}/{from_d}")
    return normalize_candles(data.get("data", {}).get("candles", []))


def normalize_candles(raw):
    out = []
    for c in raw:
        try:
            ts = int(datetime.fromisoformat(c[0]).timestamp())
        except Exception:
            continue
        out.append({
            "time": ts,
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]) if len(c) > 5 else 0,
        })
    out.sort(key=lambda x: x["time"])
    return out


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "EMAProxy/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    # --- response helpers ---
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Allow other local bots/pages to fetch this API (CORS)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    # --- routing ---
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            # OAuth callback can arrive on ANY registered redirect path
            # (this app registered http://localhost:8765/index.html) — if a
            # 'code' is present, treat the request as the token-exchange step.
            if "code" in qs and path in ("/", "/index.html", "/callback"):
                return self.handle_callback(qs)
            if path == "/api/auth-status":
                return self.handle_auth_status()
            if path == "/api/session/load":
                return self.handle_session_load()
            if path == "/api/trading-status":
                return self.send_json({"status": "ok", "trading_token": bool(load_trading_token())})
            if path == "/api/version":
                v = 0
                for fn in ("index.html", "app.js", "styles.css"):
                    fp = os.path.join(BASE_DIR, fn)
                    if os.path.exists(fp):
                        v = max(v, int(os.path.getmtime(fp)))
                return self.send_json({"status": "ok", "version": v})
            if path == "/api/symbols":
                return self.send_json({"symbols": SYMBOLS})
            if path == "/api/intraday":
                return self.handle_intraday(qs)
            if path == "/api/ltp":
                return self.handle_ltp(qs)
            if path == "/api/stream":
                return self.handle_stream()
            if path == "/api/expiries":
                return self.handle_expiries(qs)
            if path == "/api/atm":
                return self.handle_atm(qs)
            if path == "/api/optionwatch":
                return self.handle_optionwatch(qs)
            if path == "/api/optintraday":
                return self.handle_optintraday(qs)
            if path == "/login":
                return self.handle_login()
            if path == "/callback":
                return self.handle_callback(qs)
            return self.handle_static(path)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            self.send_json({"status": "error", "code": e.code,
                            "message": str(e), "detail": detail}, status=502)
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)}, status=500)

    # --- handlers ---
    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/session/save":
                data = self._read_body()
                ensure_data_dir()
                with open(SESSION_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                return self.send_json({"status": "ok"})
            if parsed.path == "/api/trading-token":
                data = self._read_body()
                tok = (data.get("token") or "").strip()
                if not tok:
                    return self.send_json({"status": "error", "message": "empty token"}, 400)
                ensure_data_dir()
                with open(TRADING_TOKEN_PATH, "w", encoding="utf-8") as f:
                    json.dump({"access_token": tok}, f)
                return self.send_json({"status": "ok"})
            if parsed.path == "/api/order":
                return self.handle_order(self._read_body())
            return self.send_json({"status": "error", "message": "unknown endpoint"}, 404)
        except Exception as e:
            return self.send_json({"status": "error", "message": str(e)}, 500)

    def handle_order(self, body):
        # Place a REAL order via Upstox using the separate TRADING token.
        tt = load_trading_token()
        if not tt:
            return self.send_json({"status": "error", "message": "no trading token — set it first"}, 400)
        instrument = body.get("instrument_key")
        txn = (body.get("transaction_type") or "BUY").upper()
        qty = int(body.get("quantity") or 0)
        if not instrument or qty <= 0:
            return self.send_json({"status": "error", "message": "instrument_key & quantity required"}, 400)

        payload = {
            "quantity": qty,
            "product": body.get("product", "I"),       # I = intraday
            "validity": "DAY",
            "price": 0,
            "tag": "ema-bot",
            "instrument_token": instrument,
            "order_type": body.get("order_type", "MARKET"),
            "transaction_type": txn,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        req = urllib.request.Request(
            UPSTOX + "/v2/order/place",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + tt, "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            ok = result.get("status") == "success"
            oid = (result.get("data") or {}).get("order_id", "")
            log_real_order(txn, instrument, qty, "OK" if ok else "ERR", oid)
            return self.send_json({"status": "ok" if ok else "error", "result": result})
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            log_real_order(txn, instrument, qty, "HTTP%d" % e.code, "")
            return self.send_json({"status": "error", "code": e.code, "detail": detail}, 502)

    def handle_session_load(self):
        if os.path.exists(SESSION_PATH):
            try:
                with open(SESSION_PATH, "r", encoding="utf-8") as f:
                    return self.send_json({"status": "ok", "session": json.load(f)})
            except Exception:
                pass
        return self.send_json({"status": "ok", "session": {"trades": [], "paperOrders": [], "tradeCounter": 0}})

    def handle_auth_status(self):
        t = load_token()
        exp = t.get("expires_at", 0) if t else 0
        valid = token_valid()
        self.send_json({
            "authenticated": valid,
            "expires_at": exp,
            "expires_in": max(0, exp - now_epoch()) if exp else 0,
            "login_url": "/login",
        })

    def handle_intraday(self, qs):
        symbol = (qs.get("symbol", ["NIFTY"])[0]).upper()
        interval = qs.get("interval", ["1"])[0]
        unit = qs.get("unit", ["minutes"])[0]
        if symbol not in SYMBOLS:
            return self.send_json({"status": "error", "message": "unknown symbol"}, 400)
        if not token_valid():
            return self.send_json({"status": "unauthorized", "login_url": "/login"}, 401)

        key = urllib.parse.quote(SYMBOLS[symbol]["key"], safe="")
        candles = intraday_or_history(key, unit, interval)
        store_candles(symbol, interval, candles)
        self.send_json({"status": "ok", "symbol": symbol,
                        "interval": interval, "candles": candles})

    def handle_ltp(self, qs):
        syms = qs.get("symbols", ["NIFTY"])[0].split(",")
        syms = [s.strip().upper() for s in syms if s.strip() in SYMBOLS]
        if not syms:
            return self.send_json({"status": "error", "message": "no valid symbols"}, 400)
        if not token_valid():
            return self.send_json({"status": "unauthorized", "login_url": "/login"}, 401)

        keys = ",".join(SYMBOLS[s]["key"] for s in syms)
        q = urllib.parse.quote(keys, safe=",")
        data = upstox_get(f"/v2/market-quote/ltp?instrument_key={q}")
        prices = {}
        for _, v in data.get("data", {}).items():
            itok = v.get("instrument_token")
            sym = KEY_TO_SYMBOL.get(itok)
            if sym:
                prices[sym] = v.get("last_price")
                store_tick(sym, v.get("last_price"))
        self.send_json({"status": "ok", "prices": prices})

    def handle_expiries(self, qs):
        symbol = (qs.get("symbol", ["NIFTY"])[0]).upper()
        if symbol not in SYMBOLS:
            return self.send_json({"status": "error", "message": "unknown symbol"}, 400)
        if not token_valid():
            return self.send_json({"status": "unauthorized"}, 401)
        self.send_json({"status": "ok", "symbol": symbol,
                        "expiries": get_expiries(symbol)})

    def handle_atm(self, qs):
        symbol = (qs.get("symbol", ["NIFTY"])[0]).upper()
        side = (qs.get("side", ["CE"])[0]).upper()        # CE (call) or PE (put)
        expiry = qs.get("expiry", [None])[0]
        if symbol not in SYMBOLS:
            return self.send_json({"status": "error", "message": "unknown symbol"}, 400)
        if not token_valid():
            return self.send_json({"status": "unauthorized"}, 401)

        key = SYMBOLS[symbol]["key"]
        if not expiry:
            exps = get_expiries(symbol)
            if not exps:
                return self.send_json({"status": "error", "message": "no expiries"}, 502)
            expiry = exps[0]

        q = urllib.parse.quote(key, safe="")
        chain = upstox_get(f"/v2/option/chain?instrument_key={q}&expiry_date={expiry}")
        rows = chain.get("data", [])
        if not rows:
            return self.send_json({"status": "error", "message": "empty chain"}, 502)

        spot = rows[0].get("underlying_spot_price") or 0
        # ATM = strike closest to spot
        atm = min(rows, key=lambda r: abs(r.get("strike_price", 0) - spot))
        leg = atm.get("call_options") if side == "CE" else atm.get("put_options")
        md = (leg or {}).get("market_data", {}) or {}
        result = {
            "status": "ok",
            "symbol": symbol,
            "side": side,
            "expiry": expiry,
            "spot": spot,
            "strike": atm.get("strike_price"),
            "instrument_key": (leg or {}).get("instrument_key"),
            "ltp": md.get("ltp", 0),
            "greeks": compute_greeks(spot, atm.get("strike_price"), expiry, side, md.get("ltp", 0)),
        }
        store_option_snapshot(symbol, expiry, result)
        self.send_json(result)

    def handle_optintraday(self, qs):
        # intraday 1-min candles for ANY instrument key (used for option premium lines)
        key = qs.get("key", [None])[0]
        interval = qs.get("interval", ["1"])[0]
        if not key:
            return self.send_json({"status": "error", "message": "key required"}, 400)
        if not token_valid():
            return self.send_json({"status": "unauthorized"}, 401)
        q = urllib.parse.quote(key, safe="")
        candles = intraday_or_history(q, "minutes", interval)
        self.send_json({"status": "ok", "key": key, "candles": candles})

    def handle_optionwatch(self, qs):
        symbol = (qs.get("symbol", ["NIFTY"])[0]).upper()
        rng = int(qs.get("range", ["3"])[0])
        expiry = qs.get("expiry", [None])[0]
        if symbol not in SYMBOLS:
            return self.send_json({"status": "error", "message": "unknown symbol"}, 400)
        if not token_valid():
            return self.send_json({"status": "unauthorized"}, 401)

        key = SYMBOLS[symbol]["key"]
        if not expiry:
            exps = get_expiries(symbol)
            if not exps:
                return self.send_json({"status": "error", "message": "no expiries"}, 502)
            expiry = exps[0]

        q = urllib.parse.quote(key, safe="")
        chain = upstox_get(f"/v2/option/chain?instrument_key={q}&expiry_date={expiry}")
        rows = sorted(chain.get("data", []), key=lambda r: r.get("strike_price", 0))
        if not rows:
            return self.send_json({"status": "error", "message": "empty chain"}, 502)

        spot = rows[0].get("underlying_spot_price") or 0
        atm_i = min(range(len(rows)), key=lambda i: abs(rows[i].get("strike_price", 0) - spot))
        lo, hi = max(0, atm_i - rng), min(len(rows), atm_i + rng + 1)

        def leg(o):
            md = (o or {}).get("market_data", {}) or {}
            ltp = md.get("ltp", 0) or 0
            close = md.get("close_price", 0) or 0
            chng = round(ltp - close, 2) if close else 0
            pct = round(chng / close * 100, 2) if close else 0
            return {"key": (o or {}).get("instrument_key"), "ltp": ltp,
                    "oi": md.get("oi", 0), "prev_oi": md.get("prev_oi", 0),
                    "volume": md.get("volume", 0), "close": close,
                    "chng": chng, "pct": pct,
                    "bid": md.get("bid_price", 0), "ask": md.get("ask_price", 0)}

        strikes = []
        for r in rows[lo:hi]:
            sk = r.get("strike_price")
            ce = leg(r.get("call_options"))
            pe = leg(r.get("put_options"))
            ce["greeks"] = compute_greeks(spot, sk, expiry, "CE", ce.get("ltp"))
            pe["greeks"] = compute_greeks(spot, sk, expiry, "PE", pe.get("ltp"))
            strikes.append({"strike": sk, "ce": ce, "pe": pe})

        # PCR + OI levels + Max Pain from the FULL chain
        tot_ce = tot_pe = 0.0
        ce_oi = {}; pe_oi = {}
        for r in rows:
            sp = r.get("strike_price")
            c_oi = ((r.get("call_options") or {}).get("market_data", {}) or {}).get("oi", 0) or 0
            p_oi = ((r.get("put_options") or {}).get("market_data", {}) or {}).get("oi", 0) or 0
            ce_oi[sp] = c_oi; pe_oi[sp] = p_oi
            tot_ce += c_oi; tot_pe += p_oi
        pcr = round(tot_pe / tot_ce, 2) if tot_ce else 0

        # OI Resistance = strike with highest CALL OI; Support = highest PUT OI
        oi_res = max(ce_oi, key=ce_oi.get) if ce_oi else None
        oi_sup = max(pe_oi, key=pe_oi.get) if pe_oi else None
        # Max Pain = strike where total option-buyer pain is minimised
        all_sk = sorted(ce_oi)
        max_pain = None; best_pain = None
        for K in all_sk:
            pain = 0.0
            for s in all_sk:
                if s < K:
                    pain += (K - s) * ce_oi[s]      # ITM calls
                elif s > K:
                    pain += (s - K) * pe_oi[s]      # ITM puts
            if best_pain is None or pain < best_pain:
                best_pain = pain; max_pain = K

        today = datetime.now().strftime("%Y-%m-%d")
        result = {"status": "ok", "symbol": symbol, "expiry": expiry,
                  "spot": spot, "is_expiry_day": (expiry == today),
                  "atm_strike": rows[atm_i].get("strike_price"), "strikes": strikes,
                  "pcr": pcr, "total_ce_oi": int(tot_ce), "total_pe_oi": int(tot_pe),
                  "max_pain": max_pain, "oi_support": oi_sup, "oi_resistance": oi_res}
        store_optionwatch(symbol, expiry, result)
        self.send_json(result)

    def handle_stream(self):
        # Server-Sent Events: push live ticks from the Upstox WebSocket feed
        if not FEED_AVAILABLE or FEED is None:
            return self.send_json({"status": "error",
                                   "message": "live feed not available"}, 503)
        if not token_valid():
            return self.send_json({"status": "unauthorized"}, 401)

        FEED.ensure_started()
        q = FEED.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            # initial comment to open the stream
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    data = ("data: " + json.dumps(msg) + "\n\n").encode("utf-8")
                    self.wfile.write(data)
                    self.wfile.flush()
                except queue.Empty:
                    # heartbeat keeps the connection alive
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            FEED.unsubscribe(q)

    def handle_login(self):
        cfg = load_config()
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": cfg["api_key"],
            "redirect_uri": cfg["redirect_uri"],
        })
        self.redirect(UPSTOX + "/v2/login/authorization/dialog?" + params)

    def handle_callback(self, qs):
        code = qs.get("code", [None])[0]
        if not code:
            return self.send_json({"status": "error", "message": "missing code"}, 400)
        cfg = load_config()
        if "PASTE_YOUR" in cfg.get("api_secret", ""):
            return self.send_json({"status": "error",
                "message": "api_secret not set in config.json"}, 400)
        result = upstox_token_exchange(code)
        tok = result.get("access_token")
        if not tok:
            return self.send_json({"status": "error", "detail": result}, 502)
        # Upstox tokens expire next day 03:30 IST; we don't get exact exp here,
        # store 0 (treated as "valid, unknown expiry") -> validated on next call.
        save_token(tok, 0)
        self.redirect("/?login=success")

    def handle_static(self, path):
        rel = path.lstrip("/")
        if rel == "" or rel == "/":
            rel = "index.html"
        if rel in PROTECTED or rel.startswith("data") or ".." in rel:
            self.send_json({"status": "error", "message": "forbidden"}, 403)
            return
        full = os.path.join(BASE_DIR, rel)
        if not os.path.isfile(full):
            self.send_json({"status": "error", "message": "not found", "path": rel}, 404)
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def main():
    cfg = load_config()
    port = cfg.get("port", 5000)
    ensure_data_dir()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 60)
    print(" EMA Strategy Dashboard - Live Upstox Proxy")
    print("=" * 60)
    print(f" Dashboard : http://localhost:{port}")
    print(f" Login     : http://localhost:{port}/login")
    print(f" Token     : {'VALID' if token_valid() else 'MISSING/EXPIRED -> open /login'}")
    print(f" Data dir  : {DATA_DIR}")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
