"""Technical alerts to the ops bot (@nseserverbot), and 'done' marks the Pi's health check uses to spot missed jobs.

    ops.alert("NSE announcements failing 3 times in a row", key="nse-ann")   # at most once an hour per key
    ops.mark("brief")                                                       # record that today's brief went out

Does nothing (prints only) when OPS_BOT_TOKEN or OWNER_CHAT is not set, e.g. on GitHub Actions.
"""
import html
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = os.environ.get("OPS_BOT_TOKEN", "")
OWNER = os.environ.get("OWNER_CHAT", "")
STATE = os.path.expanduser("~/.nse-ops.json")


def _load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"sent": {}, "marks": {}}


def _save(st):
    try:
        json.dump(st, open(STATE, "w"))
    except Exception:
        pass


def alert(text, key=None, every=3600):
    """Send a technical alert. The same key is sent at most once per `every` seconds."""
    key = key or text[:40]
    st = _load()
    if time.time() - st.setdefault("sent", {}).get(key, 0) < every:
        return False
    st["sent"][key] = time.time()
    st["sent"] = {k: v for k, v in st["sent"].items() if time.time() - v < 86400}
    _save(st)
    msg = f"🛠 <b>Scanner</b>\n{html.escape(str(text))[:3500]}"
    if not (TOKEN and OWNER):
        print("[ops alert]", text)
        return False
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": OWNER, "text": msg, "parse_mode": "HTML"}, timeout=20)
        return True
    except Exception as e:
        print("ops alert failed:", e)
        return False


def mark(name):
    """Record that a scheduled step finished today (read by pi/health.py)."""
    st = _load()
    st.setdefault("marks", {})[name] = datetime.now(IST).isoformat(timespec="seconds")
    _save(st)


def marked_today(name):
    v = _load().get("marks", {}).get(name, "")
    return v[:10] == datetime.now(IST).date().isoformat()


class Streak:
    """Alert after `n` failures in a row; send an 'OK again' note when it recovers."""

    def __init__(self, what, n=3):
        self.what, self.n, self.count, self.alerted = what, n, 0, False

    def fail(self, err):
        self.count += 1
        if self.count >= self.n and not self.alerted:
            self.alerted = alert(f"{self.what} failed {self.count} times in a row: {err}", key=f"streak:{self.what}") or True

    def ok(self):
        if self.alerted:
            alert(f"{self.what} is working again.", key=f"ok:{self.what}", every=60)
        self.count, self.alerted = 0, False
