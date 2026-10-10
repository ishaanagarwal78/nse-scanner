"""Called by systemd when a service fails (OnFailure=nse-notify@%n.service): sends its last log lines to the ops bot."""
import html
import os
import subprocess
import sys

import requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scanner.ipv4  # noqa: E402,F401  every outgoing connection over IPv4 (scanner/ipv4.py)

unit = sys.argv[1] if len(sys.argv) > 1 else "unknown"
token, owner = os.environ.get("OPS_BOT_TOKEN", ""), os.environ.get("OWNER_CHAT", "")
logs = subprocess.run(["journalctl", "-u", unit, "-n", "15", "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout[-2500:]
text = (f"💥 <b>{html.escape(unit)} failed</b>\nsystemd will restart it if it is meant to be running.\n"
        f"<pre>{html.escape(logs or 'no log lines')}</pre>")
if token and owner:
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": owner, "text": text, "parse_mode": "HTML"}, timeout=20)
else:
    print(text)
