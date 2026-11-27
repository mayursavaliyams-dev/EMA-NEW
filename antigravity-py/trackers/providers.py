"""Market-data providers for the stock-options side tracker (DISPLAY ONLY — no orders).

Two interchangeable providers expose the same small interface:

    name, limit                      -> label, max instruments the feed can stream
    load_rows()                      -> normalized option rows for Universe.build()
    spots(underlying_keys)           -> {underlying_key: ltp}
    option_contracts(stock) | None   -> live contract rows (Upstox only: intraday new strikes)
    start_feed(on_data, on_status)   -> background streaming
    set_subscription(keys)           -> diff-subscribe to exactly these keys
    poll(keys)                       -> REST snapshot {key: fields} (Upstox: OI/OHLC/VWAP; Dhan: not needed)
    candles(key, interval)           -> [{time, open, high, low, close, volume, oi}]

`fields` (what on_data receives / poll returns) use one vocabulary:
    ltp ltq ltt vwap volume tbq tsq oi oi_hi oi_lo prev_oi prev_close open high low

DhanProvider   — DhanHQ v2. 5 WebSocket connections x 5000 = 25,000 instruments in FULL
                 mode (LTP, VWAP/avg price, volume, OI, OI day high/low, OHLC, buy/sell qty).
                 Packet layouts follow Dhan's official Python SDK (dhanhq 2.3.0, marketfeed.py).
UpstoxProvider — Upstox v3 (backup). 1 connection x 5000, 'ltpc' ticks + REST full quotes.
"""
import os
import csv
import gzip
import json
import time
import struct
import asyncio
import threading
import urllib.parse
from datetime import datetime, timezone, timedelta

import httpx

import config

IST = timezone(timedelta(hours=5, minutes=30))
DHAN_CRED_PATH = os.path.join(config.BASE, "dhan.json")


def _exp_ms(date_str):
    d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST)
    return int(d.timestamp() * 1000)


def _download(url, path, max_age, timeout=120):
    """Conditional download (If-Modified-Since) — cheap when the file hasn't changed."""
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
        return
    headers = {"User-Agent": config.USER_AGENT}
    if os.path.exists(path):
        headers["If-Modified-Since"] = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                                     time.gmtime(os.path.getmtime(path)))
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True, headers=headers)
        if r.status_code == 304:
            os.utime(path, None)
            return
        r.raise_for_status()
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, path)
    except Exception as e:
        if not os.path.exists(path):
            raise RuntimeError(f"instrument master download failed ({url}): {e}")


class _RateLimit:
    def __init__(self, per_sec):
        self.gap = 1.0 / per_sec
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


def vwap_series(candles):
    """Session VWAP from candles (resets each IST day). Adds 'vwap' to each candle."""
    day, pv, vv = None, 0.0, 0.0
    for c in candles:
        d = datetime.fromtimestamp(c["time"], IST).date()
        if d != day:
            day, pv, vv = d, 0.0, 0.0
        v = c.get("volume") or 0
        pv += (c["high"] + c["low"] + c["close"]) / 3 * v
        vv += v
        c["vwap"] = round(pv / vv, 2) if vv else None
    return candles


