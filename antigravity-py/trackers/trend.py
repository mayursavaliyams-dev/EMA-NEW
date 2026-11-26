"""Intraday trend engine — explains WHERE a trend started, WHEN it was confirmed, WHY, and WHERE it ended.

Runs on every tracked strike and every underlying stock, O(1) memory per instrument:
ticks are folded into `trkTrendBarMin`-minute candles; on each closed candle

  START (UP)  EMA5 > EMA13  AND  close > VWAP (when known)  AND  >= trkTrendGreen green candles
              in a row  AND  close is >= min-move % above the swing low of the last candles.
              started_at = time of that swing low ("trend started from here"),
              confirmed_at = the candle that satisfied every rule (the alert time).
  END   (UP)  close < EMA13, or close < VWAP, or the move gave back >= 50 % from its peak.
  DOWN        mirror image (red candles, below VWAP, EMA5 < EMA13, bounce >= 50 % ends it).

Every running trend carries: origin / confirm / peak / last price, move %, candles, green & red
counts, one-sided % (share of candles in the trend's direction), max pull-back %, and the
plain-language reasons for its start and end. Records are persisted by the tracker.
"""
from collections import deque
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def _t(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


class _S:
    __slots__ = ("b", "o", "h", "l", "c", "ef", "es", "n", "g", "r", "hist", "trend", "vwap")

    def __init__(self):
        self.b = None
        self.o = self.h = self.l = self.c = None
        self.ef = self.es = None
        self.n = self.g = self.r = 0
        self.hist = deque(maxlen=12)      # (t0, o, h, l, c)
        self.trend = None
        self.vwap = None


class TrendEngine:
    def __init__(self, get):
        self.get = get
        self.s = {}
        self._seq = 0

    def forget(self, keys):
        for k in keys:
            self.s.pop(k, None)

    def running(self, key):
        st = self.s.get(key)
        return st.trend if st else None

    def on_price(self, key, px, ts, vwap=None, underlying=False):
        """Feed a tick. Returns a list of events (dicts with phase START/END + the trend record)."""
        st = self.s.get(key)
        if st is None:
            st = self.s[key] = _S()
        if vwap:
            st.vwap = vwap
        span = self.get("trkTrendBarMin") * 60
        b = int(ts // span) * span
        out = []
        if st.b is None:
            st.b, st.o = b, px
            st.h = st.l = st.c = px
        elif b != st.b:
            out = self._close_bar(key, st, underlying, ts) or []
            st.b, st.o = b, px
            st.h = st.l = st.c = px
        else:
            st.h = max(st.h, px)
            st.l = min(st.l, px)
            st.c = px
        if st.trend:                      # live mark-to-market of a running trend
            tr = st.trend
            tr["last"] = px
            tr["move_pct"] = round((px - tr["origin"]) / tr["origin"] * 100, 2) if tr["origin"] else None
        return out

    def _close_bar(self, key, st, underlying, now):
        o, h, l, c, t0 = st.o, st.h, st.l, st.c, st.b
        kf, ks = 2 / 6, 2 / 14
        st.ef = c if st.ef is None else st.ef + kf * (c - st.ef)
        st.es = c if st.es is None else st.es + ks * (c - st.es)
        st.n += 1
        green, red = c > o, c < o
        st.g = st.g + 1 if green else 0
        st.r = st.r + 1 if red else 0
        st.hist.append((t0, o, h, l, c))
        vw = st.vwap
        tr = st.trend
        ev = []
        if tr:
            tr["candles"] += 1
            tr["green"] += int(green)
            tr["red"] += int(red)
            with_dir = tr["green"] if tr["dir"] == "UP" else tr["red"]
            tr["one_sided_pct"] = round(with_dir / tr["candles"] * 100)
            end = None
            if tr["dir"] == "UP":
                tr["peak"] = max(tr["peak"], h)
                pb = (tr["peak"] - l) / tr["peak"] * 100 if tr["peak"] else 0
                tr["max_pullback_pct"] = round(max(tr["max_pullback_pct"], pb), 2)
                gave = (tr["peak"] - c) / (tr["peak"] - tr["origin"]) if tr["peak"] > tr["origin"] else 0
                if c < st.es:
                    end = f"candle closed {c:g} below EMA13 {st.es:.2f}"
                elif vw and c < vw:
                    end = f"candle closed {c:g} below VWAP {vw:g}"
                elif gave >= 0.5:
                    end = f"gave back {gave * 100:.0f}% of the move from peak {tr['peak']:g}"
            else:
                tr["peak"] = min(tr["peak"], l)
                pb = (h - tr["peak"]) / tr["peak"] * 100 if tr["peak"] else 0
                tr["max_pullback_pct"] = round(max(tr["max_pullback_pct"], pb), 2)
                gave = (c - tr["peak"]) / (tr["origin"] - tr["peak"]) if tr["origin"] > tr["peak"] else 0
                if c > st.es:
                    end = f"candle closed {c:g} above EMA13 {st.es:.2f}"
                elif vw and c > vw:
                    end = f"candle closed {c:g} above VWAP {vw:g}"
                elif gave >= 0.5:
                    end = f"bounced back {gave * 100:.0f}% of the fall from low {tr['peak']:g}"
            if end:
                tr.update({"status": "ENDED", "ended_at": now, "ended_time": _t(now), "end_price": c,
                           "reason_end": end, "last": c,
                           "move_pct": round((c - tr["origin"]) / tr["origin"] * 100, 2) if tr["origin"] else None,
                           "best_move_pct": round((tr["peak"] - tr["origin"]) / tr["origin"] * 100, 2) if tr["origin"] else None,
                           "duration_min": round((now - tr["started_at"]) / 60)})
                st.trend = None
                ev.append({"phase": "END", "trend": tr})
            return ev
        # ---- look for a new trend ----
        if st.n < self.get("trkTrendMinBars"):
            return ev
        need_g = self.get("trkTrendGreen")
        min_pct = self.get("trkTrendMinPctStock" if underlying else "trkTrendMinPct")
        if not underlying and c < self.get("trkMinPremium"):
            return ev
        lows = list(st.hist)
        if st.ef > st.es and (not vw or c > vw) and st.g >= need_g:
            org = min(lows, key=lambda x: x[3])
            mv = (c - org[3]) / org[3] * 100 if org[3] else 0
            if mv >= min_pct:
                ev.append(self._start(key, st, "UP", org[0], org[3], c, now, mv, need_g, vw, underlying))
        elif st.ef < st.es and (not vw or c < vw) and st.r >= need_g:
            org = max(lows, key=lambda x: x[2])
            mv = (c - org[2]) / org[2] * 100 if org[2] else 0
            if -mv >= min_pct:
                ev.append(self._start(key, st, "DOWN", org[0], org[2], c, now, mv, need_g, vw, underlying))
        return ev

    def _start(self, key, st, d, t_org, p_org, c, now, mv, need_g, vw, underlying):
        self._seq += 1
        n = st.g if d == "UP" else st.r
        rel = ">" if d == "UP" else "<"
        why = [f"EMA5 {st.ef:.2f} {rel} EMA13 {st.es:.2f}",
               f"{n} {'green' if d == 'UP' else 'red'} candles in a row (need {need_g})",
               f"{mv:+.1f}% from the {'low' if d == 'UP' else 'high'} {p_org:g} at {_t(t_org)}"]
        if vw:
            why.insert(1, f"close {c:g} {'above' if d == 'UP' else 'below'} VWAP {vw:g}")
        tr = {"id": f"T{int(now)}-{self._seq}", "key": key, "underlying": underlying, "dir": d, "status": "RUNNING",
              "started_at": t_org, "started_time": _t(t_org), "confirmed_at": now, "confirmed_time": _t(now),
              "origin": p_org, "confirm_price": c, "peak": c, "last": c, "move_pct": round(mv, 2),
              "candles": n, "green": n if d == "UP" else 0, "red": n if d == "DOWN" else 0, "one_sided_pct": 100,
              "max_pullback_pct": 0.0, "bar_min": self.get("trkTrendBarMin"),
              "reason_start": "; ".join(why), "ended_at": None, "reason_end": None}
        st.trend = tr
        return {"phase": "START", "trend": tr}
