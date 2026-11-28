"""Dhan Pro data for the stock-options side tracker (DISPLAY ONLY — never drives trades).

Needs the Dhan "Data APIs Pro" plan (₹999). Three REST APIs, all fail-soft:

  News       POST /v2/data/newsheadline   latest 50 headlines with sentiment, mapped to the stock
  Movers     POST /v2/data/marketmovers   top OI / OI gainers / losers / volume / price, per segment
  Technical  POST /v2/data/technical      RSI, ADX, EMA 20/50, ATR, MACD per F&O stock (15-min candles)

Pacing: Dhan Data APIs allow 5 req/s and 100,000/day. We use <= 2 req/s here (the price feed is a
different budget: market-quote 1 req/s). Per day at the defaults: news ~400, movers ~1,700,
technical ~16,000 calls.  A plan error (DH-902 "requires a Pro Traders plan") is reported once in
`errors` and the feature backs off for 30 minutes instead of retrying every cycle.
"""
import time
import threading
from datetime import datetime, timezone, timedelta

import httpx

IST = timezone(timedelta(hours=5, minutes=30))

MOVER_SETS = [   # (label, exchangeSegment, instrument, category, needs expiry)
    ("Options · highest OI", "NSE_FNO", "OPTSTK", "HIGHEST_OI", True),
    ("Options · OI gainers", "NSE_FNO", "OPTSTK", "OI_GAINERS", True),
    ("Options · OI losers", "NSE_FNO", "OPTSTK", "OI_LOSERS", True),
    ("Options · top volume", "NSE_FNO", "OPTSTK", "TOP_VOLUME", True),
    ("Futures · OI gainers", "NSE_FNO", "FUTSTK", "OI_GAINERS", True),
    ("Futures · OI losers", "NSE_FNO", "FUTSTK", "OI_LOSERS", True),
    ("Futures · top volume", "NSE_FNO", "FUTSTK", "TOP_VOLUME", True),
    ("Stocks · price gainers", "NSE_EQ", "EQUITY", "PRICE_GAINERS", False),
    ("Stocks · price losers", "NSE_EQ", "EQUITY", "PRICE_LOSERS", False),
]
TECH_IND = ["RSI_14", "ADX_14", "EMA_20", "EMA_50", "ATR_14", "MACD_HIST"]
SENT = {"POSITIVE": 1, "BULLISH": 1, "NEGATIVE": -1, "BEARISH": -1}


