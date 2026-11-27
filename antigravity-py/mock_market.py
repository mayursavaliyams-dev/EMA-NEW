"""MOCK TEST: run the real tracker against a fake, local Dhan (no broker, no orders).

    python mock_market.py            -> dashboard on http://localhost:8001

What is real:  the whole tracker + DhanProvider code path (master CSV download, REST
               /marketfeed/ltp + /charts/intraday, 5 WebSocket connections, binary FULL
               packets, subscribe/unsubscribe, alerts, trends, greeks, dashboard).
What is fake:  the prices. A local server plays "Dhan": 50 stocks random-walk, option
               premiums come from Black-Scholes, and every ~40 s one stock starts a
               3-minute "story" (sharp move + OI build-up + volume burst) so alerts fire.

It never contacts Dhan or Upstox and keeps all its files in data-mock/ (the real
tracker's data/ and settings are untouched). Port 8001, so it can run next to the
real tracker on 8000.
"""
import os
import sys
import csv
import json
import math
import time
import random
import struct
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["AG_DATA_DIR"] = os.path.join(HERE, "data-mock")     # before importing config
os.environ.pop("DHAN_CLIENT_ID", None)
os.environ.pop("DHAN_ACCESS_TOKEN", None)
sys.path.insert(0, HERE)

IST = timezone(timedelta(hours=5, minutes=30))
HTTP_PORT, WS_PORT, APP_PORT = 18765, 18766, 8001
random.seed(7)

# symbol, approx spot, lot  (MOCK numbers, only roughly realistic)
STOCKS = [
    ("RELIANCE", 1480, 500), ("HDFCBANK", 1990, 550), ("ICICIBANK", 1380, 700), ("INFY", 1560, 400),
    ("TCS", 3150, 175), ("SBIN", 905, 750), ("AXISBANK", 1210, 625), ("KOTAKBANK", 2110, 400),
    ("LT", 3950, 175), ("ITC", 415, 1600), ("BHARTIARTL", 2060, 475), ("HINDUNILVR", 2420, 300),
    ("BAJFINANCE", 1010, 750), ("MARUTI", 15800, 50), ("TATAMOTORS", 690, 800), ("SUNPHARMA", 1720, 350),
    ("TITAN", 3720, 175), ("ASIANPAINT", 2480, 250), ("M&M", 3500, 200), ("NTPC", 335, 1500),
    ("POWERGRID", 290, 1900), ("ONGC", 245, 2250), ("COALINDIA", 385, 1350), ("TATASTEEL", 168, 5500),
    ("JSWSTEEL", 1150, 675), ("HINDALCO", 790, 1400), ("ADANIENT", 2380, 300), ("ADANIPORTS", 1450, 475),
    ("WIPRO", 255, 3000), ("HCLTECH", 1620, 350), ("TECHM", 1540, 600), ("ULTRACEMCO", 12100, 50),
    ("NESTLEIND", 1260, 500), ("DRREDDY", 1290, 625), ("CIPLA", 1530, 375), ("BAJAJ-AUTO", 9000, 75),
    ("EICHERMOT", 6900, 175), ("HEROMOTOCO", 5600, 150), ("DLF", 760, 825), ("INDUSINDBK", 760, 700),
    ("VEDL", 520, 1150), ("BEL", 415, 1425), ("HAL", 4700, 150), ("TRENT", 4300, 100),
    ("ZOMATO", 310, 2425), ("PNB", 120, 8000), ("BANKBARODA", 285, 2925), ("CANBK", 140, 6750),
    ("IRCTC", 720, 875), ("DMART", 4100, 150),
]


def last_tuesday(y, m):
    d = datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d.date()


