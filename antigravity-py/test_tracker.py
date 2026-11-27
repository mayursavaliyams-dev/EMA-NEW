"""Offline tests for the stock-options side tracker (no token, no network).

Covers: 25,000-strike allocation, live subset, re-centring, intraday new strikes, detectors
(price / OI / volume, incl. a brute-force check of the O(1) window), news matching, REST merge,
per-stock totals, VWAP, Dhan binary packets (SDK layout), Dhan 5-connection split, Dhan master
CSV, and a real WebSocket round-trip against a local fake Dhan feed server.

Run:  py test_tracker.py
"""
import os
import csv
import json
import time
import random
import struct
import asyncio
import tempfile
import threading
from datetime import datetime, timedelta

import config
from trackers import stock_options as T
from trackers import providers as P

TMP = tempfile.mkdtemp()
T.ALERTS_PATH = os.path.join(TMP, "alerts.json")          # never touch real data
T.TRENDS_PATH = os.path.join(TMP, "trends.json")
T.ALERTS_DIR = os.path.join(TMP, "alerts")                  # day files (kept one expiry cycle)
T.TRENDS_DIR = os.path.join(TMP, "trends")
SET = dict(T.SETTINGS, trkAlwaysOn=True)
T.setting = lambda k: SET[k]
IST = T.IST
PASS = []
_TG = []


def check(name, cond, detail=""):
    assert cond, f"FAIL {name} {detail}"
    PASS.append(name)
    print("  ok ", name, detail)


