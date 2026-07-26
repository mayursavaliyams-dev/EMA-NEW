"""Upstox connector (REST via httpx). Live data; paper order stub."""
import urllib.parse
from datetime import datetime, timedelta
import httpx

import config


class UpstoxConnector:
    name = "upstox"

    def __init__(self):
        self._client = httpx.Client(
            base_url=config.UPSTOX, timeout=20,
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        )

    def _h(self):
        return {"Authorization": "Bearer " + (config.access_token() or "")}

    def authed(self):
        # analytics token is read-only (no /user/profile) — test a DATA endpoint instead
        try:
            q = urllib.parse.quote("NSE_INDEX|Nifty 50", safe="")
            r = self._client.get(f"/v2/market-quote/ltp?instrument_key={q}", headers=self._h())
            return r.status_code == 200 and r.json().get("status") == "success"
        except Exception:
            return False

    def ltp(self, instrument_keys):
        q = urllib.parse.quote(",".join(instrument_keys), safe=",")
        r = self._client.get(f"/v2/market-quote/ltp?instrument_key={q}", headers=self._h())
        out = {}
        for _, v in (r.json().get("data", {}) or {}).items():
            out[v.get("instrument_token")] = v.get("last_price")
        return out

    def expiries(self, underlying_key):
        q = urllib.parse.quote(underlying_key, safe="")
        r = self._client.get(f"/v2/option/contract?instrument_key={q}", headers=self._h())
        ex = sorted({c.get("expiry") for c in r.json().get("data", []) if c.get("expiry")})
        return ex

    def option_chain(self, underlying_key, expiry):
        q = urllib.parse.quote(underlying_key, safe="")
        r = self._client.get(
            f"/v2/option/chain?instrument_key={q}&expiry_date={expiry}", headers=self._h())
        rows = r.json().get("data", []) or []
        out = []
        for row in rows:
            ce = row.get("call_options") or {}
            pe = row.get("put_options") or {}
            cmd = ce.get("market_data", {}) or {}
            pmd = pe.get("market_data", {}) or {}
            out.append({
                "strike": row.get("strike_price"),
                "spot": row.get("underlying_spot_price"),
                "ce": {"key": ce.get("instrument_key"), "ltp": cmd.get("ltp", 0),
                       "oi": cmd.get("oi", 0), "volume": cmd.get("volume", 0)},
                "pe": {"key": pe.get("instrument_key"), "ltp": pmd.get("ltp", 0),
                       "oi": pmd.get("oi", 0), "volume": pmd.get("volume", 0)},
            })
        out.sort(key=lambda x: x["strike"])
        return out

    def candles(self, instrument_key, interval="5"):
        """Intraday candles; fall back to last 7 days if market closed/empty."""
        q = urllib.parse.quote(instrument_key, safe="")

        def norm(rows):
            out = []
            for c in rows:
                try:
                    ts = int(datetime.fromisoformat(c[0]).timestamp())
                    out.append({"time": ts, "open": float(c[1]), "high": float(c[2]),
                                "low": float(c[3]), "close": float(c[4])})
                except Exception:
                    pass
            out.sort(key=lambda x: x["time"])
            return out

        r = self._client.get(f"/v3/historical-candle/intraday/{q}/minutes/{interval}", headers=self._h())
        rows = (r.json().get("data", {}) or {}).get("candles", [])
        c = norm(rows)
        if c:
            return c
        to_d = datetime.now().strftime("%Y-%m-%d")
        from_d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        r = self._client.get(f"/v3/historical-candle/{q}/minutes/{interval}/{to_d}/{from_d}", headers=self._h())
        return norm((r.json().get("data", {}) or {}).get("candles", []))

    def place_order(self, instrument_key, side, qty, **kw):
        # PAPER mode: never hit the broker.
        if config.cfg("TRADE_MODE") != "live":
            return {"status": "paper", "instrument_key": instrument_key,
                    "side": side, "qty": qty, "note": "simulated (paper mode)"}
        # LIVE path intentionally NOT implemented — requires explicit user authorization,
        # a trading token, margin pre-check and kill-switch. Refuse by default.
        return {"status": "error", "message": "live order path disabled — not authorized"}
