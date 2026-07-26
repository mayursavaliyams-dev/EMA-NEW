#!/usr/bin/env python3
"""
Upstox v3 live market-data feed -> decoded -> broadcast to SSE clients.

- Connects to the Upstox v3 WebSocket feed (protobuf encoded).
- Subscribes to the dashboard's instrument keys in 'ltpc' mode.
- Decodes each FeedResponse and keeps the latest LTP per symbol.
- Any browser connected via server.py's /api/stream (SSE) gets each tick pushed.

Runs in its own background thread with its own asyncio loop. Lazy-started on the
first SSE subscriber. Auto-reconnects (re-authorizes) on drop.
"""

import json
import queue
import threading
import asyncio
import urllib.request

import websockets
import upstox_feed_pb2 as pb

UPSTOX = "https://api.upstox.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class MarketFeed:
    def __init__(self, get_token, symbols):
        """
        get_token: callable -> current access token string
        symbols:   dict  symbol -> {"key": instrument_key, ...}
        """
        self.get_token = get_token
        self.symbols = symbols
        self.key_to_symbol = {v["key"]: s for s, v in symbols.items()}
        self.latest = {}                 # symbol -> {"ltp":.., "ts":..}
        self.subscribers = set()         # set of queue.Queue for SSE clients
        self._lock = threading.Lock()
        self._thread = None
        self._started = False

    # ---- public API (called from server threads) ----
    def ensure_started(self):
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(target=self._run_thread, daemon=True)
            self._thread.start()

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self.subscribers.add(q)
        # send a snapshot of current values immediately
        for sym, v in self.latest.items():
            try:
                q.put_nowait({"symbol": sym, "ltp": v["ltp"], "ts": v.get("ts", 0)})
            except queue.Full:
                pass
        return q

    def unsubscribe(self, q):
        with self._lock:
            self.subscribers.discard(q)

    # ---- internal ----
    def _broadcast(self, msg):
        with self._lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def _authorize(self):
        req = urllib.request.Request(
            UPSTOX + "/v3/feed/market-data-feed/authorize",
            headers={
                "Authorization": "Bearer " + (self.get_token() or ""),
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["data"]["authorizedRedirectUri"]

    def _run_thread(self):
        asyncio.run(self._run())

    async def _run(self):
        while True:
            try:
                ws_url = self._authorize()
                async with websockets.connect(
                    ws_url,
                    additional_headers={"User-Agent": USER_AGENT},
                    max_size=None,
                    ping_interval=20,
                ) as ws:
                    sub = {
                        "guid": "ema-dashboard",
                        "method": "sub",
                        "data": {
                            "mode": "ltpc",
                            "instrumentKeys": [v["key"] for v in self.symbols.values()],
                        },
                    }
                    await ws.send(json.dumps(sub).encode("utf-8"))
                    print("[feed] connected & subscribed:",
                          list(self.symbols.keys()))
                    async for message in ws:
                        if isinstance(message, str):
                            continue
                        self._handle(message)
            except Exception as e:
                print("[feed] connection error, retrying in 5s:", e)
                await asyncio.sleep(5)

    def _handle(self, raw):
        resp = pb.FeedResponse()
        try:
            resp.ParseFromString(raw)
        except Exception as e:
            print("[feed] decode error:", e)
            return
        ts = resp.currentTs
        for key, feed in resp.feeds.items():
            ltp = self._extract_ltp(feed)
            if ltp is None:
                continue
            sym = self.key_to_symbol.get(key)
            if not sym:
                continue
            self.latest[sym] = {"ltp": ltp, "ts": ts}
            self._broadcast({"symbol": sym, "ltp": ltp, "ts": ts})

    @staticmethod
    def _extract_ltp(feed):
        which = feed.WhichOneof("FeedUnion")
        try:
            if which == "ltpc":
                return feed.ltpc.ltp
            if which == "firstLevelWithGreeks":
                return feed.firstLevelWithGreeks.ltpc.ltp
            if which == "fullFeed":
                ff = feed.fullFeed
                inner = ff.WhichOneof("FullFeedUnion")
                if inner == "marketFF":
                    return ff.marketFF.ltpc.ltp
                if inner == "indexFF":
                    return ff.indexFF.ltpc.ltp
        except Exception:
            return None
        return None
