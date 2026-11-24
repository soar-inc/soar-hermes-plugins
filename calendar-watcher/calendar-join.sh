#!/bin/bash
WATCH_DIR="/home/hermes/.hermes/profiles/long-horizon/calendar-watcher"
export PULSE_SERVER=unix:/run/hermes-meet-audio/native
exec /usr/bin/python3 "$WATCH_DIR/calendar_watcher.py" \
  --credentials "$WATCH_DIR/calendar-credentials.json" --join-due --flowmind-chat
