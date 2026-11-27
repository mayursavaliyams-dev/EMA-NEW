"""More NSE end-of-day / intraday data for the tracker (DISPLAY ONLY).

  participant OI   nsearchives …/content/nsccl/fao_participant_oi_<DDMMYYYY>.csv
                   FII / DII / Pro / Client long & short contracts in index & stock futures/options.
  delivery         nsearchives …/products/content/sec_bhavdata_full_<DDMMYYYY>.csv
                   per stock: traded qty, delivered qty, delivery %  (kept 25 days -> 5/20-day averages)
  large deals      www.nseindia.com/api/snapshot-capital-market-largedeal   (bulk / block deals today)
  sectors          nsearchives …/content/indices/ind_nifty500list.csv        (symbol -> industry)

Everything is cached under data/nse_extra/ and every fetch is fail-soft.
"""
import os
import io
import csv
import json
import time
from datetime import datetime, timedelta, timezone

import httpx

import config

IST = timezone(timedelta(hours=5, minutes=30))
CACHE = os.path.join(config.DATA, "nse_extra")
ARCH = "https://nsearchives.nseindia.com"
PART_COLS = ["fut_idx_long", "fut_idx_short", "fut_stk_long", "fut_stk_short", "opt_idx_call_long", "opt_idx_put_long",
             "opt_idx_call_short", "opt_idx_put_short", "opt_stk_call_long", "opt_stk_put_long", "opt_stk_call_short",
             "opt_stk_put_short", "total_long", "total_short"]


