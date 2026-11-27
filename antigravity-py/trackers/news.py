"""News sources for the stock-options side tracker (DISPLAY ONLY — never drives trades).

Upstox (the broker) has no public news API, so news comes from two free, key-less sources:

  1. NSE corporate announcements  (official exchange filings: results, board meetings,
     orders, M&A, fund-raise ...)   https://www.nseindia.com/api/corporate-announcements
     - global latest list polled every ~60 s  + per-symbol fetch when a stock spikes.
  2. Google News RSS               (media coverage) — fetched per stock, on demand.
  3. Dhan News Headlines API       (POST /v2/data/newsheadline, Dhan "Pro Traders" data plan)
     - only when the tracker runs on Dhan; latest 50 polled every 2 min + per stock on demand.
     - Dhan's response item fields are not publicly documented (SDK 2.4.0rc1 only says the list
       is under data.latestNews), so parse_dhan_news() accepts several likely field names and
       the raw reply is kept (dhan_raw) for /api/tracker/news/dhan-raw to check the real names.

Everything is cached; every network call is fail-soft (an outage = "no news", never a crash).
"""
import re
import json
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


# ---------------- Dhan news parsing (field names tolerant) ----------------
def _pick(d, names):
    for n in names:
        v = d.get(n)
        if v not in (None, "", []):
            return v
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, "", []):
            return v
    return None


def _parse_any_dt(v):
    """epoch s / ms, ISO-8601, or common Indian date strings -> aware IST datetime."""
    if v is None:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        return datetime.fromtimestamp(x, IST) if x > 1e9 else None
    s = str(v).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


_T_TITLE = ("headline", "title", "newsHeadline", "news_headline", "heading", "newsTitle", "text")
_T_SUM = ("summary", "description", "newsSummary", "content", "body", "shortDescription")
_T_TIME = ("publishedAt", "published_at", "publishTime", "publishedTime", "newsTime", "timestamp",
           "time", "dateTime", "datetime", "date", "createdAt", "pubDate")
_T_SRC = ("source", "sourceName", "publisher", "provider")
_T_URL = ("url", "link", "newsUrl", "sourceUrl", "articleUrl")
_T_SENT = ("sentiment", "sentimentLabel", "newsSentiment", "tone", "sentimentScore")
_T_SID = ("securityId", "security_id", "securityIds", "stockList", "securities", "scrips", "sid")
_T_SYM = ("symbol", "tradingSymbol", "trading_symbol", "symbols", "stockSymbol", "ticker", "stocks")


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            if isinstance(x, dict):
                x = _pick(x, _T_SID + _T_SYM)
            if x not in (None, ""):
                out.append(str(x))
        return out
    return [p.strip() for p in str(v).split(",") if p.strip()]


def dhan_news_items(payload):
    """The list of raw news dicts inside a Dhan reply (data.latestNews per the SDK)."""
    d = payload
    for _ in range(3):
        if isinstance(d, list):
            return [x for x in d if isinstance(x, dict)]
        if not isinstance(d, dict):
            return []
        nxt = None
        for k in ("latestNews", "data", "news", "items", "result", "headlines"):
            if k in d:
                nxt = d[k]
                break
        if nxt is None:
            return []
        d = nxt
    return d if isinstance(d, list) else []


