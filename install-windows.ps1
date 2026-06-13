# One-paste web installer for the Wedding Speech Translator (Windows).
# Users run this from the README in PowerShell with:
#   irm https://raw.githubusercontent.com/t-reyn/wedding-speech-translator/main/install-windows.ps1 | iex
# It installs Git/Python if needed, clones the repo, sets up the Python
# environment (with NVIDIA GPU support if present), and downloads the models.
$ErrorActionPreference = "Stop"
$repo = "https://github.com/t-reyn/wedding-speech-translator.git"
$dest = "$HOME\Documents\wedding-speech-translator"

Write-Host "============================================================"
Write-Host "  Wedding Speech Translator  -  setup (Windows)"
Write-Host "============================================================"

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

$justInstalled = $false
if (-not (Have git)) {
  Write-Host "Installing Git..."
  winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
  $justInstalled = $true
}
if (-not (Have python)) {
  Write-Host "Installing Python 3.12..."
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  $justInstalled = $true
}
if ($justInstalled) {
  Write-Host ""
  Write-Host ">>  Git/Python were just installed, but this window can't see them yet."
  Write-Host ">>  Please CLOSE PowerShell, open a NEW PowerShell window, and paste the line again."
  return
}

if (Test-Path "$dest\.git") {
  Write-Host "Updating your existing copy..."
  git -C $dest pull --ff-only
} else {
  Write-Host "Downloading the app to $dest ..."
  git clone --depth 1 $repo $dest
}
Set-Location $dest

Write-Host ""
Write-Host "Setting up Python (a few minutes)..."
if (Test-Path venv) { Remove-Item -Recurse -Force venv }
python -m venv venv
$py = ".\venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements-windows.txt

if (Have nvidia-smi) {
  Write-Host "NVIDIA GPU detected - installing GPU acceleration (3-4x faster)..."
  & $py -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
}

Write-Host ""
Write-Host "Downloading models (~4 GB, one-time). Resumes automatically if it stalls."
& $py setup_models.py

explorer $dest
Write-Host ""
Write-Host "============================================================"
Write-Host "  All done!  In the window that just opened, double-click"
Write-Host "  Start Captions.bat  to run it."
Write-Host "============================================================"
