"""Ops bot for the Raspberry Pi (@nseserverbot): status, fans, scanner control, logs, updates, reboot, stress test.

Runs as nse-opsbot.service. Uses long polling (the Pi needs no public address for this). Only OWNER_CHAT may use it.

Fans: works when a relay or transistor module switches the fans from a GPIO pin (FAN_GPIO, default 14,
FAN_ACTIVE_HIGH=1). Modes: on, off, auto (on above FAN_ON_C, off below FAN_OFF_C). Without that module the fans
are wired to 5V and always run; the bot says so.
"""
import html
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scanner.ipv4  # noqa: E402,F401  every outgoing connection over IPv4 (scanner/ipv4.py)

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = os.environ.get("OPS_BOT_TOKEN", "")
OWNER = str(os.environ.get("OWNER_CHAT", ""))
API = f"https://api.telegram.org/bot{TOKEN}"
STATE = os.path.expanduser("~/.nse-opsbot.json")
FAN_GPIO = int(os.environ.get("FAN_GPIO", "14"))
FAN_ACTIVE_HIGH = os.environ.get("FAN_ACTIVE_HIGH", "1") == "1"
FAN_WIRED = os.environ.get("FAN_WIRED", "0") == "1"   # set to 1 once the relay/transistor module is connected
# How the pin drives the relay: "float" = on by pulling the pin low, off by letting it float (5V active-low relay
# modules that a 3.3V "high" cannot switch off); "push" = drive high/low using FAN_ACTIVE_HIGH.
FAN_DRIVE = os.environ.get("FAN_DRIVE", "float")
FAN_ON_C, FAN_OFF_C = float(os.environ.get("FAN_ON_C", "55")), float(os.environ.get("FAN_OFF_C", "48"))
UNITS = {"day": "nse-day.service", "evening": "nse-evening.service", "update": "nse-update.service",
         "tracker": "nse-tracker.service"}


def sh(*cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return str(e)


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"fan": "auto", "offset": 0}


def save(st):
    json.dump(st, open(STATE, "w"))


def temp_c():
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except Exception:
        return None


_local = threading.local()


def _session():
    if not hasattr(_local, "s"):   # one kept-alive connection per thread
        _local.s = requests.Session()
    return _local.s


def tg(method, **body):
    try:
        return _session().post(f"{API}/{method}", json=body, timeout=60).json()
    except Exception as e:
        _local.__dict__.pop("s", None)
        return {"ok": False, "description": str(e)}


def B(text, data):
    return {"text": text, "callback_data": data}


MENU = [[B("📊 Status", "status"), B("📈 Usage", "usage")],
        [B("⚡ Power", "power"), B("🌀 Fans", "fan")],
        [B("🟢 Scanner", "scanner"), B("📜 Logs", "logs")],
        [B("🛡 Updates", "updates"), B("⬆️ Update now", "update")],
        [B("🧪 Stress test", "stress")],
        [B("🔁 Reboot Pi", "reboot"), B("⏻ Power off", "poweroff")]]
POWER = None   # power.Monitor, started in main()

# Written by nse-clean-shutdown.service while the Pi shuts down properly; missing at boot = power cut or crash
CLEAN_MARK = os.path.expanduser("~/.nse-clean-shutdown")


# ---------- fans ----------
def fan_set(on):
    if not FAN_WIRED:
        return False
    if FAN_DRIVE == "float":
        if on:
            sh("pinctrl", "set", str(FAN_GPIO), "op", "dl")
        else:
            sh("pinctrl", "set", str(FAN_GPIO), "ip", "pn")
    else:
        sh("pinctrl", "set", str(FAN_GPIO), "op", "dh" if on == FAN_ACTIVE_HIGH else "dl")
    return True


def fan_is_on():
    out = sh("pinctrl", "get", str(FAN_GPIO))
    if FAN_DRIVE == "float":
        return " op " in f" {out} " and "| lo" in out
    hi = "| hi" in out or " hi " in out
    return hi == FAN_ACTIVE_HIGH


def fan_loop():
    st = load()
    if FAN_WIRED and st.get("fan") in ("on", "off"):   # restore a manual choice after a restart
        fan_set(st["fan"] == "on")
    while True:
        st = load()
        t = temp_c()
        if FAN_WIRED and st.get("fan", "auto") == "auto" and t is not None:
            if t >= FAN_ON_C and not fan_is_on():
                fan_set(True)
            elif t <= FAN_OFF_C and fan_is_on():
                fan_set(False)
        time.sleep(30)


