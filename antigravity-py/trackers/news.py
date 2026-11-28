"""News sources for the stock-options side tracker (DISPLAY ONLY — never drives trades).

Upstox (the broker) has no public news API, so news comes from two free, key-less sources:

  1. NSE corporate announcements  (official exchange filings: results, board meetings,
     orders, M&A, fund-raise ...)   https://www.nseindia.com/api/corporate-announcements
     - global latest list polled every ~60 s  + per-symbol fetch when a stock spikes.
  2. Google News RSS               (media coverage) — fetched per stock, on demand.

Everything is cached; every network call is fail-soft (an outage = "no news", never a crash).
"""
import re
import time
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import httpx

IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
NSE_API = "https://www.nseindia.com/api/corporate-announcements"
GNEWS = "https://news.google.com/rss/search"

_STOP = {"LTD", "LIMITED", "INDIA", "OF", "THE", "AND", "&", "CO", "COMPANY", "CORPORATION",
         "CORP", "INDUSTRIES", "BANK", "FINANCE", "FINANCIAL", "SERVICES", "ENTERPRISES",
         "INTERNATIONAL", "HOLDINGS", "(INDIA)", "PVT", "PRIVATE"}


def clean_name(name):
    """'RELIANCE INDUSTRIES LTD' -> 'Reliance Industries' (drop legal suffixes)."""
    words = re.sub(r"[^A-Za-z0-9& ]", " ", name or "").split()
    while words and words[-1].upper() in ("LTD", "LIMITED", "LTD.", "CO", "CORP"):
        words.pop()
    return " ".join(w.capitalize() if not w.isupper() or len(w) > 3 else w for w in words)


def relevance_tokens(symbol, name):
    """Distinctive words that a headline must contain to count as about this stock."""
    toks = {symbol.upper()}
    for w in re.sub(r"[^A-Za-z0-9& ]", " ", name or "").upper().split():
        if w not in _STOP and len(w) >= 3:
            toks.add(w)
    return toks


def is_relevant(title, symbol, name):
    t = (title or "").upper()
    return any(re.search(r"\b" + re.escape(tok) + r"\b", t) for tok in relevance_tokens(symbol, name))


def _parse_nse_dt(s):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except Exception:
            pass
    return None


