"""Live bilingual (English <-> Cantonese) speech caption server.

Pipeline: mic -> Silero VAD segmentation -> Whisper ASR -> NLLB translation -> WebSocket.
Display:  display/index.html, served at http://localhost:8765/

    python server.py                 # full pipeline (models required, see README)
    python server.py --demo         # scripted captions, no models or mic needed
    python server.py --list-devices # show audio input devices
"""

import os

# Windows-only: torch/CTranslate2/numpy each bundle their own Intel OpenMP/MKL
# runtime; loading several in one process causes random native crashes on
# Windows. Pinning to one runtime + a single thread fixes that. On macOS this
# MUST be skipped — a single thread throttles CTranslate2 to one core and is the
# main cause of caption latency.
if os.name == "nt":
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("FOR_DISABLE_CONSOLE_CTRL_HANDLER", "1")

import argparse
import asyncio
import faulthandler
import json
import sys
import threading
from pathlib import Path

# Dump the C-level stack of every thread if a native lib (PortAudio, CTranslate2,
# cuDNN) hard-crashes the process — those leave no Python traceback otherwise.
faulthandler.enable()

from aiohttp import WSMsgType, web

# Windows consoles default to cp1252, which can't encode CJK/Vietnamese caption logs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# Whisper model sizes, fastest -> most accurate. `--model` overrides config.json
# so you can trade a few seconds of lag for accuracy at startup. Each entry is
# (mlx-whisper repo for Apple Silicon, faster-whisper name for everything else).
# All multilingual (EN + Cantonese both need detecting). turbo == config default.
WHISPER_MODELS = {
    "tiny":   ("mlx-community/whisper-tiny-mlx", "tiny"),
    "base":   ("mlx-community/whisper-base-mlx", "base"),
    "small":  ("mlx-community/whisper-small-mlx", "small"),
    "medium": ("mlx-community/whisper-medium-mlx", "medium"),
    "turbo":  ("mlx-community/whisper-large-v3-turbo", "large-v3-turbo"),
}

clients: set[web.WebSocketResponse] = set()
# Bound to the server's event loop in main(); constructing it at import time would
# bind its internal futures to the wrong loop (broadcaster then dies with
# "got Future attached to a different loop" and no captions reach the browser).
out_queue: asyncio.Queue = None  # type: ignore[assignment]


async def index(request):
    return web.FileResponse(ROOT / "display" / "index.html")


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    clients.add(ws)
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        clients.discard(ws)
    return ws


async def broadcaster():
    while True:
        msg = await out_queue.get()
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(clients):
            try:
                await ws.send_str(data)
            except ConnectionResetError:
                clients.discard(ws)


def emit_threadsafe(loop, msg):
    loop.call_soon_threadsafe(out_queue.put_nowait, msg)


DEMO_SCRIPT = [
    ("en", "Good evening everyone, and a very warm welcome.", [
        ("yue", "各位晚上好，熱烈歡迎大家。"),
        ("vi", "Kính chào quý vị, nhiệt liệt chào mừng mọi người."),
    ]),
    ("yue", "多謝大家今晚抽空嚟同我哋一齊慶祝。", [
        ("en", "Thank you all for making time to celebrate with us tonight."),
        ("vi", "Cảm ơn mọi người đã dành thời gian đến chung vui với chúng tôi tối nay."),
    ]),
    ("en", "Tonight we celebrate two families becoming one.", [
        ("yue", "今晚我們慶祝兩個家庭結為一家。"),
        ("vi", "Tối nay chúng ta ăn mừng hai gia đình trở thành một."),
    ]),
    ("yue", "我第一次見到佢嗰陣，就知道佢係一個好特別嘅人。", [
        ("en", "The first time I met her, I knew she was someone special."),
        ("vi", "Lần đầu tiên gặp cô ấy, tôi đã biết cô ấy là một người đặc biệt."),
    ]),
    ("en", "Please raise your glasses and join me in a toast to the happy couple.", [
        ("yue", "請大家舉杯，同我一齊祝福呢對新人。"),
        ("vi", "Xin mọi người cùng nâng ly chúc mừng đôi uyên ương."),
    ]),
]


async def demo_feed():
    await asyncio.sleep(2)
    while True:
        for lang, original, translations in DEMO_SCRIPT:
            words = original.split(" ") if lang == "en" else [
                original[i:i + 3] for i in range(0, len(original), 3)]
            shown = ""
            for w in words:
                shown = (shown + " " + w).strip() if lang == "en" else shown + w
                await out_queue.put({"type": "partial", "lang": lang, "original": shown})
                await asyncio.sleep(0.28)
            await asyncio.sleep(0.9)
            await out_queue.put({
                "type": "final", "lang": lang, "original": original,
                "translations": [{"lang": t, "text": x} for t, x in translations],
            })
            await asyncio.sleep(4.5)