def fan_view():
    st = load()
    t = temp_c()
    if not FAN_WIRED:
        return ("🌀 <b>Fans</b>\n\nThe fans are wired straight to the Pi's 5V pins, so they always run and software "
                "can't switch them.\n\nTo control them from here: connect a small relay or transistor module between the "
                f"fans and GPIO {FAN_GPIO} (physical pin 8), then set FAN_WIRED=1 in the Pi's settings. "
                f"Auto mode then runs them above {FAN_ON_C:.0f}°C only.\n\nNow: {t:.0f}°C."), [[B("⬅️ Menu", "menu")]]
    mode = st.get("fan", "auto")
    text = (f"🌀 <b>Fans</b>: {'running' if fan_is_on() else 'off'} · mode <b>{mode}</b>\n"
            f"Pi at {t:.0f}°C. Auto runs them above {FAN_ON_C:.0f}°C and stops them below {FAN_OFF_C:.0f}°C.")
    kb = [[B(("✅ " if mode == m else "") + label, f"fan:{m}") for m, label in (("on", "On"), ("off", "Off"), ("auto", "Auto"))],
          [B("⬅️ Menu", "menu")]]
    return text, kb


# ---------- status ----------
def throttle_words():
    out = sh("vcgencmd", "get_throttled")
    try:
        v = int(out.split("=")[1], 16)
    except Exception:
        return "unknown"
    w = []
    if v & 0x1:
        w.append("under-voltage NOW")
    if v & 0x4:
        w.append("slowed down NOW")
    if v & 0x10000:
        w.append("under-voltage since boot")
    if v & 0x40000:
        w.append("slowed down since boot")
    return ", ".join(w) or "OK"


def status_view():
    t = temp_c()
    mem = sh("free", "-m").splitlines()
    avail = mem[1].split()[-1] if len(mem) > 1 else "?"
    disk = sh("df", "-h", "/").splitlines()[-1].split()
    act = {k: sh("systemctl", "is-active", u) for k, u in UNITS.items()}
    nxt = sh("systemctl", "list-timers", "nse-day.timer", "--no-pager", "--no-legend").split("  ")[0].strip()
    live = "–"
    try:
        live = "up" if requests.get("http://127.0.0.1:8765/health", timeout=3).ok else "down"
    except Exception:
        live = "off (runs in market hours)"
    now = datetime.now(IST)
    text = "\n".join([
        f"📊 <b>Pi status</b> · {now:%a %d %b, %I:%M %p}",
        f"🌡 {t:.1f}°C · ⚡ power {throttle_words()}" + (f" · ≈{POWER.now_w:.1f} W" if POWER and POWER.now_w else ""),
        f"🧠 {avail} MB memory free · 💾 {disk[3]} free of {disk[1]}",
        f"⏱ up {sh('uptime', '-p').replace('up ', '')}",
        "",
        f"🟢 Market-hours scanner: <b>{act['day']}</b>" + (f" · next start {html.escape(nxt)}" if act['day'] != 'active' and nxt else ""),
        f"📡 Live stream server: {live}",
        f"🌙 Evening reports: {act['evening']}",
        f"🗓 Lock-in refresh (7:30 pm): {act['tracker']}",
    ])
    return text, [[B("🔄 Refresh", "status"), B("⬅️ Menu", "menu")]]


def _net_bytes():
    rx = tx = 0
    for line in open("/proc/net/dev").read().splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() in ("lo",):
            continue
        f = data.split()
        rx, tx = rx + int(f[0]), tx + int(f[8])
    return rx, tx


def _cpu_pct(interval=1.0):
    def snap():
        f = open("/proc/stat").readline().split()[1:]
        v = list(map(int, f))
        return sum(v), v[3] + v[4]
    t1, i1 = snap()
    time.sleep(min(interval, 0.4))
    t2, i2 = snap()
    return 100 * (1 - (i2 - i1) / max(1, t2 - t1))