def _num(x):
    try:
        return float(str(x).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


class NseExtra:
    def __init__(self, cache=CACHE, fetch=True):
        self.cache, self.fetch = cache, fetch
        os.makedirs(cache, exist_ok=True)
        self._c = httpx.Client(headers={"User-Agent": config.USER_AGENT, "Referer": "https://www.nseindia.com/",
                                        "Accept": "*/*"}, timeout=25, follow_redirects=True)
        self.participants = []      # [{date, rows{FII:{...}, DII:{...}, Pro:{...}, Client:{...}}}] ascending
        self.delivery = {}          # sym -> [{date, close, prev_close, qty, deliv_qty, deliv_pct}] ascending
        self.deals = {"date": None, "bulk": [], "block": []}
        self.sector = {}            # sym -> industry
        self.errors = {}
        self._t = {"eod": 0.0, "deals": 0.0, "sector": 0.0}

    # ---------------- generic cached daily file ----------------
    def _daily(self, name, url):
        path = os.path.join(self.cache, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        if not self.fetch:
            return None
        r = self._c.get(url)
        if r.status_code != 200 or len(r.content) < 50 or r.content.lstrip()[:1] == b"<":
            return None
        txt = r.content.decode("utf-8", errors="replace")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(txt)
        os.replace(path + ".tmp", path)
        return txt

    @staticmethod
    def _days(n, today=None):
        d = today or datetime.now(IST).date()
        out = []
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d -= timedelta(days=1)
        return out[::-1]

    # ---------------- participant-wise OI ----------------
    @staticmethod
    def parse_participants(txt):
        rows = {}
        for x in csv.reader(io.StringIO(txt)):
            if len(x) >= 15 and x[0].strip() in ("Client", "DII", "FII", "Pro", "TOTAL"):
                vals = [_num(v) for v in x[1:15]]
                if None not in vals:
                    rows[x[0].strip()] = dict(zip(PART_COLS, vals))
        return rows

    def load_participants(self, n=20, today=None):
        out = []
        for d in self._days(n + 5, today):
            txt = self._daily(f"part_{d:%Y%m%d}.csv", f"{ARCH}/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv")
            rows = self.parse_participants(txt) if txt else {}
            if rows:
                out.append({"date": d.strftime("%Y-%m-%d"), "rows": rows})
        self.participants = out[-n:]

    def participant_view(self):
        """Per participant: net long in each bucket today, change vs previous day, net index-futures streak."""
        if not self.participants:
            return {"date": None, "rows": []}
        cur = self.participants[-1]
        prev = self.participants[-2] if len(self.participants) > 1 else None
        out = []
        for who in ("FII", "DII", "Pro", "Client"):
            r = cur["rows"].get(who)
            if not r:
                continue
            p = prev["rows"].get(who) if prev else None

            def net(x, a, b):
                return x[a] - x[b] if x else None
            nets = {"fut_idx": ("fut_idx_long", "fut_idx_short"), "fut_stk": ("fut_stk_long", "fut_stk_short"),
                    "idx_call": ("opt_idx_call_long", "opt_idx_call_short"), "idx_put": ("opt_idx_put_long", "opt_idx_put_short"),
                    "stk_call": ("opt_stk_call_long", "opt_stk_call_short"), "stk_put": ("opt_stk_put_long", "opt_stk_put_short")}
            row = {"who": who}
            for k, (a, b) in nets.items():
                row[k] = net(r, a, b)
                row[k + "_chg"] = (row[k] - net(p, a, b)) if p else None
            row["fut_idx_long_pct"] = round(r["fut_idx_long"] / (r["fut_idx_long"] + r["fut_idx_short"]) * 100, 1) \
                if (r["fut_idx_long"] + r["fut_idx_short"]) else None
            series = [d["rows"][who]["fut_idx_long"] - d["rows"][who]["fut_idx_short"]
                      for d in self.participants if who in d["rows"]]
            ch = [b - a for a, b in zip(series, series[1:])]
            s = 0
            for v in reversed(ch):
                if v > 0 and s >= 0:
                    s += 1
                elif v < 0 and s <= 0:
                    s -= 1
                else:
                    break
            row["fut_idx_streak"] = s
            row["fut_idx_series"] = series[-10:]
            out.append(row)
        return {"date": cur["date"], "prev_date": prev["date"] if prev else None, "rows": out}

    # ---------------- delivery ----------------
    def load_delivery(self, n=25, today=None, symbols=None):
        deliv = {}
        for d in self._days(n + 5, today):
            txt = self._daily(f"deliv_{d:%Y%m%d}.csv", f"{ARCH}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv")
            if not txt:
                continue
            rd = csv.reader(io.StringIO(txt))
            head = [h.strip() for h in next(rd, [])]
            ix = {h: i for i, h in enumerate(head)}
            if "DELIV_PER" not in ix:
                continue
            for x in rd:
                if len(x) < len(head) or x[ix["SERIES"]].strip() != "EQ":
                    continue
                sym = x[ix["SYMBOL"]].strip()
                if symbols and sym not in symbols:
                    continue
                dp = _num(x[ix["DELIV_PER"]])
                if dp is None:
                    continue
                deliv.setdefault(sym, []).append({"date": d.strftime("%Y-%m-%d"), "close": _num(x[ix["CLOSE_PRICE"]]),
                                                  "prev_close": _num(x[ix["PREV_CLOSE"]]), "qty": _num(x[ix["TTL_TRD_QNTY"]]),
                                                  "deliv_qty": _num(x[ix["DELIV_QTY"]]), "deliv_pct": dp})
        self.delivery = {s: v[-n:] for s, v in deliv.items()}

    def delivery_info(self, sym):
        v = self.delivery.get(sym.upper())
        if not v:
            return None
        last = v[-1]
        prior = v[:-1]

        def avg(k, n):
            xs = [x[k] for x in prior[-n:] if x[k] is not None]
            return round(sum(xs) / len(xs), 2) if xs else None
        a5, a20 = avg("deliv_pct", 5), avg("deliv_pct", 20)
        q20 = avg("deliv_qty", 20)
        chg = round((last["close"] - last["prev_close"]) / last["prev_close"] * 100, 2) \
            if last["close"] and last["prev_close"] else None
        return {"date": last["date"], "deliv_pct": last["deliv_pct"], "deliv_avg5": a5, "deliv_avg20": a20,
                "deliv_qty": last["deliv_qty"], "deliv_qty_x20": round(last["deliv_qty"] / q20, 2) if q20 else None,
                "price_chg_pct": chg}

    # ---------------- bulk / block deals ----------------
    def load_deals(self):
        r = None
        for _ in range(3):
            r = self._c.get("https://www.nseindia.com/api/snapshot-capital-market-largedeal")
            if r.status_code == 200 and r.content.lstrip()[:1] == b"{":
                break
            self._c.get("https://www.nseindia.com")
        r.raise_for_status()
        d = r.json()

        def norm(xs, kind):
            out = []
            for x in xs or []:
                if not x.get("symbol"):
                    continue
                out.append({"kind": kind, "symbol": x["symbol"].upper(), "name": x.get("name"), "client": x.get("clientName"),
                            "side": x.get("buySell"), "qty": _num(x.get("qty")), "price": _num(x.get("watp")),
                            "date": x.get("date")})
            return out
        self.deals = {"date": d.get("as_on_date"), "bulk": norm(d.get("BULK_DEALS_DATA"), "BULK"),
                      "block": norm(d.get("BLOCK_DEALS_DATA"), "BLOCK")}

    def deals_for(self, sym):
        sym = sym.upper()
        return [x for x in self.deals["bulk"] + self.deals["block"] if x["symbol"] == sym]

    # ---------------- sectors ----------------
    def load_sectors(self):
        txt = self._daily(f"nifty500_{datetime.now(IST):%Y%m}.csv", f"{ARCH}/content/indices/ind_nifty500list.csv")
        if not txt:
            raise RuntimeError("sector list unavailable")
        self.sector = {x["Symbol"].strip().upper(): x["Industry"].strip()
                       for x in csv.DictReader(io.StringIO(txt)) if x.get("Symbol") and x.get("Industry")}

    # ---------------- refresh ----------------
    def refresh(self, symbols=None, force=False):
        now = time.time()
        jobs = []
        if force or now - self._t["eod"] > 1800:
            self._t["eod"] = now
            jobs += [("participants", self.load_participants), ("delivery", lambda: self.load_delivery(symbols=symbols))]
        if force or now - self._t["deals"] > 900:
            self._t["deals"] = now
            jobs.append(("deals", self.load_deals))
        if force or now - self._t["sector"] > 86400 or not self.sector:
            self._t["sector"] = now
            jobs.append(("sectors", self.load_sectors))
        for name, fn in jobs:
            try:
                fn()
                self.errors.pop(name, None)
            except Exception as e:
                self.errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
        return [n for n, _ in jobs]

    def info(self, sym):
        sym = sym.upper()
        return {"sector": self.sector.get(sym), "delivery": self.delivery_info(sym), "deals": self.deals_for(sym)}
