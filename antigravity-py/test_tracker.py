"""Offline tests for the stock-options side tracker (no token, no network).

Run:  py test_tracker.py
"""
import os
import tempfile
import time
from datetime import datetime, timedelta

import config
from trackers import stock_options as T

T.ALERTS_PATH = os.path.join(tempfile.mkdtemp(), "alerts.json")   # never touch real data
SET = dict(T.SETTINGS, trkAlwaysOn=True)
T.setting = lambda k: SET[k]
IST = T.IST


def exp_ms(days):
    d = (datetime.now(IST) + timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=0)
    return int(d.timestamp() * 1000)


def master(n_stocks=218, strikes=30, step=10.0, base=1000.0):
    rows = []
    for i in range(n_stocks):
        sym, ukey = f"STK{i}", f"NSE_EQ|INE{i:06d}"
        rows.append({"segment": "NSE_EQ", "instrument_key": ukey, "name": f"STOCK {i} LIMITED"})
        for e in (exp_ms(5), exp_ms(35)):
            for j in range(strikes):
                k = base + j * step
                for opt in ("CE", "PE"):
                    rows.append({"segment": "NSE_FO", "instrument_type": opt, "underlying_type": "EQUITY",
                                 "underlying_symbol": sym, "underlying_key": ukey, "expiry": e,
                                 "strike_price": k, "lot_size": 500,
                                 "instrument_key": f"NSE_FO|{sym}-{e}-{k:.0f}-{opt}",
                                 "trading_symbol": f"{sym} {k:.0f} {opt}"})
    return rows


class FakeConn:
    def __init__(self, spot=1150.0):
        self.spot = spot
        self.contract_calls = []
        self.extra_contracts = []
        self.q = {}

    def ltp(self, keys):
        return {k: self.spot for k in keys}

    def quotes(self, keys):
        return {k: self.q[k] for k in keys if k in self.q}

    def option_contracts(self, ukey):
        self.contract_calls.append(ukey)
        return self.extra_contracts


class FakeNews:
    def __init__(self):
        self.items = {}
        self.errors = {}

    def poll_nse(self, force=False):
        pass

    def news_for(self, symbol, name, since_ts=None, until_ts=None, fetch=True):
        return [i for i in self.items.get(symbol, [])
                if (since_ts is None or i["ts"] >= since_ts) and (until_ts is None or i["ts"] <= until_ts)]


def make(spot=1150.0, **kw):
    u = T.Universe()
    u.refresh(rows=master(**kw))
    c, n = FakeConn(spot), FakeNews()
    t = T.StockOptionsTracker(c, n, universe=u, detector=T.Detector(get=lambda k: SET[k]))
    t.alerts.clear()
    return t, c, n


PASS = []


def check(name, cond, detail=""):
    assert cond, f"FAIL {name} {detail}"
    PASS.append(name)
    print("  ok ", name, detail)


def test_budget_5000():
    t, c, n = make()
    r = t.rebalance()
    per = {}
    for m in t.sel.values():
        per[m["symbol"]] = per.get(m["symbol"], 0) + 1
    check("218 stocks covered", len(per) == 218, f"({len(per)})")
    check("<= 5000 instruments", len(t.sel) <= 5000, f"({len(t.sel)})")
    check("budget nearly filled", len(t.sel) >= 4990, f"({len(t.sel)})")
    check("11-12 strikes per stock (22-24 legs)", min(per.values()) >= 22 and max(per.values()) <= 24,
          f"({min(per.values())}-{max(per.values())})")
    m = next(iter(t.sel.values()))
    check("front expiry chosen", m["expiry"] == (datetime.now(IST) + timedelta(days=5)).strftime("%Y-%m-%d"))
    strikes = sorted({m["strike"] for m in t.sel.values() if m["symbol"] == "STK0"})
    check("window centred on ATM", strikes[0] <= 1150 <= strikes[-1] and abs((strikes[0] + strikes[-1]) / 2 - 1150) <= 10,
          f"({strikes[0]}..{strikes[-1]})")
    check("returns diff", r["added"] == len(t.sel) and r["removed"] == 0)


def test_recentre_on_spot_move():
    t, c, n = make()
    t.rebalance()
    c.spot = 1200.0
    r = t.rebalance()
    strikes = sorted({m["strike"] for m in t.sel.values() if m["symbol"] == "STK0"})
    check("re-centres when spot moves", strikes[0] <= 1200 <= strikes[-1], f"({strikes[0]}..{strikes[-1]})")
    check("unsub old / sub new diff", r["added"] > 0 and r["removed"] == r["added"], f"(+{r['added']} -{r['removed']})")


