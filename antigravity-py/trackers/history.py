"""Multi-day history from the official NSE F&O bhavcopy (DISPLAY ONLY).

Answers two questions brokers' live feeds cannot:
  * where has OI been building for many days (per stock, per side, per strike), and is it
    the same kind of activity day after day (long buildup / short buildup / short covering /
    long unwinding on the CALL or PUT side)?
  * what is each option contract's ALL-TIME high / low (since listing)?

Source: https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip
One row per contract per day (open/high/low/close, prev close, OI, change in OI, volume,
underlying price). `FinInstrmId` is the exchange token — the same id Upstox (NSE_FO|id) and
Dhan (NSE_FNO|id) use, so history joins straight onto live strikes.

Each day is cached as a slim gzip CSV (stock options only, ~0.4 MB). Lookback defaults to 100
calendar days — enough to cover the whole life of current, next and far monthly contracts.
"""
import os
import io
import csv
import gzip
import time
import zipfile
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

import config

IST = timezone(timedelta(hours=5, minutes=30))
CACHE = os.path.join(config.DATA, "bhav_slim")
URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
FIELDS = ["tok", "sym", "exp", "strike", "opt", "open", "high", "low", "close", "prev", "oi", "choi", "vol", "undl"]
LABELS = {"LB": "Long buildup", "SB": "Short buildup", "SC": "Short covering", "LU": "Long unwinding"}
KEEP_OI = 21                      # per-contract OI kept for the last N days (5d / 20d change)


def label(price_chg, oi_chg):
    if oi_chg > 0:
        return "LB" if price_chg > 0 else "SB" if price_chg < 0 else None
    if oi_chg < 0:
        return "SC" if price_chg > 0 else "LU" if price_chg < 0 else None
    return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


