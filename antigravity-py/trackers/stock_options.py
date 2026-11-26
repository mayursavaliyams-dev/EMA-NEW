"""Stock-options SIDE TRACKER — up to 25,000 CE/PE strikes across all F&O stocks, matched to news.

DISPLAY / RESEARCH ONLY. Nothing here places orders or feeds the strangle engine.

Pipeline
--------
1. Provider   — Dhan (preferred when dhan.json is present: 5 WS x 5000 = 25,000 live in FULL
                mode) else Upstox (WS ticks for the 5,000 strikes nearest ATM + REST full quotes
                for all 25,000, paced under Upstox's rate limits). See trackers/providers.py.
2. Universe   — all ~218 F&O stocks, front expiry, strikes nearest spot, CE+PE, water-filled
                into the budget. Re-centred every trkRebalanceMin (subscription diffed). New
                strikes: master conditionally re-downloaded; Upstox also pulls live contracts
                when spot nears the last listed strike.
3. State      — per strike: LTP, VWAP, volume, OI, OI day hi/lo, prev OI, OHLC, buy/sell qty.
4. Detect     — PRICE  : premium ±trkSpikePct within trkWindowMin
                OI     : OI ±trkOiPct within trkOiWindowMin (>= trkMinOiLots lots)
                VOLUME : volume in trkVolWindowMin >= trkVolMult x the day's average pace
                (all O(1) amortised per update — monotonic deques)
5. Match      — news (NSE filings + Google News) in the prior trkNewsLookbackMin -> NEWS-MATCH,
                else UNEXPLAINED, upgraded to NEWS-AFTER if news lands within trkNewsAfterMin.
6. Views      — per-stock totals (CE/PE OI, PCR, OI change, volume, OI support/resistance,
                max-volume strike), strike ladder with high/low mapping & VWAP, candles + VWAP + OI.
"""
import os
import json
import time
import queue
import threading
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta

import config
from trackers.providers import DhanProvider, UpstoxProvider

IST = timezone(timedelta(hours=5, minutes=30))
ALERTS_PATH = os.path.join(config.DATA, "tracker_alerts.json")
HARD_CAP = 25000

