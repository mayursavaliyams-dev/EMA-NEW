"""Stock-options SIDE TRACKER — 5000 CE/PE strikes across all F&O stocks, matched to news.

DISPLAY / RESEARCH ONLY. Nothing here places orders or feeds the strangle engine.

How it works
------------
1. Universe   — Upstox instrument master (all NSE F&O stock options, ~218 stocks) is
                downloaded and refreshed hourly. Per stock: nearest expiry, strikes nearest
                to the live spot (ATM ±N), CE + PE. The 5000-instrument budget (Upstox v3
                'ltpc' per-connection limit) is split evenly: 5000 // (2*218) = 11 strikes
                per stock, the remainder goes to the biggest contracts.
2. Stay fresh — every `trkRebalanceMin` the window is re-centred on the new spot and the
                WebSocket subscription is diffed (unsub old / sub new). If spot sits near the
                last listed strike, that stock's LIVE contract list is pulled from
                /v2/option/contract so strikes NSE opens intraday are picked up the same day.
3. Data       — Upstox v3 WebSocket (ltpc) for tick-level premium; REST full quotes every
                `trkQuotePollSec` for OI/volume (and as a fallback price source).
4. Detect     — premium move >= trkSpikePct within trkWindowMin  (UP or DOWN), or
                OI change >= trkOiPct within trkOiWindowMin (and >= trkMinOiLots lots).
5. Match      — on a spike, news for that stock (NSE filings + Google News) inside the last
                trkNewsLookbackMin -> NEWS-MATCH, else UNEXPLAINED. UNEXPLAINED alerts are
                re-checked for trkNewsAfterMin; news that lands later upgrades it to
                NEWS-AFTER (the move came first — often the most interesting case).
"""
import os
import sys
import gzip
import json
import time
import queue
import asyncio
import threading
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta

import httpx

import config

IST = timezone(timedelta(hours=5, minutes=30))
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
MASTER_PATH = os.path.join(config.DATA, "instruments_NSE.json.gz")
ALERTS_PATH = os.path.join(config.DATA, "tracker_alerts.json")
WS_LIMIT = 5000          # Upstox v3 ltpc instruments per connection — hard ceiling

SETTINGS = {
    "TRACKER_ENABLED": True,
    "trkBudget": 5000,          # instruments to track (capped at WS_LIMIT)
    "trkSpikePct": 20,          # premium move % ...
    "trkWindowMin": 5,          # ... within this many minutes
    "trkMinPremium": 2,         # ignore options cheaper than ₹ (noise)
    "trkMinAbsMove": 1,         # and moves smaller than ₹
    "trkOiPct": 25,             # OI change % ...
    "trkOiWindowMin": 15,       # ... within this many minutes
    "trkMinOiLots": 50,         # and at least this many lots of OI change
    "trkCooldownMin": 15,       # same contract+direction won't re-alert inside this
    "trkNewsLookbackMin": 60,   # news this far BEFORE the move counts as a match
    "trkNewsAfterMin": 60,      # keep re-checking UNEXPLAINED alerts this long
    "trkRebalanceMin": 3,       # re-centre ATM window / diff subscription
    "trkQuotePollSec": 60,      # REST OI/volume poll
    "trkAlwaysOn": False,       # True = run outside market hours too (testing)
}
for _k, _v in SETTINGS.items():
    config.DEFAULTS.setdefault(_k, _v)


def setting(k):
    v = config.cfg(k)
    return SETTINGS[k] if v is None else v


def now_ist():
    return datetime.now(IST)


def market_window(n=None):
    n = n or now_ist()
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 9 * 60 <= m <= 15 * 60 + 35