def usage_view():
    gb = lambda b: f"{b / 1e9:.2f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"
    rx, tx = _net_bytes()
    load = open("/proc/loadavg").read().split()[:3]
    mem = sh("free", "-m").splitlines()[1].split()
    scan_mem = sh("systemctl", "show", UNITS["day"], "-p", "MemoryCurrent", "--value")
    try:
        scan_mem = f"{int(scan_mem) / 1e6:.0f} MB"
    except Exception:
        scan_mem = "not running"
    live = {}
    try:
        live = requests.get("http://127.0.0.1:8765/health", timeout=3).json()
    except Exception:
        pass
    data_dir = os.path.expanduser("~/nse-data")
    files = sh("du", "-sh", data_dir).split()[0] if os.path.isdir(data_dir) else "0"
    text = "\n".join([
        "📈 <b>Usage</b>",
        f"⚙️ CPU {_cpu_pct():.0f}% · load {' / '.join(load)} (1, 5, 15 min; 4 cores)",
        f"🧠 Memory {mem[2]} MB used of {mem[1]} MB · scanner {scan_mem}",
        f"💾 Recorded market data: {files}",
        f"🌐 Data since boot: {gb(rx)} down, {gb(tx)} up",
        f"📡 NSE live connections: {live.get('nse_connected', '–')} of {live.get('followed', '–')} · "
        f"{live.get('nse_messages', 0):,} updates today · {live.get('nse_drops', 0)} drops" if live else "📡 Live stream: off (runs in market hours)",
        f"👀 Website viewers on the live stream now: {live.get('viewers', 0)}" if live else "",
    ])
    return text, [[B("🔄 Refresh", "usage"), B("⬅️ Menu", "menu")]]


UPD = {"pending": "?", "sec": "?", "behind": "?", "at": 0}


def _refresh_updates():
    app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sh("git", "-C", app, "fetch", "-q", timeout=60)
    UPD["behind"] = sh("git", "-C", app, "rev-list", "--count", "HEAD..@{u}")
    lst = sh("bash", "-c", "apt list --upgradable 2>/dev/null", timeout=120)
    UPD["pending"] = str(lst.count("upgradable"))
    UPD["sec"] = str(sum(1 for line in lst.splitlines() if "security" in line))
    UPD["at"] = time.time()


def updates_loop():
    while True:
        try:
            _refresh_updates()
        except Exception:
            pass
        time.sleep(3600)


def updates_view():
    app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    behind = UPD["behind"]
    here = sh("git", "-C", app, "log", "-1", "--format=%h %s (%cr)")
    pending, sec = UPD["pending"], UPD["sec"]
    last = sh("bash", "-c", "grep -h 'Packages that will be upgraded' /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null | tail -1")
    text = "\n".join([
        "🛡 <b>Updates</b>",
        f"Scanner code: <code>{html.escape(here)}</code>",
        f"{'✅ Up to date' if behind in ('0', '') else f'⬆️ {behind} new change(s) on GitHub'} · checks itself nightly at 3 am",
        f"System packages waiting: {pending} ({sec} security) · security updates install themselves daily",
        f"<i>Checked {int((time.time() - UPD['at']) // 60)} min ago</i>" if UPD["at"] else "<i>First check running…</i>",
        f"<i>{html.escape(last[-200:])}</i>" if last else "",
    ])
    return text, [[B("⬆️ Update scanner now", "update"), B("🔄 Check again", "updates:check")], [B("⬅️ Menu", "menu")]]


def scanner_view():
    a = sh("systemctl", "is-active", UNITS["day"])
    text = (f"🟢 <b>Market-hours scanner</b>: {a}\n\nIt starts by itself on weekdays at 8:15 am and stops at 6:50 pm. "
            "Use these only if something looks stuck.")
    return text, [[B("🔄 Restart", "svc:restart"), B("▶️ Start", "svc:start"), B("⏹ Stop", "svc:stop")],
                  [B("🌙 Run evening reports now", "svc:evening")], [B("🗓 Run lock-in refresh now", "svc:tracker")],
                  [B("⬅️ Menu", "menu")]]


def logs_view():
    out = sh("journalctl", "-u", UNITS["day"], "-u", UNITS["evening"], "-u", UNITS["tracker"], "-n", "25", "--no-pager", "-o", "cat")
    out = out[-3300:] or "No log lines yet."
    return f"📜 <b>Last log lines</b>\n<pre>{html.escape(out)}</pre>", [[B("🔄 Refresh", "logs"), B("⬅️ Menu", "menu")]]


