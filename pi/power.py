"""Power and load monitor for the Raspberry Pi 3B, run inside the ops bot (pi/opsbot.py).

Every 2 seconds it reads the firmware's power flags (vcgencmd get_throttled), the CPU load and clock, and counts:
  * under-voltage: the 5 V input dropped below about 4.63 V (weak adapter, thin cable, or too much load on the Pi)
  * CPU slowed: the firmware capped the clock because of low voltage or heat
  * overload: all 4 cores busy for 5 minutes, or memory nearly full
It alerts through the ops bot (each kind at most every 15-30 minutes) and keeps today's figures in ~/.nse-power.json.

Power draw: a Pi 3B has no power meter (only the Pi 5 can measure its own draw), so watts are an ESTIMATE:
  board 1.4 W idle -> 3.7 W with all 4 cores busy at 1.2 GHz, plus FAN_WATTS (default 1.0 W for two small 5 V fans and
  the relay coil) while the fans are on. Expect +-30%. A USB power meter on the cable gives the true figure.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STATE = os.path.expanduser("~/.nse-power.json")
IDLE_W, FULL_W, MAX_MHZ = 1.4, 3.7, 1200.0
FAN_W = float(os.environ.get("FAN_WATTS", "1.0"))
RATE = float(os.environ.get("POWER_RATE", "8"))        # ₹ per unit (kWh), for the monthly estimate
EVERY = 2.0
REPEAT = {"uv": 900, "slow": 1800, "load": 1800, "mem": 1800}


def _sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def flags():
    try:
        return int(_sh("vcgencmd", "get_throttled").split("=")[1], 16)
    except Exception:
        return None


def mhz():
    try:
        return int(_sh("vcgencmd", "measure_clock", "arm").split("=")[1]) / 1e6
    except Exception:
        return None


def core_volts():
    v = _sh("vcgencmd", "measure_volts", "core")
    return v.split("=")[1] if "=" in v else "?"


def _cpu_snap():
    v = list(map(int, open("/proc/stat").readline().split()[1:]))
    return sum(v), v[3] + v[4]


def _mem_avail_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


def _fresh(day):
    return {"day": day, "wh": 0.0, "secs": 0.0, "peak_w": 0.0, "uv_events": [], "uv_secs": 0.0, "slow_events": [],
            "brief_uv": False, "max_load": 0.0}


class Monitor:
    def __init__(self, send, fan_on):
        self.send, self.fan_on = send, fan_on
        try:
            self.st = json.load(open(STATE))
        except Exception:
            self.st = {}
        today = datetime.now(IST).date().isoformat()
        if self.st.get("today", {}).get("day") != today:
            self.st = {"today": _fresh(today), "yesterday": self.st.get("today")}
        self.last_alert = {}
        self.now_w, self.util, self.clock, self.flags = None, 0.0, None, None
        self.busy_since = None

    def _alert(self, kind, text):
        if time.time() - self.last_alert.get(kind, 0) >= REPEAT[kind]:
            self.last_alert[kind] = time.time()
            self.send(text)

    def _roll_day(self, now):
        if self.st["today"]["day"] != now.date().isoformat():
            self.st = {"today": _fresh(now.date().isoformat()), "yesterday": self.st["today"]}

    def run(self):
        prev_uv = prev_slow = False
        t1, i1 = _cpu_snap()
        last_t, last_clock, last_save = time.time(), 0.0, 0.0
        while True:
            time.sleep(EVERY)
            now = datetime.now(IST)
            self._roll_day(now)
            d = self.st["today"]
            dt = time.time() - last_t
            last_t = time.time()
            t2, i2 = _cpu_snap()
            self.util = max(0.0, min(1.0, 1 - (i2 - i1) / max(1, t2 - t1)))
            t1, i1 = t2, i2
            if time.time() - last_clock >= 10:
                self.clock, last_clock = mhz() or self.clock, time.time()
            f = flags()
            self.flags = f
            # estimated draw
            w = IDLE_W + (FULL_W - IDLE_W) * self.util * ((self.clock or MAX_MHZ) / MAX_MHZ) + (FAN_W if self.fan_on() else 0)
            self.now_w = w
            d["wh"] += w * dt / 3600
            d["secs"] += dt
            d["peak_w"] = max(d["peak_w"], w)
            if f is not None:
                uv, slow = bool(f & 0x1), bool(f & 0x6)
                if uv:
                    d["uv_secs"] += dt
                if uv and not prev_uv:
                    d["uv_events"].append(now.strftime("%H:%M:%S"))
                    n = len(d["uv_events"])
                    self._alert("uv", f"⚡ <b>Under-voltage</b> at {now:%H:%M:%S}: the Pi's 5 V input dropped below about 4.63 V "
                                      f"({n} time{'s' if n > 1 else ''} today). Use the official 5 V 2.5 A adapter and a short, "
                                      "thick cable; heavy load or the fans can tip a weak adapter over.")
                if slow and not prev_slow:
                    d["slow_events"].append(now.strftime("%H:%M:%S"))
                    why = "low voltage" if uv or f & 0x10000 else "heat"
                    self._alert("slow", f"🐢 <b>CPU slowed down</b> at {now:%H:%M:%S} because of {why} "
                                        f"(clock {self.clock or 0:.0f} MHz of {MAX_MHZ:.0f}).")
                # a dip shorter than our 2-second look still sets the 'since boot' flag
                if f & 0x10000 and not d["uv_events"] and not d["brief_uv"]:
                    d["brief_uv"] = True
                    self._alert("uv", "⚡ <b>A brief under-voltage happened</b> (shorter than 2 seconds, between checks). "
                                      "Usually a weak adapter or cable at a moment of peak load.")
                prev_uv, prev_slow = uv, slow
            # overload
            load1 = float(open("/proc/loadavg").read().split()[0])
            d["max_load"] = max(d["max_load"], load1)
            if self.util > 0.95 and load1 >= 3.8:
                self.busy_since = self.busy_since or time.time()
                if time.time() - self.busy_since >= 300:
                    self._alert("load", f"🔥 <b>Overload</b>: all 4 cores have been flat out for "
                                        f"{(time.time() - self.busy_since) / 60:.0f} minutes (load {load1:.1f}). "
                                        "Check the stress test or a stuck process under 📈 Usage.")
            else:
                self.busy_since = None
            mem = _mem_avail_mb()
            if mem is not None and mem < 100:
                self._alert("mem", f"🧠 <b>Memory nearly full</b>: {mem} MB left. The scanner may be restarted by the system.")
            if time.time() - last_save >= 60:
                last_save = time.time()
                try:
                    json.dump(self.st, open(STATE, "w"))
                except Exception:
                    pass

    def view(self):
        d, y = self.st["today"], self.st.get("yesterday") or {}
        f = self.flags
        avg = d["wh"] / (d["secs"] / 3600) if d["secs"] > 60 else None
        day_kwh = (avg or self.now_w or IDLE_W) * 24 / 1000
        state = []
        if f is not None:
            state.append("⚠️ under-voltage NOW" if f & 0x1 else "✅ voltage OK now")
            if f & 0x6:
                state.append("🐢 CPU slowed now")
            if f & 0x10000:
                state.append("under-voltage has happened since boot")
            if f & 0x40000:
                state.append("CPU was slowed since boot")
        uv = d["uv_events"]
        lines = [
            "⚡ <b>Power</b>",
            "Input: " + (" · ".join(state) or "unknown"),
            f"Core voltage {core_volts()} · CPU {self.clock or 0:.0f} MHz of {MAX_MHZ:.0f} · busy {self.util * 100:.0f}%",
            "",
            f"Draw now ≈ <b>{self.now_w or 0:.1f} W</b> (estimate){' · fans on' if self.fan_on() else ''}",
            f"Today: ≈ {d['wh']:.0f} Wh used, average {avg or 0:.1f} W, peak {d['peak_w']:.1f} W",
            f"≈ {day_kwh:.2f} units a day · ≈ {day_kwh * 30:.1f} units a month · ≈ ₹{day_kwh * 30 * RATE:.0f} a month at ₹{RATE:g}/unit",
            "",
            f"Under-voltage today: {len(uv)} time{'s' if len(uv) != 1 else ''}"
            + (f" (last {uv[-1]}, {d['uv_secs']:.0f} s in total)" if uv else (" (one brief dip)" if d.get("brief_uv") else "")),
            f"CPU slowed today: {len(d['slow_events'])} time{'s' if len(d['slow_events']) != 1 else ''}"
            + (f" (last {d['slow_events'][-1]})" if d["slow_events"] else ""),
            f"Highest load today: {d['max_load']:.1f} (4.0 = all cores busy)",
        ]
        if y:
            lines.append(f"Yesterday: ≈ {y.get('wh', 0):.0f} Wh, {len(y.get('uv_events', []))} under-voltage, "
                         f"peak {y.get('peak_w', 0):.1f} W")
        lines.append("\n<i>A Pi 3B can't measure its own draw, so watts are estimated from CPU load, clock and fans (±30%). "
                     "A USB power meter on the cable shows the real figure.</i>")
        return "\n".join(lines)