def test_new_strikes_intraday():
    # spot runs past the last listed strike -> fetch live contracts -> new strikes join
    t, c, n = make(spot=1280.0)                 # last listed strike 1290
    e = datetime.fromtimestamp(exp_ms(5) / 1000, IST).strftime("%Y-%m-%d")
    c.extra_contracts = [{"instrument_type": opt, "instrument_key": f"NSE_FO|NEW-{k}-{opt}", "expiry": e,
                          "strike_price": k, "trading_symbol": f"NEW {k} {opt}"}
                         for k in (1300.0, 1310.0, 1320.0) for opt in ("CE", "PE")]
    t.rebalance()
    check("edge stocks trigger live contract fetch", len(c.contract_calls) > 0, f"({len(c.contract_calls)} calls)")
    check("new strikes merged & subscribed", any(k.startswith("NSE_FO|NEW-1300") for k in t.sel))
    t.rebalance()
    check("contract fetch: 25/round, never the same stock twice inside cooldown",
          len(c.contract_calls) == 50 and len(set(c.contract_calls)) == 50, f"({len(c.contract_calls)})")


def test_detector():
    d = T.Detector(get=lambda k: SET[k])
    t0 = 1_000_000.0
    check("no alert on first tick", d.on_price("A", 10, t0) is None)
    check("no alert on small move", d.on_price("A", 11, t0 + 30) is None)
    ev = d.on_price("A", 12.5, t0 + 60)
    check("UP spike >=20% in window", ev and ev["dir"] == "UP" and ev["pct"] == 25.0, str(ev and ev["pct"]))
    check("cooldown blocks repeat", d.on_price("A", 13, t0 + 90) is None)
    d.on_price("B", 1.0, t0)
    check("cheap option ignored (noise)", d.on_price("B", 1.8, t0 + 30) is None)
    d.on_price("C", 20, t0)
    check("move outside window ignored", d.on_price("C", 26, t0 + 6 * 60) is None)
    d.on_price("D", 50, t0)
    ev = d.on_price("D", 38, t0 + 60)
    check("DOWN crash detected", ev and ev["dir"] == "DOWN", str(ev and ev["pct"]))
    d.on_oi("E", 100000, t0, 500)
    check("small OI change ignored", d.on_oi("E", 110000, t0 + 60, 500) is None)
    ev = d.on_oi("E", 150000, t0 + 120, 500)
    check("OI build-up detected", ev and ev["kind"] == "OI" and ev["pct"] == 50.0)


def test_alerts_news_match():
    t, c, n = make()
    t.rebalance()
    keys = [k for k, m in t.sel.items() if m["symbol"] == "STK3" and m["opt"] == "CE"][:2]
    now = time.time()
    n.items["STK3"] = [{"source": "NSE", "ts": now - 600, "title": "Outcome of Board Meeting", "id": "x"}]
    for k in keys:
        t.on_tick(k, 10, now - 120)
        t.on_tick(k, 13, now)
    t._drain_events()
    a = [x for x in t.alerts if x["symbol"] == "STK3"]
    check("one alert per stock (strikes merged)", len(a) == 1 and len(a[0]["legs"]) == 2, f"({len(a)} alerts)")
    check("NEWS-MATCH when news precedes move", a[0]["status"] == "NEWS-MATCH")

    key = [k for k, m in t.sel.items() if m["symbol"] == "STK7"][0]
    t.on_tick(key, 10, now - 60)
    t.on_tick(key, 15, now)
    t._drain_events()
    b = [x for x in t.alerts if x["symbol"] == "STK7"][0]
    check("UNEXPLAINED when no news", b["status"] == "UNEXPLAINED")
    n.items["STK7"] = [{"source": "Google News", "ts": now + 300, "title": "STK7 wins order", "id": "y"}]
    t.recheck_unexplained(now=now + 400)
    check("upgraded to NEWS-AFTER when news lands later", b["status"] == "NEWS-AFTER")


def test_quote_poll_oi():
    t, c, n = make()
    t.rebalance()
    k = next(iter(t.sel))
    c.q = {k: {"ltp": 20, "oi": 100000, "volume": 10}}
    t.poll_quotes()
    c.q = {k: {"ltp": 20, "oi": 200000, "volume": 50}}
    t.poll_quotes()
    t._drain_events()
    check("OI alert from REST poll", any(a["kind"] == "OI" for a in t.alerts))
    check("board shows the strike ladder", len(t.board(t.sel[k]["symbol"])) >= 22)


def test_news_relevance():
    from trackers.news import is_relevant, clean_name
    check("clean_name drops LIMITED", clean_name("RELIANCE INDUSTRIES LIMITED") == "Reliance Industries")
    check("headline about stock is relevant", is_relevant("Reliance shares jump 3%", "RELIANCE", "RELIANCE INDUSTRIES LTD"))
    check("generic headline rejected", not is_relevant("Industries see slow growth", "RELIANCE", "RELIANCE INDUSTRIES LTD"))


if __name__ == "__main__":
    for fn in [test_budget_5000, test_recentre_on_spot_move, test_new_strikes_intraday, test_detector,
               test_alerts_news_match, test_quote_poll_oi, test_news_relevance]:
        print(fn.__name__)
        fn()
    print(f"\nALL {len(PASS)} CHECKS PASSED")