def _num(v):
    """Technical values: docs show {"value": x}; accept a bare number or the first numeric field."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("value", "val", "v"):
            if isinstance(v.get(k), (int, float)):
                return float(v[k])
        for x in v.values():
            if isinstance(x, (int, float)):
                return float(x)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trend_label(price, t):
    """UP / DOWN / SIDE from price vs EMA20/EMA50 and ADX strength (ADX >= 20 = trending)."""
    e20, e50, adx = t.get("EMA_20"), t.get("EMA_50"), t.get("ADX_14")
    if not (price and e20 and e50):
        return None
    strong = adx is not None and adx >= 20
    if price > e20 > e50:
        return "UP" if strong else "UP (weak)"
    if price < e20 < e50:
        return "DOWN" if strong else "DOWN (weak)"
    return "SIDE"


class _Pace:
    def __init__(self, per_sec):
        self.gap, self.next, self.lock = 1.0 / per_sec, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


class DhanPro:
    API = "https://api.dhan.co/v2"

    def __init__(self, creds, api=None, per_sec=2.0, keep=400):
        """`creds` = callable returning {"client_id", "access_token"} (reads dhan.json each time)."""
        self.creds = creds
        if api:
            self.API = api
        self._http = httpx.Client(timeout=20)
        self._pace = _Pace(per_sec)
        self._lock = threading.Lock()
        self.keep = keep
        self.news = {}            # id -> item
        self.movers = {}          # label -> {"time", "rows": [...]}
        self.tech = {}            # symbol -> {"time", "tf", indicators..., "trend", "price"}
        self.errors = {}          # feature -> last error
        self.backoff = {}         # feature -> monotonic time until which it is paused
        self.calls = {"news": 0, "movers": 0, "technical": 0}
        self.last = {"news": None, "movers": None, "technical": None}

    # ---------------- plumbing ----------------
    def available(self):
        c = self.creds() or {}
        return bool(c.get("access_token") and c.get("client_id"))

    def _post(self, feature, path, body):
        if time.monotonic() < self.backoff.get(feature, 0):
            raise RuntimeError(self.errors.get(feature) or "paused")
        c = self.creds() or {}
        body = dict(body, dhanClientId=str(c.get("client_id", "")))
        for attempt in range(3):
            self._pace.wait()
            self.calls[feature] += 1
            r = self._http.post(self.API + path, json=body,
                                headers={"access-token": c.get("access_token", ""), "client-id": str(c.get("client_id", "")),
                                         "Content-Type": "application/json", "Accept": "application/json"})
            if r.status_code == 200:
                self.errors.pop(feature, None)
                return r.json()
            txt = r.text[:300]
            if r.status_code == 429 or "RL001" in txt or "RATE_LIMIT" in txt:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (401, 403) or "DH-90" in txt:
                self.errors[feature] = f"HTTP {r.status_code}: {txt[:160]}"
                self.backoff[feature] = time.monotonic() + 1800      # plan/token problem: retry in 30 min
                raise RuntimeError(self.errors[feature])
            raise RuntimeError(f"HTTP {r.status_code}: {txt[:160]}")
        raise RuntimeError("Dhan rate limit (RL001) persisted after retries")

    def _fail(self, feature, e):
        self.errors[feature] = f"{type(e).__name__}: {e}"[:200]

    # ---------------- News ----------------
    @staticmethod
    def parse_news(data, symbols=None):
        out = []
        for n in ((data or {}).get("data") or {}).get("latestNews") or []:
            o = n.get("newsObject") or {}
            sym = (n.get("smSymbol") or n.get("displaySymbol") or "").upper()
            if symbols is not None and sym not in symbols:
                continue
            try:
                dt = datetime.strptime(n.get("publishDate", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            except ValueError:
                continue
            sent = (o.get("overallSentiment") or "NEUTRAL").upper()
            title = o.get("title") or ""
            out.append({"source": "Dhan", "symbol": sym, "id": f"dhan:{sym}:{dt.timestamp():.0f}:{title[:60]}",
                        "time": dt.isoformat(), "ts": dt.timestamp(), "title": title,
                        "summary": (o.get("text") or "")[:400], "url": "",
                        "sentiment": sent, "sent": SENT.get(sent, 0),
                        "category": n.get("category"), "sub": n.get("subCategory"),
                        "stock": n.get("stockName"), "isin": n.get("isinCode")})
        return out

    def poll_news(self, symbols=None):
        try:
            d = self._post("news", "/data/newsheadline", {"categories": ["ALL"], "limit": 50})
        except Exception as e:
            self._fail("news", e)
            return []
        items = self.parse_news(d, symbols)
        cutoff = time.time() - 36 * 3600
        with self._lock:
            for it in items:
                self.news[it["id"]] = it
            old = sorted(self.news, key=lambda k: self.news[k]["ts"])
            for k in old[:max(0, len(old) - self.keep)] + [k for k in old if self.news[k]["ts"] < cutoff]:
                self.news.pop(k, None)
        self.last["news"] = datetime.now(IST).strftime("%H:%M:%S")
        return items

    def news_list(self, symbol=None, since_ts=None, sentiment=None, limit=200):
        with self._lock:
            items = list(self.news.values())
        if symbol:
            items = [i for i in items if i["symbol"] == symbol.upper()]
        if since_ts is not None:
            items = [i for i in items if i["ts"] >= since_ts]
        if sentiment:
            items = [i for i in items if i["sentiment"] == sentiment.upper()]
        items.sort(key=lambda i: -i["ts"])
        return items[:limit]

    # ---------------- Movers ----------------
    @staticmethod
    def parse_movers(data):
        rows = []
        for i, m in enumerate((data or {}).get("data") or []):
            if not isinstance(m, dict):
                continue
            rows.append({"rank": i + 1, "security_id": str(m.get("securityId") or ""),
                         "symbol": m.get("tradingSymbol") or m.get("displayName") or "",
                         "name": m.get("displayName"), "instrument": m.get("instrument"),
                         "expiry": m.get("expiry"), "strike": m.get("strikePrice") or None,
                         "ltp": m.get("ltp"), "chg": m.get("change"), "chg_pct": m.get("changePercent"),
                         "volume": m.get("volume"), "value": m.get("tradedValue"),
                         "oi": m.get("openInterest"), "oi_chg": m.get("openInterestChange"),
                         "oi_chg_pct": m.get("openInterestChangePercent"), "pcr": m.get("putCallRatio") or None,
                         "underlying_ltp": m.get("underlyingLtp") or None,
                         "prem_pct": m.get("premiumDiscountPercent") or None})
        return rows

    def poll_movers(self, expiry=None, fut_expiry=None, limit=25):
        n = 0
        for label, seg, inst, cat, need_exp in MOVER_SETS:
            body = {"exchangeSegment": seg, "instrument": [inst], "category": cat, "limit": limit}
            if need_exp:
                e = fut_expiry if inst == "FUTSTK" else expiry
                if not e:
                    continue
                body["expiry"] = e
            else:
                body["universe"] = "FNO_STOCKS"
            try:
                rows = self.parse_movers(self._post("movers", "/data/marketmovers", body))
            except Exception as e:
                self._fail("movers", e)
                if time.monotonic() < self.backoff.get("movers", 0):
                    break
                continue
            with self._lock:
                self.movers[label] = {"time": datetime.now(IST).strftime("%H:%M:%S"), "rows": rows,
                                      "category": cat, "instrument": inst, "expiry": body.get("expiry")}
            n += 1
        if n:
            self.last["movers"] = datetime.now(IST).strftime("%H:%M:%S")
        return n

    # ---------------- Technical ----------------
    def poll_technical(self, sym_sid, prices, timeframe="15", stop=None):
        """sym_sid = {symbol: dhan security id}; prices = {symbol: ltp}. Returns stocks updated."""
        n = 0
        for sym, sid in sorted(sym_sid.items()):
            if stop and stop():
                break
            try:
                d = self._post("technical", "/data/technical", {"securityId": str(sid), "exchangeSegment": "NSE_EQ",
                                                                "instrument": "EQUITY", "timeframe": str(timeframe),
                                                                "indicators": TECH_IND})
            except Exception as e:
                self._fail("technical", e)
                if time.monotonic() < self.backoff.get("technical", 0):
                    break
                continue
            vals = {k: _num(v) for k, v in ((d or {}).get("data") or {}).items()}
            t = {k: (round(v, 2) if v is not None else None) for k, v in vals.items()}
            px = prices.get(sym)
            t.update({"time": datetime.now(IST).strftime("%H:%M"), "tf": str(timeframe), "price": px,
                      "trend": trend_label(px, t)})
            with self._lock:
                self.tech[sym] = t
            n += 1
        if n:
            self.last["technical"] = datetime.now(IST).strftime("%H:%M:%S")
        return n

    def tech_of(self, symbol):
        return self.tech.get(symbol)

    def status(self):
        return {"available": self.available(), "news": len(self.news), "movers": len(self.movers),
                "technical": len(self.tech), "calls": dict(self.calls), "last": dict(self.last),
                "errors": dict(self.errors)}
