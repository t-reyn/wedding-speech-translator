#!/bin/bash
# Wedding Speech Translator — demo display (macOS). No mic or models needed;
# shows the projector page with scripted captions so you can check the look.
cd "$(dirname "$0")"

if [ -d venv ]; then
  source venv/bin/activate
  PY=python
else
  PY=python3
fi

echo "Starting demo (scripted captions, no mic/models)..."
echo "Press Ctrl+C to stop."
echo

( sleep 2; open "http://localhost:8765/display" ) &
"$PY" -u server.py --demo
