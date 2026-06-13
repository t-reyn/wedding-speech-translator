#!/bin/bash
# Convert the Cantonese Whisper model for this Mac. Double-click in Finder.
# One-time, downloads ~1.6 GB — do it on wifi at home, not at the venue.
cd "$(dirname "$0")"

echo "============================================================"
echo "  Converting the Cantonese Whisper model for this Mac."
echo "  One-time, downloads ~1.6 GB. Do this on wifi, not at the venue."
echo "============================================================"
echo

if [ -d venv ]; then
  source venv/bin/activate
  PY=python
else
  PY=python3
fi

"$PY" convert_cantonese.py
echo
echo "Done. To use it: double-click 'Start Captions' and choose the cantonese model."
echo "Press any key to close."
read -r -n 1
