# CLAUDE.md — wedding-speech-translator

Offline live EN↔Cantonese caption system for wedding speeches, projected via a
browser page. Target machine is a 2025 MacBook Pro (Apple Silicon); this Windows
workspace is dev-only.

## Architecture (read README.md for the full picture)

- `server.py` — everything runs in one process: aiohttp serves the display +
  WebSocket; plain threads run audio capture → VAD segmentation → ASR worker.
  Thread → asyncio bridging via `loop.call_soon_threadsafe`.
- `asr.py` — pluggable Whisper backend: `mlx-whisper` on Apple Silicon,
  `faster-whisper` elsewhere, selected at import time (`backend: "auto"`).
- `translate.py` — NLLB-600M via CTranslate2. **Whisper's translate task is
  deliberately unused** — large-v3-turbo was trained without it. Don't "simplify"
  by switching to `task=translate`.
- `display/index.html` — the "Beacon" projector page: near-black canvas, the
  **spoken** language shown as a large cream primary block with the other two
  beneath it as accent-coloured translations (EN grey, Cantonese green, VI gold),
  a top listening bar (animated waveform + live partial). No build step, vanilla
  JS over the WebSocket. Fonts (Archivo + IBM Plex Mono) are **self-hosted under
  `display/fonts/` and served via the server's `/fonts/` static route** so it
  works offline; Cantonese uses the system CJK font. Re-fetch fonts with
  `download_fonts.py`. Older "system fonts only" rule is superseded.

## Key decisions

- Trilingual output: every final shows original + the translations listed in
  `config.json` → `languages.targets` (EN → zh-Hant + VI; YUE → EN + VI).
  Routing is fully config-driven; adding an input language is a config edit.
- English → `zho_Hant` (Standard Written Chinese, Traditional), not written
  colloquial Cantonese — it's what HK subtitles use and MT for 口語 is weak.
- Cantonese → Vietnamese pivots through the English translation (NLLB's direct
  yue→vie pair is weak). Pivot requires `"en"` to precede `"vi"` in the targets
  list; if it doesn't, translation falls back to the direct pair.
- All displayed Chinese is OpenCC-normalised to HK Traditional (`s2hk`),
  because Whisper sometimes emits Simplified for Cantonese audio.
- Finals pass through a hallucination blocklist (`config.json` → `filter`) —
  Whisper invents "字幕由 Amara.org 社群提供" on quiet Cantonese audio.
- Detected `zh` is treated as `yue` (HK speakers are often detected as `zh`).

## Dev workflow

- No tests (workspace convention). Verify with `python server.py --demo` —
  scripted captions, needs only aiohttp, no models or mic.
- Real pipeline needs `pip install -r requirements-windows.txt` +
  `python setup_models.py` (~4 GB download).
- Models live in `models/` (gitignored). Config is `config.json`.