def stress(chat, msg_id, minutes=10):
    """Run all 4 cores flat out for `minutes`, sampling temperature, then report."""
    tg("editMessageText", chat_id=chat, message_id=msg_id, parse_mode="HTML",
       text=f"🧪 Stress test running for {minutes} minutes on all 4 cores… I'll post the result here.")
    procs = [subprocess.Popen(["python3", "-c", "while True: pass"]) for _ in range(4)]
    peak, start, samples = 0.0, time.time(), []
    try:
        while time.time() - start < minutes * 60:
            t = temp_c() or 0
            peak = max(peak, t)
            samples.append(t)
            if t >= 82:
                break
            time.sleep(10)
    finally:
        for p in procs:
            p.kill()
    flags = throttle_words()
    ok = peak < 75 and "NOW" not in flags and "since boot" not in flags
    tg("sendMessage", chat_id=chat, parse_mode="HTML",
       text=(f"🧪 <b>Stress test {'passed' if ok else 'needs attention'}</b>\n"
             f"Peak {peak:.1f}°C over {len(samples) * 10 // 60} min at full load · power {flags}\n"
             + ("The Pi handles full load comfortably; the scanner uses a small fraction of that." if ok else
                "Check the power supply and fans before relying on it 24/7.")))


# ---------- bot loop ----------
def show(chat, view, msg_id=None):
    text, kb = view
    if msg_id:
        r = tg("editMessageText", chat_id=chat, message_id=msg_id, text=text, parse_mode="HTML", reply_markup={"inline_keyboard": kb})
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return
    tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup={"inline_keyboard": kb})


def handle(chat, data, msg_id=None):
    st = load()
    if data in ("menu", "start", "help"):
        return show(chat, ("🍓 <b>NSE server</b>\nRaspberry Pi controls and technical alerts. Market alerts stay in the main bot.", MENU), msg_id)
    if data == "status":
        return show(chat, status_view(), msg_id)
    if data == "fan":
        return show(chat, fan_view(), msg_id)
    if data.startswith("fan:"):
        mode = data.split(":")[1]
        st["fan"] = mode
        save(st)
        if mode in ("on", "off"):
            fan_set(mode == "on")
        return show(chat, fan_view(), msg_id)
    if data == "scanner":
        return show(chat, scanner_view(), msg_id)
    if data.startswith("svc:"):
        act = data.split(":")[1]
        if act in ("evening", "tracker"):
            sh("sudo", "systemctl", "start", "--no-block", UNITS[act])
        else:
            sh("sudo", "systemctl", act, UNITS["day"])
        time.sleep(2)
        return show(chat, scanner_view(), msg_id)
    if data == "power":
        text = POWER.view() if POWER else "⚡ The power monitor is starting; try again in a few seconds."
        return show(chat, (text, [[B("🔄 Refresh", "power"), B("⬅️ Menu", "menu")]]), msg_id)
    if data == "usage":
        return show(chat, usage_view(), msg_id)
    if data == "updates":
        return show(chat, updates_view(), msg_id)
    if data == "updates:check":
        show(chat, ("🛡 Checking for updates…", []), msg_id)
        _refresh_updates()
        return show(chat, updates_view(), msg_id)
    if data == "logs":
        return show(chat, logs_view(), msg_id)
    if data == "update":
        show(chat, ("⬆️ Updating… this takes up to a minute.", []), msg_id)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        before = sh("git", "-C", repo, "rev-parse", "HEAD")
        sh("sudo", "systemctl", "start", UNITS["update"], timeout=300)
        rev = sh("git", "-C", repo, "log", "-1", "--format=%h %s")
        bot_changed = before != sh("git", "-C", repo, "rev-parse", "HEAD") and \
            sh("git", "-C", repo, "diff", "--name-only", before, "HEAD", "--", "pi/", "scanner/ipv4.py") != ""
        show(chat, (f"⬆️ Updated to <code>{html.escape(rev)}</code>.\nThe scanner picks it up on its next start "
                    "(or tap Restart under Scanner)."
                    + ("\n🔄 This update changes the ops bot itself, so it restarts now; back in about 15 seconds." if bot_changed else ""),
                    [[B("⬅️ Menu", "menu")]]), msg_id)
        if bot_changed:
            time.sleep(1)
            os._exit(0)   # systemd (Restart=always) starts the bot again with the new code
        return
    if data == "stress":
        return show(chat, ("🧪 Run all 4 cores at full power for 10 minutes and record the peak temperature? "
                           "Best done outside market hours.", [[B("✅ Run it", "stress:go"), B("⬅️ Menu", "menu")]]), msg_id)
    if data == "stress:go":
        threading.Thread(target=stress, args=(chat, msg_id), daemon=True).start()
        return
    if data == "reboot":
        return show(chat, ("🔁 Reboot the Pi now? It's back in about 2 minutes; the scanner restarts by itself.",
                           [[B("✅ Yes, reboot", "reboot:go"), B("⬅️ No", "menu")]]), msg_id)
    if data == "reboot:go":
        show(chat, ("🔁 Rebooting… services stop first, then the Pi restarts. I'll message you when I'm back.", []), msg_id)
        st["rebooting"] = True
        save(st)
        sh("sync")
        sh("sudo", "systemctl", "reboot")
        return
    if data == "poweroff":
        return show(chat, ("⏻ <b>Power off the Pi?</b>\n\nIt stops every service, saves everything to the card and shuts down. "
                           "<b>It will not come back by itself</b>: a Pi 3B has no power button, so to start it again "
                           "unplug the power and plug it back in.\n\nWhile it's off, the scanner, live prices and evening "
                           "reports stop. The website and the main bot keep working.",
                           [[B("✅ Yes, power off", "poweroff:go"), B("⬅️ No", "menu")]]), msg_id)
    if data == "poweroff:go":
        hb = hb_grace("1 week")
        hb_note = ("Honeybadger's alarm is paused until the Pi is back (up to a week)." if hb else
                   "Honeybadger will email once that the Pi went quiet; that's expected." if os.environ.get("HONEYBADGER_CHECKIN") else "")
        show(chat, ("⏻ Shutting down… It's safe to unplug once the green light on the Pi has stopped flashing "
                    "(about 30 seconds). Plug it back in to start it; I'll message you when it's up.\n" + hb_note, []), msg_id)
        st["powering_off"] = True
        save(st)
        _heartbeat_off()
        sh("sync")
        sh("sudo", "systemctl", "poweroff")
        return
    show(chat, ("Use the buttons below.", MENU), msg_id)


