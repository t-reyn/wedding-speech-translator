#!/bin/bash
# One-paste web installer for the Wedding Speech Translator (macOS).
# Users run this from the README with:
#   curl -fsSL https://raw.githubusercontent.com/t-reyn/wedding-speech-translator/main/install-mac.sh | bash
# It clones the repo, builds a Python environment, and downloads the models.
set -e

REPO="https://github.com/t-reyn/wedding-speech-translator.git"
DEST="$HOME/Documents/wedding-speech-translator"

echo "============================================================"
echo "  Wedding Speech Translator  -  setup (macOS)"
echo "============================================================"

# git (and python3) come with Apple's Command Line Tools.
if ! command -v git >/dev/null 2>&1; then
  echo "First we need Apple's developer tools (one-time)."
  echo "A popup will appear: click Install, wait for it to finish,"
  echo "then paste the same line again."
  xcode-select --install >/dev/null 2>&1 || true
  exit 0
fi

if [ -d "$DEST/.git" ]; then
  echo "Updating your existing copy in $DEST ..."
  git -C "$DEST" pull --ff-only || true
else
  echo "Downloading the app to $DEST ..."
  mkdir -p "$(dirname "$DEST")"
  git clone --depth 1 "$REPO" "$DEST"
fi

cd "$DEST"
chmod +x ./*.command 2>/dev/null || true

echo
echo "Setting up Python (a few minutes)..."
rm -rf venv
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-mac.txt

echo
echo "Downloading models (~4 GB, one-time). Resumes automatically if it stalls."
python setup_models.py

# Friendly shortcut (points straight at the launcher) + open the folder.
ln -sfn "$DEST/Start Captions.command" "$HOME/Desktop/Wedding Captions.command" 2>/dev/null || true
open "$DEST" 2>/dev/null || true

echo
echo "============================================================"
echo "  All done!"
echo "  Double-click 'Wedding Captions' on your Desktop (a control"
echo "  page opens in your browser — pick your model + mic and click"
echo "  Start)."
echo "============================================================"
