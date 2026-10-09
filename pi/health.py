"""Raspberry Pi health check, run every 5 minutes by nse-health.timer.

Reads temperature, the firmware's under-voltage / throttling flags, memory and disk, and whether the scanner is
running when it should be. Sends a Telegram message to OWNER_CHAT when something needs attention (at most one
message per problem per hour), and a short status message each morning with --daily.

Heat protection: above 82 °C the market-hours service is stopped; it is started again below 65 °C.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER = os.environ.get("OWNER_CHAT", "") or os.environ.get("PREVIEW_CHAT", "")
STATE = os.path.expanduser("~/.nse-health.json")
WARN_C, STOP_C, RESUME_C = 70.0, 82.0, 65.0
DAY_UNIT = "nse-day.service"


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def temp_c():
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except Exception:
        return None


def throttled():
    """Firmware flags: bit 0 under-voltage now, 1 frequency capped, 2 throttled, 16-18 the same 'since boot'."""
    out = sh("vcgencmd", "get_throttled")
    try:
        v = int(out.split("=")[1], 16)
    except Exception:
        return None, []
    words = []
    if v & 0x1:
        words.append("under-voltage right now (check the power supply)")
    if v & 0x4:
        words.append("slowed down right now")
    if v & 0x10000 and not v & 0x1:
        words.append("under-voltage happened since the last boot")
    return v, words


def send(text):
    if not (TOKEN and OWNER):
        print(text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": OWNER, "text": text, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        print("telegram failed:", e)


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def market_hours(now):
    return now.weekday() < 5 and "08:25" <= now.strftime("%H:%M") <= "18:50"


def main(daily=False):
    now = datetime.now(IST)
    st = load()
    t = temp_c()
    flags, words = throttled()
    disk = shutil.disk_usage("/")
    disk_free = disk.free / disk.total * 100
    mem = sh("free", "-m").splitlines()
    avail = int(mem[1].split()[-1]) if len(mem) > 1 else None
    day_active = sh("systemctl", "is-active", DAY_UNIT) == "active"
    up = sh("uptime", "-p").replace("up ", "")
    problems = []

    if t is not None and t >= STOP_C and day_active:
        sh("sudo", "systemctl", "stop", DAY_UNIT)
        st["heat_stopped"] = True
        problems.append(f"🔥 {t:.0f}°C: stopped the market-hours service to cool down. It restarts by itself below {RESUME_C:.0f}°C.")
    elif t is not None and t >= WARN_C:
        problems.append(f"🌡 Running hot: {t:.0f}°C. Check the fans and airflow.")
    if st.get("heat_stopped") and t is not None and t < RESUME_C:
        st["heat_stopped"] = False
        if market_hours(now):
            sh("sudo", "systemctl", "start", DAY_UNIT)
        problems.append(f"✅ Cooled to {t:.0f}°C; market-hours service restarted.")
    if words:
        problems.append("⚡ Power: " + "; ".join(words) + ".")
    if disk_free < 10:
        problems.append(f"💾 Storage almost full: {disk_free:.0f}% free.")
    if avail is not None and avail < 80:
        problems.append(f"🧠 Low memory: {avail} MB free.")
    if market_hours(now) and now.strftime("%H:%M") >= "08:35" and not day_active and not st.get("heat_stopped"):
        problems.append("⚠️ The market-hours scanner is not running. Run: sudo systemctl start nse-day")

    sent = st.get("sent", {})
    fresh = [p for p in problems if time.time() - sent.get(p[:40], 0) > 3600]
    if fresh:
        send("🍓 <b>Pi health</b>\n" + "\n".join(fresh))
        for p in fresh:
            sent[p[:40]] = time.time()
    st["sent"] = {k: v for k, v in sent.items() if time.time() - v < 86400}

    if daily:
        ok = "all good" if not problems else f"{len(problems)} issue(s) above"
        send(f"🍓 <b>Pi status, {now:%a %d %b}</b>\n"
             f"Up {up} · {t:.0f}°C · {avail} MB memory free · {disk_free:.0f}% storage free\n"
             f"Power {'OK' if not words else 'see warning'} · {ok}.\n"
             f"Market-hours scanner starts at 8:25 am{' (weekend: not today)' if now.weekday() >= 5 else ''}.")
    json.dump(st, open(STATE, "w"))
    print(f"{now:%H:%M} temp={t} throttled={hex(flags) if flags is not None else None} free_mem={avail} disk_free={disk_free:.0f}% day={day_active}")


if __name__ == "__main__":
    main(daily="--daily" in sys.argv)