class History:
    def __init__(self, days=100, cache=CACHE, fetch=True):
        self.lookback = days
        self.cache = cache
        self.fetch = fetch
        os.makedirs(cache, exist_ok=True)
        self._http = httpx.Client(headers={"User-Agent": config.USER_AGENT, "Referer": "https://www.nseindia.com/"},
                                  timeout=30, follow_redirects=True)
        self._lock = threading.Lock()
        self.dates = []               # trading dates loaded (YYYY-MM-DD), ascending
        self.contracts = {}           # tok -> contract record
        self.stock_days = {}          # sym -> [ {date, undl, ce_oi, pe_oi, ce_choi, pe_choi, ce_vol, pe_vol, ce_lab{}, pe_lab{}} ]
        self.status = "not loaded"
        self.loaded_at = 0.0

    # ---------------- download / cache ----------------
    def _slim_path(self, d):
        return os.path.join(self.cache, d.strftime("%Y%m%d") + ".csv.gz")

    def _download(self, d):
        """-> 'ok' | 'cached' | 'none' (holiday / not yet published) | 'err:...'"""
        path = self._slim_path(d)
        if os.path.exists(path):
            return "cached"
        if not self.fetch:
            return "none"
        try:
            r = self._http.get(URL.format(ymd=d.strftime("%Y%m%d")))
            if r.status_code in (404, 403):
                return "none"
            r.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(r.content))
            raw = io.StringIO(z.read(z.namelist()[0]).decode("utf-8", errors="replace"))
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(FIELDS)
                for x in csv.DictReader(raw):
                    if x.get("FinInstrmTp") != "STO":
                        continue
                    w.writerow([x["FinInstrmId"], x["TckrSymb"], x["XpryDt"], x["StrkPric"], x["OptnTp"],
                                x["OpnPric"], x["HghPric"], x["LwPric"], x["ClsPric"], x["PrvsClsgPric"],
                                x["OpnIntrst"], x["ChngInOpnIntrst"], x["TtlTradgVol"], x["UndrlygPric"]])
            os.replace(tmp, path)
            return "ok"
        except Exception as e:
            return f"err:{type(e).__name__}"

    def sync(self, today=None):
        """Make sure every trading day in the lookback is cached (polite, sequential)."""
        today = today or datetime.now(IST).date()
        d = today - timedelta(days=self.lookback)
        got, errs = 0, 0
        while d <= today:
            if d.weekday() < 5:
                r = self._download(d)
                if r in ("ok", "cached"):
                    got += 1
                elif r.startswith("err"):
                    errs += 1
                if r == "ok":
                    time.sleep(0.25)
            d += timedelta(days=1)
        return got, errs

    # ---------------- build ----------------
    def load(self, today=None):
        today = today or datetime.now(IST).date()
        self.status = "downloading NSE bhavcopy…"
        got, errs = self.sync(today)
        self.status = "building…"
        files = sorted(f for f in os.listdir(self.cache) if f.endswith(".csv.gz"))
        cutoff = (today - timedelta(days=self.lookback)).strftime("%Y%m%d")
        files = [f for f in files if f[:8] >= cutoff]
        contracts, stock_days, dates = {}, defaultdict(list), []
        today_s = today.strftime("%Y-%m-%d")
        for fn in files:
            date = f"{fn[:4]}-{fn[4:6]}-{fn[6:8]}"
            dates.append(date)
            agg = {}
            with gzip.open(os.path.join(self.cache, fn), "rt", encoding="utf-8", newline="") as f:
                for x in csv.DictReader(f):
                    tok, sym = x["tok"], x["sym"]
                    hi, lo, cl, pv = _f(x["high"]), _f(x["low"]), _f(x["close"]), _f(x["prev"])
                    oi, choi, vol = int(_f(x["oi"])), int(_f(x["choi"])), int(_f(x["vol"]))
                    c = contracts.get(tok)
                    if c is None:
                        c = contracts[tok] = {"sym": sym, "exp": x["exp"], "strike": _f(x["strike"]), "opt": x["opt"],
                                              "listed": date, "ath": None, "ath_d": None, "atl": None, "atl_d": None,
                                              "oi_hist": [], "oi_streak": 0, "last_d": None, "last_oi": 0,
                                              "last_close": None, "lab_hist": []}
                    if vol > 0 and hi > 0:
                        if c["ath"] is None or hi > c["ath"]:
                            c["ath"], c["ath_d"] = hi, date
                        if lo > 0 and (c["atl"] is None or lo < c["atl"]):
                            c["atl"], c["atl_d"] = lo, date
                    c["oi_hist"].append((date, oi))
                    if len(c["oi_hist"]) > KEEP_OI:
                        del c["oi_hist"][0]
                    c["oi_streak"] = c["oi_streak"] + 1 if choi > 0 else (c["oi_streak"] - 1 if c["oi_streak"] < 0 and choi < 0 else (-1 if choi < 0 else 0))
                    lab = label(cl - pv, choi) if (vol > 0 or choi) else None
                    c["lab_hist"].append(lab)
                    if len(c["lab_hist"]) > 10:
                        del c["lab_hist"][0]
                    c["last_d"], c["last_oi"], c["last_close"] = date, oi, cl
                    a = agg.get(sym)
                    if a is None:
                        a = agg[sym] = {"date": date, "undl": _f(x["undl"]), "ce_oi": 0, "pe_oi": 0, "ce_choi": 0,
                                        "pe_choi": 0, "ce_vol": 0, "pe_vol": 0,
                                        "ce_lab": {"LB": 0, "SB": 0, "SC": 0, "LU": 0},
                                        "pe_lab": {"LB": 0, "SB": 0, "SC": 0, "LU": 0}}
                    side = "ce" if x["opt"] == "CE" else "pe"
                    a[side + "_oi"] += oi
                    a[side + "_choi"] += choi
                    a[side + "_vol"] += vol
                    if lab:
                        a[side + "_lab"][lab] += abs(choi)
            for sym, a in agg.items():
                a["ce_day"] = max(a["ce_lab"], key=a["ce_lab"].get) if any(a["ce_lab"].values()) else None
                a["pe_day"] = max(a["pe_lab"], key=a["pe_lab"].get) if any(a["pe_lab"].values()) else None
                a["pcr"] = round(a["pe_oi"] / a["ce_oi"], 2) if a["ce_oi"] else None
                stock_days[sym].append(a)
        # drop expired contracts (keeps memory small); ATH/ATL only matter for live ones
        live = {t: c for t, c in contracts.items() if c["exp"] >= today_s}
        with self._lock:
            self.contracts, self.stock_days, self.dates = live, dict(stock_days), dates
        self.loaded_at = time.time()
        self.status = (f"{len(dates)} trading days ({dates[0]} → {dates[-1]})" if dates else "no bhavcopy data") + \
                      (f" · {errs} download errors" if errs else "")
        return self

    # ---------------- per-contract ----------------
    def contract(self, tok, today=None):
        c = self.contracts.get(str(tok))
        if not c:
            return None
        today = today or datetime.now(IST).strftime("%Y-%m-%d")
        oh = c["oi_hist"]
        prev = [o for d, o in oh if d < today]
        n5 = oh[-6][1] if len(oh) >= 6 else (oh[0][1] if oh else None)
        n20 = oh[-21][1] if len(oh) >= 21 else (oh[0][1] if oh else None)
        return {"ath": c["ath"], "ath_d": c["ath_d"], "atl": c["atl"], "atl_d": c["atl_d"], "listed": c["listed"],
                "prev_oi": prev[-1] if prev else None, "oi_streak": c["oi_streak"],
                "oi_5d": (c["last_oi"] - n5) if n5 is not None else None,
                "oi_20d": (c["last_oi"] - n20) if n20 is not None else None,
                "labels": c["lab_hist"][-5:], "last_d": c["last_d"]}

    # ---------------- per-stock ----------------
    @staticmethod
    def _streak(vals):
        """Consecutive days (from the latest backwards) with the same sign. + = up days, - = down days."""
        s = 0
        for v in reversed(vals):
            if v > 0 and s >= 0:
                s += 1
            elif v < 0 and s <= 0:
                s -= 1
            else:
                break
        return s

    @staticmethod
    def _run(labels):
        """(label, days) of the run of identical daily labels ending on the latest day."""
        if not labels or labels[-1] is None:
            return None, 0
        n = 0
        for x in reversed(labels):
            if x != labels[-1]:
                break
            n += 1
        return labels[-1], n

    def stock_record(self, sym, n=None):
        days = self.stock_days.get(sym.upper(), [])
        return days[-n:] if n else list(days)

    def stock_table(self, window=5):
        """One row per stock: multi-day OI build per side + persistent activity."""
        out = []
        for sym, days in self.stock_days.items():
            if not days:
                continue
            last = days[-1]

            def chg(side, n):
                if len(days) <= n:
                    return None
                base = days[-1 - n][side + "_oi"]
                return round((last[side + "_oi"] - base) / base * 100, 1) if base else None

            ce_labs = [d["ce_day"] for d in days]
            pe_labs = [d["pe_day"] for d in days]
            ce_run, pe_run = self._run(ce_labs), self._run(pe_labs)
            recent = days[-window:]
            ce_cnt = {k: sum(1 for d in recent if d["ce_day"] == k) for k in LABELS}
            pe_cnt = {k: sum(1 for d in recent if d["pe_day"] == k) for k in LABELS}
            ce_dom = max(ce_cnt, key=ce_cnt.get) if any(ce_cnt.values()) else None
            pe_dom = max(pe_cnt, key=pe_cnt.get) if any(pe_cnt.values()) else None
            u0 = days[-1 - window]["undl"] if len(days) > window else days[0]["undl"]
            out.append({"symbol": sym, "date": last["date"], "days": len(days), "undl": last["undl"],
                        "undl_chg_pct": round((last["undl"] - u0) / u0 * 100, 1) if u0 else None,
                        "ce_oi": last["ce_oi"], "pe_oi": last["pe_oi"], "pcr": last["pcr"],
                        "ce_oi_5d": chg("ce", 5), "pe_oi_5d": chg("pe", 5),
                        "ce_oi_20d": chg("ce", 20), "pe_oi_20d": chg("pe", 20),
                        "ce_streak": self._streak([d["ce_choi"] for d in days]),
                        "pe_streak": self._streak([d["pe_choi"] for d in days]),
                        "ce_run": ce_run[0], "ce_run_days": ce_run[1], "pe_run": pe_run[0], "pe_run_days": pe_run[1],
                        "ce_dom": ce_dom, "ce_dom_days": ce_cnt.get(ce_dom, 0) if ce_dom else 0,
                        "pe_dom": pe_dom, "pe_dom_days": pe_cnt.get(pe_dom, 0) if pe_dom else 0,
                        "window": window})
        out.sort(key=lambda r: -max(abs(r["ce_streak"]), abs(r["pe_streak"])))
        return out

    def strike_build(self, sym, n=5, limit=20, expiry=None):
        """Strikes of one stock where OI was built most over the last n days (from bhavcopy)."""
        sym = sym.upper()
        rows = []
        for tok, c in self.contracts.items():
            if c["sym"] != sym or (expiry and c["exp"] != expiry):
                continue
            oh = c["oi_hist"]
            if len(oh) < 2:
                continue
            base = oh[-1 - n][1] if len(oh) > n else oh[0][1]
            rows.append({"tok": tok, "strike": c["strike"], "opt": c["opt"], "exp": c["exp"], "oi": c["last_oi"],
                         "oi_chg": c["last_oi"] - base, "oi_streak": c["oi_streak"], "labels": c["lab_hist"][-n:],
                         "ath": c["ath"], "atl": c["atl"]})
        rows.sort(key=lambda r: -r["oi_chg"])
        return rows[:limit]
