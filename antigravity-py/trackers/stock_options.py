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
import heapq
import queue
import threading
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta

import config
from trackers.providers import DhanProvider, DhanRestProvider, UpstoxProvider
from trackers.history import History, label as bu_label, LABELS
from trackers.nse_context import NseContext
from trackers.nse_extra import NseExtra
from trackers.notify import Telegram, format_alert
from trackers.trend import TrendEngine
from trackers import ivcalc

IST = timezone(timedelta(hours=5, minutes=30))
ALERTS_PATH = os.path.join(config.DATA, "tracker_alerts.json")
TRENDS_PATH = os.path.join(config.DATA, "tracker_trends.json")
HARD_CAP = 25000

SETTINGS = {
    "TRACKER_ENABLED": True,
    "trkProvider": "auto",      # auto | dhan_rest | dhan | upstox
                                # auto = dhan_rest (REST, 0 WebSocket) if dhan.json works, else Upstox.
                                # "dhan" (5 WebSockets) only on request: it would disconnect a bot on the same Dhan id.
    "trkBudget": 25000,         # strikes to track (Dhan: all live; Upstox: 5000 live + rest via REST)
    "trkDhanConnections": 5,    # Dhan allows 5 WS connections x 5000 instruments
    "trkDhanRestPerSec": 0.8,   # dhan_rest: market-quote calls/sec (Dhan cap 1/sec per account; 1000 instruments each)
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
    "trkVolOiRatio": 2.0,       # UNUSUAL: today's volume >= this x yesterday's OI
    "trkIvSpikePts": 5,         # IV: implied vol moves >= this many points ...
    "trkIvWindowMin": 15,       # ... within this many minutes
    "trkTrendBarMin": 3,        # trend engine candle size (min)
    "trkTrendMinBars": 5,       # candles needed before a trend can be called
    "trkTrendGreen": 3,         # green (red) candles in a row to start an up (down) trend
    "trkTrendMinPct": 15,       # option premium move from swing low/high to call a trend
    "trkTrendMinPctStock": 1.0, # underlying stock move to call a trend
    "trkAtlAlerts": False,      # all-time-LOW alerts (off: OTM options decay to new lows daily)
    "trkAthMaxDistPct": 10,     # all-time-HIGH alerts only for strikes within this % of spot
    "trkPcrShift": 0.25,        # PCR alert: intraday PCR moves this much from the day's first reading
    "trkStraddlePct": 15,       # ATM straddle (CE+PE) premium expands this % from the day's first reading
    "trkOrbMin": 15,            # stock opening range = first N minutes from 09:15
    "trkFutPricePct": 1.0,      # FUT_BUILDUP: futures price moved >= this % vs previous close ...
    "trkFutOiPct": 5,           # ... and futures OI moved >= this % vs previous day
    "trkBasisMinPct": 0.1,      # BASIS flip alert only when |basis| >= this % on the new side
    "trkDelivMult": 2.0,        # DELIVERY alert: delivered qty >= this x its 20-day average ...
    "trkDelivPctAbove": 10,     # ... and delivery % >= 20-day average + this many points
    "trkDealAlerts": True,      # bulk / block deal alerts for F&O stocks
    "trkTelegramKinds": "TREND,TREND_END,FUT_BUILDUP,UNUSUAL,OI_SHIFT,ATH,EXP_MOVE,MAXPAIN,BASIS,BAN,DELIVERY,DEAL,FII",
    "trkMwplAlertPct": 90,      # alert when a stock's MWPL usage crosses this (ban at 95)
    "trkHistDays": 100,         # NSE bhavcopy lookback for multi-day OI / all-time high-low
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


_OPEN_CACHE = {}


def session_open_ts(ts):
    """09:15 IST of ts's day (cached per day — called on every volume tick)."""
    day = int((ts + 19800) // 86400)
    v = _OPEN_CACHE.get(day)
    if v is None:
        d = datetime.fromtimestamp(ts, IST)
        v = _OPEN_CACHE[day] = d.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
    return v


# ======================================================================
# Universe
# ======================================================================
class Universe:
    def __init__(self):
        self.stocks = {}        # symbol -> {symbol, underlying_key, name, lot, options{exp_ms: {strike: {CE,PE}}}}
        self.futs = {}          # symbol -> [futures rows by expiry]
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

    def build_futures(self, rows):
        """Stock futures per symbol, ascending expiry (only symbols that have options)."""
        today_ms = int(now_ist().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        futs = {}
        for r in rows or []:
            if r["symbol"] in self.stocks and r["expiry_ms"] >= today_ms:
                futs.setdefault(r["symbol"], []).append(r)
        for v in futs.values():
            v.sort(key=lambda r: r["expiry_ms"])
        self.futs = futs

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
def _fmt(v):
    if v is None:
        return "--"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, int) else f"{v:g}"


class StockOptionsTracker:
    def __init__(self, conn, news, universe=None, detector=None, providers=None, history=None, context=None, extra=None):
        self.conn, self.news = conn, news
        self.u = universe or Universe()
        self.det = detector or Detector()
        self.trend = TrendEngine(self.det.get)
        self.providers = providers or {"dhan": DhanProvider(setting("trkDhanConnections")),
                                       "dhan_rest": DhanRestProvider(setting("trkDhanRestPerSec")),
                                       "upstox": UpstoxProvider(conn)}
        self.hist = history if history is not None else History(days=setting("trkHistDays"))
        self.ctx = context if context is not None else NseContext()
        self.extra = extra if extra is not None else NseExtra()
        self.anal = {}                    # symbol -> live analytics (max pain, GEX, skew, expected move, IV rank)
        self._seen_deals = set()
        self.tg = Telegram()
        self._part_date = None
        self.p = None                     # active provider
        self.sel = {}                     # key -> meta (everything tracked)
        self.live_keys = set()            # subset streamed over WebSocket
        self.ukeys = {}                   # underlying key -> symbol (streamed for live spot + stock trends)
        self.fut_meta = {}                # futures key -> {symbol, expiry, front, lot}  (front + next month)
        self.latest = {}                  # key -> fields
        self.spots = {}
        self.greeks = {}                  # key -> {iv, delta, gamma, theta, vega}
        self.ivwin = defaultdict(_Win)
        self.alerts = deque(maxlen=2000)
        self.trends = deque(maxlen=5000)
        self._alert_seq = 0
        self._once = {}                   # once-per-day alert keys -> date
        self._events = queue.Queue()
        self._newsq = queue.Queue()
        self._sync_news = True            # tests / direct use resolve news inline; start() moves it to a thread
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._contract_ts = {}
        self._feeds_started = set()
        self._dirty_trends = False
        self.stats = {"feed": "idle", "ticks": 0, "rest_polls": 0, "last_rebalance": None,
                      "last_rest": None, "rest_every_sec": None, "new_strikes_today": 0, "errors": {}}
        self._t_rebal = self._t_quote = self._t_master = self._t_recheck = self._t_iv = self._t_save = 0.0
        self._t_hist = self._t_ctx = 0.0
        self.sig = {}                     # symbol -> intraday stock-level signal state (OI walls, PCR, straddle, ORB)
        self._load()

    # ---------------- persistence ----------------
    def _load(self):
        for path, dq in ((ALERTS_PATH, self.alerts), (TRENDS_PATH, self.trends)):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for a in json.load(f)[-dq.maxlen:]:
                        dq.append(a)
            except Exception:
                pass
        self._alert_seq = max((a.get("id", 0) for a in self.alerts), default=0)
        for t in self.trends:                 # a restart can't continue a running trend: close it honestly
            if t.get("status") == "RUNNING":
                t.update({"status": "ENDED", "reason_end": "tracker restarted (trend not followed further)"})

    def _dump(self, path, data):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception as e:
            self.stats["errors"]["save"] = str(e)[:200]

    def _save_alerts(self):
        self._dump(ALERTS_PATH, list(self.alerts))

    def _save_trends(self):
        self._dump(TRENDS_PATH, list(self.trends))
        self._dirty_trends = False

    # ---------------- lifecycle ----------------
    def start(self):
        self._sync_news = False
        threading.Thread(target=self._main_loop, name="trk-main", daemon=True).start()
        for i in range(3):               # parallel news lookups (Google News / NSE are ~1 s each)
            threading.Thread(target=self._news_loop, name=f"trk-news-{i}", daemon=True).start()
        threading.Thread(target=self._slow_loop, name="trk-slow", daemon=True).start()

    def active(self):
        return bool(setting("TRACKER_ENABLED")) and (setting("trkAlwaysOn") or market_window())

    def _main_loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.stats["errors"]["main"] = f"{type(e).__name__}: {e}"[:200]
            self._stop.wait(1)

    def _slow_loop(self):
        """History (NSE bhavcopy) + NSE context — slow downloads, kept off the main loop."""
        while not self._stop.is_set():
            now = time.time()
            try:
                if self.hist is not False and (now - self._t_hist > 1800 or not self.hist.dates):
                    self._t_hist = now
                    self.hist.load()
            except Exception as e:
                self.stats["errors"]["history"] = f"{type(e).__name__}: {e}"[:200]
            try:
                if self.extra is not False:
                    if self.extra.refresh(symbols=set(self.u.stocks) or None):
                        self.extra_alerts()
            except Exception as e:
                self.stats["errors"]["extra"] = f"{type(e).__name__}: {e}"[:200]
            try:
                if self.ctx is not False and now - self._t_ctx > 3600:
                    self._t_ctx = now
                    self.ctx.refresh(force=True)
                    self.context_alerts()
            except Exception as e:
                self.stats["errors"]["context"] = f"{type(e).__name__}: {e}"[:200]
            self._stop.wait(30)

    def choose_provider(self):
        want = str(setting("trkProvider")).lower()
        d, dr, u = self.providers.get("dhan"), self.providers.get("dhan_rest"), self.providers.get("upstox")
        if want == "dhan" and d:
            p = d                         # explicit: 5 WebSockets (would bump a bot on the same Dhan id)
        elif want in ("auto", "dhan_rest") and dr and dr.available():
            p = dr                        # REST only, 0 WebSocket connections
        elif want == "auto" and not dr and d and d.available():
            p = d                         # (custom provider sets without dhan_rest)
        else:
            p = u or dr or d
        if hasattr(p, "is_active"):
            p.is_active = self.active
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
            if hasattr(self.p, "load_futures"):
                self.u.build_futures(self.p.load_futures())
        if now - self._t_rebal > setting("trkRebalanceMin") * 60 or not self.sel:
            self._t_rebal = now
            self.rebalance()
        if self.p.name == "dhan_rest":
            ps = self.p.stats
            self.stats["rest_every_sec"] = ps["last_cycle_sec"]
            self.stats["rest_polls"] = ps["cycles"]
            if ps.get("last_error"):
                self.stats["errors"]["dhan_rest"] = ps["last_error"]
        if self.p.name == "upstox":
            calls = max(1, -(-len(self.sel) // 500))
            every = max(float(setting("trkQuotePollSec")), calls * 60 / 40.0)   # <= 40 calls/min (Upstox: 2000/30min, shared)
            self.stats["rest_every_sec"] = round(every)
            if now - self._t_quote > every:
                self._t_quote = now
                self.poll_rest()
        if now - self._t_iv > 30:
            self._t_iv = now
            self.compute_greeks(now)
            self.stock_signals(now)
            self.analytics(now)
        self.stats["feed"] = self.p.status_text()
        self._drain_events()
        if now - self._t_recheck > 120:
            self._t_recheck = now
            self.recheck_unexplained()
        if self._dirty_trends and now - self._t_save > 5:
            self._t_save = now
            self._save_trends()

    # ---------------- universe / subscription ----------------
    def rebalance(self):
        p = self.p or self.choose_provider()
        ukeys = {s["underlying_key"]: s["symbol"] for s in self.u.stocks.values()}
        spots = p.spots(list(ukeys))
        if not spots:
            raise RuntimeError(f"no spot prices from {p.name} (token expired?)")
        for k, v in spots.items():          # keep a fresher streamed spot if we have one
            self.spots[k] = self.spots.get(k) or v
        self.spots.update({k: v for k, v in spots.items() if k not in self.latest})
        fut_meta = {}
        cut = now_ist()
        for sym, fl in self.u.futs.items():
            ahead = [f for f in fl if datetime.fromtimestamp(f["expiry_ms"] / 1000, IST).date() > cut.date()
                     or (datetime.fromtimestamp(f["expiry_ms"] / 1000, IST).date() == cut.date() and cut.hour * 60 + cut.minute < 930)]
            for i, f in enumerate(ahead[:2]):              # front + next month (rollover)
                fut_meta[f["key"]] = {"symbol": sym, "expiry": datetime.fromtimestamp(f["expiry_ms"] / 1000, IST).strftime("%Y-%m-%d"),
                                      "front": i == 0, "lot": f.get("lot"), "ts": f.get("ts")}
        reserved = len(ukeys) + len(fut_meta)
        budget = setting("trkBudget")
        if p.limit >= HARD_CAP:              # all-live provider: reserve room for underlyings + futures
            budget = min(budget, p.limit - reserved)
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
        room = max(0, p.limit - reserved)
        live = set(sel) if len(sel) <= room else set(sorted(sel, key=lambda k: sel[k]["dist"])[:room])
        with self._lock:
            gone = set(self.sel) - set(sel)
            self.sel, self.live_keys, self.ukeys, self.fut_meta = sel, live, ukeys, fut_meta
        self.det.forget(gone)
        self.trend.forget(gone)
        for k in gone:
            self.latest.pop(k, None)
            self.greeks.pop(k, None)
            self.ivwin.pop(k, None)
        d = p.set_subscription(live | set(ukeys) | set(fut_meta))
        self.stats["last_rebalance"] = now_ist().strftime("%H:%M:%S")
        self.stats["edge_stocks"] = len(edge)
        return {"tracked": len(sel), "live": len(live), **d}

    # ---------------- data in ----------------
    def on_data(self, key, f, ts=None):
        """Provider callback (WS thread or REST poll): merge fields, run detectors + trend engine."""
        ts = ts or time.time()
        m = self.sel.get(key)
        if m is None and key in self.fut_meta:
            fm = self.fut_meta[key]
            cur = self.latest.setdefault(key, {})
            cur.update(f)
            cur["ts"] = ts
            self.stats["ticks"] += 1
            if f.get("oi") is not None:
                ev = self.det.on_oi(key, f["oi"], ts, fm["lot"])
                if ev:
                    ev["symbol"] = fm["symbol"]
                    self._events.put(ev)
            if fm["front"]:
                self._fut_check(fm["symbol"], ts)
            return
        if m is None:
            sym = self.ukeys.get(key)
            if sym and f.get("ltp"):         # underlying stock tick: live spot + stock trend + ORB
                self.spots[key] = f["ltp"]
                self._orb(sym, key, f["ltp"], ts)
                cur = self.latest.setdefault(key, {})
                cur.update(f)
                for e in self.trend.on_price(key, f["ltp"], ts, f.get("vwap") or cur.get("vwap"), underlying=True):
                    self._trend_event(e, sym, None, ts)
            return
        cur = self.latest.get(key)
        if cur is None:
            cur = self.latest[key] = {}
        # only run detectors on values that actually changed (a FULL packet repeats most fields)
        ltp, oi, vol = f.get("ltp"), f.get("oi"), f.get("volume")
        new_px = ltp is not None and ltp != cur.get("ltp")
        new_oi = oi is not None and oi != cur.get("oi")
        new_vol = bool(vol) and vol != cur.get("volume")
        cur.update(f)
        cur["ts"] = ts
        self.stats["ticks"] += 1
        put = self._events.put
        if new_px:
            ev = self.det.on_price(key, ltp, ts)
            if ev:
                put(ev)
            for e in self.trend.on_price(key, ltp, ts, cur.get("vwap")):
                self._trend_event(e, m["symbol"], m, ts)
            self._ath_check(key, m, cur, ts)
        if new_oi:
            ev = self.det.on_oi(key, oi, ts, m["lot"])
            if ev:
                put(ev)
        if new_vol:
            ev = self.det.on_volume(key, vol, ts, m["lot"])
            if ev:
                put(ev)
            self._unusual_check(key, m, cur, ts)

    def poll_rest(self):
        with self._lock:
            keys = list(self.sel) + list(self.fut_meta)
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

    # ---------------- derived per-strike data ----------------
    def _tok(self, key):
        return key.split("|", 1)[1] if "|" in key else key

    def hist_of(self, key):
        """History snapshot for a contract — cached until the history reloads (called on every tick)."""
        h = self.hist
        if not h:
            return None
        stamp = getattr(h, "loaded_at", 0)
        if getattr(self, "_hc_stamp", None) != stamp:
            self._hc, self._hc_stamp = {}, stamp
        v = self._hc.get(key, False)
        if v is False:
            v = self._hc[key] = h.contract(self._tok(key))
        return v

    def prev_oi(self, key, lv=None):
        lv = lv if lv is not None else self.latest.get(key, {})
        if lv.get("prev_oi"):
            return lv["prev_oi"]
        hc = self.hist_of(key)
        return hc.get("prev_oi") if hc else None

    def _once_today(self, k):
        d = now_ist().strftime("%Y-%m-%d")
        if self._once.get(k) == d:
            return False
        self._once[k] = d
        return True

    def _ath_check(self, key, m, cur, ts):
        hc = self.hist_of(key)
        if not hc or hc.get("ath") is None:
            return
        ltp = cur.get("ltp")
        if not ltp or ltp < setting("trkMinPremium") or abs(m["strike"] - m["spot"]) / m["spot"] * 100 > setting("trkAthMaxDistPct"):
            return
        if ltp > hc["ath"] and self._once_today(("ATH", key)):
            self._events.put({"kind": "ATH", "dir": "UP", "key": key, "from": hc["ath"], "to": ltp,
                              "pct": round((ltp - hc["ath"]) / hc["ath"] * 100, 1), "ts": ts,
                              "reason": f"premium {_fmt(ltp)} broke the contract's ALL-TIME HIGH {_fmt(hc['ath'])} "
                                        f"(set {hc['ath_d']}, listed {hc['listed']})"})
        elif setting("trkAtlAlerts") and hc.get("atl") and ltp < hc["atl"] and self._once_today(("ATL", key)):
            self._events.put({"kind": "ATL", "dir": "DOWN", "key": key, "from": hc["atl"], "to": ltp,
                              "pct": round((ltp - hc["atl"]) / hc["atl"] * 100, 1), "ts": ts,
                              "reason": f"premium {_fmt(ltp)} broke the contract's ALL-TIME LOW {_fmt(hc['atl'])} "
                                        f"(set {hc['atl_d']}, listed {hc['listed']})"})

    def _unusual_check(self, key, m, cur, ts):
        vol, poi = cur.get("volume"), self.prev_oi(key, cur)
        if not vol or not poi or vol < setting("trkMinVolLots") * (m["lot"] or 1):
            return
        ratio = vol / poi
        if ratio >= setting("trkVolOiRatio") and self._once_today(("UNUSUAL", key)):
            self._events.put({"kind": "UNUSUAL", "dir": "UP", "key": key, "from": poi, "to": vol,
                              "pct": round(ratio, 1), "ts": ts,
                              "reason": f"today's volume {_fmt(vol)} = {ratio:.1f}x yesterday's OI {_fmt(poi)} "
                                        f"(threshold {setting('trkVolOiRatio')}x) — fresh positions, not just churn"})

    # ---------------- stock futures ----------------
    def fut_row(self, sym):
        """Front-month futures snapshot for a stock: price, basis, OI change, buildup, rollover."""
        front = nxt = None
        for k, fm in self.fut_meta.items():
            if fm["symbol"] == sym:
                if fm["front"]:
                    front = k
                else:
                    nxt = k
        if not front:
            return None
        lv = self.latest.get(front, {})
        ltp, pc, oi, vol = lv.get("ltp"), lv.get("prev_close"), lv.get("oi"), lv.get("volume")
        poi = self.prev_oi(front, lv)
        spot = self.spots.get(self.u.stocks.get(sym, {}).get("underlying_key"))
        noi = self.latest.get(nxt, {}).get("oi") if nxt else None
        pch = (ltp - pc) / pc * 100 if ltp and pc else None
        och = (oi - poi) / poi * 100 if oi is not None and poi else None
        lab = bu_label(pch, och) if pch is not None and och is not None and abs(pch) >= 0.2 and abs(och) >= 0.5 else None
        return {"symbol": sym, "key": front, "expiry": self.fut_meta[front]["expiry"], "ltp": ltp, "prev_close": pc,
                "chg_pct": round(pch, 2) if pch is not None else None, "spot": spot,
                "basis": round(ltp - spot, 2) if ltp and spot else None,
                "basis_pct": round((ltp - spot) / spot * 100, 2) if ltp and spot else None,
                "oi": oi, "prev_oi": poi, "oi_chg": (oi - poi) if oi is not None and poi else None,
                "oi_chg_pct": round(och, 2) if och is not None else None, "buildup": lab,
                "volume": vol, "vwap": lv.get("vwap"), "high": lv.get("high"), "low": lv.get("low"),
                "next_oi": noi, "rollover_pct": round(noi / (noi + oi) * 100, 1) if noi and oi else None}

    def _fut_check(self, sym, ts):
        r = self.fut_row(sym)
        if not r or r["chg_pct"] is None:
            return
        st = self._sig(sym, ts)
        # buildup: strong price + OI move, once per day per label
        if (r["oi_chg_pct"] is not None and abs(r["chg_pct"]) >= setting("trkFutPricePct")
                and abs(r["oi_chg_pct"]) >= setting("trkFutOiPct")):
            lab = bu_label(r["chg_pct"], r["oi_chg_pct"])
            if lab and self._once_today(("FUT", sym, lab)):
                btxt = "" if r["basis_pct"] is None else f", basis {r['basis_pct']:+.2f}%"
                self._events.put({"kind": "FUT_BUILDUP", "dir": "UP" if lab in ("LB", "SC") else "DOWN", "key": r["key"],
                                  "symbol": sym, "from": r["prev_oi"], "to": r["oi"], "pct": r["oi_chg_pct"], "ts": ts,
                                  "reason": f"{sym} FUTURES {LABELS[lab].upper()}: price {_fmt(r['prev_close'])} → {_fmt(r['ltp'])} "
                                            f"({r['chg_pct']:+.2f}%), OI {_fmt(r['prev_oi'])} → {_fmt(r['oi'])} ({r['oi_chg_pct']:+.1f}%)"
                                            f"{btxt}"})
        # basis flip premium <-> discount
        b = r["basis_pct"]
        if b is not None and abs(b) >= setting("trkBasisMinPct"):
            side = "PREMIUM" if b > 0 else "DISCOUNT"
            prev = st.get("basis_side")
            st["basis_side"] = side
            if prev and prev != side and self._once_today(("BASIS", sym, side)):
                self._events.put({"kind": "BASIS", "dir": "UP" if side == "PREMIUM" else "DOWN", "key": r["key"], "symbol": sym,
                                  "from": None, "to": r["basis"], "pct": b, "ts": ts,
                                  "reason": f"{sym} futures flipped from {prev.lower()} to {side.lower()}: futures {_fmt(r['ltp'])} vs "
                                            f"spot {_fmt(r['spot'])} = {r['basis']:+.2f} ({b:+.2f}%) — "
                                            f"{'longs willing to pay up' if side == 'PREMIUM' else 'shorts pressing / carry negative'}"})

    def futures(self):
        return [r for r in (self.fut_row(sym) for sym in sorted(self.u.stocks)) if r]

    # ---------------- per-stock analytics: max pain, GEX, skew, expected move, IV rank ----------------
    def analytics(self, now=None):
        now = now or time.time()
        by = defaultdict(list)
        with self._lock:
            sel = list(self.sel.items())
        for k, m in sel:
            if m.get("front", True) and k in self.latest:
                by[m["symbol"]].append((k, m))
        for sym, items in by.items():
            uk = self.u.stocks.get(sym, {}).get("underlying_key")
            spot = self.spots.get(uk) or items[0][1]["spot"]
            ce_oi, pe_oi, gex = defaultdict(float), defaultdict(float), defaultdict(float)
            calls, puts, atm = [], [], {}
            for k, m in items:
                lv, g = self.latest[k], self.greeks.get(k)
                oi = lv.get("oi") or 0
                (ce_oi if m["opt"] == "CE" else pe_oi)[m["strike"]] += oi
                if g:
                    # dealer-convention GEX (calls +, puts -) in ₹ per 1% move — an estimate, not a fact
                    gex[m["strike"]] += (1 if m["opt"] == "CE" else -1) * g["gamma"] * oi * spot * spot * 0.01
                    (calls if m["opt"] == "CE" else puts).append((g["delta"], g["iv"]))
                if m["strike"] == m["atm"] and lv.get("ltp"):
                    atm[m["opt"]] = lv["ltp"]
                    if g:
                        atm[m["opt"] + "_iv"] = g["iv"]
            strikes = sorted(set(ce_oi) | set(pe_oi))
            mp = None
            if strikes and (sum(ce_oi.values()) + sum(pe_oi.values())) > 0:
                mp = min(strikes, key=lambda K: sum((K - x) * ce_oi[x] for x in strikes if x < K)
                         + sum((x - K) * pe_oi[x] for x in strikes if x > K))
            gtot = sum(gex.values()) if gex else None
            flip, cum = None, 0.0
            for K in sorted(gex):
                prev = cum
                cum += gex[K]
                if prev and (prev < 0) != (cum < 0):
                    flip = K
                    break
            skew = None
            if calls and puts:
                c25 = min(calls, key=lambda x: abs(x[0] - 0.25))
                p25 = min(puts, key=lambda x: abs(x[0] + 0.25))
                skew = round(p25[1] - c25[1], 2)
            straddle = atm.get("CE", 0) + atm.get("PE", 0) if "CE" in atm and "PE" in atm else None
            atm_iv = (atm["CE_iv"] + atm["PE_iv"]) / 2 if "CE_iv" in atm and "PE_iv" in atm else None
            ivs = self.hist.iv_stats(sym, atm_iv) if self.hist else None
            a = self.anal.setdefault(sym, {})
            a.update({"max_pain": mp, "gex_cr": round(gtot / 1e7, 2) if gtot is not None else None, "gex_flip": flip,
                      "skew": skew, "straddle": round(straddle, 2) if straddle else None,
                      "exp_move_pct": round(straddle / spot * 100, 2) if straddle and spot else None,
                      "exp_hi": round(spot + straddle, 2) if straddle else None,
                      "exp_lo": round(spot - straddle, 2) if straddle else None, "atm_iv": round(atm_iv, 2) if atm_iv else None})
            if ivs:
                a.update({k: ivs[k] for k in ("iv_rank", "iv_pctile", "iv_min", "iv_max", "iv_days", "iv_term", "iv_next",
                                              "iv_extreme")})
            st = self._sig(sym, now)
            # max-pain shift (must hold two checks)
            if mp is not None:
                if st.get("mp") is None:
                    st["mp"] = mp
                elif mp != st["mp"]:
                    if st.get("mp_cand") == mp:
                        old, st["mp"], st["mp_cand"] = st["mp"], mp, None
                        self._events.put({"kind": "MAXPAIN", "dir": "UP" if mp > old else "DOWN", "key": items[0][0],
                                          "symbol": sym, "from": old, "to": mp, "pct": round((mp - old) / old * 100, 2),
                                          "ts": now, "reason": f"max pain moved {_fmt(old)} → {_fmt(mp)} (spot {_fmt(spot)}) — "
                                                               f"option writers' pain point shifted {'up' if mp > old else 'down'}"})
                    else:
                        st["mp_cand"] = mp
                else:
                    st["mp_cand"] = None
            # expected-move break: range fixed from the day's first straddle reading
            if straddle and spot:
                if st.get("em_lo") is None:
                    st["em_lo"], st["em_hi"], st["em_done"] = spot - straddle, spot + straddle, set()
                for d, hit, lvl in (("UP", spot > st["em_hi"], st["em_hi"]), ("DOWN", spot < st["em_lo"], st["em_lo"])):
                    if hit and d not in st["em_done"]:
                        st["em_done"].add(d)
                        self._events.put({"kind": "EXP_MOVE", "dir": d, "key": items[0][0], "symbol": sym, "from": round(lvl, 2),
                                          "to": spot, "pct": round((spot - lvl) / lvl * 100, 2), "ts": now,
                                          "reason": f"{sym} {_fmt(spot)} broke the options-implied expected move "
                                                    f"{_fmt(round(st['em_lo'], 2))}–{_fmt(round(st['em_hi'], 2))} "
                                                    f"(ATM straddle at the day's first reading) — move bigger than priced"})
        return len(by)

    def extra_alerts(self):
        """Daily: delivery accumulation/distribution, FII positioning; intraday: new bulk/block deals."""
        ts = time.time()
        ex = self.extra
        for sym in self.u.stocks:
            d = ex.delivery_info(sym)
            if not d or d["deliv_qty_x20"] is None or d["deliv_avg20"] is None:
                continue
            if (d["deliv_qty_x20"] >= setting("trkDelivMult") and d["deliv_pct"] >= d["deliv_avg20"] + setting("trkDelivPctAbove")
                    and self._once_today(("DELIV", sym, d["date"]))):
                up = (d["price_chg_pct"] or 0) >= 0
                self._ctx_alert(sym, "DELIVERY", f"{sym} delivery {d['deliv_pct']}% on {d['date']} vs 20-day avg {d['deliv_avg20']}% "
                                                 f"({d['deliv_qty_x20']}x usual delivered qty), price {d['price_chg_pct']:+.2f}% — "
                                                 f"{'ACCUMULATION (real buying)' if up else 'DISTRIBUTION (real selling)'}", ts,
                                "UP" if up else "DOWN")
        if setting("trkDealAlerts"):
            for x in ex.deals["bulk"] + ex.deals["block"]:
                key = (x["kind"], x["symbol"], x["client"], x["side"], x["qty"], x["date"])
                if x["symbol"] in self.u.stocks and key not in self._seen_deals:
                    self._seen_deals.add(key)
                    val = (x["qty"] or 0) * (x["price"] or 0) / 1e7
                    self._ctx_alert(x["symbol"], "DEAL", f"{x['kind']} deal {x['date']}: {x['client']} {x['side']} "
                                                         f"{_fmt(x['qty'])} shares @ {_fmt(x['price'])} (₹{val:.1f} cr)", ts,
                                    "UP" if x["side"] == "BUY" else "DOWN")
        pv = ex.participant_view()
        if pv["date"] and pv["date"] != self._part_date and pv["prev_date"]:
            self._part_date = pv["date"]
            rows = {r["who"]: r for r in pv["rows"]}
            f = rows.get("FII")
            if f and self._once_today(("FII", pv["date"])):
                flip = f["fut_idx_chg"] is not None and (f["fut_idx"] > 0) != (f["fut_idx"] - f["fut_idx_chg"] > 0)
                parts = [f"{w}: index fut net {_fmt(int(r['fut_idx']))} ({int(r['fut_idx_chg']):+,})"
                         for w, r in rows.items() if r.get("fut_idx_chg") is not None]
                self._ctx_alert("MARKET", "FII", f"Participant OI {pv['date']}: FII index futures {f['fut_idx_long_pct']}% long"
                                                 f"{' — FII FLIPPED ' + ('LONG' if f['fut_idx'] > 0 else 'SHORT') if flip else ''}; "
                                                 + "; ".join(parts), ts, "UP" if f["fut_idx_chg"] and f["fut_idx_chg"] > 0 else "DOWN")

    def sectors(self):
        """Sector heatmap rows from the per-stock summary."""
        out = {}
        for a in self.summary(max_age=2.0):
            sec = a.get("sector") or "Other"
            o = out.setdefault(sec, {"sector": sec, "stocks": 0, "up": 0, "down": 0, "_chg": [], "ce_oi_chg": 0, "pe_oi_chg": 0,
                                     "fut_lb": 0, "fut_sb": 0, "fut_sc": 0, "fut_lu": 0, "alerts": 0, "members": []})
            o["stocks"] += 1
            o["members"].append({"symbol": a["symbol"], "chg_pct": a.get("chg_pct")})
            if a.get("chg_pct") is not None:
                o["_chg"].append(a["chg_pct"])
                o["up" if a["chg_pct"] > 0 else "down"] += a["chg_pct"] != 0
            o["ce_oi_chg"] += a.get("ce_oi_chg") or 0
            o["pe_oi_chg"] += a.get("pe_oi_chg") or 0
            if a.get("fut_buildup"):
                o["fut_" + a["fut_buildup"].lower()] += 1
            o["alerts"] += a.get("alerts", 0)
        res = []
        for o in out.values():
            c = o.pop("_chg")
            o["avg_chg_pct"] = round(sum(c) / len(c), 2) if c else None
            o["members"].sort(key=lambda m: -(m["chg_pct"] or -999))
            res.append(o)
        res.sort(key=lambda o: -(o["avg_chg_pct"] if o["avg_chg_pct"] is not None else -999))
        return res

    # ---------------- stock-level signals ----------------
    def _sig(self, sym, ts):
        day = datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")
        st = self.sig.get(sym)
        if st is None or st["day"] != day:
            st = self.sig[sym] = {"day": day, "orb_hi": None, "orb_lo": None, "orb_done": set(),
                                  "pcr0": None, "str0": None, "res": None, "sup": None, "res_cand": None, "sup_cand": None}
        return st

    def _orb(self, sym, key, px, ts):
        st = self._sig(sym, ts)
        o = session_open_ts(ts)
        if ts < o:
            return
        if ts <= o + setting("trkOrbMin") * 60:
            st["orb_hi"] = px if st["orb_hi"] is None else max(st["orb_hi"], px)
            st["orb_lo"] = px if st["orb_lo"] is None else min(st["orb_lo"], px)
            return
        if st["orb_hi"] is None:
            return
        for d, hit, lvl in (("UP", px > st["orb_hi"], st["orb_hi"]), ("DOWN", px < st["orb_lo"], st["orb_lo"])):
            if hit and d not in st["orb_done"]:
                st["orb_done"].add(d)
                self._events.put({"kind": "ORB", "dir": d, "key": key, "symbol": sym, "from": lvl, "to": px,
                                  "pct": round((px - lvl) / lvl * 100, 2), "ts": ts,
                                  "reason": f"{sym} broke its {setting('trkOrbMin')}-min opening range "
                                            f"{'high' if d == 'UP' else 'low'} {_fmt(lvl)} (range {_fmt(st['orb_lo'])}–"
                                            f"{_fmt(st['orb_hi'])}) → {_fmt(px)}"})

    def stock_signals(self, now=None):
        """OI-wall shift, PCR shift, ATM straddle expansion — per stock, front expiry."""
        now = now or time.time()
        rows = {}
        with self._lock:
            sel = list(self.sel.items())
        for k, m in sel:
            if not m.get("front", True):
                continue
            lv = self.latest.get(k)
            if not lv or lv.get("oi") is None:
                continue
            r = rows.setdefault(m["symbol"], {"ce": 0, "pe": 0, "res": (0, None, None), "sup": (0, None, None), "atm": {}})
            side = "ce" if m["opt"] == "CE" else "pe"
            r[side] += lv["oi"]
            if side == "ce" and lv["oi"] > r["res"][0]:
                r["res"] = (lv["oi"], m["strike"], k)
            if side == "pe" and lv["oi"] > r["sup"][0]:
                r["sup"] = (lv["oi"], m["strike"], k)
            if m["strike"] == m["atm"] and lv.get("ltp"):
                r["atm"][m["opt"]] = (lv["ltp"], k)
        for sym, r in rows.items():
            st = self._sig(sym, now)
            # --- OI walls (needs the new strike to hold for 2 checks in a row) ---
            for wall, name in (("res", "Resistance (max CALL OI)"), ("sup", "Support (max PUT OI)")):
                oi, strike, key = r[wall]
                if strike is None:
                    continue
                if st[wall] is None:
                    st[wall] = strike
                elif strike != st[wall]:
                    if st[wall + "_cand"] == strike:
                        old = st[wall]
                        st[wall], st[wall + "_cand"] = strike, None
                        up = strike > old
                        self._events.put({"kind": "OI_SHIFT", "dir": "UP" if up else "DOWN", "key": key, "symbol": sym,
                                          "from": old, "to": strike, "pct": round((strike - old) / old * 100, 2), "ts": now,
                                          "reason": f"{name} shifted {_fmt(old)} → {_fmt(strike)} "
                                                    f"(now {_fmt(oi)} OI at {_fmt(strike)}) — writers moved "
                                                    f"{'higher (bullish)' if up else 'lower (bearish)'}"})
                    else:
                        st[wall + "_cand"] = strike
                else:
                    st[wall + "_cand"] = None
            # --- PCR shift ---
            if r["ce"]:
                pcr = r["pe"] / r["ce"]
                if st["pcr0"] is None:
                    st["pcr0"] = pcr
                elif abs(pcr - st["pcr0"]) >= setting("trkPcrShift"):
                    d = "UP" if pcr > st["pcr0"] else "DOWN"
                    if self.det._cool(sym, "PCR", d, now):
                        self._events.put({"kind": "PCR", "dir": d, "key": r["res"][2] or r["sup"][2], "symbol": sym,
                                          "from": round(st["pcr0"], 2), "to": round(pcr, 2),
                                          "pct": round(pcr - st["pcr0"], 2), "ts": now,
                                          "reason": f"PCR {st['pcr0']:.2f} → {pcr:.2f} since the day's first reading — "
                                                    f"{'puts being added (supportive)' if d == 'UP' else 'calls being added (capping)'}; "
                                                    f"check OI walls before reading it as direction"})
            # --- ATM straddle expansion ---
            if "CE" in r["atm"] and "PE" in r["atm"]:
                sp = r["atm"]["CE"][0] + r["atm"]["PE"][0]
                if st["str0"] is None:
                    st["str0"] = sp
                elif (sp - st["str0"]) / st["str0"] * 100 >= setting("trkStraddlePct") and self.det._cool(sym, "STRADDLE", "UP", now):
                    self._events.put({"kind": "STRADDLE", "dir": "UP", "key": r["atm"]["CE"][1], "symbol": sym,
                                      "from": round(st["str0"], 2), "to": round(sp, 2),
                                      "pct": round((sp - st["str0"]) / st["str0"] * 100, 1), "ts": now,
                                      "reason": f"ATM straddle (CE+PE) {st['str0']:.2f} → {sp:.2f} "
                                                f"(+{(sp - st['str0']) / st['str0'] * 100:.0f}%) — market pricing a bigger move"})

    def compute_greeks(self, now=None):
        """Vectorised IV/greeks for every strike with a price; IV-spike detection."""
        now = now or time.time()
        with self._lock:
            items = [(k, m) for k, m in self.sel.items() if self.latest.get(k, {}).get("ltp")]
        rows = []
        for k, m in items:
            sp = self.spots.get(self.u.stocks.get(m["symbol"], {}).get("underlying_key")) or m["spot"]
            exp = datetime.strptime(m["expiry"], "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST).timestamp()
            rows.append((k, sp, m["strike"], (exp - now) / (365 * 86400), self.latest[k]["ltp"], m["opt"] == "CE", m))
        if not rows:
            return 0
        g = ivcalc.iv_greeks([r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows],
                             [r[4] for r in rows], [r[5] for r in rows])
        span = setting("trkIvWindowMin") * 60
        pts = setting("trkIvSpikePts")
        minp = setting("trkMinPremium")
        for i, (k, sp, K, T, ltp, call, m) in enumerate(rows):
            iv = g["iv"][i]
            if iv != iv:          # NaN
                self.greeks.pop(k, None)
                continue
            self.greeks[k] = {"iv": float(iv), "delta": float(g["delta"][i]), "gamma": float(g["gamma"][i]),
                              "theta": float(g["theta"][i]), "vega": float(g["vega"][i])}
            if ltp < minp or abs(K - sp) / sp > 0.15:      # deep OTM IV is noise
                continue
            w = self.ivwin[k]
            w.push(now, float(iv), span, thin=20)
            if len(w.q) < 2:
                continue
            lo, hi = w.mn[0][1], w.mx[0][1]
            for d, base, ok in (("UP", lo, iv - lo >= pts), ("DOWN", hi, hi - iv >= pts)):
                if ok and self.det._cool(k, "IV", d, now):
                    self._events.put({"kind": "IV", "dir": d, "key": k, "from": round(base, 2), "to": round(float(iv), 2),
                                      "pct": round(float(iv) - base, 2), "ts": now,
                                      "reason": f"implied volatility {base:.1f}% → {iv:.1f}% "
                                                f"({float(iv) - base:+.1f} pts in {setting('trkIvWindowMin')} min, "
                                                f"threshold {pts} pts) — option {'re-priced richer' if d == 'UP' else 'IV crush'}"})
        return len(rows)

    def context_alerts(self):
        """Once a day per stock: F&O ban, MWPL usage crossing the alert level."""
        ts = time.time()
        for sym, s in self.u.stocks.items():
            info = self.ctx.info(sym)
            pct = info.get("mwpl_pct")
            if info["ban"] and self._once_today(("BAN", sym)):
                self._ctx_alert(sym, "BAN", f"{sym} is in the F&O BAN list for {self.ctx.ban_date} "
                                            f"(MWPL {pct}%) — no fresh positions allowed", ts)
            elif pct is not None and pct >= setting("trkMwplAlertPct") and self._once_today(("MWPL", sym)):
                self._ctx_alert(sym, "MWPL", f"{sym} MWPL usage {pct}% ({info.get('mwpl_chg'):+}% vs previous day) — "
                                             f"ban triggers at 95%", ts)

    def _ctx_alert(self, sym, kind, reason, ts, d="UP"):
        self._alert_seq += 1
        a = {"id": self._alert_seq, "symbol": sym, "name": self.u.stocks.get(sym, {}).get("name", sym), "kind": kind,
             "dir": d, "ts": ts, "time": datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S"),
             "spot": None, "expiry": None, "max_pct": 0, "legs": [], "reason": reason,
             "first_alert": self._first_alert(sym, ts), "status": "INFO", "news": [],
             "context": self.ctx.info(sym) if self.ctx else {}}
        with self._lock:
            self.alerts.append(a)
            self._save_alerts()
        self._notify(a)

    # ---------------- trends ----------------
    def _trend_event(self, e, sym, m, ts):
        tr = e["trend"]
        if e["phase"] == "START":
            tr.update({"symbol": sym, "strike": m["strike"] if m else None, "opt": m["opt"] if m else "STOCK",
                       "expiry": m["expiry"] if m else None, "first_alert": self._first_alert(sym, ts)})
            with self._lock:
                self.trends.append(tr)
        self._dirty_trends = True
        what = f"{sym} {_fmt(tr['strike'])}{tr['opt']}" if m else f"{sym} (stock)"
        if e["phase"] == "START":
            reason = (f"{what}: {tr['dir']} trend started at {tr['started_time']} from {_fmt(tr['origin'])}, "
                      f"confirmed {tr['confirmed_time']} at {_fmt(tr['confirm_price'])} — {tr['reason_start']}")
        else:
            reason = (f"{what}: {tr['dir']} trend that started {tr['started_time']} ENDED {tr['ended_time']} at "
                      f"{_fmt(tr['end_price'])} — {tr['reason_end']}. Result {tr['move_pct']:+.1f}% "
                      f"(best {tr['best_move_pct']:+.1f}%), {tr['candles']} candles, {tr['green']} green / {tr['red']} red, "
                      f"one-sided {tr['one_sided_pct']}%, max pull-back {tr['max_pullback_pct']}%, {tr['duration_min']} min")
        self._events.put({"kind": "TREND" if e["phase"] == "START" else "TREND_END", "dir": tr["dir"],
                          "key": tr["key"], "from": tr["origin"],
                          "to": tr["confirm_price"] if e["phase"] == "START" else tr["end_price"],
                          "pct": tr["move_pct"], "ts": ts, "reason": reason, "trend_id": tr["id"],
                          "symbol": sym})

    def trend_list(self, status=None, symbol=None, limit=300):
        out = []
        for t in reversed(self.trends):
            if status and t.get("status") != status.upper():
                continue
            if symbol and t.get("symbol") != symbol.upper():
                continue
            out.append(t)
            if len(out) >= limit:
                break
        return out

    # ---------------- alerts + news ----------------
    def _drain_events(self, budget=0.5):
        t_end = time.monotonic() + budget
        while time.monotonic() < t_end:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                self.handle_event(ev)
            except Exception as e:
                self.stats["errors"]["alert"] = f"{type(e).__name__}: {e}"[:200]

    def _first_alert(self, sym, ts):
        """Time of the first alert for this stock today (so every alert shows where it all began)."""
        day = datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")
        first = None
        for a in self.alerts:
            if a["symbol"] == sym and a["time"][:10] == day:
                first = a
                break
        return {"time": first["time"][11:], "kind": first["kind"], "reason": first.get("reason")} if first else None

    def _reason(self, ev, m):
        k = ev["kind"]
        if ev.get("reason"):
            return ev["reason"]
        if k == "PRICE":
            return (f"premium {_fmt(ev['from'])} → {_fmt(ev['to'])} ({ev['pct']:+.1f}%) within {setting('trkWindowMin')} min "
                    f"(threshold {setting('trkSpikePct')}%)")
        if k == "OI":
            return (f"OI {_fmt(ev['from'])} → {_fmt(ev['to'])} ({ev['pct']:+.1f}%) within {setting('trkOiWindowMin')} min "
                    f"(threshold {setting('trkOiPct')}%, >= {setting('trkMinOiLots')} lots)")
        if k == "VOLUME":
            return (f"{_fmt(ev['to'])} traded in {setting('trkVolWindowMin')} min = {ev['pct']}x the day's normal pace "
                    f"({_fmt(ev['from'])}) (threshold {setting('trkVolMult')}x)")
        return k

    def handle_event(self, ev):
        m = self.sel.get(ev["key"]) or {}
        fm = self.fut_meta.get(ev["key"])
        sym = (ev.get("symbol") or m.get("symbol") or (fm or {}).get("symbol")
               or self.ukeys.get(ev["key"], "?"))
        lv = self.latest.get(ev["key"], {})
        gk = self.greeks.get(ev["key"], {})
        reason = self._reason(ev, m)
        leg = {"key": ev["key"], "strike": m.get("strike"), "opt": m.get("opt") or ("FUT" if fm else "STOCK"),
               "from": ev["from"], "to": ev["to"], "pct": ev["pct"], "ltp": lv.get("ltp"), "vwap": lv.get("vwap"),
               "oi": lv.get("oi"), "volume": lv.get("volume"), "iv": gk.get("iv"), "reason": reason,
               "trend_id": ev.get("trend_id"), "time": datetime.fromtimestamp(ev["ts"], IST).strftime("%H:%M:%S")}
        if m:
            leg["buildup"] = self._buildup(ev["key"], lv)
        with self._lock:
            for a in reversed(self.alerts):
                if a["ts"] < ev["ts"] - setting("trkWindowMin") * 60:
                    break
                if a["symbol"] == sym and a["kind"] == ev["kind"] and a["dir"] == ev["dir"]:
                    a["legs"].append(leg)
                    a["max_pct"] = max(a["max_pct"], abs(ev["pct"] or 0))
                    self._save_alerts()
                    return a
        s = self.u.stocks.get(sym, {})
        first = self._first_alert(sym, ev["ts"])
        self._alert_seq += 1
        a = {"id": self._alert_seq, "symbol": sym, "name": s.get("name", sym), "kind": ev["kind"],
             "dir": ev["dir"], "ts": ev["ts"],
             "time": datetime.fromtimestamp(ev["ts"], IST).strftime("%Y-%m-%d %H:%M:%S"),
             "spot": m.get("spot") or self.spots.get(s.get("underlying_key")), "expiry": m.get("expiry") or (fm or {}).get("expiry"),
             "max_pct": abs(ev["pct"] or 0), "legs": [leg], "reason": reason,
             "first_alert": first or {"time": datetime.fromtimestamp(ev["ts"], IST).strftime("%H:%M:%S"),
                                      "kind": ev["kind"], "reason": reason},
             "trend_id": ev.get("trend_id"), "context": self.ctx.info(sym) if self.ctx else {},
             "status": "CHECKING", "news": []}
        with self._lock:
            self.alerts.append(a)
        self._newsq.put(a)                 # news lookup off the hot path (network: ~1 s per stock)
        if self._sync_news:
            self._news_pass()
        return a

    def _news_pass(self):
        """Resolve CHECKING alerts: NEWS-MATCH (news before the move) or UNEXPLAINED."""
        done = 0
        while True:
            try:
                a = self._newsq.get_nowait()
            except queue.Empty:
                break
            try:
                news = self.news.news_for(a["symbol"], a.get("name", a["symbol"]),
                                          since_ts=a["ts"] - setting("trkNewsLookbackMin") * 60, until_ts=a["ts"] + 60)
            except Exception as e:
                news = []
                self.stats["errors"]["news"] = str(e)[:200]
            a["status"], a["news"] = ("NEWS-MATCH" if news else "UNEXPLAINED"), news[:8]
            self._notify(a)
            done += 1
        if done:
            with self._lock:
                self._save_alerts()
        return done

    def _notify(self, a):
        kinds = {k.strip().upper() for k in str(setting("trkTelegramKinds") or "").split(",") if k.strip()}
        if a.get("kind") in kinds and self.tg.enabled():
            self.tg.push(format_alert(a))

    def _news_loop(self):
        while not self._stop.is_set():
            try:
                self._news_pass()
            except Exception as e:
                self.stats["errors"]["news"] = f"{type(e).__name__}: {e}"[:200]
            self._stop.wait(1)

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
    def _buildup(self, k, lv):
        ltp, pc, oi = lv.get("ltp"), lv.get("prev_close"), lv.get("oi")
        poi = self.prev_oi(k, lv)
        if not ltp or not pc or oi is None or not poi:
            return None
        pch, och = (ltp - pc) / pc * 100, (oi - poi) / poi * 100
        if abs(pch) < 1 or abs(och) < 2:
            return None
        return bu_label(pch, och)

    def _row(self, k, m):
        lv = self.latest.get(k, {})
        ltp, hi, lo, vwap = lv.get("ltp"), lv.get("high"), lv.get("low"), lv.get("vwap")
        oi, pc = lv.get("oi"), lv.get("prev_close")
        poi = self.prev_oi(k, lv)
        hc = self.hist_of(k) or {}
        gk = self.greeks.get(k, {})
        bid, ask = lv.get("bid"), lv.get("ask")
        tbq, tsq = lv.get("tbq"), lv.get("tsq")
        ath = max(x for x in (hc.get("ath"), hi) if x) if (hc.get("ath") or hi) else None
        atl_c = [x for x in (hc.get("atl"), lo) if x]
        atl = min(atl_c) if atl_c else None
        tr = self.trend.running(k)
        vol = lv.get("volume")
        return {"key": k, "symbol": m["symbol"], "strike": m["strike"], "opt": m["opt"], "atm": m["atm"],
                "spot": m["spot"], "expiry": m["expiry"], "front": m.get("front", True), "lot": m["lot"],
                "live": k in self.live_keys,
                "ltp": ltp, "chg_pct": round((ltp - pc) / pc * 100, 1) if ltp and pc else None,
                "vwap": vwap, "above_vwap": (ltp > vwap) if ltp and vwap else None,
                "open": lv.get("open"), "high": hi, "low": lo,
                "range_pos": round((ltp - lo) / (hi - lo) * 100) if ltp and hi and lo and hi > lo else None,
                "volume": vol, "vol_win": self.det.recent_volume(k),
                "vol_oi": round(vol / poi, 2) if vol and poi else None,
                "oi": oi, "prev_oi": poi, "oi_chg": (oi - poi) if oi is not None and poi else None,
                "oi_chg_pct": round((oi - poi) / poi * 100, 1) if oi is not None and poi else None,
                "oi_hi": lv.get("oi_hi"), "oi_lo": lv.get("oi_lo"),
                "buildup": self._buildup(k, lv),
                "oi_5d": hc.get("oi_5d"), "oi_20d": hc.get("oi_20d"), "oi_streak": hc.get("oi_streak"),
                "hist_labels": hc.get("labels"),
                "ath": ath, "ath_d": hc.get("ath_d") if hc.get("ath") and (not hi or hc["ath"] >= hi) else "today",
                "atl": atl, "atl_d": hc.get("atl_d") if hc.get("atl") and (not lo or hc["atl"] <= lo) else "today",
                "listed": hc.get("listed"),
                "iv": gk.get("iv"), "delta": gk.get("delta"), "gamma": gk.get("gamma"), "theta": gk.get("theta"),
                "vega": gk.get("vega"),
                "bid": bid, "ask": ask,
                "spread_pct": round((ask - bid) / ((ask + bid) / 2) * 100, 1) if bid and ask else None,
                "aggressor": ("BUY" if ltp and ask and ltp >= ask else "SELL" if ltp and bid and ltp <= bid else None),
                "tbq": tbq, "tsq": tsq,
                "buy_pct": round(tbq / (tbq + tsq) * 100) if tbq is not None and tsq and (tbq + tsq) else None,
                "trend": ({"dir": tr["dir"], "since": tr["started_time"], "move_pct": tr["move_pct"], "id": tr["id"]}
                          if tr else None),
                "chg_win_pct": self.det.change_pct(k)}

    def board(self, symbol=None, limit=60, sort="move", expiry=None, sign=None):
        """symbol -> full ladder. Otherwise the top `limit` strikes by `sort`, optionally only UP/DOWN.
        Fast path: rank on a cheap key first, build full rows only for the winners."""
        with self._lock:
            sel = list(self.sel.items())
        if symbol:
            symbol = symbol.upper()
            rows = [self._row(k, m) for k, m in sel if m["symbol"] == symbol
                    and (m["expiry"] == expiry if expiry else m.get("front", True))]
            rows.sort(key=lambda r: (r["strike"], r["opt"]))
            return rows
        latest, det, greeks = self.latest, self.det, self.greeks

        def oi_pct(k):
            lv = latest.get(k)
            if not lv or lv.get("oi") is None:
                return None
            poi = self.prev_oi(k, lv)
            return (lv["oi"] - poi) / poi * 100 if poi else None

        def vol_oi(k):
            lv = latest.get(k)
            if not lv or not lv.get("volume"):
                return None
            poi = self.prev_oi(k, lv)
            return lv["volume"] / poi if poi else None

        def day_chg(k):
            lv = latest.get(k)
            if not lv or not lv.get("ltp") or not lv.get("prev_close"):
                return None
            return (lv["ltp"] - lv["prev_close"]) / lv["prev_close"]

        if sort == "volume":
            rank, sgn = det.recent_volume, day_chg
        elif sort == "oi":
            rank, sgn = oi_pct, oi_pct
        elif sort == "unusual":
            rank, sgn = vol_oi, day_chg
        elif sort == "iv":
            rank, sgn = (lambda k: greeks[k]["iv"] if k in greeks else None), day_chg
        else:
            rank, sgn = det.change_pct, det.change_pct
        cand = []
        for k, m in sel:
            v = rank(k)
            if v is None or (sort == "volume" and not v):
                continue
            if sign:
                sv = sgn(k)
                if sv is None or (sign == "UP" and sv <= 0) or (sign == "DOWN" and sv >= 0):
                    continue
            cand.append((abs(v) if sort in ("move", "oi") else v, k, m))
        top = heapq.nlargest(limit, cand, key=lambda x: x[0])
        return [self._row(k, m) for _, k, m in top]

    def summary(self, max_age=0.0):
        """Cached for `max_age` s — the dashboard, sectors and futures views all read it."""
        c = getattr(self, "_sum_cache", None)
        if c and time.time() - c[0] < max_age:
            return c[1]
        out = self._summary()
        self._sum_cache = (time.time(), out)
        return out

    def _summary(self):
        """Per-stock totals & OI/volume mapping across every tracked strike + context + trends."""
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
                                        "ce_bu": {x: 0 for x in LABELS}, "pe_bu": {x: 0 for x in LABELS},
                                        "_res": (0, None), "_sup": (0, None), "_mv": (0, None), "_iv": []}
            a["strikes"] += 1
            lv = self.latest.get(k)
            if not lv:
                continue
            side = "ce" if m["opt"] == "CE" else "pe"
            oi, vol = lv.get("oi") or 0, lv.get("volume") or 0
            a[side + "_oi"] += oi
            a[side + "_vol"] += vol
            poi = self.prev_oi(k, lv)
            if poi and oi:
                a[side + "_oi_chg"] += oi - poi
                b = self._buildup(k, lv)
                if b:
                    a[side + "_bu"][b] += abs(oi - poi)
            if side == "ce" and oi > a["_res"][0]:
                a["_res"] = (oi, m["strike"])
            if side == "pe" and oi > a["_sup"][0]:
                a["_sup"] = (oi, m["strike"])
            if vol > a["_mv"][0]:
                a["_mv"] = (vol, f'{m["strike"]:g}{m["opt"]}')
            if m["strike"] == m["atm"] and k in self.greeks:
                a["_iv"].append(self.greeks[k]["iv"])
        today = now_ist().strftime("%Y-%m-%d")
        n_alerts = defaultdict(int)
        for al in self.alerts:
            if al["time"][:10] == today:
                n_alerts[al["symbol"]] += 1
        running = defaultdict(int)
        for t in self.trends:
            if t.get("status") == "RUNNING":
                running[t.get("symbol")] += 1
        out = []
        for a in agg.values():
            a["pcr"] = round(a["pe_oi"] / a["ce_oi"], 2) if a["ce_oi"] else None
            a["resistance"], a["support"], a["max_vol_strike"] = a.pop("_res")[1], a.pop("_sup")[1], a.pop("_mv")[1]
            ivs = a.pop("_iv")
            a["atm_iv"] = round(sum(ivs) / len(ivs), 1) if ivs else None
            for side in ("ce", "pe"):
                bu = a.pop(side + "_bu")
                a[side + "_buildup"] = max(bu, key=bu.get) if any(bu.values()) else None
            a["alerts"] = n_alerts.get(a["symbol"], 0)
            a["trends"] = running.get(a["symbol"], 0)
            st = self.trend.running(self.u.stocks.get(a["symbol"], {}).get("underlying_key"))
            a["stock_trend"] = {"dir": st["dir"], "since": st["started_time"], "move_pct": st["move_pct"]} if st else None
            a.update(self.ctx.info(a["symbol"]) if self.ctx else {})
            uk = self.u.stocks.get(a["symbol"], {}).get("underlying_key")
            ul = self.latest.get(uk, {})
            sp, pcl = self.spots.get(uk), ul.get("prev_close")
            a["spot"] = round(sp, 2) if sp else a["spot"]
            a["chg_pct"] = round((sp - pcl) / pcl * 100, 2) if sp and pcl else None
            an = self.anal.get(a["symbol"], {})
            a.update({k: an.get(k) for k in ("max_pain", "gex_cr", "gex_flip", "skew", "straddle", "exp_move_pct",
                                             "exp_hi", "exp_lo", "iv_rank", "iv_pctile", "iv_term", "iv_extreme")})
            if an.get("atm_iv"):
                a["atm_iv"] = an["atm_iv"]
            if self.extra:
                ei = self.extra.info(a["symbol"])
                dl = ei["delivery"] or {}
                a.update({"sector": ei["sector"], "deliv_pct": dl.get("deliv_pct"), "deliv_avg20": dl.get("deliv_avg20"),
                          "deliv_x20": dl.get("deliv_qty_x20"), "deals": len(ei["deals"])})
            fr = self.fut_row(a["symbol"])
            if fr:
                a.update({"fut_ltp": fr["ltp"], "fut_basis_pct": fr["basis_pct"], "fut_oi_chg_pct": fr["oi_chg_pct"],
                          "fut_buildup": fr["buildup"], "rollover_pct": fr["rollover_pct"]})
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
        today = now_ist().strftime("%Y-%m-%d")
        active = self.active()
        if not active:   # the main loop may be mid-tick; never show a stale "polling" feed while paused
            self.stats["feed"] = "paused (outside market hours)" if setting("TRACKER_ENABLED") else "disabled"
        return {"enabled": bool(setting("TRACKER_ENABLED")), "active": active,
                "provider": p.name if p else None, "live_limit": p.limit if p else None,
                "dhan_available": self.providers["dhan"].available() if "dhan" in self.providers else False,
                "dhan_error": getattr(self.providers.get("dhan"), "fatal", None)
                or getattr(self.providers.get("dhan_rest"), "fatal", None),
                "dhan_rest": dict(self.providers["dhan_rest"].stats) if "dhan_rest" in self.providers else None,
                "stocks_in_master": len(self.u.stocks), "stocks_tracked": len(per),
                "instruments_tracked": n, "instruments_live": nl, "underlyings_live": len(self.ukeys),
                "futures_live": len(self.fut_meta),
                "budget": min(int(setting("trkBudget")), HARD_CAP),
                "per_stock": (min(per.values()), max(per.values())) if per else (0, 0),
                "alerts_today": sum(1 for a in self.alerts if a["time"][:10] == today),
                "trends_running": sum(1 for t in self.trends if t.get("status") == "RUNNING"),
                "trends_today": sum(1 for t in self.trends
                                    if datetime.fromtimestamp(t.get("confirmed_at") or 0, IST).strftime("%Y-%m-%d") == today),
                "greeks": len(self.greeks),
                "telegram": {"enabled": self.tg.enabled(), "sent": self.tg.sent, "error": self.tg.error},
                "extra_errors": self.extra.errors if self.extra else None,
                "history": self.hist.status if self.hist else "off",
                "context": ({"ban": sorted(self.ctx.ban), "ban_date": self.ctx.ban_date, "errors": self.ctx.errors}
                            if self.ctx else None),
                "news_errors": self.news.errors, **self.stats,
                "settings": {k: setting(k) for k in SETTINGS}}

    def expiries(self, symbol):
        return sorted({m["expiry"] for m in self.sel.values() if m["symbol"] == symbol.upper()})

    def symbols(self):
        return sorted({m["symbol"] for m in self.sel.values()}) or sorted(self.u.stocks)