SETTINGS = {
    "TRACKER_ENABLED": True,
    "trkProvider": "auto",      # auto | dhan | upstox   (auto = Dhan if dhan.json works, else Upstox)
    "trkBudget": 25000,         # strikes to track (Dhan: all live; Upstox: 5000 live + rest via REST)
    "trkDhanConnections": 5,    # Dhan allows 5 WS connections x 5000 instruments
    "trkSpikePct": 20,          # premium move % ...
    "trkWindowMin": 5,          # ... within this many minutes
    "trkMinPremium": 2,         # ignore options cheaper than ₹ (noise)
    "trkMinAbsMove": 1,         # and moves smaller than ₹
    "trkOiPct": 25,             # OI change % ...
    "trkOiWindowMin": 15,       # ... within this many minutes
    "trkMinOiLots": 50,         # and at least this many lots of OI change
    "trkVolMult": 5,            # volume in window >= this x the day's average pace
    "trkVolWindowMin": 5,
    "trkMinVolLots": 20,        # and at least this many lots traded in the window
    "trkCooldownMin": 15,       # same contract+kind+direction won't re-alert inside this
    "trkNewsLookbackMin": 60,   # news this far BEFORE the move counts as a match
    "trkNewsAfterMin": 60,      # keep re-checking UNEXPLAINED alerts this long
    "trkRebalanceMin": 3,       # re-centre ATM window / diff subscription
    "trkQuotePollSec": 30,      # REST snapshot pace (auto-slowed to respect broker limits)
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


def session_open_ts(ts):
    d = datetime.fromtimestamp(ts, IST)
    return d.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()


# ======================================================================
# Universe
# ======================================================================
class Universe:
    def __init__(self):
        self.stocks = {}        # symbol -> {symbol, underlying_key, name, lot, options{exp_ms: {strike: {CE,PE}}}}
        self.loaded_at = 0.0

    def build(self, rows):
        """rows: normalized dicts {symbol, underlying_key, name, lot, expiry_ms, strike, opt, key, ts}."""
        today_ms = int(now_ist().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        stocks = {}
        for r in rows:
            if r["expiry_ms"] < today_ms:
                continue
            s = stocks.get(r["symbol"])
            if s is None:
                s = stocks[r["symbol"]] = {"symbol": r["symbol"], "underlying_key": r["underlying_key"],
                                           "name": r.get("name") or r["symbol"], "lot": r.get("lot"),
                                           "options": {}}
            self._add(s, r["expiry_ms"], r["strike"], r["opt"], r["key"], r.get("ts"))
        for sym, old in self.stocks.items():          # keep intraday-discovered strikes
            if sym in stocks and old["underlying_key"] == stocks[sym]["underlying_key"]:
                for exp, strikes in old["options"].items():
                    if exp >= today_ms:
                        for k, legs in strikes.items():
                            for opt, leg in legs.items():
                                self._add(stocks[sym], exp, k, opt, leg["key"], leg.get("ts"))
        self.stocks = stocks
        self.loaded_at = time.time()

    @staticmethod
    def _add(s, exp, strike, opt, key, tsym=None):
        legs = s["options"].setdefault(exp, {}).setdefault(strike, {})
        if opt not in legs:
            legs[opt] = {"key": key, "ts": tsym}
            return True
        return False

    def merge_contracts(self, symbol, contracts):
        """Merge Upstox /v2/option/contract rows (live list). Returns count of NEW legs."""
        s = self.stocks.get(symbol)
        if not s:
            return 0
        added = 0
        for c in contracts or []:
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

    @staticmethod
    def expiries_ahead(s, n=None):
        n = n or now_ist()
        cutoff = n.replace(hour=15, minute=30, second=0, microsecond=0)
        out = []
        for e in sorted(s["options"]):
            d = datetime.fromtimestamp(e / 1000, IST)
            if d.date() > n.date() or (d.date() == n.date() and n < cutoff):
                out.append(e)
        return out

    def select(self, spots, budget):
        """Legs nearest spot, water-filled across stocks to fit `budget`: front expiry first;
        if the broker lists fewer front-expiry strikes than the budget (Upstox ~12.6k), the
        remainder goes to the next (then far) expiry. Returns (selection {key: meta}, edge_symbols)."""
        budget = min(int(budget), HARD_CAP)
        sel, edge = self._fill(spots, budget, 0)
        for idx in (1, 2):                       # then next / far expiry until the budget is used
            left = budget - len(sel)
            if left <= 0:
                break
            sel.update(self._fill(spots, left, idx)[0])
        return sel, edge

    def _fill(self, spots, budget, exp_idx):
        info = {}
        for s in self.stocks.values():
            spot = spots.get(s["underlying_key"])
            exps = self.expiries_ahead(s) if spot else []
            if len(exps) <= exp_idx:
                continue
            exp = exps[exp_idx]
            legs = [(st, opt, leg) for st, d in s["options"][exp].items() for opt, leg in d.items()]
            legs.sort(key=lambda x: (abs(x[0] - spot), x[0], x[1]))
            info[s["symbol"]] = (s, exp, legs, sorted(s["options"][exp]), spot)
        if not info or budget <= 0:
            return {}, []
        base = max(2, (budget // len(info)) // 2 * 2)
        alloc = {sym: min(base, len(v[2])) for sym, v in info.items()}
        order = sorted(info, key=lambda sym: -(info[sym][4] * (info[sym][0]["lot"] or 0)))
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
        for sym, (s, exp, legs, strikes, spot) in info.items():
            chosen = legs[:alloc[sym]]
            if not chosen:
                continue
            near = sorted({x[0] for x in chosen})
            atm = min(strikes, key=lambda x: (abs(x - spot), x))
            atm_i = strikes.index(atm)
            if atm_i <= 1 or atm_i >= len(strikes) - 2 or near[0] == strikes[0] or near[-1] == strikes[-1]:
                edge.append(sym)
            exp_s = datetime.fromtimestamp(exp / 1000, IST).strftime("%Y-%m-%d")
            for st, opt, leg in chosen:
                sel[leg["key"]] = {"symbol": sym, "strike": st, "opt": opt, "expiry": exp_s,
                                   "lot": s["lot"], "spot": round(spot, 2), "atm": atm, "front": exp_idx == 0,
                                   # live-stream priority: front expiry first, then nearest ATM
                                   "dist": exp_idx + abs(st - spot) / spot, "trading_symbol": leg.get("ts")}
        if len(sel) > budget:
            sel = dict(sorted(sel.items(), key=lambda kv: kv[1]["dist"])[:budget])
        return sel, edge


# ======================================================================
# Detector — O(1) amortised per update (monotonic min/max deques)
# ======================================================================
class _Win:
    """Sliding time window keeping min & max in O(1) amortised."""
    __slots__ = ("q", "mn", "mx", "last_ts")

    def __init__(self):
        self.q, self.mn, self.mx, self.last_ts = deque(), deque(), deque(), -1e18

    def push(self, ts, v, span, thin=1.0):
        # thinning: within `thin` seconds the latest sample is replaced in q (keeps q small),
        # but min/max still see every value (an intra-second spike is not lost)
        if self.q and ts - self.last_ts < thin:
            ts = self.last_ts
            self.q[-1] = (ts, v)
        else:
            self.q.append((ts, v))
            self.last_ts = ts
        mn, mx = self.mn, self.mx
        while mn and mn[-1][1] >= v:
            mn.pop()
        mn.append((ts, v))
        while mx and mx[-1][1] <= v:
            mx.pop()
        mx.append((ts, v))
        cut = ts - span
        q = self.q
        while q and q[0][0] < cut:
            q.popleft()
        while mn[0][0] < cut:
            mn.popleft()
        while mx[0][0] < cut:
            mx.popleft()

    def first(self):
        return self.q[0]


class Detector:
    def __init__(self, get=setting):
        self.get = get
        self.px = defaultdict(_Win)
        self.oi = defaultdict(_Win)
        self.vol = defaultdict(_Win)
        self.last_alert = {}

    def _cool(self, key, kind, d, ts):
        k = (key, kind, d)
        if ts - self.last_alert.get(k, -1e18) < self.get("trkCooldownMin") * 60:
            return False
        self.last_alert[k] = ts
        return True

    def on_price(self, key, ltp, ts):
        if not ltp or ltp <= 0:
            return None
        w = self.px[key]
        w.push(ts, ltp, self.get("trkWindowMin") * 60)
        if len(w.q) < 2:
            return None
        lo, hi = w.mn[0][1], w.mx[0][1]
        minp, mina, pct = self.get("trkMinPremium"), self.get("trkMinAbsMove"), self.get("trkSpikePct")
        if lo >= minp and ltp - lo >= mina and (ltp - lo) / lo * 100 >= pct and self._cool(key, "PRICE", "UP", ts):
            return {"kind": "PRICE", "dir": "UP", "key": key, "from": lo, "to": ltp,
                    "pct": round((ltp - lo) / lo * 100, 1), "ts": ts}
        if hi >= minp and hi - ltp >= mina and (hi - ltp) / hi * 100 >= pct and self._cool(key, "PRICE", "DOWN", ts):
            return {"kind": "PRICE", "dir": "DOWN", "key": key, "from": hi, "to": ltp,
                    "pct": round(-(hi - ltp) / hi * 100, 1), "ts": ts}
        return None

    def on_oi(self, key, oi, ts, lot):
        if oi is None or oi < 0:
            return None
        w = self.oi[key]
        w.push(ts, oi, self.get("trkOiWindowMin") * 60, thin=10)
        base = w.first()[1]
        if len(w.q) < 2 or base <= 0:
            return None
        chg = oi - base
        if abs(chg) < self.get("trkMinOiLots") * (lot or 1):
            return None
        pct = chg / base * 100
        if abs(pct) >= self.get("trkOiPct"):
            d = "UP" if chg > 0 else "DOWN"
            if self._cool(key, "OI", d, ts):
                return {"kind": "OI", "dir": d, "key": key, "from": base, "to": oi, "pct": round(pct, 1), "ts": ts}
        return None

    def on_volume(self, key, vol, ts, lot):
        """Cumulative day volume -> burst vs the day's average pace."""
        if vol is None or vol <= 0:
            return None
        span = self.get("trkVolWindowMin") * 60
        w = self.vol[key]
        w.push(ts, vol, span, thin=5)
        t0, v0 = w.first()
        covered = ts - t0
        elapsed = ts - session_open_ts(ts)
        if covered < span * 0.6 or elapsed < span * 2:
            return None
        recent = vol - v0
        avg = (vol - recent) / max(1.0, elapsed - covered) * covered     # pace BEFORE this window
        if recent < self.get("trkMinVolLots") * (lot or 1) or recent < self.get("trkVolMult") * max(avg, 1):
            return None
        if self._cool(key, "VOLUME", "UP", ts):
            return {"kind": "VOLUME", "dir": "UP", "key": key, "from": round(avg), "to": recent,
                    "pct": round(recent / max(avg, 1), 1), "ts": ts}
        return None

    def change_pct(self, key):
        w = self.px.get(key)
        if not w or len(w.q) < 2 or not w.q[0][1]:
            return None
        return round((w.q[-1][1] - w.q[0][1]) / w.q[0][1] * 100, 1)

    def recent_volume(self, key):
        w = self.vol.get(key)
        if not w or len(w.q) < 2:
            return None
        return w.q[-1][1] - w.q[0][1]

    def forget(self, keys):
        for k in keys:
            self.px.pop(k, None)
            self.oi.pop(k, None)
            self.vol.pop(k, None)


# ======================================================================
# Tracker
# ======================================================================
class StockOptionsTracker:
    def __init__(self, conn, news, universe=None, detector=None, providers=None):
        self.conn, self.news = conn, news
        self.u = universe or Universe()
        self.det = detector or Detector()
        self.providers = providers or {"dhan": DhanProvider(setting("trkDhanConnections")),
                                       "upstox": UpstoxProvider(conn)}
        self.p = None                     # active provider
        self.sel = {}                     # key -> meta (everything tracked)
        self.live_keys = set()            # subset streamed over WebSocket
        self.latest = {}                  # key -> fields
        self.spots = {}
        self.alerts = deque(maxlen=1000)
        self._alert_seq = 0
        self._events = queue.Queue()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._contract_ts = {}
        self._feeds_started = set()
        self.stats = {"feed": "idle", "ticks": 0, "rest_polls": 0, "last_rebalance": None,
                      "last_rest": None, "rest_every_sec": None, "new_strikes_today": 0, "errors": {}}
        self._t_rebal = self._t_quote = self._t_master = self._t_recheck = 0.0
        self._load_alerts()

    # ---------------- persistence ----------------
    def _load_alerts(self):
        try:
            with open(ALERTS_PATH, "r", encoding="utf-8") as f:
                for a in json.load(f)[-1000:]:
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

    def active(self):
        return bool(setting("TRACKER_ENABLED")) and (setting("trkAlwaysOn") or market_window())

    def _main_loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.stats["errors"]["main"] = f"{type(e).__name__}: {e}"[:200]
            self._stop.wait(1)

    def choose_provider(self):
        want = str(setting("trkProvider")).lower()
        d, u = self.providers.get("dhan"), self.providers.get("upstox")
        if want == "upstox" or not d:
            p = u
        elif want == "dhan" or d.available():
            p = d if d.available() or want == "dhan" else u
        else:
            p = u
        if p is not self.p:
            if self.p is not None:
                self.p.set_subscription(set())
            self.p = p
            self.sel, self.live_keys = {}, set()
            self._t_master = self._t_rebal = 0
            self.stats["provider"] = p.name
        if p.name not in self._feeds_started:
            self._feeds_started.add(p.name)
            p.start_feed(self.on_data, self._on_status)
        return p

    def _on_status(self, text):
        self.stats["feed"] = text

    def tick(self, now=None):
        now = now or time.time()
        self.news.poll_nse()
        if not self.active():
            self.stats["feed"] = "paused (outside market hours)" if setting("TRACKER_ENABLED") else "disabled"
            self._drain_events()
            return
        self.choose_provider()
        if now - self._t_master > 3600 or not self.u.stocks:
            self._t_master = now
            self.u.build(self.p.load_rows())
        if now - self._t_rebal > setting("trkRebalanceMin") * 60 or not self.sel:
            self._t_rebal = now
            self.rebalance()
        if self.p.name == "upstox":
            calls = max(1, -(-len(self.sel) // 500))
            every = max(float(setting("trkQuotePollSec")), calls * 60 / 40.0)   # <= 40 calls/min (Upstox: 2000/30min, shared with the other bots)
            self.stats["rest_every_sec"] = round(every)
            if now - self._t_quote > every:
                self._t_quote = now
                self.poll_rest()
        self.stats["feed"] = self.p.status_text()
        self._drain_events()
        if now - self._t_recheck > 120:
            self._t_recheck = now
            self.recheck_unexplained()

    # ---------------- universe / subscription ----------------
    def rebalance(self):
        p = self.p or self.choose_provider()
        spots = p.spots([s["underlying_key"] for s in self.u.stocks.values()])
        if not spots:
            raise RuntimeError(f"no spot prices from {p.name} (token expired?)")
        self.spots = spots
        budget = setting("trkBudget")
        sel, edge = self.u.select(spots, budget)
        due = [x for x in edge if time.time() - self._contract_ts.get(x, 0) > 900][:25]
        added = 0
        for sym in due:
            self._contract_ts[sym] = time.time()
            try:
                added += self.u.merge_contracts(sym, p.option_contracts(self.u.stocks[sym]))
            except Exception as e:
                self.stats["errors"]["contracts"] = str(e)[:200]
        if added:
            self.stats["new_strikes_today"] += added
            sel, edge = self.u.select(spots, budget)
        live = set(sel) if len(sel) <= p.limit else set(sorted(sel, key=lambda k: sel[k]["dist"])[:p.limit])
        with self._lock:
            gone = set(self.sel) - set(sel)
            self.sel, self.live_keys = sel, live
        self.det.forget(gone)
        for k in gone:
            self.latest.pop(k, None)
        d = p.set_subscription(live)
        self.stats["last_rebalance"] = now_ist().strftime("%H:%M:%S")
        self.stats["edge_stocks"] = len(edge)
        return {"tracked": len(sel), "live": len(live), **d}

    # ---------------- data in ----------------
    def on_data(self, key, f, ts=None):
        """Provider callback (WS thread or REST poll): merge fields, run detectors."""
        m = self.sel.get(key)
        if m is None:
            return
        ts = ts or time.time()
        cur = self.latest.get(key)
        if cur is None:
            cur = self.latest[key] = {}
        cur.update(f)
        cur["ts"] = ts
        self.stats["ticks"] += 1
        put = self._events.put
        if "ltp" in f:
            ev = self.det.on_price(key, f["ltp"], ts)
            if ev:
                put(ev)
        if f.get("oi") is not None:
            ev = self.det.on_oi(key, f["oi"], ts, m["lot"])
            if ev:
                put(ev)
        if f.get("volume"):
            ev = self.det.on_volume(key, f["volume"], ts, m["lot"])
            if ev:
                put(ev)

    def poll_rest(self):
        with self._lock:
            keys = list(self.sel)
        if not keys:
            return
        q = self.p.poll(keys)
        ts = time.time()
        live = self.live_keys
        for k, f in q.items():
            if k in live and self.latest.get(k, {}).get("ltp") is not None and "ltp" in f:
                f = dict(f)
                f.pop("ltp")          # WS owns the tick price for live keys
            self.on_data(k, f, ts)
        self.stats["rest_polls"] += 1
        self.stats["last_rest"] = now_ist().strftime("%H:%M:%S")

    # ---------------- alerts + news ----------------
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
        lv = self.latest.get(ev["key"], {})
        leg = {"key": ev["key"], "strike": m.get("strike"), "opt": m.get("opt"), "from": ev["from"],
               "to": ev["to"], "pct": ev["pct"], "ltp": lv.get("ltp"), "vwap": lv.get("vwap"),
               "oi": lv.get("oi"), "volume": lv.get("volume"),
               "time": datetime.fromtimestamp(ev["ts"], IST).strftime("%H:%M:%S")}
        with self._lock:
            for a in reversed(self.alerts):
                if a["ts"] < ev["ts"] - setting("trkWindowMin") * 60:
                    break
                if a["symbol"] == sym and a["kind"] == ev["kind"] and a["dir"] == ev["dir"]:
                    a["legs"].append(leg)
                    a["max_pct"] = max(a["max_pct"], abs(ev["pct"]))
                    self._save_alerts()
                    return a
        s = self.u.stocks.get(sym, {})
        news = self.news.news_for(sym, s.get("name", sym),
                                  since_ts=ev["ts"] - setting("trkNewsLookbackMin") * 60, until_ts=ev["ts"] + 60)
        self._alert_seq += 1
        a = {"id": self._alert_seq, "symbol": sym, "name": s.get("name", sym), "kind": ev["kind"],
             "dir": ev["dir"], "ts": ev["ts"],
             "time": datetime.fromtimestamp(ev["ts"], IST).strftime("%Y-%m-%d %H:%M:%S"),
             "spot": m.get("spot"), "expiry": m.get("expiry"), "max_pct": abs(ev["pct"]),
             "legs": [leg], "status": "NEWS-MATCH" if news else "UNEXPLAINED", "news": news[:8]}
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

    # ---------------- views ----------------
    def _row(self, k, m):
        lv = self.latest.get(k, {})
        ltp, hi, lo, vwap = lv.get("ltp"), lv.get("high"), lv.get("low"), lv.get("vwap")
        oi, poi, pc = lv.get("oi"), lv.get("prev_oi"), lv.get("prev_close")
        return {"key": k, "symbol": m["symbol"], "strike": m["strike"], "opt": m["opt"], "atm": m["atm"],
                "spot": m["spot"], "expiry": m["expiry"], "front": m.get("front", True), "lot": m["lot"],
                "live": k in self.live_keys,
                "ltp": ltp, "chg_pct": round((ltp - pc) / pc * 100, 1) if ltp and pc else None,
                "vwap": vwap, "above_vwap": (ltp > vwap) if ltp and vwap else None,
                "open": lv.get("open"), "high": hi, "low": lo,
                "range_pos": round((ltp - lo) / (hi - lo) * 100) if ltp and hi and lo and hi > lo else None,
                "volume": lv.get("volume"), "vol_win": self.det.recent_volume(k),
                "oi": oi, "oi_chg": (oi - poi) if oi is not None and poi else None,
                "oi_chg_pct": round((oi - poi) / poi * 100, 1) if oi is not None and poi else None,
                "oi_hi": lv.get("oi_hi"), "oi_lo": lv.get("oi_lo"),
                "tbq": lv.get("tbq"), "tsq": lv.get("tsq"), "chg_win_pct": self.det.change_pct(k)}

    def board(self, symbol=None, limit=60, sort="move", expiry=None):
        with self._lock:
            sel = list(self.sel.items())
        if symbol:
            symbol = symbol.upper()
            rows = [self._row(k, m) for k, m in sel if m["symbol"] == symbol
                    and (m["expiry"] == expiry if expiry else m.get("front", True))]
            rows.sort(key=lambda r: (r["strike"], r["opt"]))
            return rows
        if sort == "volume":
            rows = [self._row(k, m) for k, m in sel if self.det.recent_volume(k)]
            rows.sort(key=lambda r: -(r["vol_win"] or 0))
        elif sort == "oi":
            rows = [r for r in (self._row(k, m) for k, m in sel) if r["oi_chg_pct"] is not None]
            rows.sort(key=lambda r: -abs(r["oi_chg_pct"]))
        else:
            rows = [self._row(k, m) for k, m in sel if self.det.change_pct(k) is not None]
            rows.sort(key=lambda r: -abs(r["chg_win_pct"]))
        return rows[:limit]

    def summary(self):
        """Per-stock totals & OI/volume mapping across every tracked strike."""
        with self._lock:
            sel = list(self.sel.items())
        agg = {}
        for k, m in sel:
            if not m.get("front", True):          # totals / PCR / mapping on the front expiry
                continue
            a = agg.get(m["symbol"])
            if a is None:
                a = agg[m["symbol"]] = {"symbol": m["symbol"], "spot": m["spot"], "atm": m["atm"],
                                        "expiry": m["expiry"], "strikes": 0, "ce_oi": 0, "pe_oi": 0,
                                        "ce_oi_chg": 0, "pe_oi_chg": 0, "ce_vol": 0, "pe_vol": 0,
                                        "_res": (0, None), "_sup": (0, None), "_mv": (0, None)}
            a["strikes"] += 1
            lv = self.latest.get(k)
            if not lv:
                continue
            side = "ce" if m["opt"] == "CE" else "pe"
            oi, vol = lv.get("oi") or 0, lv.get("volume") or 0
            a[side + "_oi"] += oi
            a[side + "_vol"] += vol
            if lv.get("prev_oi"):
                a[side + "_oi_chg"] += oi - lv["prev_oi"]
            if side == "ce" and oi > a["_res"][0]:
                a["_res"] = (oi, m["strike"])
            if side == "pe" and oi > a["_sup"][0]:
                a["_sup"] = (oi, m["strike"])
            if vol > a["_mv"][0]:
                a["_mv"] = (vol, f'{m["strike"]:g}{m["opt"]}')
        today = now_ist().strftime("%Y-%m-%d")
        n_alerts = defaultdict(int)
        for al in self.alerts:
            if al["time"][:10] == today:
                n_alerts[al["symbol"]] += 1
        out = []
        for a in agg.values():
            a["pcr"] = round(a["pe_oi"] / a["ce_oi"], 2) if a["ce_oi"] else None
            a["resistance"], a["support"], a["max_vol_strike"] = a.pop("_res")[1], a.pop("_sup")[1], a.pop("_mv")[1]
            a["alerts"] = n_alerts.get(a["symbol"], 0)
            out.append(a)
        out.sort(key=lambda a: -(a["ce_vol"] + a["pe_vol"]))
        return out

    def candles(self, key, interval="1"):
        p = self.p or self.choose_provider()
        return p.candles(key, interval)

    def status(self):
        with self._lock:
            n, nl = len(self.sel), len(self.live_keys)
            per = defaultdict(int)
            for m in self.sel.values():
                per[m["symbol"]] += 1
        p = self.p
        return {"enabled": bool(setting("TRACKER_ENABLED")), "active": self.active(),
                "provider": p.name if p else None, "live_limit": p.limit if p else None,
                "dhan_available": self.providers["dhan"].available() if "dhan" in self.providers else False,
                "dhan_error": getattr(self.providers.get("dhan"), "fatal", None),
                "stocks_in_master": len(self.u.stocks), "stocks_tracked": len(per),
                "instruments_tracked": n, "instruments_live": nl,
                "budget": min(int(setting("trkBudget")), HARD_CAP),
                "per_stock": (min(per.values()), max(per.values())) if per else (0, 0),
                "alerts_today": sum(1 for a in self.alerts if a["time"][:10] == now_ist().strftime("%Y-%m-%d")),
                "news_errors": self.news.errors, **self.stats,
                "settings": {k: setting(k) for k in SETTINGS}}

    def expiries(self, symbol):
        return sorted({m["expiry"] for m in self.sel.values() if m["symbol"] == symbol.upper()})

    def symbols(self):
        return sorted({m["symbol"] for m in self.sel.values()}) or sorted(self.u.stocks)
