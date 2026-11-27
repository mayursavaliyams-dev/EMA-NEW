"""Telegram push for important tracker alerts (optional, DISPLAY ONLY).

Create antigravity-py/telegram.json (gitignored):
    {"bot_token": "123456:ABC...", "chat_id": "987654321"}
Get a token from @BotFather, send your bot one message, then read chat_id from
https://api.telegram.org/bot<token>/getUpdates . Which alert kinds are pushed is the
`trkTelegramKinds` setting (comma-separated). Messages are queued and sent at <= 1/sec.
"""
import os
import json
import time
import queue
import threading

import httpx

import config

PATH = os.path.join(config.BASE, "telegram.json")
ICON = {"UP": "🟢", "DOWN": "🔴"}


def load_creds(path=PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            c = json.load(f)
        return c if c.get("bot_token") and c.get("chat_id") else None
    except Exception:
        return None


def format_alert(a):
    head = f"{ICON.get(a.get('dir'), '•')} {a.get('kind')} · {a.get('symbol')}"
    if a.get("status") and a["status"] not in ("INFO", "CHECKING"):
        head += f" · {a['status']}"
    lines = [head, a.get("time", ""), (a.get("reason") or "")[:600]]
    fa = a.get("first_alert") or {}
    if fa.get("time") and fa.get("time") != (a.get("time") or "")[11:]:
        lines.append(f"first alert today {fa['time']} ({fa.get('kind')})")
    for n in (a.get("news") or [])[:2]:
        lines.append(f"📰 {n.get('source')}: {n.get('title', '')[:160]}")
    return "\n".join(x for x in lines if x)


class Telegram:
    def __init__(self, path=PATH, send=None):
        self.path = path
        self.q = queue.Queue(maxsize=500)
        self.sent = 0
        self.error = None
        self._send = send or self._http_send
        self._started = False

    def enabled(self):
        return load_creds(self.path) is not None

    def _http_send(self, text):
        c = load_creds(self.path)
        if not c:
            return False
        r = httpx.post(f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
                       json={"chat_id": c["chat_id"], "text": text, "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:120]}")
        return True

    def push(self, text):
        if not self.enabled():
            return False
        if not self._started:
            self._started = True
            threading.Thread(target=self._loop, name="telegram", daemon=True).start()
        try:
            self.q.put_nowait(text)
            return True
        except queue.Full:
            self.error = "queue full (too many alerts) — some messages dropped"
            return False

    def _loop(self):
        while True:
            text = self.q.get()
            try:
                if self._send(text):
                    self.sent += 1
                    self.error = None
            except Exception as e:
                self.error = str(e)[:200]
            time.sleep(1.05)

    def flush_sync(self):
        """Tests: send everything queued right now."""
        while not self.q.empty():
            t = self.q.get_nowait()
            if self._send(t):
                self.sent += 1