# ======================================================================
# Universe
# ======================================================================
class Universe:
    def __init__(self):
        self.stocks = {}        # symbol -> {underlying_key, name, lot, options{expiry_ms: {strike: {CE,PE}}}}
        self.meta = {}          # option key -> {symbol, strike, opt, expiry, lot}
        self.loaded_at = 0.0
        self.source = None

    @staticmethod
    def _need_download(path=MASTER_PATH):
        if not os.path.exists(path):
            return True
        age = time.time() - os.path.getmtime(path)
        return age > 3600       # Upstox republishes daily; hourly check is cheap (2 MB)

    def refresh(self, force=False, path=MASTER_PATH, rows=None):
        if rows is None:
            if force or self._need_download(path):
                try:
                    r = httpx.get(MASTER_URL, timeout=60, follow_redirects=True)
                    r.raise_for_status()
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(r.content)
                    os.replace(tmp, path)
                except Exception as e:
                    if not os.path.exists(path):
                        raise RuntimeError(f"instrument master download failed: {e}")
            with gzip.open(path, "rt", encoding="utf-8") as f:
                rows = json.load(f)
            self.source = "upstox-master"
        self._build(rows)
        self.loaded_at = time.time()

    def _build(self, rows):
        eq_names = {r["instrument_key"]: r.get("name") for r in rows if r.get("segment") == "NSE_EQ"}
        today_ms = int(now_ist().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        stocks = {}
        for r in rows:
            if (r.get("segment") != "NSE_FO" or r.get("instrument_type") not in ("CE", "PE")
                    or r.get("underlying_type") != "EQUITY"):
                continue
            exp = int(r.get("expiry") or 0)
            if exp < today_ms:
                continue
            sym = r["underlying_symbol"]
            s = stocks.setdefault(sym, {"symbol": sym, "underlying_key": r["underlying_key"],
                                        "name": eq_names.get(r["underlying_key"]) or r.get("name") or sym,
                                        "lot": r.get("lot_size"), "options": {}})
            self._add(s, exp, float(r["strike_price"]), r["instrument_type"], r["instrument_key"],
                      r.get("trading_symbol"))
        # keep any intraday-discovered strikes from the old universe
        for sym, old in self.stocks.items():
            if sym in stocks:
                for exp, strikes in old["options"].items():
                    for k, legs in strikes.items():
                        for opt, leg in legs.items():
                            if exp >= today_ms:
                                self._add(stocks[sym], exp, k, opt, leg["key"], leg.get("ts"))
        self.stocks = stocks

    @staticmethod
    def _add(s, exp, strike, opt, key, tsym=None):
        legs = s["options"].setdefault(exp, {}).setdefault(strike, {})
        if opt not in legs:
            legs[opt] = {"key": key, "ts": tsym}
            return True
        return False

    def merge_contracts(self, symbol, contracts):
        """Merge /v2/option/contract rows (live list). Returns count of NEW strikes."""
        s = self.stocks.get(symbol)
        if not s:
            return 0
        added = 0
        for c in contracts:
            opt = c.get("instrument_type")
            if opt not in ("CE", "PE") or not c.get("instrument_key"):
                continue
            try:
                d = datetime.strptime(c["expiry"], "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST)
            except Exception:
                continue
            exp = self._match_expiry(s, int(d.timestamp() * 1000))
            if self._add(s, exp, float(c["strike_price"]), opt, c["instrument_key"], c.get("trading_symbol")):
                added += 1
        return added

    @staticmethod
    def _match_expiry(s, exp_ms):
        # master stores expiry as end-of-day ms; snap API dates onto the same calendar day
        day = datetime.fromtimestamp(exp_ms / 1000, IST).date()
        for e in s["options"]:
            if datetime.fromtimestamp(e / 1000, IST).date() == day:
                return e
        return exp_ms

    @staticmethod
    def front_expiry(s, n=None):
        n = n or now_ist()
        cutoff = n.replace(hour=15, minute=30, second=0, microsecond=0)
        for e in sorted(s["options"]):
            d = datetime.fromtimestamp(e / 1000, IST)
            if d.date() > n.date() or (d.date() == n.date() and n < cutoff):
                return e
        return None

    def select(self, spots, budget):
        """Pick strikes nearest to spot, CE+PE, fitting `budget`.
        Returns (selection {key: meta}, edge_symbols [stocks whose ATM window hit the list edge])."""
        budget = min(int(budget), WS_LIMIT)
        live = [s for s in self.stocks.values() if spots.get(s["underlying_key"])]
        if not live:
            return {}, []
        # water-fill in LEGS (some strikes list only a CE or only a PE): equal share per
        # stock, leftovers go (2 legs at a time) to the biggest contracts with legs to spare
        info = {}
        for s in live:
            exp = self.front_expiry(s)
            if exp is None:
                continue
            spot = spots[s["underlying_key"]]
            legs = [(st, opt, leg) for st, d in s["options"][exp].items() for opt, leg in d.items()]
            legs.sort(key=lambda x: (abs(x[0] - spot), x[0], x[1]))
            info[s["symbol"]] = (s, exp, legs, sorted(s["options"][exp]))
        if not info:
            return {}, []
        base = max(2, (budget // len(info)) // 2 * 2)
        alloc = {sym: min(base, len(v[2])) for sym, v in info.items()}
        order = sorted(info, key=lambda sym: -(spots[info[sym][0]["underlying_key"]] * (info[sym][0]["lot"] or 0)))
        left = budget - sum(alloc.values())
        while left > 0:
            grew = False
            for sym in order:
                if left <= 0:
                    break
                room = len(info[sym][2]) - alloc[sym]
                if room > 0:
                    step = min(2, room, left)
                    alloc[sym] += step
                    left -= step
                    grew = True
            if not grew:
                break
        sel, edge = {}, []
        for sym, (s, exp, legs, strikes) in info.items():
            spot = spots[s["underlying_key"]]
            chosen = legs[:alloc[sym]]
            near = sorted({x[0] for x in chosen})
            atm = min(strikes, key=lambda x: (abs(x - spot), x))
            atm_i = strikes.index(atm)
            if (atm_i <= 1 or atm_i >= len(strikes) - 2
                    or near[0] == strikes[0] or near[-1] == strikes[-1]):
                edge.append(s["symbol"])
            for st, opt, leg in chosen:
                sel[leg["key"]] = {"symbol": s["symbol"], "strike": st, "opt": opt,
                                   "expiry": datetime.fromtimestamp(exp / 1000, IST).strftime("%Y-%m-%d"),
                                   "lot": s["lot"], "spot": round(spot, 2), "atm": atm,
                                   "trading_symbol": leg.get("ts")}
        # strict cap (a stock may list only CE or PE at some strike; never exceed)
        if len(sel) > budget:
            sel = dict(sorted(sel.items(), key=lambda kv: abs(kv[1]["strike"] - kv[1]["spot"]) / kv[1]["spot"])[:budget])
        return sel, edge


# ======================================================================
# Detector (pure — unit-tested in test_tracker.py)
# ======================================================================
class Detector:
    def __init__(self, get=setting):
        self.get = get
        self.px = defaultdict(deque)     # key -> deque[(ts, ltp)]
        self.oi = defaultdict(deque)     # key -> deque[(ts, oi)]
        self.last_alert = {}             # (key, kind, dir) -> ts

    def _cool(self, key, kind, d, ts):
        k = (key, kind, d)
        if ts - self.last_alert.get(k, -1e18) < self.get("trkCooldownMin") * 60:
            return False
        self.last_alert[k] = ts
        return True

    def on_price(self, key, ltp, ts):
        if not ltp or ltp <= 0:
            return None
        h = self.px[key]
        if h and ts - h[-1][0] < 1:          # thin to 1 sample/sec
            h[-1] = (h[-1][0], ltp)
        else:
            h.append((ts, ltp))
        win = self.get("trkWindowMin") * 60
        while h and ts - h[0][0] > win:
            h.popleft()
        if len(h) < 2:
            return None
        lo = min(p for _, p in h)
        hi = max(p for _, p in h)
        minp, mina, pct = self.get("trkMinPremium"), self.get("trkMinAbsMove"), self.get("trkSpikePct")
        if lo >= minp and ltp - lo >= mina and (ltp - lo) / lo * 100 >= pct:
            if self._cool(key, "PRICE", "UP", ts):
                return {"kind": "PRICE", "dir": "UP", "key": key, "from": lo, "to": ltp,
                        "pct": round((ltp - lo) / lo * 100, 1), "ts": ts}
        if hi >= minp and hi - ltp >= mina and (hi - ltp) / hi * 100 >= pct:
            if self._cool(key, "PRICE", "DOWN", ts):
                return {"kind": "PRICE", "dir": "DOWN", "key": key, "from": hi, "to": ltp,
                        "pct": round(-(hi - ltp) / hi * 100, 1), "ts": ts}
        return None

    def on_oi(self, key, oi, ts, lot):
        if oi is None or oi < 0:
            return None
        h = self.oi[key]
        h.append((ts, oi))
        win = self.get("trkOiWindowMin") * 60
        while h and ts - h[0][0] > win:
            h.popleft()
        base = h[0][1]
        if len(h) < 2 or base <= 0:
            return None
        chg = oi - base
        if abs(chg) < self.get("trkMinOiLots") * (lot or 1):
            return None
        pct = chg / base * 100
        if abs(pct) >= self.get("trkOiPct"):
            d = "UP" if chg > 0 else "DOWN"
            if self._cool(key, "OI", d, ts):
                return {"kind": "OI", "dir": d, "key": key, "from": base, "to": oi,
                        "pct": round(pct, 1), "ts": ts}
        return None

    def change_pct(self, key):
        h = self.px.get(key)
        if not h or len(h) < 2 or not h[0][1]:
            return None
        return round((h[-1][1] - h[0][1]) / h[0][1] * 100, 1)

    def forget(self, keys):
        for k in keys:
            self.px.pop(k, None)
            self.oi.pop(k, None)


# ======================================================================
# Tracker (orchestrator)
# ======================================================================
class StockOptionsTracker:
    def __init__(self, conn, news, universe=None, detector=None):
        self.conn, self.news = conn, news
        self.u = universe or Universe()
        self.det = detector or Detector()
        self.sel = {}                     # key -> meta (current subscription)
        self.latest = {}                  # key -> {ltp, oi, volume, ts}
        self.spots = {}
        self.alerts = deque(maxlen=1000)
        self._alert_seq = 0
        self._events = queue.Queue()
        self._lock = threading.RLock()
        self._ws_cmds = None              # asyncio.Queue inside the WS loop
        self._ws_loop = None
        self._stop = threading.Event()
        self.stats = {"ws": "idle", "ws_ticks": 0, "quote_polls": 0, "last_rebalance": None,
                      "last_quote": None, "new_strikes_today": 0, "errors": {}}
        self._contract_ts = {}            # symbol -> last live-contract fetch
        self._t_rebal = self._t_quote = self._t_master = self._t_recheck = 0.0
        self._load_alerts()

    # ---------------- persistence ----------------
    def _load_alerts(self):
        try:
            with open(ALERTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for a in data[-1000:]:
                self.alerts.append(a)
            self._alert_seq = max((a.get("id", 0) for a in self.alerts), default=0)
        except Exception:
            pass

    def _save_alerts(self):
        try:
            tmp = ALERTS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(list(self.alerts), f)
            os.replace(tmp, ALERTS_PATH)
        except Exception as e:
            self.stats["errors"]["save"] = str(e)[:200]

    # ---------------- lifecycle ----------------
    def start(self):
        threading.Thread(target=self._main_loop, name="trk-main", daemon=True).start()
        threading.Thread(target=self._ws_thread, name="trk-ws", daemon=True).start()

    def active(self):
        return bool(setting("TRACKER_ENABLED")) and (setting("trkAlwaysOn") or market_window())

    def _main_loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.stats["errors"]["main"] = f"{type(e).__name__}: {e}"[:200]
            self._stop.wait(2)

    def tick(self, now=None):
        """One scheduler pass (also called directly by tests)."""
        now = now or time.time()
        self.news.poll_nse()                                  # cheap, also off-hours
        if not self.active():
            self.stats["ws"] = "paused (outside market hours)" if setting("TRACKER_ENABLED") else "disabled"
            self._drain_events()
            return
        if now - self._t_master > 3600 or not self.u.stocks:
            self._t_master = now
            self.u.refresh()
        if now - self._t_rebal > setting("trkRebalanceMin") * 60 or not self.sel:
            self._t_rebal = now
            self.rebalance()
        if now - self._t_quote > setting("trkQuotePollSec"):
            self._t_quote = now
            self.poll_quotes()
        self._drain_events()
        if now - self._t_recheck > 120:
            self._t_recheck = now
            self.recheck_unexplained()

    # ---------------- universe / subscription ----------------
    def rebalance(self):
        keys = [s["underlying_key"] for s in self.u.stocks.values()]
        spots = {}
        for i in range(0, len(keys), 450):
            spots.update({k: v for k, v in self.conn.ltp(keys[i:i + 450]).items() if v})
        if not spots:
            raise RuntimeError("no spot prices (token expired?)")
        self.spots = spots
        sel, edge = self.u.select(spots, setting("trkBudget"))
        if edge:                         # spot near the last listed strike -> look for new strikes
            added = 0
            due = [x for x in edge if time.time() - self._contract_ts.get(x, 0) > 900][:25]
            for sym in due:
                self._contract_ts[sym] = time.time()
                try:
                    added += self.u.merge_contracts(sym, self.conn.option_contracts(
                        self.u.stocks[sym]["underlying_key"]))
                except Exception as e:
                    self.stats["errors"]["contracts"] = str(e)[:200]
            if added:
                self.stats["new_strikes_today"] += added
                sel, edge = self.u.select(spots, setting("trkBudget"))
        with self._lock:
            old = set(self.sel)
            self.sel = sel
        new = set(sel)
        gone, fresh = old - new, new - old
        self.det.forget(gone)
        for k in gone:
            self.latest.pop(k, None)
        self._ws_send("unsub", sorted(gone))
        self._ws_send("sub", sorted(fresh))
        self.stats["last_rebalance"] = now_ist().strftime("%H:%M:%S")
        self.stats["edge_stocks"] = edge
        return {"tracked": len(sel), "added": len(fresh), "removed": len(gone)}

    # ---------------- REST quotes (OI + fallback price) ----------------
    def poll_quotes(self):
        with self._lock:
            sel = dict(self.sel)
        if not sel:
            return
        q = self.conn.quotes(list(sel))
        ts = time.time()
        for k, v in q.items():
            m = sel.get(k)
            if not m:
                continue
            cur = self.latest.setdefault(k, {})
            cur.update({"oi": v.get("oi"), "volume": v.get("volume")})
            if v.get("ltp") and cur.get("src") != "ws":
                cur.update({"ltp": v["ltp"], "ts": ts})
                ev = self.det.on_price(k, v["ltp"], ts)
                if ev:
                    self._events.put(ev)
            ev = self.det.on_oi(k, v.get("oi"), ts, m.get("lot"))
            if ev:
                self._events.put(ev)
        self.stats["quote_polls"] += 1
        self.stats["last_quote"] = now_ist().strftime("%H:%M:%S")

    def on_tick(self, key, ltp, ts=None):
        """WebSocket tick (called from the WS thread)."""
        ts = ts or time.time()
        if key not in self.sel:
            return
        cur = self.latest.setdefault(key, {})
        cur.update({"ltp": ltp, "ts": ts, "src": "ws"})
        self.stats["ws_ticks"] += 1
        ev = self.det.on_price(key, ltp, ts)
        if ev:
            self._events.put(ev)

    # ---------------- alerts + news matching ----------------
    def _drain_events(self):
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            self.handle_event(ev)

    def handle_event(self, ev):
        m = self.sel.get(ev["key"]) or {}
        sym = m.get("symbol", "?")
        leg = {"key": ev["key"], "strike": m.get("strike"), "opt": m.get("opt"),
               "from": ev["from"], "to": ev["to"], "pct": ev["pct"],
               "time": datetime.fromtimestamp(ev["ts"], IST).strftime("%H:%M:%S")}
        with self._lock:
            # merge strikes of the same stock moving together into ONE alert
            for a in reversed(self.alerts):
                if (a["symbol"] == sym and a["kind"] == ev["kind"] and a["dir"] == ev["dir"]
                        and ev["ts"] - a["ts"] <= setting("trkWindowMin") * 60):
                    a["legs"].append(leg)
                    a["max_pct"] = max(a["max_pct"], abs(ev["pct"]))
                    self._save_alerts()
                    return a
        s = self.u.stocks.get(sym, {})
        news = self.news.news_for(sym, s.get("name", sym),
                                  since_ts=ev["ts"] - setting("trkNewsLookbackMin") * 60,
                                  until_ts=ev["ts"] + 60)
        self._alert_seq += 1
        a = {"id": self._alert_seq, "symbol": sym, "name": s.get("name", sym),
             "kind": ev["kind"], "dir": ev["dir"], "ts": ev["ts"],
             "time": datetime.fromtimestamp(ev["ts"], IST).strftime("%Y-%m-%d %H:%M:%S"),
             "spot": m.get("spot"), "expiry": m.get("expiry"), "max_pct": abs(ev["pct"]),
             "legs": [leg], "status": "NEWS-MATCH" if news else "UNEXPLAINED",
             "news": news[:8]}
        with self._lock:
            self.alerts.append(a)
            self._save_alerts()
        return a

    def recheck_unexplained(self, now=None):
        now = now or time.time()
        horizon = setting("trkNewsAfterMin") * 60
        changed = False
        for a in list(self.alerts):
            if a["status"] != "UNEXPLAINED" or now - a["ts"] > horizon:
                continue
            items = self.news.news_for(a["symbol"], a.get("name", a["symbol"]),
                                       since_ts=a["ts"], until_ts=a["ts"] + horizon)
            if items:
                a["status"], a["news"] = "NEWS-AFTER", items[:8]
                changed = True
        if changed:
            self._save_alerts()

    # ---------------- WebSocket (Upstox v3, ltpc) ----------------
    def _ws_send(self, method, keys):
        if not keys or not self._ws_loop or not self._ws_cmds:
            return
        self._ws_loop.call_soon_threadsafe(self._ws_cmds.put_nowait, (method, list(keys)))

    def _ws_thread(self):
        try:
            import websockets                      # noqa: F401
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if root not in sys.path:
                sys.path.append(root)
            import upstox_feed_pb2                # noqa: F401  (repo root)
        except Exception as e:
            self.stats["ws"] = f"unavailable ({e}) — REST-only mode"
            return
        asyncio.run(self._ws_main())

    async def _ws_main(self):
        import websockets
        import upstox_feed_pb2 as pb
        self._ws_loop = asyncio.get_running_loop()
        self._ws_cmds = asyncio.Queue()
        while not self._stop.is_set():
            if not self.active() or not self.sel:
                await asyncio.sleep(5)
                continue
            if not config.access_token():
                self.stats["ws"] = "no Upstox token (token.json) — waiting"
                await asyncio.sleep(10)
                continue
            try:
                r = await asyncio.to_thread(httpx.get, config.UPSTOX + "/v3/feed/market-data-feed/authorize",
                                            headers={"Authorization": "Bearer " + (config.access_token() or ""),
                                                     "Accept": "application/json", "User-Agent": config.USER_AGENT},
                                            timeout=20, follow_redirects=False)
                if r.status_code in (301, 302, 307, 308):
                    url = r.headers["location"]
                else:
                    r.raise_for_status()
                    url = r.json()["data"]["authorizedRedirectUri"]
                async with websockets.connect(url, additional_headers={"User-Agent": config.USER_AGENT},
                                              max_size=None, ping_interval=20) as ws:
                    while not self._ws_cmds.empty():        # full (re)subscribe below covers queued diffs
                        self._ws_cmds.get_nowait()
                    await self._ws_sub(ws, "sub", list(self.sel))
                    self.stats["ws"] = f"connected ({len(self.sel)} subscribed)"
                    recv = asyncio.ensure_future(ws.recv())
                    cmd = asyncio.ensure_future(self._ws_cmds.get())
                    while self.active():
                        done, _ = await asyncio.wait({recv, cmd}, timeout=10,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if recv in done:
                            msg = recv.result()
                            if isinstance(msg, (bytes, bytearray)):
                                self._decode(pb, msg)
                            recv = asyncio.ensure_future(ws.recv())
                        if cmd in done:
                            method, keys = cmd.result()
                            await self._ws_sub(ws, method, keys)
                            self.stats["ws"] = f"connected ({len(self.sel)} subscribed)"
                            cmd = asyncio.ensure_future(self._ws_cmds.get())
                    recv.cancel()
                    cmd.cancel()
                    self.stats["ws"] = "paused (outside market hours)"
            except Exception as e:
                self.stats["ws"] = f"reconnecting: {type(e).__name__}: {str(e)[:120]}"
                await asyncio.sleep(5)

    @staticmethod
    async def _ws_sub(ws, method, keys):
        for i in range(0, len(keys), 1000):
            await ws.send(json.dumps({"guid": f"trk-{method}-{i}", "method": method,
                                      "data": {"mode": "ltpc", "instrumentKeys": keys[i:i + 1000]}}).encode())

    def _decode(self, pb, raw):
        resp = pb.FeedResponse()
        try:
            resp.ParseFromString(raw)
        except Exception:
            return
        ts = (resp.currentTs or 0) / 1000 or time.time()
        for key, feed in resp.feeds.items():
            which = feed.WhichOneof("FeedUnion")
            ltp = None
            if which == "ltpc":
                ltp = feed.ltpc.ltp
            elif which == "fullFeed" and feed.fullFeed.WhichOneof("FullFeedUnion") == "marketFF":
                ltp = feed.fullFeed.marketFF.ltpc.ltp
            elif which == "firstLevelWithGreeks":
                ltp = feed.firstLevelWithGreeks.ltpc.ltp
            if ltp:
                self.on_tick(key, ltp, ts)

    # ---------------- views for the API ----------------
    def status(self):
        with self._lock:
            n = len(self.sel)
            per = defaultdict(int)
            for m in self.sel.values():
                per[m["symbol"]] += 1
        return {"enabled": bool(setting("TRACKER_ENABLED")), "active": self.active(),
                "stocks_in_master": len(self.u.stocks), "stocks_tracked": len(per),
                "instruments_tracked": n, "budget": min(int(setting("trkBudget")), WS_LIMIT),
                "per_stock": (min(per.values()), max(per.values())) if per else (0, 0),
                "alerts_today": sum(1 for a in self.alerts if a["time"][:10] == now_ist().strftime("%Y-%m-%d")),
                "news_errors": self.news.errors, **self.stats,
                "settings": {k: setting(k) for k in SETTINGS}}

    def board(self, symbol=None, limit=60):
        """Live table. With symbol: that stock's whole strike ladder. Without: top movers."""
        with self._lock:
            sel = dict(self.sel)
        rows = []
        for k, m in sel.items():
            if symbol and m["symbol"] != symbol.upper():
                continue
            lv = self.latest.get(k, {})
            rows.append({**m, "key": k, "ltp": lv.get("ltp"), "oi": lv.get("oi"),
                         "volume": lv.get("volume"), "chg_win_pct": self.det.change_pct(k)})
        if symbol:
            rows.sort(key=lambda r: (r["strike"], r["opt"]))
            return rows
        rows = [r for r in rows if r["chg_win_pct"] is not None]
        rows.sort(key=lambda r: -abs(r["chg_win_pct"]))
        return rows[:limit]

    def symbols(self):
        return sorted({m["symbol"] for m in self.sel.values()}) or sorted(self.u.stocks)