# ======================================================================
# Dhan
# ======================================================================
class DhanProvider:
    name = "dhan"
    API = "https://api.dhan.co/v2"
    WSS = "wss://api-feed.dhan.co"
    MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    MASTER_PATH = os.path.join(config.DATA, "dhan_master_detailed.csv")
    PER_CONN = 5000
    SEG_NAME = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ",
                5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO"}
    # response code -> (packet size, struct format)  — from dhanhq SDK marketfeed.py
    PKT = {2: (16, "<BHBIfI"),                       # ticker: ltp, ltt
           4: (50, "<BHBIfHIfIIIffff"),              # quote
           5: (12, "<BHBII"),                        # OI
           6: (16, "<BHBIfI"),                       # prev close: prev_close, prev_oi
           7: (8, "<BHBI"),                          # market status
           8: (162, "<BHBIfHIfIIIIIIffff100s"),      # full
           50: (10, "<BHBIH")}                       # server disconnect (reason code)
    DISCONNECT = {805: "too many WebSocket connections (Dhan allows 5)",
                  806: "Data API subscription not active on Dhan",
                  807: "Dhan access token expired", 808: "invalid Dhan client id",
                  809: "Dhan authentication failed"}

    def __init__(self, connections=5, creds=None, wss=None, api=None):
        self.n_conn = max(1, min(5, int(connections)))
        self.limit = self.n_conn * self.PER_CONN
        self._creds = creds
        if wss:
            self.WSS = wss
        if api:
            self.API = api
        self._http = httpx.Client(timeout=20)
        self._rl_quote = _RateLimit(1)       # Dhan: market-quote APIs 1 req/sec
        self._rl_data = _RateLimit(4)        # charts etc. (kept under 5/sec)
        self._rl_news = _RateLimit(1)        # news headlines (limit undocumented: stay gentle)
        self.on_data = self.on_status = None
        self.conn_keys = [set() for _ in range(self.n_conn)]
        self.key_conn = {}
        self._queues = [None] * self.n_conn
        self._loop = None
        self.conn_state = ["idle"] * self.n_conn
        self.fatal = None                    # set on auth-type disconnects -> fall back to Upstox
        self._lock = threading.Lock()

    # ---------------- credentials ----------------
    def creds(self):
        if self._creds:
            return self._creds
        c = {}
        try:
            with open(DHAN_CRED_PATH, "r", encoding="utf-8") as f:
                c = json.load(f)
        except Exception:
            pass
        c.setdefault("client_id", os.environ.get("DHAN_CLIENT_ID", ""))
        c.setdefault("access_token", os.environ.get("DHAN_ACCESS_TOKEN", ""))
        if c.get("access_token") and not c.get("client_id"):
            try:   # client id is not secret; derive it from the token via /profile
                r = self._http.get(self.API + "/profile", headers={"access-token": c["access_token"],
                                                                   "Accept": "application/json"})
                c["client_id"] = str(r.json().get("dhanClientId") or "")
            except Exception:
                pass
        return c

    def available(self):
        c = self.creds()
        return bool(c.get("access_token") and c.get("client_id")) and not self.fatal

    # ---------------- token renewal (Dhan tokens live ~24 h) ----------------
    @staticmethod
    def _token_from(reply):
        """New token / expiry from a RenewToken reply (field names undocumented: accept the likely ones)."""
        d = reply if isinstance(reply, dict) else {}
        for _ in range(2):
            tok = d.get("accessToken") or d.get("access_token") or d.get("token")
            if tok:
                return str(tok), d.get("expiryTime") or d.get("expiry") or d.get("tokenExpiry")
            d = d.get("data") if isinstance(d.get("data"), dict) else {}
        return None, None

    def token_age_h(self, path=None):
        path = path or DHAN_CRED_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f).get("saved_at")
            if saved:
                return (time.time() - float(saved)) / 3600
            return (time.time() - os.path.getmtime(path)) / 3600
        except Exception:
            return None

    def renew(self, path=None):
        """GET /v2/RenewToken (DhanHQ SDK DhanLogin.renew_token). Saves the new token to dhan.json.
        Only renews the tracker's own token; never creates a new login (that could log out a live bot).
        Returns (ok, message) — message never contains the token."""
        path = path or DHAN_CRED_PATH
        c = self.creds()
        if not c.get("access_token") or not c.get("client_id"):
            return False, "no Dhan token"
        try:
            r = self._http.get(self.API + "/RenewToken", headers={"access-token": c["access_token"],
                                                                  "dhanClientId": str(c["client_id"]),
                                                                  "Accept": "application/json"})
        except Exception as e:
            return False, f"renew failed: {type(e).__name__}"
        if r.status_code != 200:
            return False, f"renew refused (HTTP {r.status_code})"
        try:
            tok, exp = self._token_from(r.json())
        except Exception:
            tok, exp = None, None
        if not tok:
            return False, "renew reply had no token (field names differ?)"
        if self._creds:
            self._creds = dict(self._creds, access_token=tok)
        else:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"client_id": str(c["client_id"]), "access_token": tok, "saved_at": time.time(),
                           "expiry": exp}, f)
            os.replace(tmp, path)
        if self.fatal and "expired" in str(self.fatal):
            self.fatal = None
        return True, f"token renewed{(' until ' + str(exp)) if exp else ''}"

    def auto_ok(self):
        """May trkProvider=auto pick Dhan? Only with explicit creds (dhan.json or passed in).
        DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN env vars alone are NOT enough: a live bot on the same
        account may own them and all 5 Dhan WS connections (a 6th -> error 805 kicks the bot)."""
        if self._creds:
            return self.available()
        try:
            with open(DHAN_CRED_PATH, "r", encoding="utf-8") as f:
                has_file = bool(json.load(f).get("access_token"))
        except Exception:
            has_file = False
        return has_file and self.available()

    def _h(self):
        c = self.creds()
        return {"access-token": c.get("access_token", ""), "client-id": str(c.get("client_id", "")),
                "Content-Type": "application/json", "Accept": "application/json"}

    def _post(self, path, body, rl):
        rl.wait()
        r = self._http.post(self.API + path, headers=self._h(), json=body)
        if r.status_code != 200:
            raise RuntimeError(f"Dhan {path} HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    # ---------------- instruments ----------------
    def load_rows(self, path=None):
        path = path or self.MASTER_PATH
        if path == self.MASTER_PATH:
            _download(self.MASTER_URL, path, max_age=3 * 3600)
        names, rows, futs = {}, [], []
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                if r.get("EXCH_ID") != "NSE":
                    continue
                inst = r.get("INSTRUMENT")
                if inst == "EQUITY":
                    names[r["SECURITY_ID"]] = r.get("SYMBOL_NAME") or r.get("DISPLAY_NAME")
                elif inst == "FUTSTK":
                    try:
                        futs.append({"symbol": r["UNDERLYING_SYMBOL"], "underlying_key": "NSE_EQ|" + r["UNDERLYING_SECURITY_ID"],
                                     "lot": int(float(r.get("LOT_SIZE") or 0)) or None,
                                     "expiry_ms": _exp_ms(r["SM_EXPIRY_DATE"]), "key": "NSE_FNO|" + r["SECURITY_ID"],
                                     "ts": r.get("DISPLAY_NAME")})
                    except (KeyError, ValueError):
                        continue
                elif inst == "OPTSTK" and r.get("OPTION_TYPE") in ("CE", "PE"):
                    try:
                        rows.append({"symbol": r["UNDERLYING_SYMBOL"],
                                     "underlying_key": "NSE_EQ|" + r["UNDERLYING_SECURITY_ID"],
                                     "lot": int(float(r.get("LOT_SIZE") or 0)) or None,
                                     "expiry_ms": _exp_ms(r["SM_EXPIRY_DATE"]),
                                     "strike": float(r["STRIKE_PRICE"]), "opt": r["OPTION_TYPE"],
                                     "key": "NSE_FNO|" + r["SECURITY_ID"], "ts": r.get("DISPLAY_NAME"),
                                     "_uid": r["UNDERLYING_SECURITY_ID"]})
                    except (KeyError, ValueError):
                        continue
        for x in rows:
            x["name"] = names.get(x.pop("_uid")) or x["symbol"]
        self.fut_rows = futs
        return rows

    def load_futures(self):
        """Stock futures rows {symbol, underlying_key, key, expiry_ms, lot, ts} (from the last master read)."""
        if getattr(self, "fut_rows", None) is None:
            self.load_rows()
        return self.fut_rows

    # ---------------- REST ----------------
    def spots(self, underlying_keys):
        out = {}
        ids = [k.split("|", 1)[1] for k in underlying_keys if k.startswith("NSE_EQ|")]
        for i in range(0, len(ids), 1000):
            d = self._post("/marketfeed/ltp", {"NSE_EQ": [int(x) for x in ids[i:i + 1000]]}, self._rl_quote)
            for sid, v in ((d.get("data") or {}).get("NSE_EQ") or {}).items():
                if v.get("last_price"):
                    out["NSE_EQ|" + str(sid)] = float(v["last_price"])
        return out

    def news(self, body):
        """Dhan News Headlines (Pro Traders data plan): POST /data/newsheadline, list in data.latestNews."""
        c = self.creds()
        return self._post("/data/newsheadline", dict(body, dhanClientId=str(c.get("client_id", ""))), self._rl_news)

    def option_contracts(self, stock):
        return None              # new strikes arrive via the (conditionally re-downloaded) master

    def poll(self, keys):
        return {}                # FULL packets already carry OI / OHLC / VWAP / volume

    def candles(self, key, interval="1", days=4):
        seg, sid = key.split("|", 1)
        inst = {"NSE_FNO": "OPTSTK", "NSE_EQ": "EQUITY"}.get(seg, "OPTSTK")
        today = datetime.now(IST).date()
        body = {"securityId": sid, "exchangeSegment": seg, "instrument": inst,
                "interval": str(interval), "oi": seg == "NSE_FNO",
                "fromDate": (today - timedelta(days=days + 3)).strftime("%Y-%m-%d"),
                "toDate": (today + timedelta(days=1)).strftime("%Y-%m-%d")}
        d = self._post("/charts/intraday", body, self._rl_data)
        d = d.get("data", d)
        ts = d.get("timestamp") or d.get("start_Time") or []
        oi = d.get("open_interest") or d.get("oi") or [None] * len(ts)
        out = []
        for i, t in enumerate(ts):
            try:
                out.append({"time": int(t), "open": float(d["open"][i]), "high": float(d["high"][i]),
                            "low": float(d["low"][i]), "close": float(d["close"][i]),
                            "volume": float(d["volume"][i]) if d.get("volume") else 0,
                            "oi": oi[i] if i < len(oi) else None})
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return vwap_series(out)

    # ---------------- WebSocket feed (5 connections) ----------------
    def start_feed(self, on_data, on_status):
        self.on_data, self.on_status = on_data, on_status
        threading.Thread(target=lambda: asyncio.run(self._main()), name="dhan-feed", daemon=True).start()

    def set_subscription(self, keys):
        """Diff to exactly `keys`; new keys go to the least-loaded connection."""
        keys = set(list(keys)[:self.limit])
        with self._lock:
            gone = set(self.key_conn) - keys
            new = keys - set(self.key_conn)
            unsub = [[] for _ in range(self.n_conn)]
            sub = [[] for _ in range(self.n_conn)]
            for k in gone:
                i = self.key_conn.pop(k)
                self.conn_keys[i].discard(k)
                unsub[i].append(k)
            for k in sorted(new):
                i = min(range(self.n_conn), key=lambda j: len(self.conn_keys[j]))
                self.conn_keys[i].add(k)
                self.key_conn[k] = i
                sub[i].append(k)
        for i in range(self.n_conn):
            if unsub[i]:
                self._cmd(i, 22, unsub[i])      # unsubscribe FULL = 21 + 1
            if sub[i]:
                self._cmd(i, 21, sub[i])
        return {"added": len(new), "removed": len(gone)}

    def _cmd(self, i, code, keys):
        if self._loop and self._queues[i] is not None:
            self._loop.call_soon_threadsafe(self._queues[i].put_nowait, (code, keys))

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._queues = [asyncio.Queue() for _ in range(self.n_conn)]
        await asyncio.gather(*(self._conn(i) for i in range(self.n_conn)))

    @staticmethod
    def _msgs(code, keys):
        for j in range(0, len(keys), 100):           # Dhan: 100 instruments per message
            batch = keys[j:j + 100]
            yield json.dumps({"RequestCode": code, "InstrumentCount": len(batch),
                              "InstrumentList": [{"ExchangeSegment": k.split("|", 1)[0],
                                                  "SecurityId": k.split("|", 1)[1]} for k in batch]})

    async def _conn(self, i):
        import websockets
        q = self._queues[i]
        while True:
            if self.fatal or not self.conn_keys[i]:
                self.conn_state[i] = "idle" if not self.fatal else "stopped"
                await asyncio.sleep(3)
                continue
            c = self.creds()
            url = (f"{self.WSS}?version=2&token={urllib.parse.quote(c.get('access_token', ''))}"
                   f"&clientId={urllib.parse.quote(str(c.get('client_id', '')))}&authType=2")
            try:
                async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
                    while not q.empty():                       # full re-subscribe covers queued diffs
                        q.get_nowait()
                    for m in self._msgs(21, sorted(self.conn_keys[i])):
                        await ws.send(m)
                    self.conn_state[i] = f"connected ({len(self.conn_keys[i])})"
                    self._status()
                    recv = asyncio.ensure_future(ws.recv())
                    cmd = asyncio.ensure_future(q.get())
                    while True:
                        done, _ = await asyncio.wait({recv, cmd}, return_when=asyncio.FIRST_COMPLETED)
                        if recv in done:
                            msg = recv.result()
                            if isinstance(msg, (bytes, bytearray)):
                                self.parse(msg)
                            recv = asyncio.ensure_future(ws.recv())
                        if cmd in done:
                            code, keys = cmd.result()
                            for m in self._msgs(code, keys):
                                await ws.send(m)
                            self.conn_state[i] = f"connected ({len(self.conn_keys[i])})"
                            cmd = asyncio.ensure_future(q.get())
                        if self.fatal:
                            break
                    recv.cancel()
                    cmd.cancel()
            except Exception as e:
                self.conn_state[i] = f"reconnecting: {type(e).__name__}: {str(e)[:80]}"
                self._status()
                await asyncio.sleep(5)

    def _status(self):
        if self.on_status:
            self.on_status(self.status_text())

    def status_text(self):
        if self.fatal:
            return f"Dhan stopped: {self.fatal}"
        live = sum(1 for s in self.conn_state if s.startswith("connected"))
        return f"Dhan {live}/{self.n_conn} connections · {len(self.key_conn)} subscribed"

    # precompiled packet layouts (hot path: thousands of packets / second)
    _S_TICK = struct.Struct("<BHBIfI")
    _S_QUOTE = struct.Struct("<BHBIfHIfIIIffff")
    _S_OI = struct.Struct("<BHBII")
    _S_FULL = struct.Struct("<BHBIfHIfIIIIIIffff")
    _S_DEPTH = struct.Struct("<" + "IIHHff" * 5)
    _S_DISC = struct.Struct("<BHBIH")
    _SIZE = {2: 16, 4: 50, 5: 12, 6: 16, 7: 8, 8: 162, 50: 10, 3: 112}

    @classmethod
    def _depth(cls, raw, off=0):
        """5 levels of <IIHHff (bid_qty, ask_qty, bid_orders, ask_orders, bid_price, ask_price)."""
        d = cls._S_DEPTH.unpack_from(raw, off)
        b, a = d[4], d[5]
        return {"bid": round(b, 2) if b > 0 else None, "ask": round(a, 2) if a > 0 else None,
                "bid_qty": d[0], "ask_qty": d[1],
                "depth_bid_qty": d[0] + d[6] + d[12] + d[18] + d[24], "depth_ask_qty": d[1] + d[7] + d[13] + d[19] + d[25]}

    def parse(self, buf):
        """Parse one WS message (may hold several packets). Returns list of (key, fields).
        Optimised: precompiled structs, unpack_from on the buffer (no copies), rounding only prices."""
        out, off, n = [], 0, len(buf)
        now = time.time()
        seg = self.SEG_NAME
        cb = self.on_data
        sizes = self._SIZE
        while off + 8 <= n:
            code = buf[off]
            size = sizes.get(code)
            if size is None or off + size > n:
                break
            if code == 8:
                u = self._S_FULL.unpack_from(buf, off)
                f = {"ltp": round(u[4], 2), "ltq": u[5], "ltt": u[6], "vwap": round(u[7], 2), "volume": u[8], "tsq": u[9],
                     "tbq": u[10], "oi": u[11], "oi_hi": u[12], "oi_lo": u[13], "open": round(u[14], 2),
                     "prev_close": round(u[15], 2), "high": round(u[16], 2), "low": round(u[17], 2)}
                d = self._depth(buf, off + 62)
                if d["bid"] is None:
                    del d["bid"]
                if d["ask"] is None:
                    del d["ask"]
                f.update(d)
            elif code == 2:
                u = self._S_TICK.unpack_from(buf, off)
                f = {"ltp": round(u[4], 2), "ltt": u[5]}
            elif code == 4:
                u = self._S_QUOTE.unpack_from(buf, off)
                f = {"ltp": round(u[4], 2), "ltq": u[5], "ltt": u[6], "vwap": round(u[7], 2), "volume": u[8], "tsq": u[9],
                     "tbq": u[10], "open": round(u[11], 2), "prev_close": round(u[12], 2), "high": round(u[13], 2),
                     "low": round(u[14], 2)}
            elif code == 5:
                u = self._S_OI.unpack_from(buf, off)
                f = {"oi": u[4]}
            elif code == 6:
                u = self._S_TICK.unpack_from(buf, off)
                f = {"prev_close": round(u[4], 2), "prev_oi": u[5]}
            elif code == 50:
                u = self._S_DISC.unpack_from(buf, off)
                off += size
                reason = self.DISCONNECT.get(u[4], f"server disconnect code {u[4]}")
                if u[4] in (805, 806, 807, 808, 809):
                    # 805 = Dhan's connection limit: stop instead of reconnecting, otherwise the
                    # tracker and a live bot on the same account keep kicking each other off.
                    self.fatal = reason
                self._status()
                continue
            else:
                off += size
                continue
            off += size
            key = f"{seg.get(u[2], u[2])}|{u[3]}"
            out.append((key, f))
            if cb:
                cb(key, f, now)
        return out


# ======================================================================
# Upstox (backup)
# ======================================================================
class UpstoxProvider:
    name = "upstox"
    limit = 5000
    MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    MASTER_PATH = os.path.join(config.DATA, "instruments_NSE.json.gz")

    def __init__(self, conn):
        self.conn = conn
        self.on_data = self.on_status = None
        self.sub = set()
        self._loop = self._q = None
        self.state = "idle"
        self.fatal = None

    def available(self):
        return bool(config.access_token())

    def load_rows(self, path=None, raw=None):
        if raw is None:
            path = path or self.MASTER_PATH
            if path == self.MASTER_PATH:
                _download(self.MASTER_URL, path, max_age=3600)
            with gzip.open(path, "rt", encoding="utf-8") as f:
                raw = json.load(f)
        names = {r["instrument_key"]: r.get("name") for r in raw if r.get("segment") == "NSE_EQ"}
        rows = []
        self.fut_rows = [{"symbol": r["underlying_symbol"], "underlying_key": r["underlying_key"], "lot": r.get("lot_size"),
                          "expiry_ms": int(r.get("expiry") or 0), "key": r["instrument_key"], "ts": r.get("trading_symbol")}
                         for r in raw if r.get("segment") == "NSE_FO" and r.get("instrument_type") == "FUT"
                         and r.get("underlying_type") == "EQUITY"]
        for r in raw:
            if (r.get("segment") != "NSE_FO" or r.get("instrument_type") not in ("CE", "PE")
                    or r.get("underlying_type") != "EQUITY"):
                continue
            rows.append({"symbol": r["underlying_symbol"], "underlying_key": r["underlying_key"],
                         "name": names.get(r["underlying_key"]) or r.get("name") or r["underlying_symbol"],
                         "lot": r.get("lot_size"), "expiry_ms": int(r.get("expiry") or 0),
                         "strike": float(r["strike_price"]), "opt": r["instrument_type"],
                         "key": r["instrument_key"], "ts": r.get("trading_symbol")})
        return rows

    def load_futures(self):
        if getattr(self, "fut_rows", None) is None:
            self.load_rows()
        return self.fut_rows

    def spots(self, underlying_keys):
        keys, out = list(underlying_keys), {}
        for i in range(0, len(keys), 450):
            out.update({k: v for k, v in self.conn.ltp(keys[i:i + 450]).items() if v})
        return out

    def option_contracts(self, stock):
        return self.conn.option_contracts(stock["underlying_key"])

    def poll(self, keys):
        return self.conn.quotes(list(keys))

    def candles(self, key, interval="1", days=4):
        q = urllib.parse.quote(key, safe="")
        h = {"Authorization": "Bearer " + (config.access_token() or ""), "Accept": "application/json"}

        def norm(rows):
            out = []
            for c in rows:
                try:
                    out.append({"time": int(datetime.fromisoformat(c[0]).timestamp()), "open": float(c[1]),
                                "high": float(c[2]), "low": float(c[3]), "close": float(c[4]),
                                "volume": float(c[5]) if len(c) > 5 else 0, "oi": c[6] if len(c) > 6 else None})
                except Exception:
                    pass
            return out

        today = datetime.now(IST)
        r = httpx.get(f"{config.UPSTOX}/v3/historical-candle/{q}/minutes/{interval}/"
                      f"{(today - timedelta(days=1)).strftime('%Y-%m-%d')}/"
                      f"{(today - timedelta(days=days + 3)).strftime('%Y-%m-%d')}", headers=h, timeout=20)
        hist = norm((r.json().get("data") or {}).get("candles", [])) if r.status_code == 200 else []
        r = httpx.get(f"{config.UPSTOX}/v3/historical-candle/intraday/{q}/minutes/{interval}", headers=h, timeout=20)
        intra = norm((r.json().get("data") or {}).get("candles", [])) if r.status_code == 200 else []
        allc = {c["time"]: c for c in hist + intra}
        return vwap_series([allc[t] for t in sorted(allc)])

    # ---- WS (ltpc) ----
    def start_feed(self, on_data, on_status):
        self.on_data, self.on_status = on_data, on_status
        threading.Thread(target=self._thread, name="upstox-feed", daemon=True).start()

    def set_subscription(self, keys):
        keys = set(list(keys)[:self.limit])
        gone, new = self.sub - keys, keys - self.sub
        self.sub = keys
        if self._loop and self._q is not None:
            if gone:
                self._loop.call_soon_threadsafe(self._q.put_nowait, ("unsub", sorted(gone)))
            if new:
                self._loop.call_soon_threadsafe(self._q.put_nowait, ("sub", sorted(new)))
        return {"added": len(new), "removed": len(gone)}

    def status_text(self):
        return f"Upstox {self.state} · {len(self.sub)} subscribed"

    def _thread(self):
        try:
            import websockets          # noqa: F401
            import sys
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if root not in sys.path:
                sys.path.append(root)
            import upstox_feed_pb2     # noqa: F401
        except Exception as e:
            self.state = f"unavailable ({e}) — REST-only"
            return
        asyncio.run(self._main())

    async def _main(self):
        import websockets
        import upstox_feed_pb2 as pb
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        while True:
            if not self.sub:
                self.state = "idle"
                await asyncio.sleep(3)
                continue
            if not config.access_token():
                self.state = "no Upstox token (token.json)"
                await asyncio.sleep(10)
                continue
            try:
                r = await asyncio.to_thread(httpx.get, config.UPSTOX + "/v3/feed/market-data-feed/authorize",
                                            headers={"Authorization": "Bearer " + config.access_token(),
                                                     "Accept": "application/json", "User-Agent": config.USER_AGENT},
                                            timeout=20)
                r.raise_for_status()
                url = r.json()["data"]["authorizedRedirectUri"]
                async with websockets.connect(url, additional_headers={"User-Agent": config.USER_AGENT},
                                              max_size=None, ping_interval=20) as ws:
                    while not self._q.empty():
                        self._q.get_nowait()
                    await self._send(ws, "sub", sorted(self.sub))
                    self.state = "connected"
                    recv = asyncio.ensure_future(ws.recv())
                    cmd = asyncio.ensure_future(self._q.get())
                    while True:
                        done, _ = await asyncio.wait({recv, cmd}, return_when=asyncio.FIRST_COMPLETED)
                        if recv in done:
                            msg = recv.result()
                            if isinstance(msg, (bytes, bytearray)):
                                self._decode(pb, msg)
                            recv = asyncio.ensure_future(ws.recv())
                        if cmd in done:
                            method, keys = cmd.result()
                            await self._send(ws, method, keys)
                            cmd = asyncio.ensure_future(self._q.get())
            except Exception as e:
                self.state = f"reconnecting: {type(e).__name__}: {str(e)[:100]}"
                await asyncio.sleep(5)

    @staticmethod
    async def _send(ws, method, keys):
        for i in range(0, len(keys), 1000):
            await ws.send(json.dumps({"guid": f"trk-{method}-{i}", "method": method,
                                      "data": {"mode": "ltpc", "instrumentKeys": keys[i:i + 1000]}}).encode())

    def _decode(self, pb, raw):
        resp = pb.FeedResponse()
        try:
            resp.ParseFromString(raw)
        except Exception:
            return
        now = time.time()
        for key, feed in resp.feeds.items():
            if feed.WhichOneof("FeedUnion") == "ltpc" and feed.ltpc.ltp:
                f = {"ltp": feed.ltpc.ltp}
                if feed.ltpc.cp:
                    f["prev_close"] = feed.ltpc.cp
                self.on_data(key, f, now)
