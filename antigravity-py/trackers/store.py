"""Day-file store: one JSON file per IST trading day, kept for a whole monthly expiry cycle.

    data/<name>/2026-11-27.json   -> [ {...}, {...} ]

Used for alerts, trends and news so a month of history survives restarts without
rewriting one giant file on every save (only the current day's file is rewritten).
Files older than keep_days are deleted. Every call is fail-soft.
"""
import os
import json
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ist_day(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")


class DayStore:
    def __init__(self, folder, keep_days=35):
        self.folder, self.keep_days = folder, keep_days
        os.makedirs(folder, exist_ok=True)
        self._t_prune = 0.0

    def _path(self, day):
        return os.path.join(self.folder, f"{day}.json")

    def days(self):
        try:
            return sorted(f[:-5] for f in os.listdir(self.folder) if f.endswith(".json") and len(f) == 15)
        except OSError:
            return []

    def load_day(self, day):
        try:
            with open(self._path(day), "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def save_day(self, day, items):
        tmp = self._path(day) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, default=str)
        os.replace(tmp, self._path(day))
        if time.time() - self._t_prune > 3600:
            self.prune()

    def merge_day(self, day, items, key="id"):
        """Add items to a day's file, de-duplicated by `key` (newer copy wins)."""
        cur = {str(x.get(key)): x for x in self.load_day(day)}
        for x in items:
            cur[str(x.get(key))] = x
        out = sorted(cur.values(), key=lambda x: x.get("ts") or 0)
        self.save_day(day, out)
        return len(out)

    def load_since(self, since_ts):
        first = ist_day(since_ts)
        out = []
        for d in self.days():
            if d >= first:
                out.extend(x for x in self.load_day(d) if (x.get("ts") or 0) >= since_ts)
        return out

    def prune(self, now=None):
        self._t_prune = time.time()
        cut = (datetime.fromtimestamp(now or time.time(), IST) - timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        for d in self.days():
            if d < cut:
                try:
                    os.remove(self._path(d))
                except OSError:
                    pass
