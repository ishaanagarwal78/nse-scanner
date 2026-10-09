#!/usr/bin/env bash
# Set up the NSE scanner on a Raspberry Pi (Raspberry Pi OS Lite 64-bit). Safe to run again.
#   bash pi/install.sh            (from the cloned nse-scanner folder, as the normal user; asks for sudo)
# Needs ~/nse-scanner/.env with TELEGRAM_BOT_TOKEN, SUBSCRIBERS_KEY, DASHBOARD_URL, OWNER_CHAT (see pi/env.example).
set -euo pipefail
cd "$(dirname "$0")/.."
APP="$(pwd)"; USER_NAME="$(id -un)"; DATA="${NSE_DATA:-$HOME/nse-data}"
if [ -n "${SUDO_PASS:-}" ]; then   # non-interactive run: give sudo the password through a short-lived helper
  ASK="$(mktemp)"; printf '#!/bin/sh
echo "%s"
' "$SUDO_PASS" > "$ASK"; chmod 700 "$ASK"; export SUDO_ASKPASS="$ASK"
  trap 'rm -f "$ASK"' EXIT
  sudo() { command sudo -A "$@"; }
fi
echo "== app: $APP  user: $USER_NAME  data: $DATA"

# 1. Python environment
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p "$DATA"
[ -f .env ] || { echo "missing $APP/.env (copy pi/env.example and fill it in)"; exit 1; }
chmod 600 .env

# 2. Services and timers
sudo tee /etc/systemd/system/nse-day.service >/dev/null <<EOF
[Unit]
Description=NSE scanner, market hours (8:30 brief, pre-open, live stream, news, insiders)
After=network-online.target time-sync.target
Wants=network-online.target
[Service]
User=$USER_NAME
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
Environment=PYTHONIOENCODING=utf-8 STREAM_OUT=$DATA LIVE_SERVER_PORT=8765
ExecStart=$APP/.venv/bin/python -m scanner.run --start 08:25 --until 18:50
Restart=on-failure
RestartSec=20
TimeoutStopSec=60
MemoryMax=600M
[Install]
WantedBy=multi-user.target
EOF
sudo tee /etc/systemd/system/nse-day.timer >/dev/null <<EOF
[Unit]
Description=Start the market-hours scanner on weekdays
[Timer]
OnCalendar=Mon..Fri 08:15 Asia/Kolkata
Persistent=true
[Install]
WantedBy=timers.target
EOF
sudo tee /etc/systemd/system/nse-evening.service >/dev/null <<EOF
[Unit]
Description=NSE evening reports (money flows, insiders, shareholding)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
Environment=PYTHONIOENCODING=utf-8 STREAM_OUT=$DATA
ExecStart=$APP/.venv/bin/python -m scanner.run --evening
TimeoutStartSec=40min
EOF
sudo tee /etc/systemd/system/nse-evening.timer >/dev/null <<EOF
[Unit]
Description=Evening reports on weekdays
[Timer]
OnCalendar=Mon..Fri 19:15 Asia/Kolkata
Persistent=true
[Install]
WantedBy=timers.target
EOF
sudo tee /etc/systemd/system/nse-health.service >/dev/null <<EOF
[Unit]
Description=Pi health check
[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/.venv/bin/python pi/health.py
EOF
sudo tee /etc/systemd/system/nse-health.timer >/dev/null <<EOF
[Unit]
Description=Pi health check every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF
sudo tee /etc/systemd/system/nse-status.service >/dev/null <<EOF
[Unit]
Description=Daily Pi status message
[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/.venv/bin/python pi/health.py --daily
EOF
sudo tee /etc/systemd/system/nse-status.timer >/dev/null <<EOF
[Unit]
Description=Daily Pi status message at 8:10 am
[Timer]
OnCalendar=*-*-* 08:10 Asia/Kolkata
Persistent=true
[Install]
WantedBy=timers.target
EOF
sudo tee /etc/systemd/system/nse-update.service >/dev/null <<EOF
[Unit]
Description=Pull scanner code updates and clear old data files
After=network-online.target
[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$APP
ExecStart=/bin/bash -c 'git pull --ff-only -q && .venv/bin/pip install -q -r requirements.txt; find "$DATA" -type f -mtime +120 -delete'
EOF
sudo tee /etc/systemd/system/nse-update.timer >/dev/null <<EOF
[Unit]
Description=Nightly code update at 3 am
[Timer]
OnCalendar=*-*-* 03:00 Asia/Kolkata
Persistent=true
[Install]
WantedBy=timers.target
EOF
sudo tee /etc/systemd/system/nse-reboot.timer >/dev/null <<EOF
[Unit]
Description=Weekly reboot, Sunday 4 am
[Timer]
OnCalendar=Sun 04:00 Asia/Kolkata
[Install]
WantedBy=timers.target
EOF
sudo tee /etc/systemd/system/nse-reboot.service >/dev/null <<EOF
[Unit]
Description=Weekly reboot
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl reboot
EOF
# the health check may stop/start the market-hours service on overheating, without a password
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl stop nse-day.service, /usr/bin/systemctl start nse-day.service, /usr/bin/systemctl stop nse-day, /usr/bin/systemctl start nse-day" \
  | sudo tee /etc/sudoers.d/nse-health >/dev/null
sudo chmod 440 /etc/sudoers.d/nse-health

# 3. Protection: hardware watchdog, logs in memory, automatic security updates, timezone
sudo mkdir -p /etc/systemd/system.conf.d /etc/systemd/journald.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=40M\n' | sudo tee /etc/systemd/journald.conf.d/ram.conf >/dev/null
sudo timedatectl set-timezone Asia/Kolkata
sudo apt-get install -y -q unattended-upgrades >/dev/null
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null
sudo systemctl daemon-reexec
sudo systemctl restart systemd-journald

# 4. Turn everything on
sudo systemctl daemon-reload
sudo systemctl enable --now nse-health.timer nse-status.timer nse-update.timer nse-reboot.timer >/dev/null
if [ "${ENABLE_SCANNER:-0}" = "1" ]; then
  sudo systemctl enable --now nse-day.timer nse-evening.timer >/dev/null
  echo "== scanner timers ON"
else
  echo "== scanner timers installed but OFF (run again with ENABLE_SCANNER=1 to switch the Pi on as the main scanner)"
fi
systemctl list-timers 'nse-*' --no-pager
echo "== done"
