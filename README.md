# Wedding Speech Translator

Fully offline live captioning for wedding speeches, projected on a big screen.
English speech gets Traditional Chinese + Vietnamese subtitles; Cantonese speech
gets English + Vietnamese subtitles. The original words and both translations
are shown, so every guest can follow every speech.

Everything runs locally — no internet needed on the day once models are downloaded.

## How it works

```
mic / mixer feed
  └─ Silero VAD (utterance segmentation)
       └─ Whisper large-v3-turbo (transcription + language detection)
            └─ NLLB-200-1.3B via CTranslate2 (EN -> zho_Hant, yue_Hant -> EN)
                 └─ WebSocket -> full-screen browser page on the projector
```

- While someone speaks, a live partial transcript scrolls in the top "listening"
  bar. At each natural pause the sentence is finalised as the headline and its
  translations appear beneath it.
- English speeches are subtitled in **Standard Written Chinese, Traditional
  characters** (what HK TV subtitles use — readable by every Cantonese speaker)
  plus Vietnamese.
- Cantonese transcripts are normalised to HK Traditional via OpenCC, then
  translated to English and Vietnamese. Vietnamese is pivoted through the
  English translation (`languages.pivot_through_english`) because NLLB's direct
  Cantonese→Vietnamese pair is weak.
- Whisper's built-in translate task is not used (large-v3-turbo lacks it);
  translation always goes through NLLB.
- Languages are config-driven (`languages` in `config.json`). To accept
  Vietnamese *speeches* as input too, add `"vi"` to `languages.detect` and give
  it a `targets` list, e.g. `"vi": ["en", "yue"]`.
- Expected delay: roughly 1.5–2.5 s behind the speaker.

## Installation

> This repo is **private**, so cloning requires being signed in to the
> `t-reyn` GitHub account. The model download (~4 GB) is one-time — do it at
> home on good wifi, not at the venue.

### macOS (MacBook Pro, Apple Silicon — the wedding machine)

**1. Install the developer basics** (Terminal — gives you `git` and `python3`):

```bash
xcode-select --install
```

**2. Sign in to GitHub and clone.** Easiest is the GitHub CLI via
[Homebrew](https://brew.sh) (skip Homebrew if you already have it):

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install gh
gh auth login          # choose GitHub.com → HTTPS → log in with browser
gh repo clone t-reyn/wedding-speech-translator
cd wedding-speech-translator
```

*(Alternative: install [GitHub Desktop](https://desktop.github.com), sign in,
and clone the repo from there — then `cd` into wherever it put the folder.)*

**3. Install the Python dependencies** (in a virtualenv the launchers will
auto-detect):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-mac.txt
```

**4. Download the models** (one-time, ~4 GB; resumes automatically if the
connection stalls):

```bash
python setup_models.py
```

**5. Run it:** double-click **`Start Captions.command`** in Finder
(or `Start Captions (Demo).command` to preview the display without mic/models).

First-run macOS prompts, both expected:
- Gatekeeper may block the double-click — **right-click the file → Open → Open**
  (only needed once).
- Grant **microphone access to Terminal** when asked (or in System Settings →
  Privacy & Security → Microphone).

The Mac uses the Apple GPU automatically via MLX — no extra setup.

### Windows

**1. Install Python 3.12 and the GitHub CLI** (PowerShell):

```powershell
winget install Python.Python.3.12
winget install Git.Git
winget install GitHub.cli
```

**2. Sign in and clone** (new PowerShell window so PATH refreshes):

```powershell
gh auth login          # choose GitHub.com → HTTPS → log in with browser
gh repo clone t-reyn/wedding-speech-translator
cd wedding-speech-translator
```

**3. Install dependencies and download models:**

```powershell
python -m pip install -r requirements-windows.txt
python setup_models.py
```

**4. Run it:** double-click **`Start Captions.bat`**
(or `Start Captions (Demo).bat` for the scripted demo).

Optional — NVIDIA GPU acceleration (3-4× faster captions). If the machine has
an NVIDIA card:

```powershell
python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

The server picks up the GPU automatically next launch (look for
`Whisper: using CUDA (GPU).` in `captions_log.txt`); without it everything
still works on CPU, just slower.

> The launchers log to `captions_log.txt` next to the scripts and auto-restart
> the server if it ever exits, so captions come back by themselves mid-event.

## Running

```bash
python server.py --demo               # try the display with scripted captions, no mic
python server.py --list-devices       # find your USB audio interface's device index
python server.py --file clip.wav      # run a mono 16 kHz WAV through the real pipeline
python server.py                      # the real thing (live mic)
```

`--file` streams a recording through the exact VAD → Whisper → NLLB → display
path the live mic uses — handy for testing on a machine without a mic, or for
rehearsing with recorded speeches. The WAV must be mono, 16 kHz.

Open http://localhost:8765/ in a browser on the projector screen (the launchers
open it for you). The display shows the **spoken** language as a large headline
with live translations beneath it, colour-coded per language; it re-orders
automatically when the speaker switches between English and Cantonese.
**Hotkeys on the display:** `F` fullscreen · `H` hide captions (panic button).
Double-click also enters fullscreen.

Set `audio.device` in `config.json` to the device index from `--list-devices`
(leave `null` for system default input).

## Before the wedding — checklist

1. **Audio feed is the #1 quality factor.** Get a direct line from the DJ/venue
   mixer into a USB audio interface — do not rely on the laptop mic picking up
   the PA. Ask the venue's AV person; it's a routine request.
2. **Put every speaker's name in `config.json`** (`asr.initial_prompt`). This
   biases Whisper so names are spelled correctly on a 4-metre screen.
3. **Rehearse with the real speakers** (or voice notes from them), especially
   the Cantonese ones — Whisper's Cantonese is decent but weaker than English,
   and heavily code-switched speech (English words inside Cantonese sentences)
   is the hardest case. Test early so there's time to adjust.
4. **Mac settings on the day:** plugged into power (macOS throttles ML on
   battery), display sleep off, notifications off (Focus mode), volume of the
   Mac itself muted.
5. **Plan B:** the `H` key blanks the captions instantly if a speech goes
   somewhere captions shouldn't follow.

## Tuning (`config.json`)

| Key | What it does |
|---|---|
| `vad.min_silence_ms` | Pause length that finalises a sentence. Lower = snappier, more fragmented. |
| `vad.partial_interval_s` | How often live partial text updates (0 disables partials). |
| `vad.max_utterance_s` | Force-finalise long monologues so captions never lag too far. |
| `asr.initial_prompt` | Bias vocabulary — names, venue, "wedding speeches". |
| `languages.targets` | Which translations each input language gets on screen. |
| `languages.nllb_tgt.yue` | `zho_Hant` (default) or `yue_Hant` for colloquial written Cantonese (experimental). |
| `filter.blocklist` | Known Whisper hallucinations to suppress (e.g. the Amara.org subtitle credit). |

## If Cantonese accuracy disappoints

Stock `large-v3-turbo` is the speed/quality sweet spot, but community Cantonese
fine-tunes exist on Hugging Face (search "whisper cantonese"). Swap via
`asr.mlx_model` / `asr.fw_model` in `config.json` — any Whisper-format model works.
