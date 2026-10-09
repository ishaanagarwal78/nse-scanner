"""Keeps the daily runs going even when GitHub's own schedule does not fire.

Each run starts the next one through the workflow_dispatch API (allowed with the job's own GITHUB_TOKEN):
morning -> afternoon -> evening -> relay ... relay -> next weekday's morning.
A relay job just sleeps (public-repo minutes are free) and hands over before GitHub's 6-hour limit.
Before starting a slot, we check that no run for it is already queued, running or finished today,
so a late GitHub schedule and the chain never both send the same messages.

    python -m scanner.chain next <slot>     start the slot that follows <slot>
    python -m scanner.chain done <slot>     exit 1 if <slot> already ran successfully today (another run)
    python -m scanner.chain relay           sleep toward the next morning, then hand over
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
API = f"https://api.github.com/repos/{os.environ.get('GITHUB_REPOSITORY', 'ishaanagarwal78/nse-scanner')}"
H = {"Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}", "Accept": "application/vnd.github+json"}
RUN_ID = int(os.environ.get("GITHUB_RUN_ID", "0") or 0)

SLOTS = {
    "morning": {"mode": "session", "slot": "morning", "start": "08:25", "until": "13:00", "opening": "yes"},
    "afternoon": {"mode": "session", "slot": "afternoon", "start": "12:58", "until": "18:50", "opening": "no"},
    "evening": {"mode": "evening", "slot": "evening", "start": "19:15"},
    "relay": {"mode": "relay", "slot": "relay"},
}
NEXT = {"morning": "afternoon", "afternoon": "evening", "evening": "relay"}
MAX_SLEEP = 5 * 3600 + 30 * 60


def runs_today(slot):
    today = datetime.now(IST).date()
    r = requests.get(f"{API}/actions/workflows/scan.yml/runs?per_page=40", headers=H, timeout=30)
    out = []
    for x in r.json().get("workflow_runs", []):
        made = datetime.fromisoformat(x["created_at"].replace("Z", "+00:00")).astimezone(IST).date()
        if x["id"] != RUN_ID and made == today and x.get("display_title") == slot:
            out.append(x)
    return out


def dispatch(slot):
    if slot != "relay" and any(x["status"] != "completed" or x["conclusion"] == "success" for x in runs_today(slot)):
        print(f"{slot}: already queued, running or done today; not starting another")
        return
    r = requests.post(f"{API}/actions/workflows/scan.yml/dispatches", headers=H,
                      json={"ref": "main", "inputs": SLOTS[slot]}, timeout=30)
    print(f"started {slot}: HTTP {r.status_code} {r.text[:200]}")


def next_morning(now):
    d = now.date() + timedelta(days=0 if now.strftime("%H:%M") < "08:20" else 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, 8, 25, tzinfo=IST)


def relay():
    now = datetime.now(IST)
    target = next_morning(now)
    wait = (target - now).total_seconds()
    print(f"relay: next morning run at {target:%a %d %b %H:%M} IST, {wait / 3600:.1f} h away")
    time.sleep(max(0, min(wait, MAX_SLEEP)))
    dispatch("morning" if wait <= MAX_SLEEP else "relay")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "next":
        dispatch(NEXT.get(sys.argv[2], "relay"))
    elif cmd == "done":
        ok = [x for x in runs_today(sys.argv[2]) if x["status"] == "completed" and x["conclusion"] == "success"]
        print(f"{sys.argv[2]}: {'already done today' if ok else 'not yet run today'}")
        sys.exit(1 if ok else 0)
    elif cmd == "relay":
        relay()