# settings the owner may change from Telegram with /set NAME value (written to the scanner's .env, chmod 600)
SETTABLE = {
    "HONEYBADGER_CHECKIN": ("https://api.honeybadger.io/", "Honeybadger check-in link (outside 'Pi is alive' monitor)"),
    "HONEYBADGER_API_TOKEN": ("", "Honeybadger personal API token (lets a planned power-off pause the alarm)"),
    "HONEYBADGER_PROJECT": ("", "Honeybadger project number (from the project's web address)"),
    "SENTRY_DSN": ("https://", "Sentry link for crash reports"),
}


def hb_grace(period):
    """Change the Honeybadger check-in's grace period (Data API). Used to silence it during a planned power-off
    ("1 week") and to restore it after boot ("15 minutes"). Returns True if Honeybadger accepted it."""
    url, tok, proj = (os.environ.get(k, "") for k in ("HONEYBADGER_CHECKIN", "HONEYBADGER_API_TOKEN", "HONEYBADGER_PROJECT"))
    if not (url and tok and proj):
        return False
    try:
        r = requests.put(f"https://app.honeybadger.io/v2/projects/{proj}/check_ins/{url.rstrip('/').split('/')[-1]}",
                         auth=(tok, ""), json={"check_in": {"grace_period": period}}, timeout=15)
        return r.status_code < 300
    except Exception:
        return False


def set_setting(chat, msg_id, text):
    parts = text.split(None, 2)
    if len(parts) < 3 or parts[1].upper() not in SETTABLE:
        names = "\n".join(f"• <code>{k}</code>: {d}" for k, (_, d) in SETTABLE.items())
        tg("sendMessage", chat_id=chat, parse_mode="HTML", text=f"Use <code>/set NAME value</code>. Names:\n{names}")
        return
    name, value = parts[1].upper(), parts[2].strip()
    prefix = SETTABLE[name][0]
    tg("deleteMessage", chat_id=chat, message_id=msg_id)   # don't leave the secret in the chat
    if (not value.startswith(prefix) or any(c in value for c in " \n\r'\"`$\\")
            or (name == "HONEYBADGER_PROJECT" and not value.isdigit())):
        tg("sendMessage", chat_id=chat, text=f"⚠️ That doesn't look like a {SETTABLE[name][1]}; nothing saved.")
        return
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    lines = [l for l in open(env, encoding="utf-8").read().splitlines() if not l.startswith(name + "=")]
    lines.append(f"{name}={value}")
    tmp = env + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, env)
    os.environ[name] = value
    tg("sendMessage", chat_id=chat, parse_mode="HTML",
       text=f"✅ Saved <code>{name}</code> (your message was deleted). The health check uses it from its next run, "
            "within 5 minutes.")


