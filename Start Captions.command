#!/bin/bash
# Wedding Speech Translator — live mic launcher (macOS).
# Double-click in Finder to run. Logs to captions_log.txt and auto-restarts
# the server if it ever exits, so a live speech is never left without captions.
cd "$(dirname "$0")"
LOG="captions_log.txt"

trap 'echo; echo "Stopped."; exit 0' INT TERM

if [ -d venv ]; then
  source venv/bin/activate
  PY=python
else
  PY=python3
fi

echo "Starting Wedding Speech Translator (live mic)..."
echo

# A control page opens in the browser; the model + microphone are picked there
# and the operator clicks Start. No terminal prompt, so a live event never has
# to answer one — the page persists across the auto-restart loop below.
echo "A control page will open in your browser. Pick your Whisper model and"
echo "microphone there, then click Start. The model loads in 10-30s on first run."
echo "All output is logged to: captions_log.txt"
echo "Keep this window open. Press Ctrl+C to stop."
echo

( sleep 3; open "http://localhost:8765/" ) &

while true; do
  echo "---- server starting $(date) ----" >> "$LOG"
  "$PY" -u server.py >> "$LOG" 2>&1
  echo "---- server exited $(date) ----" >> "$LOG"
  echo
  echo "Server stopped — restarting in 3s (close this window or Ctrl+C to quit)..."
  sleep 3
done