def parse_dhan_news(payload, sid_map, default_symbol=None, now=None):
    """Normalise a Dhan news reply into tracker news items.
    sid_map: {security_id(str): (SYMBOL, company name)} for the F&O stocks being tracked."""
    now = now or datetime.now(IST)
    by_sym = {sym: name for sym, name in sid_map.values()}
    out = []
    for it in dhan_news_items(payload):
        title = _pick(it, _T_TITLE)
        if not title:
            continue
        title = str(title).strip()
        dt = _parse_any_dt(_pick(it, _T_TIME)) or now
        syms = []
        for sid in _as_list(_pick(it, _T_SID)):
            if sid in sid_map:
                syms.append(sid_map[sid][0])
        for sym in _as_list(_pick(it, _T_SYM)):
            if sym.upper() in by_sym:
                syms.append(sym.upper())
        if not syms and default_symbol:
            syms = [default_symbol]
        if not syms:                         # no tag: match the headline against tracked stocks
            syms = [s for s, n in by_sym.items() if is_relevant(title, s, n)][:3]
        sent = _pick(it, _T_SENT)
        src = _pick(it, _T_SRC)
        nid = _pick(it, ("id", "newsId", "news_id", "uuid")) or f"{title[:80]}|{dt.isoformat()}"
        summary = str(_pick(it, _T_SUM) or "")[:400]
        for sym in dict.fromkeys(syms) or [""]:
            out.append({"source": "Dhan" + (f" · {src}" if src and not isinstance(src, (dict, list)) else ""),
                        "symbol": sym, "id": f"dhan:{nid}:{sym}", "time": dt.isoformat(), "ts": dt.timestamp(),
                        "title": title, "summary": summary, "url": str(_pick(it, _T_URL) or ""),
                        "sentiment": str(sent) if sent is not None and not isinstance(sent, (dict, list)) else ""})
    return out


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
        self._dhan_post = None        # fn(body) -> json   (set by the tracker when Dhan is the provider)
        self._dhan_sids = lambda: {}  # fn() -> {security_id: (SYMBOL, name)}
        self._dhan_items = {}         # id -> item
        self._dhan_last = 0.0
        self._sym_dhan_last = {}
        self.dhan_ttl = 120
        self.dhan_raw = None          # last raw reply (to check Dhan's real field names)
        self.dhan_polls = 0

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

    # ---------------- Dhan ----------------
    def set_dhan(self, post, sid_map_fn):
        self._dhan_post, self._dhan_sids = post, sid_map_fn

    def dhan_enabled(self):
        return self._dhan_post is not None

    def _dhan_fetch(self, stock_ids=None, limit=50, default_symbol=None):
        body = {"categories": ["ALL"], "limit": limit}
        if stock_ids:
            body["stockList"] = [str(x) for x in stock_ids]
        raw = self._dhan_post(body)
        self.dhan_raw = {"request": body, "at": datetime.now(IST).isoformat(timespec="seconds"),
                         "reply": raw if len(json.dumps(raw, default=str)) < 60000 else {"truncated": dhan_news_items(raw)[:5]}}
        self.dhan_polls += 1
        return parse_dhan_news(raw, self._dhan_sids() or {}, default_symbol)

    def _store_dhan(self, items):
        cutoff = (datetime.now(IST) - self.keep).timestamp()
        with self._lock:
            for it in items:
                self._dhan_items[it["id"]] = it
            for k in [k for k, v in self._dhan_items.items() if v["ts"] < cutoff]:
                del self._dhan_items[k]

    def poll_dhan(self, force=False):
        if not self._dhan_post or (not force and time.time() - self._dhan_last < self.dhan_ttl):
            return
        self._dhan_last = time.time()
        try:
            self._store_dhan(self._dhan_fetch())
            self.errors.pop("Dhan", None)
        except Exception as e:
            self.errors["Dhan"] = str(e)[:200]

    def dhan_for(self, symbol, fetch=True):
        symbol = symbol.upper()
        if fetch and self._dhan_post and time.time() - self._sym_dhan_last.get(symbol, 0) > 300:
            self._sym_dhan_last[symbol] = time.time()
            sid = next((k for k, v in (self._dhan_sids() or {}).items() if v[0] == symbol), None)
            if sid:
                try:
                    self._store_dhan(self._dhan_fetch([sid], limit=20, default_symbol=symbol))
                    self.errors.pop("Dhan", None)
                except Exception as e:
                    self.errors["Dhan"] = str(e)[:200]
        with self._lock:
            return [v for v in self._dhan_items.values() if v["symbol"] == symbol]

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

    # ---------------- combined ----------------
    def news_for(self, symbol, name, since_ts=None, until_ts=None, fetch=True):
        items = self.nse_for(symbol, fetch) + self.gnews_for(symbol, name, fetch) + self.dhan_for(symbol, fetch)
        if since_ts is not None:
            items = [i for i in items if i["ts"] >= since_ts]
        if until_ts is not None:
            items = [i for i in items if i["ts"] <= until_ts]
        items.sort(key=lambda i: i["ts"], reverse=True)
        return items

    def latest_nse(self, symbols=None, limit=100):
        """Latest exchange filings + Dhan headlines (the dashboard's global news list)."""
        with self._lock:
            items = list(self._nse_items.values()) + [v for v in self._dhan_items.values() if v["symbol"]]
        if symbols is not None:
            items = [i for i in items if i["symbol"] in symbols]
        items.sort(key=lambda i: i["ts"], reverse=True)
        return items[:limit]
