"""What the scanner has already handled today, kept on disk so a restart (power cut, crash, manual restart)
carries on where it stopped: nothing is sent twice, and what arrived while it was down is caught up.

One small JSON file per day in STREAM_OUT: sent-YYYY-MM-DD.json. Sets are stored as lists.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DIR = os.environ.get("STREAM_OUT", "stream_data")
_lock = threading.Lock()


def _path(day):
    return os.path.join(DIR, f"sent-{day}.json")


def load(day):
    """Today's saved state, or None if this is the first start of the day."""
    try:
        with open(_path(day), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(day, **parts):
    """Merge `parts` into today's file (both the news loop and the price thread write here)."""
    with _lock:
        doc = load(day) or {}
        for k, v in parts.items():
            doc[k] = list(v) if isinstance(v, set) else v
        doc["saved_at"] = datetime.now(IST).isoformat(timespec="seconds")
        os.makedirs(DIR, exist_ok=True)
        tmp = _path(day) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, _path(day))   # atomic: a power cut mid-write leaves the old file, never half a file


def down_since(doc, now, cap_minutes=30):
    """When the scanner stopped (its last save), capped so a long outage does not flood chats with old news."""
    try:
        t = datetime.fromisoformat(doc["saved_at"])
    except (KeyError, TypeError, ValueError):
        return now - timedelta(minutes=3)
    return max(t - timedelta(minutes=3), now - timedelta(minutes=cap_minutes))