def _uptime():
    try:
        return float(open("/proc/uptime").read().split()[0])
    except Exception:
        return 1e9


def _heartbeat_off():
    """Tell the website's Pi watcher this is a planned power-off, so it does not raise a "gone quiet" alarm."""
    try:
        requests.post(os.environ.get("DASHBOARD_URL", "https://nse-lockin-tracker.netlify.app").rstrip("/") + "/api/data/heartbeat",
                      headers={"x-tracker-key": os.environ.get("SUBSCRIBERS_KEY", "")}, timeout=10,
                      json={"at": datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat(timespec="seconds"),
                            "planned_off": True})
    except Exception:
        pass


def main():
    if not (TOKEN and OWNER):
        raise SystemExit("OPS_BOT_TOKEN and OWNER_CHAT must be set")
    tg("setMyCommands", commands=[{"command": "menu", "description": "Controls"}, {"command": "status", "description": "Pi status"},
                                  {"command": "usage", "description": "CPU, memory, data, connections"}, {"command": "updates", "description": "Updates"},
                                  {"command": "fan", "description": "Fans"}, {"command": "power", "description": "Power, under-voltage, load"}, {"command": "logs", "description": "Last log lines"}])
    threading.Thread(target=fan_loop, daemon=True).start()
    threading.Thread(target=updates_loop, daemon=True).start()
    global POWER
    try:
        import power
        POWER = power.Monitor(lambda text: tg("sendMessage", chat_id=OWNER, parse_mode="HTML", text=text), fan_is_on)
        threading.Thread(target=POWER.run, daemon=True).start()
    except Exception as e:
        print("power monitor not started:", e, flush=True)
    st = load()
    booted = _uptime() < 600     # this start is a fresh boot, not just the bot restarting
    planned = "reboot" if st.pop("rebooting", False) else "power-off" if st.pop("powering_off", False) else None
    save(st)
    if booted or planned == "power-off":
        hb_grace("15 minutes")   # undo a planned power-off's pause (harmless if it wasn't paused)
    if booted:
        clean = os.path.exists(CLEAN_MARK)
        how = (f"✅ The previous shutdown was clean ({open(CLEAN_MARK).read().strip()[:16].replace('T', ' ')})." if clean else
               "⚠️ <b>The previous shutdown was NOT clean</b>: the power was cut or the Pi crashed. "
               "Check the power supply and the cable.")
        try:
            os.remove(CLEAN_MARK)
        except OSError:
            pass
        lead = {"reboot": "🍓 Back online after the reboot.", "power-off": "🍓 Powered on again after the power-off."}.get(
            planned, "🍓 The Pi has started.")
        tg("sendMessage", chat_id=OWNER, parse_mode="HTML", text=f"{lead}\n{how}")
    while True:
        st = load()
        # short long-poll: home routers often drop idle connections well before 50 s, which delays every tap
        r = tg("getUpdates", offset=st.get("offset", 0), timeout=10, allowed_updates=["message", "callback_query"])
        batch = r.get("result", [])
        if batch:
            st = load()
            st["offset"] = batch[-1]["update_id"] + 1
            save(st)
        for u in batch:
            sent = (u.get("message") or (u.get("callback_query") or {}).get("message") or {}).get("date")
            if sent and "message" in u:
                print(f"update received {time.time() - sent:.1f} s after it was sent", flush=True)
            if "callback_query" in u:
                cq = u["callback_query"]
                chat = str(cq["message"]["chat"]["id"])
                threading.Thread(target=tg, args=("answerCallbackQuery",), kwargs={"callback_query_id": cq["id"]}, daemon=True).start()
                if chat == OWNER:
                    threading.Thread(target=handle, args=(chat, cq.get("data", ""), cq["message"]["message_id"]), daemon=True).start()
            elif "message" in u:
                chat = str(u["message"]["chat"]["id"])
                if chat != OWNER:
                    tg("sendMessage", chat_id=chat, text="This bot is private.")
                    continue
                text = (u["message"].get("text") or "").strip()
                if text.lower().startswith("/set"):
                    set_setting(chat, u["message"]["message_id"], text)
                    continue
                cmd = text.lstrip("/").split("@")[0].split()[0:1]
                threading.Thread(target=handle, args=(chat, cmd[0].lower() if cmd else "menu"), daemon=True).start()
        if not r.get("ok"):
            time.sleep(10)


if __name__ == "__main__":
    main()