def exp_ms(days):
    d = (datetime.now(IST) + timedelta(days=days)).replace(hour=15, minute=30, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def rows(n_stocks=218, strikes=60, step=10.0, base=1000.0):
    out = []
    for i in range(n_stocks):
        sym, uk = f"STK{i}", f"NSE_EQ|{1000 + i}"
        for e in (exp_ms(5), exp_ms(35)):
            for j in range(strikes):
                k = base + j * step
                for opt in ("CE", "PE"):
                    out.append({"symbol": sym, "underlying_key": uk, "name": f"STOCK {i} LIMITED", "lot": 500,
                                "expiry_ms": e, "strike": k, "opt": opt, "key": f"NSE_FNO|{sym}-{e}-{k:.0f}-{opt}",
                                "ts": f"{sym} {k:.0f} {opt}"})
    return out


class FakeProvider:
    def __init__(self, name="upstox", limit=5000, spot=1300.0):
        self.name, self.limit, self.spot = name, limit, spot
        self.sub, self.contract_calls, self.extra, self.q = set(), [], [], {}
        self.fatal = None

    def available(self):
        return True

    def load_rows(self):
        return rows()

    def load_futures(self):
        return [{"symbol": f"STK{i}", "underlying_key": f"NSE_EQ|{1000 + i}", "lot": 500, "expiry_ms": exp_ms(d),
                 "key": f"NSE_FNO|FUT{i}-{d}", "ts": f"STK{i} FUT"} for i in range(218) for d in (5, 35, 65)]

    def spots(self, keys):
        return {k: self.spot for k in keys}

    def option_contracts(self, stock):
        self.contract_calls.append(stock["symbol"])
        return self.extra

    def poll(self, keys):
        return {k: self.q[k] for k in keys if k in self.q}

    def start_feed(self, on_data, on_status):
        pass

    def set_subscription(self, keys):
        keys = set(keys)
        d = {"added": len(keys - self.sub), "removed": len(self.sub - keys)}
        self.sub = keys
        return d

    def status_text(self):
        return "fake"

    def candles(self, key, interval):
        return []


class FakeNews:
    def __init__(self):
        self.items, self.errors = {}, {}

    def poll_nse(self, force=False):
        pass

    def news_for(self, symbol, name, since_ts=None, until_ts=None, fetch=True):
        return [i for i in self.items.get(symbol, [])
                if (since_ts is None or i["ts"] >= since_ts) and (until_ts is None or i["ts"] <= until_ts)]


def make(provider=None, history=False, context=False, extra=False):
    p = provider or FakeProvider()
    n = FakeNews()
    t = T.StockOptionsTracker(None, n, detector=T.Detector(get=lambda k: SET[k]), providers={"upstox": p},
                              history=history, context=context, extra=extra)
    t.tg._send = lambda text: (_TG.append(text) or True)
    t.alerts.clear()
    t.choose_provider()
    t.u.build(p.load_rows())
    t.u.build_futures(p.load_futures())
    return t, p, n


# ---------------------------------------------------------------------------
def test_25000_allocation():
    t, p, n = make()
    r = t.rebalance()
    per = {}
    for m in t.sel.values():
        per[m["symbol"]] = per.get(m["symbol"], 0) + 1
    check("218 stocks covered", len(per) == 218, f"({len(per)})")
    check("25,000 strikes tracked", len(t.sel) == 25000, f"({len(t.sel)})")
    check("Upstox: 4,346 nearest-ATM strikes + 218 stocks + 436 futures = 5,000 streamed live",
          len(t.live_keys) == 4346 and len(t.fut_meta) == 436 and p.sub == t.live_keys | set(t.ukeys) | set(t.fut_meta)
          and len(p.sub) == 5000)
    far = max(t.sel[k]["dist"] for k in t.live_keys)
    check("live subset = nearest strikes", all(t.sel[k]["dist"] >= far - 1e-12 for k in set(t.sel) - t.live_keys))
    check("front expiry only", {m["expiry"] for m in t.sel.values()} == {(datetime.now(IST) + timedelta(days=5)).strftime("%Y-%m-%d")})
    strikes = sorted({m["strike"] for m in t.sel.values() if m["symbol"] == "STK0"})
    check("window centred on ATM", strikes[0] <= 1300 <= strikes[-1], f"({strikes[0]}..{strikes[-1]})")
    t2, p2, _ = make(FakeProvider("dhan", 25000))
    t2.rebalance()
    check("Dhan: every tracked strike + 218 stocks + 436 futures live = 25,000",
          len(t2.live_keys) == len(t2.sel) == 24346 and len(p2.sub) == 25000)
    # broker lists fewer front-expiry strikes than the budget (Upstox ~12.6k) -> next expiry fills
    p3 = FakeProvider()
    p3.load_rows = lambda: rows(strikes=30)          # 218 x 30 x 2 = 13,080 legs per expiry
    t3, _, _ = make(p3)
    t3.rebalance()
    nf = sum(1 for m in t3.sel.values() if m["front"])
    check("front expiry fully used, rest from next expiry", nf == 13080 and len(t3.sel) == 25000, f"(front {nf})")
    check("live subset prefers front expiry", all(t3.sel[k]["front"] for k in t3.live_keys))
    check("ladder defaults to front expiry", len(t3.board("STK0")) == 60 and len(t3.expiries("STK0")) == 2)


def test_recentre_and_new_strikes():
    t, p, n = make()
    t.rebalance()
    p.spot = 1400.0
    r = t.rebalance()
    strikes = sorted({m["strike"] for m in t.sel.values() if m["symbol"] == "STK0"})
    check("re-centres when spot moves", strikes[0] <= 1400 <= strikes[-1] and r["added"] == r["removed"] > 0,
          f"(+{r['added']} -{r['removed']})")
    p.spot = 1585.0                                   # last listed strike is 1590
    e = datetime.fromtimestamp(exp_ms(5) / 1000, IST).strftime("%Y-%m-%d")
    p.extra = [{"instrument_type": o, "instrument_key": f"NSE_FNO|NEW-{k}-{o}", "expiry": e, "strike_price": k}
               for k in (1600.0, 1610.0) for o in ("CE", "PE")]
    p.contract_calls.clear()
    t._contract_ts.clear()
    t.rebalance()
    check("edge -> live contract fetch (25 stocks / round)", len(p.contract_calls) == 25)
    check("new strikes tracked", any(k.startswith("NSE_FNO|NEW-1600") for k in t.sel))
    t.rebalance()
    check("contract fetch cooldown (no stock twice)", len(set(p.contract_calls)) == len(p.contract_calls) == 50)


def test_detector():
    d = T.Detector(get=lambda k: SET[k])
    t0 = 1_000_000.0
    check("no alert on first tick", d.on_price("A", 10, t0) is None)
    check("no alert on small move", d.on_price("A", 11, t0 + 30) is None)
    ev = d.on_price("A", 12.5, t0 + 60)
    check("PRICE UP >=20% in window", ev and ev["dir"] == "UP" and ev["pct"] == 25.0)
    check("cooldown", d.on_price("A", 13, t0 + 90) is None)
    d.on_price("B", 1.0, t0)
    check("cheap option ignored", d.on_price("B", 1.8, t0 + 30) is None)
    d.on_price("C", 20, t0)
    check("move outside window ignored", d.on_price("C", 26, t0 + 6 * 60) is None)
    d.on_price("D", 50, t0)
    check("PRICE DOWN detected", (d.on_price("D", 38, t0 + 60) or {}).get("dir") == "DOWN")
    d.on_oi("E", 100000, t0, 500)
    check("small OI change ignored", d.on_oi("E", 110000, t0 + 60, 500) is None)
    ev = d.on_oi("E", 150000, t0 + 120, 500)
    check("OI build-up detected", ev and ev["kind"] == "OI" and ev["pct"] == 50.0)

    # monotonic window == brute force on random data
    rnd = random.Random(7)
    w, raw = T._Win(), []
    t = last = t0
    ok = True
    for _ in range(3000):
        t += rnd.choice([0.2, 0.5, 1, 3, 20])
        v = rnd.uniform(1, 100)
        w.push(t, v, 300)
        at = last if raw and t - last < 1 else t        # thinned timestamp
        last = at
        raw.append((at, v))
        cut = at - 300
        live = [x for x in raw if x[0] >= cut]
        ok &= abs(w.mn[0][1] - min(x[1] for x in live)) < 1e-9 and abs(w.mx[0][1] - max(x[1] for x in live)) < 1e-9
    check("O(1) window matches brute force (3000 random ticks)", ok)


def test_volume_burst():
    d = T.Detector(get=lambda k: SET[k])
    open_ts = T.session_open_ts(time.time())
    t, vol = open_ts, 0
    fired = None
    for i in range(1, 61):                    # first hour from 09:15: steady 100 lots / min
        vol += 100 * 500
        check_none = d.on_volume("V", vol, t + i * 60, 500)
        assert check_none is None, "steady phase must not alert"
    for i in range(1, 6):                     # burst: 2,000 lots / min
        vol += 2000 * 500
        fired = d.on_volume("V", vol, t + 3600 + i * 60, 500) or fired
    check("VOLUME burst vs day pace", fired and fired["kind"] == "VOLUME" and fired["pct"] >= 5, str(fired and fired["pct"]))
    d2 = T.Detector(get=lambda k: SET[k])
    vol = 0
    quiet = None
    for i in range(1, 300):                   # a whole steady session
        vol += 100 * 500
        quiet = d2.on_volume("W", vol, t + i * 60, 500) or quiet
    check("steady volume -> no alert", quiet is None)


def test_alerts_news_match():
    t, p, n = make()
    t.rebalance()
    keys = [k for k, m in t.sel.items() if m["symbol"] == "STK3" and m["opt"] == "CE"][:2]
    now = time.time()
    n.items["STK3"] = [{"source": "NSE", "ts": now - 600, "title": "Outcome of Board Meeting", "id": "x"}]
    for k in keys:
        t.on_data(k, {"ltp": 10}, now - 120)
        t.on_data(k, {"ltp": 13}, now)
    t._drain_events()
    a = [x for x in t.alerts if x["symbol"] == "STK3"]
    check("one alert per stock (strikes merged)", len(a) == 1 and len(a[0]["legs"]) == 2)
    check("NEWS-MATCH when news precedes move", a[0]["status"] == "NEWS-MATCH")
    key = [k for k, m in t.sel.items() if m["symbol"] == "STK7"][0]
    t.on_data(key, {"ltp": 10}, now - 60)
    t.on_data(key, {"ltp": 15}, now)
    t._drain_events()
    b = [x for x in t.alerts if x["symbol"] == "STK7"][0]
    check("UNEXPLAINED when no news", b["status"] == "UNEXPLAINED")
    n.items["STK7"] = [{"source": "Google News", "ts": now + 300, "title": "STK7 wins order", "id": "y"}]
    t.recheck_unexplained(now=now + 400)
    check("upgraded to NEWS-AFTER", b["status"] == "NEWS-AFTER")


def test_rest_merge_summary_ladder():
    t, p, n = make()
    t.rebalance()
    live_k = next(iter(t.live_keys))
    rest_k = next(iter(set(t.sel) - t.live_keys))
    t.on_data(live_k, {"ltp": 50.0})                       # WS tick
    p.q = {live_k: {"ltp": 1.0, "oi": 1000, "volume": 10},
           rest_k: {"ltp": 20.0, "vwap": 19.0, "high": 25.0, "low": 15.0, "oi": 100000, "prev_oi": 80000,
                    "prev_close": 16.0, "volume": 5000, "oi_hi": 120000, "oi_lo": 70000}}
    t.poll_rest()
    check("WS price wins for live strikes", t.latest[live_k]["ltp"] == 50.0 and t.latest[live_k]["oi"] == 1000)
    check("REST fills OI/VWAP/high-low for the rest", t.latest[rest_k]["vwap"] == 19.0)
    m = t.sel[rest_k]
    row = [r for r in t.board(m["symbol"]) if r["key"] == rest_k][0]
    check("ladder: above VWAP, range pos, OI chg, day chg",
          row["above_vwap"] is True and row["range_pos"] == 50 and row["oi_chg"] == 20000 and row["chg_pct"] == 25.0)
    # totals / mapping
    t2, p2, _ = make()
    t2.rebalance()
    sk = {(m["strike"], m["opt"]): k for k, m in t2.sel.items() if m["symbol"] == "STK1"}
    t2.on_data(sk[(1350.0, "CE")], {"oi": 9000, "prev_oi": 4000, "volume": 700})
    t2.on_data(sk[(1300.0, "CE")], {"oi": 1000, "volume": 100})
    t2.on_data(sk[(1250.0, "PE")], {"oi": 18000, "volume": 50})
    s = [x for x in t2.summary() if x["symbol"] == "STK1"][0]
    check("totals: CE/PE OI, PCR, OI chg", s["ce_oi"] == 10000 and s["pe_oi"] == 18000 and s["pcr"] == 1.8 and s["ce_oi_chg"] == 5000)
    check("mapping: resistance / support / max-volume strike",
          s["resistance"] == 1350.0 and s["support"] == 1250.0 and s["max_vol_strike"] == "1350CE")


def test_vwap():
    base = int(datetime(2026, 11, 26, 9, 15, tzinfo=IST).timestamp())
    c = P.vwap_series([{"time": base, "high": 12, "low": 8, "close": 10, "volume": 100},
                       {"time": base + 60, "high": 22, "low": 18, "close": 20, "volume": 300},
                       {"time": base + 86400, "high": 6, "low": 4, "close": 5, "volume": 10}])
    check("VWAP cumulative within day", c[1]["vwap"] == 17.5)
    check("VWAP resets next day", c[2]["vwap"] == 5.0)


# ---------------------------------------------------------------------------
# Dhan
# ---------------------------------------------------------------------------
def full_pkt(sid, ltp=12.5, vol=1000, oi=5000, seg=2):
    body = struct.pack("<BHBIfHIfIIIIIIffff", 8, 162, seg, sid, ltp, 75, 1764140000, ltp - 0.5, vol, 300, 400,
                       oi, oi + 100, oi - 100, 11.0, 10.0, 14.0, 9.0)
    return body + b"\0" * 100


def test_dhan_packets():
    d = P.DhanProvider(creds={"client_id": "1", "access_token": "x"})
    msg = (full_pkt(101) + struct.pack("<BHBIfI", 6, 16, 2, 102, 33.5, 7000)
           + struct.pack("<BHBII", 5, 12, 2, 103, 4242) + struct.pack("<BHBIfI", 2, 16, 2, 104, 7.25, 1764140000))
    out = dict(d.parse(msg))
    f = out["NSE_FNO|101"]
    check("FULL packet: ltp/vwap/volume/OI/OI hi-lo/OHLC",
          (f["ltp"], f["vwap"], f["volume"], f["oi"], f["oi_hi"], f["oi_lo"], f["open"], f["prev_close"], f["high"], f["low"], f["tbq"], f["tsq"])
          == (12.5, 12.0, 1000, 5000, 5100, 4900, 11.0, 10.0, 14.0, 9.0, 400, 300))
    check("PREV CLOSE packet -> prev OI", out["NSE_FNO|102"] == {"prev_close": 33.5, "prev_oi": 7000})
    check("OI + TICKER packets in the same message", out["NSE_FNO|103"] == {"oi": 4242} and out["NSE_FNO|104"]["ltp"] == 7.25)
    d.parse(struct.pack("<BHBIH", 50, 10, 0, 0, 807))
    check("disconnect 807 -> token expired -> provider stops", d.fatal and "expired" in d.fatal and not d.available())


def test_dhan_split():
    d = P.DhanProvider(creds={"client_id": "1", "access_token": "x"})
    keys = {f"NSE_FNO|{i}" for i in range(25000)}
    r = d.set_subscription(keys)
    sizes = [len(s) for s in d.conn_keys]
    check("25,000 over 5 connections x 5000", r["added"] == 25000 and sizes == [5000] * 5, str(sizes))
    keys2 = (keys - {f"NSE_FNO|{i}" for i in range(300)}) | {f"NSE_FNO|{i}" for i in range(30000, 30300)}
    r = d.set_subscription(keys2)
    check("diff: only changed strikes unsub/sub, still <=5000 each",
          r == {"added": 300, "removed": 300} and max(len(s) for s in d.conn_keys) <= 5000)
    msgs = [json.loads(m) for m in d._msgs(21, sorted(keys)[:250])]
    check("subscribe batches of 100 (RequestCode 21)", [m["InstrumentCount"] for m in msgs] == [100, 100, 50]
          and msgs[0]["RequestCode"] == 21 and msgs[0]["InstrumentList"][0]["ExchangeSegment"] == "NSE_FNO")


def test_dhan_master_csv():
    path = os.path.join(TMP, "dhan.csv")
    cols = ["EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SECURITY_ID", "UNDERLYING_SYMBOL",
            "SYMBOL_NAME", "DISPLAY_NAME", "LOT_SIZE", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE"]
    e = (datetime.now(IST) + timedelta(days=10)).strftime("%Y-%m-%d")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerow(["NSE", "E", "2885", "EQUITY", "", "RELIANCE", "RELIANCE INDUSTRIES LTD", "Reliance", "1.0", "", "", ""])
        w.writerow(["NSE", "D", "50001", "OPTSTK", "2885", "RELIANCE", "R-CE", "RELIANCE 1300 CALL", "500.0", e, "1300.00000", "CE"])
        w.writerow(["NSE", "D", "50002", "OPTIDX", "13", "NIFTY", "N", "NIFTY", "65.0", e, "26000", "CE"])
        w.writerow(["BSE", "D", "60001", "OPTSTK", "500325", "RELIANCE", "R", "R", "500.0", e, "1300", "PE"])
    r = P.DhanProvider(creds={"client_id": "1", "access_token": "x"}).load_rows(path)
    check("Dhan master: NSE OPTSTK only, keys/underlying/name/lot",
          len(r) == 1 and r[0]["key"] == "NSE_FNO|50001" and r[0]["underlying_key"] == "NSE_EQ|2885"
          and r[0]["name"] == "RELIANCE INDUSTRIES LTD" and r[0]["lot"] == 500 and r[0]["strike"] == 1300.0)


def test_dhan_websocket_roundtrip():
    """Real WebSocket: provider connects to a local fake Dhan feed, subscribes, receives FULL packets."""
    import websockets
    got_subs, got_q = [], []
    port_box = {}

    async def handler(ws):
        got_q.append(ws.request.path)
        async for m in ws:
            j = json.loads(m)
            got_subs.append(j)
            if j["RequestCode"] == 21:
                await ws.send(b"".join(full_pkt(int(x["SecurityId"]), ltp=20.0 + i) for i, x in enumerate(j["InstrumentList"][:3])))

    async def serve(ready):
        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            port_box["p"] = srv.sockets[0].getsockname()[1]
            ready.set()
            await asyncio.sleep(8)

    ready = threading.Event()
    threading.Thread(target=lambda: asyncio.run(serve(ready)), daemon=True).start()
    ready.wait(5)
    d = P.DhanProvider(connections=2, creds={"client_id": "1100", "access_token": "tok"},
                       wss=f"ws://127.0.0.1:{port_box['p']}")
    data = {}
    d.start_feed(lambda k, f, ts: data.__setitem__(k, f), lambda s: None)
    time.sleep(0.3)
    d.set_subscription({f"NSE_FNO|{i}" for i in range(1, 251)})
    for _ in range(50):
        if len(data) >= 6:
            break
        time.sleep(0.1)
    check("WS auth in URL (version=2, token, clientId, authType=2)",
          any("version=2" in q and "token=tok" in q and "clientId=1100" in q and "authType=2" in q for q in got_q))
    check("2 connections opened, 250 strikes subscribed in batches", len(got_q) == 2
          and sum(s["InstrumentCount"] for s in got_subs if s["RequestCode"] == 21) == 250
          and max(s["InstrumentCount"] for s in got_subs) <= 100)
    check("FULL packets parsed from the socket", len(data) >= 6 and all("oi" in f and "vwap" in f for f in data.values()),
          f"({len(data)} strikes)")
    d.set_subscription({f"NSE_FNO|{i}" for i in range(1, 241)})
    time.sleep(0.5)
    unsub = [s for s in got_subs if s["RequestCode"] == 22]
    check("live unsubscribe (RequestCode 22)", sum(s["InstrumentCount"] for s in unsub) == 10)


def test_news_relevance():
    from trackers.news import is_relevant, clean_name
    check("clean_name drops LIMITED", clean_name("RELIANCE INDUSTRIES LIMITED") == "Reliance Industries")
    check("relevant headline", is_relevant("Reliance shares jump 3%", "RELIANCE", "RELIANCE INDUSTRIES LTD"))
    check("generic headline rejected", not is_relevant("Industries see slow growth", "RELIANCE", "RELIANCE INDUSTRIES LTD"))


def test_speed():
    t, p, n = make(FakeProvider("dhan", 25000))
    t.rebalance()
    keys = list(t.sel)
    rnd = random.Random(1)
    t0 = time.perf_counter()
    now = time.time()
    N = 200_000
    for i in range(N):
        k = keys[i % len(keys)]
        t.on_data(k, {"ltp": 50 + rnd.random(), "oi": 10000 + i % 7, "volume": 1000 + i}, now + i * 0.005)
    dt = time.perf_counter() - t0
    check("throughput >= 20,000 updates/sec", N / dt >= 20000, f"({N / dt:,.0f}/s)")
    t0 = time.perf_counter()
    t.summary()
    t.board(None, 60, "move")
    check("summary + movers over 25,000 strikes < 1 s", time.perf_counter() - t0 < 1.0,
          f"({(time.perf_counter() - t0) * 1000:.0f} ms)")


# ---------------------------------------------------------------------------
# multi-day history (NSE bhavcopy), all-time high/low, trends, IV, unusual, context
# ---------------------------------------------------------------------------
import gzip as _gz
from trackers.history import History, FIELDS
from datetime import date as _date


def fake_hist(days):
    """days: list of (date, rows[tok, sym, exp, strike, opt, high, low, close, prev, oi, choi, vol, undl])"""
    cache = tempfile.mkdtemp()
    for d, rws in days:
        with _gz.open(os.path.join(cache, d.replace("-", "") + ".csv.gz"), "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            for (tok, sym, exp, k, opt, hi, lo, cl, pv, oi, choi, vol, undl) in rws:
                w.writerow([tok, sym, exp, k, opt, lo, hi, lo, cl, pv, oi, choi, vol, undl])
    return cache


def test_history():
    exp = (datetime.now(IST) + timedelta(days=20)).strftime("%Y-%m-%d")
    today = datetime.now(IST).date()
    ds = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in (6, 5, 4, 3, 2, 1)]
    days = []
    for i, d in enumerate(ds):         # CE: price up + OI up every day (long buildup), PE: price down + OI up (short buildup)
        days.append((d, [("901", "ABC", exp, 0, "FUT", 0, 0, 1010 + i * 5, 1005 + i * 5, 9000 + i * 1000, 1000, 70, 1000 + i),
                         ("101", "ABC", exp, 100, "CE", 10 + i * 2, 8 + i, 9 + i * 2, 8 + i * 2, 1000 + i * 500, 500, 50, 1000 + i),
                         ("102", "ABC", exp, 100, "PE", 20 - i, 15 - i, 16 - i, 17 - i, 2000 + i * 300, 300, 40, 1000 + i)]))
    days[2][1][1] = ("101", "ABC", exp, 100, "CE", 99, 8, 9 + 4, 8 + 4, 2000, 500, 50, 1002)      # the all-time high day
    h = History(days=30, cache=fake_hist(days), fetch=False).load(today)
    c = h.contract("101")
    check("ALL-TIME HIGH + date from bhavcopy", c["ath"] == 99 and c["ath_d"] == ds[2])
    check("ALL-TIME LOW + listing date", c["atl"] == 8 and c["atl_d"] == ds[0] and c["listed"] == ds[0])
    check("prev OI = last bhavcopy day (fills Upstox gap)", c["prev_oi"] == 1000 + 5 * 500)
    row = [r for r in h.stock_table() if r["symbol"] == "ABC"][0]
    check("multi-day: CE OI up 6 days in a row", row["ce_streak"] == 6)
    check("persistent activity: CE long buildup / PE short buildup every day",
          row["ce_run"] == "LB" and row["ce_run_days"] == 6 and row["pe_run"] == "SB" and row["pe_dom_days"] == 5)
    check("futures history: OI streak, long buildup every day, basis %",
          row["fut_streak"] == 6 and row["fut_run"] == "LB" and row["fut_run_days"] == 6 and row["fut_basis_pct"] is not None)
    sb = h.strike_build("ABC", 5)
    check("strikes where OI was built over 5 days", sb[0]["oi_chg"] == 2500 and sb[0]["opt"] == "CE")
    return h


def test_history_in_tracker():
    exp = (datetime.now(IST) + timedelta(days=20)).strftime("%Y-%m-%d")
    today = datetime.now(IST).date()
    ds = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in (2, 1)]
    t, p, n = make()
    t.rebalance()
    k = next(k for k, m in t.sel.items() if m["symbol"] == "STK0" and m["opt"] == "CE" and m["strike"] == m["atm"])
    tok = k.split("|", 1)[1]
    days = [(d, [(tok, "STK0", exp, 1300, "CE", 40, 20, 30, 28, 100000, 1000, 5000, 1300)]) for d in ds]
    t.hist = History(days=30, cache=fake_hist(days), fetch=False).load(today)
    t.on_data(k, {"ltp": 45.0, "high": 46.0, "low": 30.0, "prev_close": 30.0, "oi": 150000, "volume": 260000})
    t._drain_events()
    r = [x for x in t.board("STK0") if x["key"] == k][0]
    check("ladder: all-time high includes today's new high", r["ath"] == 46.0 and r["ath_d"] == "today" and r["atl"] == 20)
    check("OI chg vs bhavcopy prev OI + live buildup label", r["oi_chg"] == 50000 and r["buildup"] == "LB")
    check("Vol/OI ratio", r["vol_oi"] == 2.6)
    kinds = {a["kind"] for a in t.alerts if a["symbol"] == "STK0"}
    check("ATH-break alert + UNUSUAL (vol >= 2x OI) alert", {"ATH", "UNUSUAL"} <= kinds, str(kinds))
    a = [x for x in t.alerts if x["kind"] == "ATH"][0]
    check("alert carries reason + first-alert-of-day", "ALL-TIME HIGH" in a["reason"] and a["first_alert"]["time"])


def test_trend():
    eng = __import__("trackers.trend", fromlist=["TrendEngine"]).TrendEngine(lambda k: SET[k])
    base = T.session_open_ts(time.time()) + 3600
    bar = SET["trkTrendBarMin"] * 60
    evs = []
    px = [20, 19.5, 19, 18.6, 18.5, 18.4]                       # drift down, swing low 18.4
    px += [19.2, 20.4, 21.8, 23.5, 25.0, 26.4, 27.5]             # one-sided rally, green candles
    px += [27.0, 24.0, 21.0, 20.0, 19.5]                         # collapse
    for i, p in enumerate(px):
        t0 = base + i * bar
        evs += eng.on_price("K", p * 0.99, t0 + 1, vwap=21.0)    # open
        evs += eng.on_price("K", p, t0 + bar - 1, vwap=21.0)     # close (green when rising)
    evs += eng.on_price("K", 19.0, base + len(px) * bar + 1, vwap=21.0)
    st = [e for e in evs if e["phase"] == "START"]
    en = [e for e in evs if e["phase"] == "END"]
    check("TREND START detected (UP)", st and st[0]["trend"]["dir"] == "UP")
    tr = st[0]["trend"]
    check("trend 'started from here' = swing-low candle, confirmed later",
          tr["origin"] <= 18.5 and tr["started_at"] < tr["confirmed_at"], f"(origin {tr['origin']} {tr['started_time']} → {tr['confirmed_time']})")
    check("start reason explains EMA / VWAP / green candles / move",
          all(x in tr["reason_start"] for x in ("EMA5", "VWAP", "green candles", "from the low")))
    check("TREND END with reason, counts, one-sided %", en and en[0]["trend"]["reason_end"] and en[0]["trend"]["green"] >= 3
          and en[0]["trend"]["one_sided_pct"] >= 50 and en[0]["trend"]["best_move_pct"] > 15,
          f"({en and en[0]['trend']['reason_end']})")


def test_trend_saved_and_alerted():
    t, p, n = make()
    t.rebalance()
    k = next(k for k, m in t.sel.items() if m["symbol"] == "STK2" and m["strike"] == m["atm"] and m["opt"] == "CE")
    base = T.session_open_ts(time.time()) + 3600
    bar = SET["trkTrendBarMin"] * 60
    px = [20, 19.5, 19, 18.6, 18.5, 18.4, 19.2, 20.4, 21.8, 23.5, 25.0, 26.4, 27.5, 27.0, 24.0, 21.0, 20.0, 19.5, 19]
    for i, v in enumerate(px):
        t.on_data(k, {"ltp": v * 0.99, "vwap": 21.0}, base + i * bar + 1)
        t.on_data(k, {"ltp": v}, base + i * bar + bar - 1)
    t._drain_events()
    t._save_trends()
    saved = [x for d in t.trend_store.days() for x in t.trend_store.load_day(d)]
    check("trend record saved to disk with start/end details",
          saved and saved[-1]["symbol"] == "STK2" and saved[-1]["status"] == "ENDED"
          and saved[-1]["started_time"] and saved[-1]["ended_time"] and saved[-1]["reason_end"])
    ka = [a for a in t.alerts if a["symbol"] == "STK2" and a["kind"] in ("TREND", "TREND_END")]
    check("TREND + TREND_END alerts with reasons", {a["kind"] for a in ka} == {"TREND", "TREND_END"}
          and all(a["reason"] for a in ka))
    check("alert remembers the first alert of the day", ka[-1]["first_alert"]["kind"] in ("TREND", "PRICE"))


def test_iv_greeks_and_spike():
    t, p, n = make()
    t.rebalance()
    k = next(k for k, m in t.sel.items() if m["symbol"] == "STK4" and m["strike"] == m["atm"] and m["opt"] == "CE")
    now = time.time()
    t.on_data(k, {"ltp": 30.0}, now)
    t.compute_greeks(now)
    g = t.greeks.get(k)
    check("IV + greeks computed for live strikes", g and 5 < g["iv"] < 200 and 0 < g["delta"] < 1, str(g))
    t.on_data(k, {"ltp": 60.0}, now + 60)
    t.compute_greeks(now + 60)
    t._drain_events()
    check("IV spike alert", any(a["kind"] == "IV" and a["symbol"] == "STK4" for a in t.alerts))


def test_depth_and_context():
    d = P.DhanProvider(creds={"client_id": "1", "access_token": "x"})
    depth = b"".join(struct.pack("<IIHHff", 100 * (i + 1), 50 * (i + 1), 3, 2, 10.0 - i * 0.05, 10.1 + i * 0.05) for i in range(5))
    body = struct.pack("<BHBIfHIfIIIIIIffff", 8, 162, 2, 7, 10.1, 75, 1, 10.0, 1000, 300, 400, 5000, 5100, 4900, 9.5, 9.0, 10.5, 9.2)
    f = dict(d.parse(body + depth))["NSE_FNO|7"]
    check("Dhan depth: bid/ask + 5-level qty", f["bid"] == 10.0 and f["ask"] == 10.1 and f["depth_bid_qty"] == 1500)
    t, p, n = make()
    t.rebalance()
    k = next(iter(t.live_keys))
    t.on_data(k, {"ltp": 10.1, "bid": 10.0, "ask": 10.1, "tbq": 600, "tsq": 400})
    r = [x for x in t.board(t.sel[k]["symbol"]) if x["key"] == k][0]
    check("spread %, aggressor BUY, buy-pressure %", r["spread_pct"] == 1.0 and r["aggressor"] == "BUY" and r["buy_pct"] == 60)
    from trackers.nse_context import NseContext
    c = NseContext()
    c.ban, c.ban_date = {"STK5"}, "27-NOV-2026"
    c.mwpl = {"STK5": {"pct": 97.0, "chg": 2.0, "limit_next": "No Fresh Positions"},
              "STK6": {"pct": 91.0, "chg": 4.5, "limit_next": "123"}}
    c.events = {"STK6": [{"date": (datetime.now(IST) + timedelta(days=2)).strftime("%d-%b-%Y"), "purpose": "Financial Results", "desc": ""}]}
    t.ctx = c
    t.context_alerts()
    kinds = {(a["symbol"], a["kind"]) for a in t.alerts}
    check("BAN + MWPL>=90% context alerts", ("STK5", "BAN") in kinds and ("STK6", "MWPL") in kinds)
    s6 = [x for x in t.summary() if x["symbol"] == "STK6"][0]
    check("summary carries MWPL % + upcoming event", s6["mwpl_pct"] == 91.0 and s6["event"]["days"] == 2)


def test_stock_signals():
    t, p, n = make()
    t.rebalance()
    now = T.session_open_ts(time.time()) + 3 * 3600
    keys = {(m["strike"], m["opt"]): k for k, m in t.sel.items() if m["symbol"] == "STK8" and m["front"]}
    atm = t.sel[keys[(1300.0, "CE")]]["atm"]
    def load(res, sup, ce_mult, straddle):
        for (k_, o), key in keys.items():
            oi = 1000
            if o == "CE" and k_ == res: oi = 50000
            if o == "PE" and k_ == sup: oi = 40000 * ce_mult
            t.on_data(key, {"oi": oi, "ltp": (straddle / 2 if k_ == atm else 5.0)}, now)
    load(1350.0, 1250.0, 1, 40.0); t.stock_signals(now)
    load(1400.0, 1250.0, 1, 40.0); t.stock_signals(now + 30)        # first sighting of the new wall
    load(1400.0, 1250.0, 3, 52.0); t.stock_signals(now + 60)        # holds -> shift; PCR up; straddle +30%
    t._drain_events()
    k = {a["kind"]: a for a in t.alerts if a["symbol"] == "STK8"}
    check("OI wall shift (resistance 1350 -> 1400) after it holds", "OI_SHIFT" in k and "1,350 → 1,400" in k["OI_SHIFT"]["reason"])
    check("PCR shift alert", "PCR" in k and k["PCR"]["dir"] == "UP")
    check("ATM straddle expansion alert", "STRADDLE" in k and "+30%" in k["STRADDLE"]["reason"])
    uk = t.u.stocks["STK8"]["underlying_key"]
    o = T.session_open_ts(time.time())
    for i, px in enumerate([1300, 1305, 1296, 1302]):
        t.on_data(uk, {"ltp": px}, o + 60 + i * 120)                 # inside the 15-min opening range
    t.on_data(uk, {"ltp": 1310}, o + 20 * 60)
    t._drain_events()
    orb = [a for a in t.alerts if a["symbol"] == "STK8" and a["kind"] == "ORB"]
    check("opening-range breakout (stock)", orb and orb[0]["dir"] == "UP" and "1,305" in orb[0]["reason"])


def test_futures():
    t, p, n = make()
    t.rebalance()
    fm = {m["symbol"] + ("F" if m["front"] else "N"): k for k, m in t.fut_meta.items()}
    check("front + next month futures per stock", len(t.fut_meta) == 436 and "STK9F" in fm and "STK9N" in fm)
    uk = t.u.stocks["STK9"]["underlying_key"]
    now = time.time()
    t.on_data(uk, {"ltp": 1000.0}, now)
    t.on_data(fm["STK9N"], {"oi": 300000}, now)
    t.on_data(fm["STK9F"], {"ltp": 1005.0, "prev_close": 990.0, "oi": 1100000, "prev_oi": 1000000, "volume": 5000}, now)
    r = t.fut_row("STK9")
    check("futures row: basis, OI chg, long buildup, rollover",
          r["basis"] == 5.0 and r["basis_pct"] == 0.5 and r["oi_chg_pct"] == 10.0 and r["buildup"] == "LB"
          and r["rollover_pct"] == 21.4, str(r))
    t.on_data(uk, {"ltp": 1010.0}, now + 1)
    t.on_data(fm["STK9F"], {"ltp": 1004.0}, now + 2)           # futures now BELOW spot -> discount
    t._drain_events()
    k = {a["kind"]: a for a in t.alerts if a["symbol"] == "STK9"}
    check("FUT_BUILDUP alert with reason", "FUT_BUILDUP" in k and "LONG BUILDUP" in k["FUT_BUILDUP"]["reason"])
    check("BASIS flip alert (premium -> discount)", "BASIS" in k and "to discount" in k["BASIS"]["reason"])
    s9 = [x for x in t.summary() if x["symbol"] == "STK9"][0]
    check("summary carries futures basis / buildup / rollover", s9["fut_buildup"] == "LB" and s9["rollover_pct"] == 21.4)
    check("futures list endpoint data", any(x["symbol"] == "STK9" for x in t.futures()))
    t.on_data(uk, {"ltp": 1010.0, "prev_close": 1000.0}, now + 3)
    s9 = [x for x in t.summary() if x["symbol"] == "STK9"][0]
    check("stock day change % for the UP/DOWN filter", s9["chg_pct"] == 1.0 and s9["spot"] == 1010.0)


def test_analytics():
    t, p, n = make()
    t.rebalance()
    sym = "STK10"
    uk = t.u.stocks[sym]["underlying_key"]
    now = T.session_open_ts(time.time()) + 2 * 3600
    t.on_data(uk, {"ltp": 1300.0}, now)
    legs = {(m["strike"], m["opt"]): k for k, m in t.sel.items() if m["symbol"] == sym and m["front"]}
    for (K, o), k in legs.items():
        intrinsic = max(0, (1300 - K) if o == "CE" else (K - 1300))
        oi = 50000 if (o == "PE" and K == 1250) else 60000 if (o == "CE" and K == 1350) else 5000
        t.on_data(k, {"ltp": intrinsic + 18 * 2.718 ** (-abs(K - 1300) / 60), "oi": oi}, now)
    t.compute_greeks(now)
    t.analytics(now)
    a = t.anal[sym]
    check("max pain computed from live OI", a["max_pain"] is not None and 1250 <= a["max_pain"] <= 1350, str(a["max_pain"]))
    check("GEX (₹ cr per 1%) + IV skew + expected move from ATM straddle",
          a["gex_cr"] is not None and a["skew"] is not None and a["straddle"] and a["exp_hi"] > 1300 > a["exp_lo"],
          f"(gex {a['gex_cr']} skew {a['skew']} straddle {a['straddle']})")
    t.on_data(uk, {"ltp": a["exp_hi"] + 5}, now + 60)
    t.analytics(now + 60)
    t._drain_events()
    check("EXP_MOVE alert when spot breaks the implied range", any(x["kind"] == "EXP_MOVE" and x["symbol"] == sym for x in t.alerts))
    # IV rank against stored history
    class H:
        def iv_stats(self, s, cur=None):
            from trackers.history import History
            h = History.__new__(History)
            h.iv_hist = {s: [("2026-10-%02d" % i, 10.0 + i, None) for i in range(1, 21)]}
            return History.iv_stats(h, s, cur)
        def hist_of(self, *a):
            return None
    t.hist = H()
    t.hist.contract = lambda tok: None
    t.analytics(now + 90)
    check("IV rank / percentile vs history (rank clamped 0-100)", t.anal[sym]["iv_rank"] is not None and 0 <= t.anal[sym]["iv_rank"] <= 100
          and t.anal[sym]["iv_pctile"] is not None,
          f"(rank {t.anal[sym]['iv_rank']} pct {t.anal[sym]['iv_pctile']})")


def test_nse_extra_and_alerts():
    from trackers.nse_extra import NseExtra
    x = NseExtra(cache=tempfile.mkdtemp(), fetch=False)
    txt = ('"Participant wise Open Interest",,,,,,,,,,,,,,\nClient Type,a,b,c,d,e,f,g,h,i,j,k,l,m,n\n'
           "FII,1000,3000,10,5,1,1,1,1,1,1,1,1,2000,3000\nDII,500,100,10,5,1,1,1,1,1,1,1,1,600,200\n"
           "Pro,10,10,1,1,1,1,1,1,1,1,1,1,1,1\nClient,10,10,1,1,1,1,1,1,1,1,1,1,1,1\n")
    txt2 = txt.replace("FII,1000,3000", "FII,4000,2000")
    x.participants = [{"date": "2026-11-25", "rows": x.parse_participants(txt)},
                      {"date": "2026-11-26", "rows": x.parse_participants(txt2)}]
    v = x.participant_view()
    f = [r for r in v["rows"] if r["who"] == "FII"][0]
    check("participant OI: FII net index futures + change", f["fut_idx"] == 2000 and f["fut_idx_chg"] == 4000)
    x.delivery = {"STK11": [{"date": f"d{i}", "close": 100, "prev_close": 100, "qty": 1000, "deliv_qty": 300, "deliv_pct": 30}
                            for i in range(20)] + [{"date": "d20", "close": 104, "prev_close": 100, "qty": 2000,
                                                    "deliv_qty": 1300, "deliv_pct": 65}]}
    d = x.delivery_info("STK11")
    check("delivery %: vs 20-day average", d["deliv_pct"] == 65 and d["deliv_avg20"] == 30 and d["deliv_qty_x20"] == 4.33)
    x.deals = {"date": "26-Nov-2026", "bulk": [], "block": [{"kind": "BLOCK", "symbol": "STK12", "name": "S", "client": "BIG MF",
                                                             "side": "BUY", "qty": 100000, "price": 1300, "date": "26-Nov-2026"}]}
    x.sector = {"STK11": "Banks", "STK12": "IT"}
    t, p, n = make(extra=x)
    t.rebalance()
    _TG.clear()
    t.tg.path = os.path.join(TMP, "tg.json")
    json.dump({"bot_token": "T", "chat_id": "1"}, open(t.tg.path, "w"))
    t.extra_alerts()
    kinds = {(a["symbol"], a["kind"]) for a in t.alerts}
    check("DELIVERY accumulation + DEAL + FII alerts", {("STK11", "DELIVERY"), ("STK12", "DEAL"), ("MARKET", "FII")} <= kinds, str(kinds))
    fii = [a for a in t.alerts if a["kind"] == "FII"][0]
    check("FII flip detected in reason", "FLIPPED LONG" in fii["reason"])
    t.tg.flush_sync()
    check("Telegram push for selected kinds", any("DELIVERY" in m for m in _TG) and any("FII" in m for m in _TG), f"({len(_TG)} msgs)")
    s11 = [r for r in t.summary() if r["symbol"] == "STK11"][0]
    check("summary: sector + delivery", s11["sector"] == "Banks" and s11["deliv_pct"] == 65)
    sec = {o["sector"]: o for o in t.sectors()}
    check("sector heatmap rows", "Banks" in sec and "Other" in sec and sec["Banks"]["stocks"] == 1)


def test_export_api():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    main.tracker.alerts.append({"id": 1, "symbol": "X", "kind": "PRICE", "dir": "UP", "time": "2026-11-27 10:00:00",
                                "ts": 1, "reason": "test ₹", "legs": [{"a": 1}], "news": []})
    r = c.get("/api/tracker/export?what=alerts&fmt=csv")
    check("CSV export (Excel-ready, UTF-8 BOM)", r.status_code == 200 and r.text.startswith("﻿") and "reason" in r.text)
    r = c.get("/api/tracker/export?what=alerts&fmt=xlsx")
    check("XLSX export", r.status_code == 200 and r.content[:2] == b"PK")
    check("bad export target rejected", c.get("/api/tracker/export?what=nope").status_code == 400)


def test_board_fast_topn():
    t, p, n = make(FakeProvider("dhan", 25000))
    t.rebalance()
    rnd = random.Random(3)
    now = time.time()
    keys = list(t.sel)[:3000]
    for i, k in enumerate(keys):
        t.on_data(k, {"ltp": 50.0, "prev_close": 50.0, "oi": 1000, "prev_oi": 1000, "volume": 100}, now)
        t.on_data(k, {"ltp": 50 * (1 + rnd.uniform(-0.3, 0.3)), "oi": int(1000 * rnd.uniform(0.5, 2)), "volume": 100 + i}, now + 30)
    full = sorted((t._row(k, t.sel[k]) for k in keys), key=lambda r: -abs(r["chg_win_pct"]))[:60]
    fast = t.board(None, 60, "move")
    check("top-N fast path == full sort (move)", [r["key"] for r in fast] == [r["key"] for r in full]
          or [abs(r["chg_win_pct"]) for r in fast] == [abs(r["chg_win_pct"]) for r in full])
    up = t.board(None, 60, "move", sign="UP")
    dn = t.board(None, 60, "oi", sign="DOWN")
    check("server-side UP / DOWN filter", up and all(r["chg_win_pct"] > 0 for r in up) and dn and all(r["oi_chg_pct"] < 0 for r in dn))
    t0 = time.perf_counter()
    for srt in ("move", "volume", "oi", "unusual", "iv"):
        t.board(None, 60, srt)
    check("5 top-strike views over 25,000 strikes < 0.5 s", time.perf_counter() - t0 < 0.5, f"({(time.perf_counter() - t0) * 1000:.0f} ms)")


def test_auto_never_dhan_from_env_only():
    """trkProvider=auto must NOT pick Dhan from DHAN_* env vars alone (live bot owns those + all 5 WS)."""
    old_env = {k: os.environ.get(k) for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN")}
    old_path, old_prov = P.DHAN_CRED_PATH, SET["trkProvider"]
    try:
        os.environ["DHAN_CLIENT_ID"], os.environ["DHAN_ACCESS_TOKEN"] = "1100", "envtok"
        P.DHAN_CRED_PATH = os.path.join(TMP, "no_dhan.json")
        if os.path.exists(P.DHAN_CRED_PATH):
            os.remove(P.DHAN_CRED_PATH)

        def pick(mode):
            SET["trkProvider"] = mode
            d = P.DhanProvider()
            d.start_feed = lambda *a: None
            t = T.StockOptionsTracker(None, FakeNews(), detector=T.Detector(get=lambda k: SET[k]),
                                      providers={"upstox": FakeProvider("upstox"), "dhan": d},
                                      history=False, context=False, extra=False)
            return t.choose_provider().name, d
        name, d = pick("auto")
        check("auto + DHAN env vars only (no dhan.json) -> upstox", name == "upstox" and d.available())
        check("explicit trkProvider=dhan still honours env vars", pick("dhan")[0] == "dhan")
        with open(P.DHAN_CRED_PATH, "w", encoding="utf-8") as f:
            json.dump({"client_id": "1100", "access_token": "filetok"}, f)
        check("auto + dhan.json present -> dhan", pick("auto")[0] == "dhan")
        check("trkProvider=upstox wins over dhan.json", pick("upstox")[0] == "upstox")
    finally:
        os.remove(P.DHAN_CRED_PATH) if os.path.exists(P.DHAN_CRED_PATH) else None
        P.DHAN_CRED_PATH, SET["trkProvider"] = old_path, old_prov
        for k, v in old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_dhan_news_and_805():
    """Dhan News Headlines: tolerant parsing, tagging by security id, wiring into NewsHub; 805 stops the feed."""
    from trackers.news import parse_dhan_news, NewsHub
    sid = {"2885": ("RELIANCE", "RELIANCE INDUSTRIES LTD"), "1333": ("HDFCBANK", "HDFC BANK LTD")}
    a = {"data": {"latestNews": [
        {"headline": "Reliance wins big order", "publishedAt": 1795785873872, "securityId": "2885", "sentiment": "POSITIVE"},
        {"title": "HDFC Bank Q3 update", "timestamp": "2026-11-27T10:15:00+05:30", "symbols": ["HDFCBANK"]},
        {"newsHeadline": "Reliance Industries board meet", "date": "27-11-2026 09:30:00"},   # untagged -> name match
        {"headline": "Monsoon outlook improves"},                                             # no stock
        {"summary": "no title -> skipped"}]}}
    out = parse_dhan_news(a, sid)
    by = {(i["symbol"], i["title"]) for i in out}
    check("Dhan news: securityId tag, epoch-ms time, sentiment",
          ("RELIANCE", "Reliance wins big order") in by
          and next(i for i in out if i["title"] == "Reliance wins big order")["sentiment"] == "POSITIVE"
          and next(i for i in out if i["title"] == "Reliance wins big order")["time"].startswith("2026-11-27T18:54"))
    check("Dhan news: other field names (title/symbols/ISO, newsHeadline/date) + headline name match",
          ("HDFCBANK", "HDFC Bank Q3 update") in by and ("RELIANCE", "Reliance Industries board meet") in by)
    check("Dhan news: untagged market news kept without a stock, item without title skipped",
          ("", "Monsoon outlook improves") in by and len(out) == 4)
    check("Dhan news: plain list / data list replies also parse",
          len(parse_dhan_news([{"headline": "x"}], sid)) == 1 and len(parse_dhan_news({"data": [{"headline": "y"}]}, sid)) == 1)
    h = NewsHub()
    calls = []
    h.set_dhan(lambda body: calls.append(body) or a, lambda: sid)
    h.poll_dhan(force=True)
    rel = h.dhan_for("RELIANCE", fetch=False)
    check("NewsHub: Dhan headlines stored per stock, raw reply kept",
          len(rel) == 2 and calls[0]["categories"] == ["ALL"] and h.dhan_raw["request"]["limit"] == 50)
    h.dhan_for("HDFCBANK", fetch=True)
    check("NewsHub: per-stock fetch sends stockList=[security id]", calls[-1].get("stockList") == ["1333"])
    check("NewsHub: Dhan items in the global list (only tagged ones)",
          {i["symbol"] for i in h.latest_nse() if i["source"].startswith("Dhan")} == {"RELIANCE", "HDFCBANK"})
    h.set_dhan(lambda body: (_ for _ in ()).throw(RuntimeError("Dhan /data/newsheadline HTTP 403")), lambda: sid)
    h.poll_dhan(force=True)
    check("NewsHub: Dhan error shown, never raised", "403" in h.errors.get("Dhan", ""))
    d = P.DhanProvider(creds={"client_id": "1", "access_token": "x"})
    d.parse(struct.pack("<BHBIH", 50, 10, 0, 0, 805))
    check("disconnect 805 (connection limit) -> feed stops, no reconnect fight with the live bot",
          d.fatal and "5" in d.fatal and not d.available())


def test_day_store_month():
    """Alerts / trends / news kept per day for a whole expiry cycle (trkKeepDays), not 24 h."""
    from trackers.store import DayStore, ist_day
    from trackers.news import NewsHub
    d = DayStore(os.path.join(TMP, "ds"), keep_days=35)
    now = time.time()
    for back in (0, 1, 20, 34, 40):
        ts = now - back * 86400
        d.merge_day(ist_day(ts), [{"id": f"a{back}", "ts": ts}])
    d.merge_day(ist_day(now), [{"id": "a0", "ts": now, "v": 2}, {"id": "b0", "ts": now}])
    check("day files: merge de-duplicates by id (newer copy wins)",
          len(d.load_day(ist_day(now))) == 2 and next(x for x in d.load_day(ist_day(now)) if x["id"] == "a0").get("v") == 2)
    d.prune(now)
    check("day files: 34-day-old kept, 40-day-old pruned (35-day keep)",
          ist_day(now - 34 * 86400) in d.days() and ist_day(now - 40 * 86400) not in d.days())
    check("day files: load_since spans days", {x["id"] for x in d.load_since(now - 21 * 86400)} == {"a0", "b0", "a1", "a20"})
    t, p, n = make()
    t.alerts.clear()
    old = {"id": 1, "symbol": "STK1", "time": ist_day(now - 3 * 86400) + " 10:00:00", "ts": now - 3 * 86400, "status": "UNEXPLAINED", "legs": []}
    t.alert_store.save_day(ist_day(old["ts"]), [old])
    check("old day's alerts readable by date + listed in days",
          t.alerts_for_day(ist_day(old["ts"]))[0]["id"] == 1 and ist_day(old["ts"]) in t.history_days())
    h = NewsHub(store_dir=os.path.join(TMP, "news"), keep_days=35)
    it = {"source": "NSE", "symbol": "STK1", "id": "nse:old", "time": "", "ts": now - 10 * 86400, "title": "old filing", "summary": "", "url": ""}
    h.store.merge_day(ist_day(it["ts"]), [it])
    h._store_nse([{**it, "id": "nse:new", "ts": now - 60, "title": "new filing"}])
    h.persist(force=True)
    check("news: 10-day-old item served for a 30-day window, not for 24 h",
          {x["id"] for x in h.latest_nse(since_ts=now - 30 * 86400)} == {"nse:old", "nse:new"}
          and {x["id"] for x in h.latest_nse(since_ts=now - 86400)} == {"nse:new"})
    check("news: today's items persisted to the day file", any(x["id"] == "nse:new" for x in h.store.load_day(ist_day(now))))


if __name__ == "__main__":
    for fn in [test_25000_allocation, test_recentre_and_new_strikes, test_detector, test_volume_burst,
               test_alerts_news_match, test_rest_merge_summary_ladder, test_vwap, test_dhan_packets,
               test_dhan_split, test_dhan_master_csv, test_dhan_websocket_roundtrip, test_news_relevance,
               test_history, test_history_in_tracker, test_trend, test_trend_saved_and_alerted,
               test_iv_greeks_and_spike, test_depth_and_context, test_stock_signals, test_futures, test_analytics, test_nse_extra_and_alerts, test_export_api, test_board_fast_topn, test_auto_never_dhan_from_env_only, test_dhan_news_and_805, test_day_store_month, test_speed]:
        print(fn.__name__)
        fn()
    print(f"\nALL {len(PASS)} CHECKS PASSED")
