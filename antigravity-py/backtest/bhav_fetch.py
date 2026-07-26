"""Download NSE F&O bhavcopy (UDiFF) for a date range -> data/bhav/YYYYMMDD.csv.
Real data incl. EXPIRED option contracts (what brokers don't keep).
Usage:  python -m backtest.bhav_fetch 2024-04-29 2024-06-28
"""
import sys
import os
import io
import time
import zipfile
from datetime import datetime, timedelta

import httpx
import config

BHAV = os.path.join(config.DATA, "bhav")
os.makedirs(BHAV, exist_ok=True)
H = {"User-Agent": config.USER_AGENT, "Referer": "https://www.nseindia.com/"}


def fetch_day(d):
    ymd = d.strftime("%Y%m%d")
    out = os.path.join(BHAV, ymd + ".csv")
    if os.path.exists(out):
        return "cached"
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
    try:
        r = httpx.get(url, headers=H, timeout=30)
        if r.status_code != 200:
            return f"skip({r.status_code})"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        with open(out, "wb") as f:
            f.write(z.read(z.namelist()[0]))
        return "ok"
    except Exception as e:
        return f"err({e})"


def main():
    frm = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    to = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
    d, ok = frm, 0
    while d <= to:
        if d.weekday() < 5:
            r = fetch_day(d)
            if r in ("ok", "cached"):
                ok += 1
            print(d, r)
            time.sleep(0.15)
        d += timedelta(days=1)
    print(f"done — {ok} day files in {BHAV}")


if __name__ == "__main__":
    main()
