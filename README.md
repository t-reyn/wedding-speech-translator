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

## Setup (MacBook Pro, Apple Silicon)

```bash
git clone https://github.com/t-reyn/wedding-speech-translator.git
cd wedding-speech-translator
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-mac.txt
python setup_models.py        # one-time, ~4 GB of downloads — do this at home!
```

Then just **double-click `Start Captions.command`** in Finder to run it (or
`Start Captions (Demo).command` to preview the display with no mic/models). The
first time, macOS may ask you to confirm running it, and you may need to grant
Terminal microphone access in System Settings → Privacy & Security → Microphone.

On Windows (dev machine), use `requirements-windows.txt` instead — it swaps
mlx-whisper for faster-whisper — and double-click `Start Captions.bat`. An NVIDIA
GPU is used automatically if `nvidia-cublas-cu12` + `nvidia-cudnn-cu12` are
installed; otherwise it runs on CPU. (The Mac uses the Apple GPU via MLX with no
extra setup.)

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