def start_pipeline(loop, source_file=None):
    try:
        import numpy as np
        import torch
        from silero_vad import VADIterator, load_silero_vad

        from asr import create_asr
        from translate import CaptionTranslator
    except ImportError as e:
        sys.exit(
            f"Missing dependency: {e.name}.\n"
            "Run: pip install -r requirements-mac.txt (or requirements-windows.txt)\n"
            "then: python setup_models.py\n"
            "Or try the display without models: python server.py --demo"
        )

    audio_cfg = CONFIG["audio"]
    vad_cfg = CONFIG["vad"]
    sr = audio_cfg["sample_rate"]
    chunk_size = 512

    print("Loading ASR model...")
    asr = create_asr(CONFIG["asr"])
    print("Loading translation model...")
    translator = CaptionTranslator(ROOT / CONFIG["mt"]["model_dir"], CONFIG["mt"])
    vad_model = load_silero_vad()
    print("Models loaded.")

    blocklist = [s.lower() for s in CONFIG["filter"]["blocklist"]]

    def is_junk(text):
        t = text.strip().lower()
        if len(t.strip(".,!?。，！？ ")) == 0:
            return True
        # Reject hallucinated echoes of the prompt's framing (e.g. "婚禮致辭",
        # "wedding speeches and toasts") via the blocklist — NOT a substring check
        # against the whole prompt, which would now swallow real utterances that
        # are just a guest's name listed in the prompt.
        return any(b in t for b in blocklist)

    chunk_queue = []
    chunk_lock = threading.Condition()

    def feed_chunk(chunk):
        with chunk_lock:
            chunk_queue.append(chunk)
            chunk_lock.notify()

    final_jobs = []
    partial_slot = {}
    job_lock = threading.Condition()
    utt_counter = {"n": 0}

    def submit(kind, audio, utt_id):
        with job_lock:
            if kind == "final":
                final_jobs.append((audio, utt_id))
                partial_slot.clear()
            else:
                partial_slot.update({"audio": audio, "utt_id": utt_id})
            job_lock.notify()

    utt_langs = {}

    def asr_worker():
        import time as _time
        while True:
            with job_lock:
                while not final_jobs and not partial_slot:
                    job_lock.wait()
                if final_jobs:
                    kind, (audio, utt_id) = "final", final_jobs.pop(0)
                else:
                    kind, (audio, utt_id) = "partial", (
                        partial_slot["audio"], partial_slot["utt_id"])
                    partial_slot.clear()

            lang_hint = utt_langs.get(utt_id)
            t0 = _time.time()
            # Finals re-detect language from the full utterance; a hint cached
            # from a partial's first seconds can poison the whole caption.
            text, lang = asr.transcribe(
                audio, language=lang_hint if kind == "partial" else None,
                initial_prompt=CONFIG["asr"]["initial_prompt"],
                final=(kind == "final"),
            )
            if not text or is_junk(text):
                continue
            lang_cfg = CONFIG["languages"]
            if lang in ("zh", "yue"):
                lang = "yue"
            # Whisper's language ID is unreliable on short utterances (the
            # bilingual initial_prompt skews it). A Cantonese transcript routed
            # as "en" reaches NLLB tagged eng_Latn, and NLLB answers mislabeled
            # input by copying it through untranslated. The transcript's own
            # script is the ground truth, so let it override a contradiction.
            cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
            latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
            if lang == "en" and cjk > latin:
                lang = "yue"
            elif lang == "yue" and latin > cjk:
                lang = "en"
            if lang not in lang_cfg["detect"]:
                lang = lang_hint or "en"
            utt_langs[utt_id] = lang

            if lang == "yue":
                original = translator.to_traditional(text)
            else:
                original = text

            if kind == "partial":
                emit_threadsafe(loop, {"type": "partial", "lang": lang, "original": original})
                continue

            translations = []
            targets = lang_cfg["targets"][lang]
            if lang == "en":
                # Targets are independent of each other, so batch them in one call.
                tgt_codes = [lang_cfg["nllb_tgt"][t] for t in targets]
                outs = translator.translate_multi(original, "eng_Latn", tgt_codes)
                translations = [{"lang": t, "text": o} for t, o in zip(targets, outs)]
            else:
                # Sequential: Vietnamese pivots through the English translation,
                # so English must be produced first.
                en_text = None
                for tgt in targets:
                    src_text, src_code = original, lang_cfg["nllb_src"][lang]
                    if lang_cfg["pivot_through_english"] and tgt != "en" and en_text:
                        src_text, src_code = en_text, "eng_Latn"
                    out = translator.translate(src_text, src_code, lang_cfg["nllb_tgt"][tgt])
                    if tgt == "en":
                        en_text = out
                    translations.append({"lang": tgt, "text": out})
            elapsed = _time.time() - t0
            print(f"[{lang}] ({elapsed:.1f}s) {original}  =>  "
                  + "  |  ".join(t["text"] for t in translations))
            emit_threadsafe(loop, {
                "type": "final", "lang": lang,
                "original": original, "translations": translations,
            })
            utt_langs.pop(utt_id, None)

    def segmenter():
        import collections
        import time as _time

        vad = VADIterator(
            vad_model, threshold=vad_cfg["threshold"], sampling_rate=sr,
            min_silence_duration_ms=vad_cfg["min_silence_ms"], speech_pad_ms=0)
        preroll = collections.deque(
            maxlen=max(1, int(vad_cfg["preroll_ms"] / 1000 * sr / chunk_size)))
        buf, speaking, start_t, last_partial = [], False, 0.0, 0.0

        while True:
            with chunk_lock:
                while not chunk_queue:
                    chunk_lock.wait()
                chunk = chunk_queue.pop(0)
            event = vad(torch.from_numpy(chunk))

            if speaking:
                buf.append(chunk)
            if event and "start" in event:
                speaking = True
                utt_counter["n"] += 1
                buf = list(preroll) + [chunk]
                start_t = last_partial = _time.time()
            elif event and "end" in event:
                speaking = False
                submit("final", np.concatenate(buf), utt_counter["n"])
                buf = []
            elif speaking:
                now = _time.time()
                if now - start_t > vad_cfg["max_utterance_s"]:
                    submit("final", np.concatenate(buf), utt_counter["n"])
                    utt_counter["n"] += 1
                    buf, start_t, last_partial = [], now, now
                elif (vad_cfg["partial_interval_s"] > 0
                      and now - last_partial > vad_cfg["partial_interval_s"]
                      and len(buf) * chunk_size > sr):
                    submit("partial", np.concatenate(buf), utt_counter["n"])
                    last_partial = now
            if not speaking:
                preroll.append(chunk)

    threading.Thread(target=asr_worker, daemon=True).start()
    threading.Thread(target=segmenter, daemon=True).start()

    if source_file:
        def file_feeder():
            import time as _time
            import wave

            with wave.open(str(source_file), "rb") as wf:
                if wf.getframerate() != sr or wf.getnchannels() != 1:
                    sys.exit(f"{source_file} must be mono {sr} Hz WAV "
                             f"(got {wf.getnchannels()}ch {wf.getframerate()} Hz)")
                raw = wf.readframes(wf.getnframes())
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            print(f"Streaming {source_file} ({len(audio) / sr:.1f}s) through the pipeline...")
            for i in range(0, len(audio), chunk_size):
                chunk = audio[i:i + chunk_size]
                if len(chunk) < chunk_size:
                    chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
                feed_chunk(chunk)
                _time.sleep(chunk_size / sr)
            for _ in range(int(sr / chunk_size)):
                feed_chunk(np.zeros(chunk_size, dtype=np.float32))
                _time.sleep(chunk_size / sr)
            print("File finished. Captions remain on screen; Ctrl+C to exit.")

        threading.Thread(target=file_feeder, daemon=True).start()
        return None

    import sounddevice as sd
    stream = sd.InputStream(
        samplerate=sr, channels=1, dtype="float32",
        blocksize=chunk_size, device=audio_cfg["device"])
    stream.start()

    # Blocking reads on our own thread, NOT a PortAudio callback: cffi callbacks
    # fired from PortAudio's native audio thread intermittently crash the whole
    # process on Windows (access violation in _cffi_backend, see Event Viewer).
    # Python -> C blocking reads avoid that path entirely.
    def mic_reader():
        while True:
            data, overflowed = stream.read(chunk_size)
            if overflowed:
                print("[audio] input overflow", file=sys.stderr)
            feed_chunk(data[:, 0].copy())

    threading.Thread(target=mic_reader, daemon=True).start()
    device_name = sd.query_devices(stream.device)["name"]
    print(f"Listening on input device: {device_name}")
    return stream


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="scripted captions, no models")
    parser.add_argument("--file", metavar="WAV",
                        help="stream a mono 16 kHz WAV through the real pipeline instead of the mic")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--model", choices=list(WHISPER_MODELS),
                        help="Whisper model size (smaller = less lag, less accurate). "
                             "Default: config.json. tiny/base/small/medium/turbo.")
    args = parser.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    if args.model:
        mlx_model, fw_model = WHISPER_MODELS[args.model]
        CONFIG["asr"]["mlx_model"] = mlx_model
        CONFIG["asr"]["fw_model"] = fw_model
        print(f"Whisper model: {args.model} ({mlx_model})")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    global out_queue
    out_queue = asyncio.Queue()

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/fonts/", ROOT / "display" / "fonts")

    host, port = CONFIG["server"]["host"], CONFIG["server"]["port"]
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, host, port).start())
    loop.create_task(broadcaster())

    if args.demo:
        print("DEMO MODE — scripted captions, no microphone or models in use.")
        loop.create_task(demo_feed())
    else:
        start_pipeline(loop, source_file=args.file)

    print(f"Display: http://localhost:{port}/  (open full-screen on the projector)")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
