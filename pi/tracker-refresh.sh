#!/usr/bin/env bash
# The lock-in tracker's 7:30 pm refresh (python -m tracker.refresh), run on the Pi by nse-tracker.timer.
# It replaces the GitHub workflow evening.yml, whose schedule never fired on time. Needs ~/lockin-tracker cloned
# with a deploy key that can push (the data/ history is committed back), its .venv, and its .env.
set -euo pipefail
cd "${TRACKER_DIR:-$HOME/lockin-tracker}"
git pull --rebase -q
.venv/bin/pip install -q -r requirements.txt
PYTHONIOENCODING=utf-8 .venv/bin/python -m tracker.refresh
git add data
git diff --cached --quiet || git -c user.name="lockin-tracker-bot" -c user.email="actions@users.noreply.github.com" \
  commit -q -m "Evening data update $(date +%F)"
git push -q
