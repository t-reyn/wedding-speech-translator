#!/bin/bash
# Wedding Speech Translator — one-click installer (macOS).
# Double-click in Finder. Sets up a Python virtualenv, installs the packages,
# and downloads the models (~4 GB). Only needs to be done once.
cd "$(dirname "$0")"

echo "============================================================"
echo "  Wedding Speech Translator  -  one-click installer (macOS)"
echo "============================================================"
echo "This installs the Python packages and downloads the models (~4 GB)."
echo "It only needs to be done once. Leave it running."
echo

# ---- 1. ensure python3 ----
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Opening the Apple Command Line Tools installer..."
  echo "Click Install in the dialog, wait for it to finish, then run this again."
  xcode-select --install
  echo
  read -r -p "Press Return to close."
  exit 0
fi
echo "Using Python: $(command -v python3)  ($(python3 --version))"
echo

# ---- 2. clean virtual environment ----
if [ -d venv ]; then echo "Removing previous environment..."; rm -rf venv; fi
echo "Creating virtual environment..."
if ! python3 -m venv venv; then
  echo "Could not create the environment."; read -r -p "Press Return."; exit 1
fi
source venv/bin/activate

# ---- 3. install packages ----
echo
echo "Installing packages (a few minutes, lots of output is normal)..."
python -m pip install --upgrade pip
if ! python -m pip install -r requirements-mac.txt; then
  echo "Package install FAILED. Check your internet and run Install again."
  read -r -p "Press Return."; exit 1
fi

# ---- 4. download + build the models ----
echo
echo "Downloading models (~4 GB, one-time). Resumes automatically if it stalls."
if ! python setup_models.py; then
  echo "Model download did not finish - run Install again to resume."
  read -r -p "Press Return."; exit 1
fi

echo
echo "============================================================"
echo "  All done!  Double-click  \"Start Captions.command\"  to run."
echo "  (or \"Start Captions (Demo).command\" to preview the display)"
echo "============================================================"
read -r -p "Press Return to close."
