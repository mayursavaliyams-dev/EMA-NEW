"""Stock context from NSE (DISPLAY ONLY): F&O ban list, MWPL usage, corporate-event calendar.

  ban     https://nsearchives.nseindia.com/content/fo/fo_secban.csv            (next trade date)
  MWPL    https://nsearchives.nseindia.com/archives/nsccl/mwpl/combineoi_<DDMMYYYY>.zip
          -> MWPL, combined OI, future-equivalent OI; usage % = FutEqOI / MWPL.
          A stock enters the ban at 95 % and leaves below 80 %.
  events  https://www.nseindia.com/api/event-calendar?index=equities  (results, board meetings…)
Refreshed hourly; every fetch is fail-soft.
"""
import io
import csv
import time
import zipfile
from datetime import datetime, timedelta, timezone

import httpx

import config

IST = timezone(timedelta(hours=5, minutes=30))


class NseContext:
    BAN = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
    MWPL = "https://nsearchives.nseindia.com/archives/nsccl/mwpl/combineoi_{d}.zip"
    EVENTS = "https://www.nseindia.com/api/event-calendar"

    def __init__(self, ttl=3600):
        self._c = httpx.Client(headers={"User-Agent": config.USER_AGENT, "Referer": "https://www.nseindia.com/",
                                        "Accept": "*/*"}, timeout=20, follow_redirects=True)
        self.ttl = ttl
        self.ban, self.ban_date = set(), None
        self.mwpl = {}            # sym -> {mwpl, fut_eq_oi, pct, pct_prev, chg, limit_next, date}
        self.events = {}          # sym -> [ {date, purpose, desc} ]
        self.errors = {}
        self._t = 0.0

    def refresh(self, force=False):
        if not force and time.time() - self._t < self.ttl:
            return
        self._t = time.time()
        for name, fn in (("ban", self._ban), ("mwpl", self._mwpl), ("events", self._events)):
            try:
                fn()
                self.errors.pop(name, None)
            except Exception as e:
                self.errors[name] = f"{type(e).__name__}: {str(e)[:120]}"

    def _ban(self):
        r = self._c.get(self.BAN)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        self.ban_date = lines[0].split("Date")[-1].strip(" :") if lines else None
        self.ban = {ln.split(",")[1].strip().upper() for ln in lines[1:] if "," in ln}

    def _mwpl_day(self, d):
        r = self._c.get(self.MWPL.format(d=d.strftime("%d%m%Y")))
        if r.status_code != 200:
            return None
        z = zipfile.ZipFile(io.BytesIO(r.content))
        name = [n for n in z.namelist() if n.endswith(".csv")][0]
        out = {}
        rows = csv.reader(io.StringIO(z.read(name).decode("utf-8", errors="replace")))
        next(rows, None)
        for x in rows:
            if len(x) < 8:
                continue
            try:
                m, fe = float(x[4]), float(x[6])
            except ValueError:
                continue
            out[x[3].strip().upper()] = {"mwpl": m, "fut_eq_oi": fe, "pct": round(fe / m * 100, 1) if m else None,
                                         "limit_next": x[7].strip(), "date": x[0].strip()}
        return out

    def _mwpl(self):
        d = datetime.now(IST).date()
        days = []
        for _ in range(10):               # latest two published days
            if d.weekday() < 5:
                got = self._mwpl_day(d)
                if got:
                    days.append(got)
                    if len(days) == 2:
                        break
            d -= timedelta(days=1)
        if not days:
            raise RuntimeError("no combined-OI file found")
        cur = days[0]
        prev = days[1] if len(days) > 1 else {}
        for s, v in cur.items():
            p = prev.get(s, {}).get("pct")
            v["pct_prev"] = p
            v["chg"] = round(v["pct"] - p, 1) if v["pct"] is not None and p is not None else None
        self.mwpl = cur

    def _events(self):
        r = None
        for _ in range(3):
            r = self._c.get(self.EVENTS, params={"index": "equities"})
            if r.status_code == 200 and r.content.strip().startswith(b"["):
                break
            self._c.get("https://www.nseindia.com")
        r.raise_for_status()
        ev = {}
        for e in r.json():
            ev.setdefault((e.get("symbol") or "").upper(), []).append(
                {"date": e.get("date"), "purpose": e.get("purpose"), "desc": (e.get("bm_desc") or "")[:300]})
        self.events = ev

    def info(self, sym):
        sym = sym.upper()
        m = self.mwpl.get(sym, {})
        today = datetime.now(IST).date()
        upcoming = []
        for e in self.events.get(sym, []):
            try:
                d = datetime.strptime(e["date"], "%d-%b-%Y").date()
            except Exception:
                continue
            if d >= today:
                upcoming.append({**e, "days": (d - today).days})
        upcoming.sort(key=lambda e: e["days"])
        return {"ban": sym in self.ban, "mwpl_pct": m.get("pct"), "mwpl_chg": m.get("chg"),
                "mwpl_note": m.get("limit_next"), "event": upcoming[0] if upcoming else None}