def expiries(n=3):
    today = datetime.now(IST).date()
    out, y, m = [], today.year, today.month
    while len(out) < n:
        e = last_tuesday(y, m)
        if e >= today:
            out.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def step_for(px):
    return 2.5 if px < 200 else 5 if px < 500 else 10 if px < 1000 else 20 if px < 2500 else 50 if px < 6000 else 100


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(s, k, t, iv, opt):
    t = max(t, 1 / 365 / 24)
    d1 = (math.log(s / k) + (0.065 + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if opt == "CE":
        return s * ncdf(d1) - k * math.exp(-0.065 * t) * ncdf(d2)
    return k * math.exp(-0.065 * t) * ncdf(-d2) - s * ncdf(-d1)


class Market:
    """The fake exchange: spots, futures, option premiums, OI, volume."""

    def __init__(self):
        self.lock = threading.Lock()
        self.inst = {}            # sid -> dict(kind, sym, strike, opt, exp, lot)
        self.state = {}           # sid -> dict(ltp, open, high, low, prev_close, vol, oi, prev_oi, pv)
        self.spot = {}            # sym -> spot
        self.eq_sid = {}          # sym -> equity sid
        self.story = {}           # sym -> (drift_per_sec, until)
        self.news = []            # fake Dhan headlines (newest last)
        self.rows = []
        sid = 100000
        exps = expiries()
        for sym, px, lot in STOCKS:
            sid += 1
            self.eq_sid[sym] = sid
            self.spot[sym] = px * random.uniform(0.97, 1.03)
            self.inst[sid] = {"kind": "EQ", "sym": sym}
            self.rows.append(["NSE", "E", sid, "EQUITY", "", sym, f"{sym} LTD (MOCK)", sym, "1.0", "", "", ""])
            for e in exps:
                sid += 1
                self.inst[sid] = {"kind": "FUT", "sym": sym, "exp": e, "lot": lot}
                self.rows.append(["NSE", "D", sid, "FUTSTK", self.eq_sid[sym], sym, f"{sym}-FUT", f"{sym} {e:%b} FUT",
                                  f"{lot}.0", e.isoformat(), "", ""])
                st = step_for(px)
                atm = round(px / st) * st
                for j in range(-20, 21):
                    k = atm + j * st
                    if k <= 0:
                        continue
                    for opt in ("CE", "PE"):
                        sid += 1
                        self.inst[sid] = {"kind": "OPT", "sym": sym, "strike": k, "opt": opt, "exp": e, "lot": lot}
                        self.rows.append(["NSE", "D", sid, "OPTSTK", self.eq_sid[sym], sym, f"{sym}-{k}-{opt}",
                                          f"{sym} {e:%d %b} {k:g} {'CALL' if opt == 'CE' else 'PUT'}",
                                          f"{lot}.0", e.isoformat(), f"{k}", opt])
        self.iv = {sym: random.uniform(0.22, 0.38) for sym, _, _ in STOCKS}
        now = time.time()
        for s, m in self.inst.items():
            p = self.price(m)
            oi = 0 if m["kind"] == "EQ" else int(m["lot"] * random.uniform(50, 2500) * math.exp(-abs(self._mny(m)) * 8))
            oi = max(oi, m.get("lot", 1) * 10) if m["kind"] != "EQ" else 0
            self.state[s] = {"ltp": p, "open": p, "high": p, "low": p, "prev_close": p * random.uniform(0.97, 1.03),
                             "vol": 0, "oi": oi, "prev_oi": int(oi * random.uniform(0.9, 1.05)), "pv": 0.0, "t": now}

    def _mny(self, m):
        return (m.get("strike", self.spot[m["sym"]]) / self.spot[m["sym"]]) - 1

    def price(self, m):
        s = self.spot[m["sym"]]
        if m["kind"] == "EQ":
            return s
        t = (datetime.combine(m["exp"], datetime.min.time()).replace(hour=15, minute=30, tzinfo=IST)
             - datetime.now(IST)).total_seconds() / (365 * 86400)
        if m["kind"] == "FUT":
            return s * math.exp(0.065 * max(t, 0))
        skew = 0.08 * abs(self._mny(m)) * 10
        return max(0.05, round(bs(s, m["strike"], t, self.iv[m["sym"]] * (1 + skew), m["opt"]) * 20) / 20)

    def step(self):
        """Advance the market by one second."""
        now = time.time()
        with self.lock:
            if random.random() < 1 / 40:                       # a new 'story' every ~40 s
                sym = random.choice(list(self.spot))
                up = random.random() < 0.5
                self.story[sym] = ((1 if up else -1) * random.uniform(0.0002, 0.0004), now + 180)
                if random.random() < 0.7:     # most stories come with a headline (30% stay 'unexplained')
                    self.news.append({"id": f"n{len(self.news) + 1}", "securityId": str(self.eq_sid[sym]),
                                      "headline": f"(MOCK) {sym} " + random.choice(
                                          ["wins large order from government", "Q3 update beats estimates",
                                           "board approves buyback", "block deal: promoter buys stake"] if up else
                                          ["faces regulatory probe", "Q3 update misses estimates",
                                           "promoter pledges more shares", "plant shutdown reported"]),
                                      "summary": "Mock headline generated by the mock market.",
                                      "publishedAt": int(now * 1000) - 20000, "source": "MockWire",
                                      "sentiment": "POSITIVE" if up else "NEGATIVE"})
            for sym in self.spot:
                drift, until = self.story.get(sym, (0, 0))
                if until < now:
                    drift = 0
                    self.story.pop(sym, None)
                self.spot[sym] *= math.exp(drift + random.gauss(0, 0.0004))
            for s, m in self.inst.items():
                st = self.state[s]
                hot = m["sym"] in self.story
                p = self.price(m)
                st["ltp"] = p
                st["high"], st["low"] = max(st["high"], p), min(st["low"], p)
                if random.random() < (0.9 if hot else 0.3):
                    q = m.get("lot", 1) * random.randint(1, 40 if hot else 4)
                    st["vol"] += q
                    st["pv"] += q * p
                    if m["kind"] != "EQ" and random.random() < 0.5:
                        st["oi"] = max(0, st["oi"] + int(q * random.uniform(-0.3, 1.0 if hot else 0.6)))

    def full_packet(self, sid):
        st, seg = self.state[sid], 1 if self.inst[sid]["kind"] == "EQ" else 2
        vwap = st["pv"] / st["vol"] if st["vol"] else st["ltp"]
        p = st["ltp"]
        head = struct.pack("<BHBIfHIfIIIIIIffff", 8, 162, seg, sid, p, 25, int(time.time()), vwap, st["vol"],
                           random.randint(100, 9000), random.randint(100, 9000), st["oi"], max(st["oi"], st["prev_oi"]),
                           min(st["oi"], st["prev_oi"]), st["open"], st["prev_close"], st["high"], st["low"])
        depth = b"".join(struct.pack("<IIHHff", random.randint(25, 2000), random.randint(25, 2000), 3, 4,
                                     max(0.05, p - 0.05 * (i + 1)), p + 0.05 * (i + 1)) for i in range(5))
        return head + depth

    def prev_packet(self, sid):
        st = self.state[sid]
        return struct.pack("<BHBIfI", 6, 16, 1 if self.inst[sid]["kind"] == "EQ" else 2, sid, st["prev_close"], st["prev_oi"])

    def candles(self, sid, interval):
        """Synthetic intraday 1-min history ending at the current price."""
        st = self.state[sid]
        now = datetime.now(IST)
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now < start:
            start -= timedelta(days=1)
        n = max(5, min(375, int((now - start).total_seconds() // 60)))
        rnd = random.Random(sid)
        px, rows = st["ltp"], []
        for i in range(n):
            o = px
            px = max(0.05, px * math.exp(rnd.gauss(0, 0.004 if self.inst[sid]["kind"] == "OPT" else 0.0008)))
            rows.append((o, max(o, px) * 1.001, min(o, px) * 0.999, px))
        rows.reverse()
        rows = [(c, max(o, c) * 1.001, min(o, c) * 0.999, o) for o, h, l, c in rows]   # walk backwards from now
        ts = [int((start + timedelta(minutes=i)).timestamp()) for i in range(n)]
        iv = int(interval) if str(interval).isdigit() else 1
        out = {"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": [], "open_interest": []}
        for j in range(0, n, iv):
            g = rows[j:j + iv]
            out["open"].append(round(g[0][0], 2))
            out["high"].append(round(max(x[1] for x in g), 2))
            out["low"].append(round(min(x[2] for x in g), 2))
            out["close"].append(round(g[-1][3], 2))
            out["volume"].append(rnd.randint(1, 60) * self.inst[sid].get("lot", 1))
            out["timestamp"].append(ts[j])
            out["open_interest"].append(st["oi"])
        return out


MK = None


class FakeDhanHTTP(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/master.csv"):
            import io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SECURITY_ID", "UNDERLYING_SYMBOL",
                        "SYMBOL_NAME", "DISPLAY_NAME", "LOT_SIZE", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE"])
            w.writerows(MK.rows)
            return self._send(200, buf.getvalue().encode(), "text/csv")
        if self.path.startswith("/v2/profile"):
            return self._send(200, {"dhanClientId": "MOCK"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.startswith("/v2/marketfeed/ltp"):
            with MK.lock:
                d = {str(s): {"last_price": round(MK.state[s]["ltp"], 2)} for s in body.get("NSE_EQ", []) if s in MK.state}
            return self._send(200, {"data": {"NSE_EQ": d}, "status": "success"})
        if self.path.startswith("/v2/data/newsheadline"):
            want = {str(x) for x in body.get("stockList") or []}
            with MK.lock:
                items = [n for n in MK.news if not want or n["securityId"] in want]
            return self._send(200, {"data": {"latestNews": list(reversed(items))[:int(body.get("limit") or 10)]}})
        if self.path.startswith("/v2/charts/intraday"):
            sid = int(body.get("securityId", 0))
            if sid not in MK.state:
                return self._send(400, {"error": "unknown securityId"})
            with MK.lock:
                return self._send(200, MK.candles(sid, body.get("interval", "1")))
        self._send(404, {"error": "not found"})


async def ws_handler(ws):
    """One fake Dhan feed connection: subscribe/unsubscribe JSON in, binary FULL packets out."""
    subs = set()

    async def reader():
        async for m in ws:
            j = json.loads(m)
            ids = {int(x["SecurityId"]) for x in j.get("InstrumentList", [])}
            if j.get("RequestCode") == 21:
                new = ids - subs
                subs.update(ids)
                with MK.lock:
                    pk = b"".join(MK.prev_packet(s) + MK.full_packet(s) for s in new if s in MK.state)
                if pk:
                    await ws.send(pk)
            elif j.get("RequestCode") == 22:
                subs.difference_update(ids)

    async def writer():
        while True:
            await asyncio.sleep(1)
            with MK.lock:
                ids = [s for s in subs if s in MK.state and (MK.inst[s]["sym"] in MK.story or random.random() < 0.5)]
                pk = [MK.full_packet(s) for s in ids]
            for i in range(0, len(pk), 400):
                await ws.send(b"".join(pk[i:i + 400]))

    r, w = asyncio.ensure_future(reader()), asyncio.ensure_future(writer())
    await asyncio.wait({r, w}, return_when=asyncio.FIRST_COMPLETED)
    r.cancel()
    w.cancel()


def start_fake_dhan():
    global MK
    MK = Market()
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), FakeDhanHTTP).serve_forever,
                     daemon=True).start()

    def clock():
        while True:
            MK.step()
            time.sleep(1)
    threading.Thread(target=clock, daemon=True).start()

    async def serve():
        import websockets
        async with websockets.serve(ws_handler, "127.0.0.1", WS_PORT, max_size=None):
            await asyncio.Future()
    threading.Thread(target=lambda: asyncio.run(serve()), daemon=True).start()
    n_opt = sum(1 for m in MK.inst.values() if m["kind"] == "OPT")
    print(f"[mock] fake Dhan up: {len(STOCKS)} stocks, {n_opt} option strikes, "
          f"{sum(1 for m in MK.inst.values() if m['kind'] == 'FUT')} futures")


def build_app():
    import config
    config.save_overrides({"TRADE_MODE": "paper", "STRANGLE_ENGINE_ENABLED": False,
                           "trkProvider": "dhan", "trkDhanConnections": 5, "trkAlwaysOn": True})
    from trackers import providers as P
    import main
    from trackers.stock_options import StockOptionsTracker
    d = P.DhanProvider(5, creds={"client_id": "MOCK", "access_token": "mock-token"},
                       wss=f"ws://127.0.0.1:{WS_PORT}", api=f"http://127.0.0.1:{HTTP_PORT}/v2")
    d.MASTER_URL = f"http://127.0.0.1:{HTTP_PORT}/master.csv"
    d.MASTER_PATH = os.path.join(config.DATA, "dhan_master_mock.csv")
    if os.path.exists(d.MASTER_PATH):
        os.remove(d.MASTER_PATH)
        # history / NSE context / NSE extras are REAL NSE data (bhavcopy, ban list, FII/DII, deals): they fill
    # the multi-day / FII / deals tables; their prices won't match the mock prices.
    main.tracker = StockOptionsTracker(main.conn, main.news_hub, providers={"dhan": d})
    main.engine.try_enter = lambda *a, **k: None          # belt and braces: the mock never trades
    return main.app


if __name__ == "__main__":
    import uvicorn
    start_fake_dhan()
    app = build_app()
    print(f"[mock] MOCK TEST - nakli bhav. Dashboard: http://localhost:{APP_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=APP_PORT, log_level="warning", access_log=False)
