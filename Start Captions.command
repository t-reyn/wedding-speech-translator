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

# Pick the Whisper model size once, before the auto-restart loop (so a live
# event never re-prompts). Smaller = less lag but less accurate. Empty answer
# or 10s with no input keeps the accurate default from config.json.
echo "Whisper model — smaller reacts faster but is less accurate:"
echo "  1) tiny     2) base     3) small     4) medium     5) turbo  (default, most accurate)"
echo "  6) cantonese  (turbo size — Cantonese-tuned; run 'Convert Cantonese' once first)"
read -r -t 10 -p "Choice [5]: " MODEL_CHOICE
echo
case "$MODEL_CHOICE" in
  1) MODEL_ARG="--model tiny" ;;
  2) MODEL_ARG="--model base" ;;
  3) MODEL_ARG="--model small" ;;
  4) MODEL_ARG="--model medium" ;;
  6) MODEL_ARG="--model cantonese" ;;
  *) MODEL_ARG="" ;;  # 5 / empty / timeout -> config default
esac
[ -n "$MODEL_ARG" ] && echo "Using ${MODEL_ARG#--model }." || echo "Using default model (turbo)."

echo "Models can take 10-30s to load on first run. Watch the browser tab."
echo "All output is logged to: captions_log.txt"
echo "Keep this window open. Press Ctrl+C to stop."
echo

( sleep 3; open "http://localhost:8765/" ) &

while true; do
  echo "---- server starting $(date) ----" >> "$LOG"
  "$PY" -u server.py $MODEL_ARG >> "$LOG" 2>&1
  echo "---- server exited $(date) ----" >> "$LOG"
  echo
  echo "Server stopped — restarting in 3s (close this window or Ctrl+C to quit)..."
  sleep 3
done
