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
CACHE = os.path.join(config.DATA, "bhav_slim2")      # v2: stock options + stock futures
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
        self.iv_hist = {}             # sym -> [(date, front ATM IV %, next ATM IV % | None)]
        self.stock_days = {}          # sym -> [ {date, undl, ce_oi, pe_oi, ce_choi, pe_choi, ce_vol, pe_vol, ce_lab{}, pe_lab{}} ]
        self.status = "not loaded"
        self._state = None
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
                    tp = x.get("FinInstrmTp")
                    if tp not in ("STO", "STF"):
                        continue
                    w.writerow([x["FinInstrmId"], x["TckrSymb"], x["XpryDt"], x["StrkPric"] or "0",
                                x["OptnTp"] if tp == "STO" else "FUT",
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
    STATE = "_state.pkl"
    STATE_VER = 3          # 3: a re-issued NSE token starts a new contract record (rebuild once)

    def load(self, today=None):
        """Incremental: the built state is pickled; only bhavcopy days not yet processed are parsed.
        First build ~25 s, later loads / the 30-min refresh well under a second."""
        today = today or datetime.now(IST).date()
        self.status = "downloading NSE bhavcopy…"
        got, errs = self.sync(today)
        files = sorted(f for f in os.listdir(self.cache) if f.endswith(".csv.gz"))
        cutoff = (today - timedelta(days=self.lookback)).strftime("%Y%m%d")
        files = [f for f in files if f[:8] >= cutoff]
        st = self._state or self._read_state()
        if not st or st.get("ver") != self.STATE_VER or st["files"] != files[:len(st["files"])]:
            st = {"ver": self.STATE_VER, "files": [], "contracts": {}, "stock_days": defaultdict(list),
                  "dates": [], "iv_rows": []}
        new = files[len(st["files"]):]
        if new:
            self.status = f"building… ({len(new)} new day file(s))"
            for fn in new:
                self._process_file(fn, st)
            st["files"] = st["files"] + new
            today_s = today.strftime("%Y-%m-%d")
            st["contracts"] = {t: c for t, c in st["contracts"].items() if c["exp"] >= today_s}   # drop expired
            self._write_state(st)
        self._state = st
        today_s = today.strftime("%Y-%m-%d")
        live = {t: c for t, c in st["contracts"].items() if c["exp"] >= today_s}
        if new or not self.iv_hist:
            self.iv_hist = self._iv_history(st["iv_rows"])
        with self._lock:
            self.contracts, self.stock_days, self.dates = live, dict(st["stock_days"]), list(st["dates"])
        self.loaded_at = time.time()
        dates = self.dates
        self.status = (f"{len(dates)} trading days ({dates[0]} → {dates[-1]})" if dates else "no bhavcopy data") + \
                      (f" · {errs} download errors" if errs else "")
        return self

    def _read_state(self):
        try:
            import pickle
            with open(os.path.join(self.cache, self.STATE), "rb") as f:
                st = pickle.load(f)
            st["stock_days"] = defaultdict(list, st["stock_days"])
            return st
        except Exception:
            return None

    def _write_state(self, st):
        try:
            import pickle
            path = os.path.join(self.cache, self.STATE)
            with open(path + ".tmp", "wb") as f:
                pickle.dump({**st, "stock_days": dict(st["stock_days"])}, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(path + ".tmp", path)
        except Exception:
            pass

    def _process_file(self, fn, st):
        contracts, stock_days, iv_rows = st["contracts"], st["stock_days"], st["iv_rows"]
        date = f"{fn[:4]}-{fn[4:6]}-{fn[6:8]}"
        st["dates"].append(date)
        agg, futd, atmc = {}, {}, {}
        with gzip.open(os.path.join(self.cache, fn), "rt", encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            head = next(rd, None)
            if head != FIELDS:
                return
            for x in rd:                       # tok sym exp strike opt open high low close prev oi choi vol undl
                if len(x) < 14:
                    continue
                tok, sym, exp, opt = x[0], x[1], x[2], x[4]
                hi, lo, cl, pv = _f(x[6]), _f(x[7]), _f(x[8]), _f(x[9])
                oi, choi, vol = int(_f(x[10])), int(_f(x[11])), int(_f(x[12]))
                c = contracts.get(tok)
                # NSE re-issues the token of an expired contract to a new one (e.g. 117144: IREDA Aug
                # 122.5 CE, later HDFCBANK Dec 860 CE; ~18,000 of 76,000 tokens over 100 days). A token
                # whose contract changed starts a fresh record, else the new contract's history is
                # mixed into the old one and dropped with it when the old expiry passes.
                if c is not None and (c["sym"] != sym or c["exp"] != exp or c["opt"] != opt
                                      or c["strike"] != _f(x[3])):
                    c = None
                if c is None:
                    c = contracts[tok] = {"sym": sym, "exp": exp, "strike": _f(x[3]), "opt": opt,
                                          "listed": date, "ath": None, "ath_d": None, "atl": None, "atl_d": None,
                                          "oi_hist": [], "oi_streak": 0, "last_d": None, "last_oi": 0,
                                          "last_close": None, "lab_hist": []}
                if vol > 0 and hi > 0:
                    if c["ath"] is None or hi > c["ath"]:
                        c["ath"], c["ath_d"] = hi, date
                    if lo > 0 and (c["atl"] is None or lo < c["atl"]):
                        c["atl"], c["atl_d"] = lo, date
                oh = c["oi_hist"]
                oh.append((date, oi))
                if len(oh) > KEEP_OI:
                    del oh[0]
                s_ = c["oi_streak"]
                c["oi_streak"] = s_ + 1 if choi > 0 and s_ >= 0 else 1 if choi > 0 else (s_ - 1 if s_ < 0 else -1) if choi < 0 else 0
                lab = label(cl - pv, choi) if (vol > 0 or choi) else None
                lh = c["lab_hist"]
                lh.append(lab)
                if len(lh) > 10:
                    del lh[0]
                c["last_d"], c["last_oi"], c["last_close"] = date, oi, cl
                if opt == "FUT":
                    futd.setdefault(sym, []).append((exp, oi, choi, cl, pv, vol, _f(x[13])))
                    continue
                if exp > date and cl > 0:
                    # ATM-IV candidates: per stock / expiry / strike keep CE & PE close
                    e = atmc.get(sym)
                    if e is None:
                        e = atmc[sym] = {"_u": _f(x[13])}
                    e.setdefault(exp, {}).setdefault(_f(x[3]), {})[opt] = cl
                a = agg.get(sym)
                if a is None:
                    a = agg[sym] = {"date": date, "undl": _f(x[13]), "ce_oi": 0, "pe_oi": 0, "ce_choi": 0,
                                    "pe_choi": 0, "ce_vol": 0, "pe_vol": 0,
                                    "ce_lab": {"LB": 0, "SB": 0, "SC": 0, "LU": 0},
                                    "pe_lab": {"LB": 0, "SB": 0, "SC": 0, "LU": 0}}
                side = "ce" if opt == "CE" else "pe"
                a[side + "_oi"] += oi
                a[side + "_choi"] += choi
                a[side + "_vol"] += vol
                if lab:
                    a[side + "_lab"][lab] += abs(choi)
        d0 = datetime.strptime(date, "%Y-%m-%d")
        for sym, ex in atmc.items():
            u = ex.pop("_u", 0)
            if not u:
                continue
            for slot, e in enumerate(sorted(ex)[:2]):          # front, next expiry
                both = [(abs(k - u), k, v) for k, v in ex[e].items() if "CE" in v and "PE" in v]
                if not both:
                    continue
                _, k, v = min(both)
                T = max((datetime.strptime(e, "%Y-%m-%d") - d0).days, 0.25) / 365
                iv_rows.append((sym, date, slot, u, k, T, v["CE"], v["PE"]))
        for sym, fl in futd.items():
            a = agg.get(sym)
            if a is None:
                continue
            fl.sort()
            front = fl[0]                                   # nearest expiry = front month
            tot = sum(v[1] for v in fl)
            a.update({"fut_oi": tot, "fut_choi": sum(v[2] for v in fl), "fut_close": front[3],
                      "fut_basis_pct": round((front[3] - front[6]) / front[6] * 100, 2) if front[6] else None,
                      "fut_day": label(front[3] - front[4], front[2]) if front[5] or front[2] else None,
                      "rollover_pct": round((tot - front[1]) / tot * 100, 1) if tot else None})
        for sym, a in agg.items():
            a["ce_day"] = max(a["ce_lab"], key=a["ce_lab"].get) if any(a["ce_lab"].values()) else None
            a["pe_day"] = max(a["pe_lab"], key=a["pe_lab"].get) if any(a["pe_lab"].values()) else None
            a["pcr"] = round(a["pe_oi"] / a["ce_oi"], 2) if a["ce_oi"] else None
            stock_days[sym].append(a)

    # ---------------- implied-volatility history (ATM, from closing prices) ----------------
    @staticmethod
    def _iv_history(rows):
        if not rows:
            return {}
        from trackers.ivcalc import iv_greeks
        S = [r[3] for r in rows] * 2
        K = [r[4] for r in rows] * 2
        T = [r[5] for r in rows] * 2
        P = [r[6] for r in rows] + [r[7] for r in rows]
        C = [True] * len(rows) + [False] * len(rows)
        iv = iv_greeks(S, K, T, P, C)["iv"]
        n = len(rows)
        per = {}
        for i, (sym, date, slot, *_rest) in enumerate(rows):
            vals = [v for v in (iv[i], iv[i + n]) if v == v]       # drop NaN
            if not vals:
                continue
            per.setdefault(sym, {}).setdefault(date, [None, None])[slot] = round(float(sum(vals) / len(vals)), 2)
        return {s: [(d, v[0], v[1]) for d, v in sorted(dd.items()) if v[0] is not None] for s, dd in per.items()}

    def iv_stats(self, sym, cur=None):
        """IV rank / percentile of today's ATM IV vs the stored history (default: last close)."""
        h = self.iv_hist.get(sym.upper())
        if not h:
            return None
        vals = [x[1] for x in h]
        now = cur if cur is not None else vals[-1]
        lo, hi = min(vals), max(vals)
        last = h[-1]
        rank = max(0, min(100, round((now - lo) / (hi - lo) * 100))) if hi > lo else None
        return {"iv_now": round(now, 2), "iv_rank": rank,
                "iv_extreme": "NEW HIGH" if now > hi else "NEW LOW" if now < lo else None,
                "iv_pctile": round(sum(1 for v in vals if v < now) / len(vals) * 100), "iv_min": lo, "iv_max": hi,
                "iv_days": len(vals), "iv_next": last[2],
                "iv_term": round(last[2] - last[1], 2) if last[2] is not None else None,
                "iv_series": [(d, v) for d, v, _ in h[-30:]]}

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
                base = days[-1 - n].get(side + "_oi")
                cur = last.get(side + "_oi")
                return round((cur - base) / base * 100, 1) if base and cur is not None else None

            ce_labs = [d["ce_day"] for d in days]
            pe_labs = [d["pe_day"] for d in days]
            ce_run, pe_run = self._run(ce_labs), self._run(pe_labs)
            recent = days[-window:]
            ce_cnt = {k: sum(1 for d in recent if d["ce_day"] == k) for k in LABELS}
            pe_cnt = {k: sum(1 for d in recent if d["pe_day"] == k) for k in LABELS}
            fut_run = self._run([d.get("fut_day") for d in days])
            fut_cnt = {k: sum(1 for d in recent if d.get("fut_day") == k) for k in LABELS}
            fut_dom = max(fut_cnt, key=fut_cnt.get) if any(fut_cnt.values()) else None
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
                        "fut_oi": last.get("fut_oi"), "fut_oi_5d": chg("fut", 5), "fut_oi_20d": chg("fut", 20),
                        "fut_streak": self._streak([d.get("fut_choi") or 0 for d in days]),
                        "fut_run": fut_run[0], "fut_run_days": fut_run[1],
                        "fut_dom": fut_dom, "fut_dom_days": fut_cnt.get(fut_dom, 0) if fut_dom else 0,
                        "fut_basis_pct": last.get("fut_basis_pct"), "rollover_pct": last.get("rollover_pct"),
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
            if c["sym"] != sym or c["opt"] == "FUT" or (expiry and c["exp"] != expiry):
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