class NewsHub:
    def __init__(self, nse_ttl=60, gnews_ttl=180, keep_hours=36):
        self._c = httpx.Client(headers={"User-Agent": UA, "Accept": "application/json, */*",
                                        "Accept-Language": "en-US,en;q=0.9",
                                        "Referer": "https://www.nseindia.com/"},
                               timeout=15, follow_redirects=True)
        self._lock = threading.Lock()
        self.nse_ttl, self.gnews_ttl = nse_ttl, gnews_ttl
        self.keep = timedelta(hours=keep_hours)
        self._nse_items = {}          # seq_id -> item   (global + per-symbol)
        self._nse_last = 0.0
        self._sym_nse_last = {}       # symbol -> ts of last per-symbol fetch
        self._gnews = {}              # symbol -> (ts, [items])
        self.errors = {}              # source -> last error text (shown on dashboard)
        self._ext = {}                # id -> item from other feeds (Dhan Pro news with sentiment)

    # ---------------- NSE ----------------
    def _nse_fetch(self, symbol=None):
        params = {"index": "equities"}
        if symbol:
            params["symbol"] = symbol
        r = self._c.get(NSE_API, params=params)
        if r.status_code != 200 or not r.content.strip().startswith(b"["):
            # Akamai sometimes wants its cookies first — prime them once and retry
            self._c.get("https://www.nseindia.com")
            r = self._c.get(NSE_API, params=params)
        r.raise_for_status()
        out = []
        for a in r.json():
            dt = _parse_nse_dt(a.get("an_dt") or a.get("sort_date") or "")
            if not dt:
                continue
            out.append({"source": "NSE", "symbol": (a.get("symbol") or "").upper(),
                        "id": "nse:" + str(a.get("seq_id")),
                        "time": dt.isoformat(), "ts": dt.timestamp(),
                        "title": ((a.get("desc") or "Announcement") + " — "
                                  + (a.get("attchmntText") or "")[:180]).strip(" —"),
                        "summary": (a.get("attchmntText") or "")[:400],
                        "url": a.get("attchmntFile") or ""})
        return out

    def _store_nse(self, items):
        cutoff = (datetime.now(IST) - self.keep).timestamp()
        with self._lock:
            for it in items:
                self._nse_items[it["id"]] = it
            for k in [k for k, v in self._nse_items.items() if v["ts"] < cutoff]:
                del self._nse_items[k]

    def poll_nse(self, force=False):
        if not force and time.time() - self._nse_last < self.nse_ttl:
            return
        self._nse_last = time.time()
        try:
            self._store_nse(self._nse_fetch())
            self.errors.pop("NSE", None)
        except Exception as e:
            self.errors["NSE"] = str(e)[:200]

    def nse_for(self, symbol, fetch=True):
        symbol = symbol.upper()
        if fetch and time.time() - self._sym_nse_last.get(symbol, 0) > self.nse_ttl:
            self._sym_nse_last[symbol] = time.time()
            try:
                self._store_nse(self._nse_fetch(symbol))
            except Exception as e:
                self.errors["NSE"] = str(e)[:200]
        with self._lock:
            return [v for v in self._nse_items.values() if v["symbol"] == symbol]

    # ---------------- Google News ----------------
    def _gnews_fetch(self, symbol, name):
        q = f'"{clean_name(name) or symbol}" when:2d'
        r = self._c.get(GNEWS, params={"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
                        headers={"Accept": "application/rss+xml, */*"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for it in root.findall(".//item")[:40]:
            title = it.findtext("title") or ""
            if not is_relevant(title, symbol, name):
                continue
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate")).astimezone(IST)
            except Exception:
                continue
            out.append({"source": "Google News", "symbol": symbol,
                        "id": "g:" + (it.findtext("guid") or title)[:120],
                        "time": dt.isoformat(), "ts": dt.timestamp(), "title": title,
                        "summary": it.findtext("source") or "", "url": it.findtext("link") or ""})
        return out

    def gnews_for(self, symbol, name, fetch=True):
        symbol = symbol.upper()
        cached = self._gnews.get(symbol)
        if fetch and (not cached or time.time() - cached[0] > self.gnews_ttl):
            try:
                cached = (time.time(), self._gnews_fetch(symbol, name))
                self._gnews[symbol] = cached
                self.errors.pop("Google News", None)
            except Exception as e:
                self.errors["Google News"] = str(e)[:200]
        return list(cached[1]) if cached else []

    # ---------------- other feeds (Dhan Pro) ----------------
    def add_external(self, items):
        cutoff = (datetime.now(IST) - self.keep).timestamp()
        with self._lock:
            for it in items:
                self._ext[it["id"]] = it
            for k in [k for k, v in self._ext.items() if v["ts"] < cutoff]:
                del self._ext[k]

    def ext_for(self, symbol):
        symbol = symbol.upper()
        with self._lock:
            return [v for v in self._ext.values() if v["symbol"] == symbol]

    def latest_ext(self, symbols=None, limit=100):
        with self._lock:
            items = list(self._ext.values())
        if symbols is not None:
            items = [i for i in items if i["symbol"] in symbols]
        items.sort(key=lambda i: i["ts"], reverse=True)
        return items[:limit]

    # ---------------- combined ----------------
    def news_for(self, symbol, name, since_ts=None, until_ts=None, fetch=True):
        items = self.nse_for(symbol, fetch) + self.gnews_for(symbol, name, fetch) + self.ext_for(symbol)
        if since_ts is not None:
            items = [i for i in items if i["ts"] >= since_ts]
        if until_ts is not None:
            items = [i for i in items if i["ts"] <= until_ts]
        items.sort(key=lambda i: i["ts"], reverse=True)
        return items

    def latest_nse(self, symbols=None, limit=100):
        with self._lock:
            items = list(self._nse_items.values())
        if symbols is not None:
            items = [i for i in items if i["symbol"] in symbols]
        items.sort(key=lambda i: i["ts"], reverse=True)
        return items[:limit]
