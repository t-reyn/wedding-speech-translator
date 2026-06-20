"""Live bilingual (English <-> Cantonese) speech caption server.

Pipeline: mic -> Silero VAD segmentation -> Whisper ASR -> NLLB translation -> WebSocket.
Control:  display/control.html, served at http://localhost:8765/   (operator panel)
Display:  display/index.html,   served at http://localhost:8765/display (projector)

    python server.py                 # boot the server + control panel; START from the browser
    python server.py --model turbo  # convenience: auto-start that model on the default mic
    python server.py --demo         # scripted captions, no models or mic needed
    python server.py --file foo.wav # stream a WAV through the real pipeline, headless
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
import gc
import json
import re
import sys
import threading
import traceback
import zlib
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
    # Cantonese-specialised turbo fine-tune (bilingual EN+yue, code-switch aware).
    # These point at LOCAL folders produced by convert_cantonese.py — run that
    # once per machine first, or this option errors with "model not found".
    "cantonese": ("models/whisper-cantonese-mlx", "models/whisper-cantonese-ct2"),
}

# Friendly labels for the control panel dropdown.
MODEL_LABELS = {
    "tiny":   "Tiny — fastest, least accurate",
    "base":   "Base — very fast, low accuracy",
    "small":  "Small — faster, less accurate",
    "medium": "Medium — balanced",
    "turbo":  "Turbo — most accurate (recommended)",
    "cantonese": "Cantonese-tuned (turbo size)",
}

clients: set[web.WebSocketResponse] = set()
# Bound to the server's event loop in main(); constructing it at import time would
# bind its internal futures to the wrong loop (broadcaster then dies with
# "got Future attached to a different loop" and no captions reach the browser).
out_queue: asyncio.Queue = None  # type: ignore[assignment]
# The single PipelineController, created in main() once out_queue/loop exist.
controller: "PipelineController" = None  # type: ignore[assignment]


def _resolve(m):
    """Locally-converted models (e.g. "cantonese") are stored as repo-relative
    folders; make them absolute so the model loads regardless of CWD. Hub repo
    IDs ("mlx-community/...") have no local folder, so pass through unchanged."""
    local = ROOT / m
    return str(local) if local.exists() else m


def cantonese_available():
    """The Cantonese fine-tune ships as local converted folders only; it's only
    selectable once convert_cantonese.py has produced one of them."""
    mlx_model, fw_model = WHISPER_MODELS["cantonese"]
    return (ROOT / mlx_model).exists() or (ROOT / fw_model).exists()


def model_available(key):
    return cantonese_available() if key == "cantonese" else True


async def control_page(request):
    return web.FileResponse(ROOT / "display" / "control.html")


async def display_page(request):
    return web.FileResponse(ROOT / "display" / "index.html")


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    clients.add(ws)
    # New client gets the current pipeline state immediately so its UI is correct
    # without waiting for the next transition.
    if controller is not None:
        try:
            await ws.send_str(json.dumps(controller.status_msg(), ensure_ascii=False))
        except Exception:
            pass
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
        # Concurrent fan-out with a per-client send timeout: a serial loop lets one
        # slow/half-closed client freeze captions for ALL clients. gather collects
        # every result (return_exceptions=True), so a timeout/reset on one socket
        # only drops THAT socket. wait_for wrapping each send bounds the slow case.
        snapshot = list(clients)
        if not snapshot:
            continue
        results = await asyncio.gather(
            *[asyncio.wait_for(ws.send_str(data), 2.0) for ws in snapshot],
            return_exceptions=True,
        )
        for ws, res in zip(snapshot, results):
            # BaseException catches TimeoutError/CancelledError too, so a stuck
            # client is evicted rather than retried forever.
            if isinstance(res, BaseException):
                clients.discard(ws)


def emit_threadsafe(loop, msg):
    # Hop to the loop thread and enqueue there. The queue is bounded (back-pressure
    # against a stalled broadcaster), so when full we drop the OLDEST message —
    # partials are disposable and the freshest caption matters most. Done on the
    # loop thread so the full-check + drop + put is atomic w.r.t. the consumer.
    def _put():
        if out_queue.full():
            try:
                out_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        out_queue.put_nowait(msg)
    loop.call_soon_threadsafe(_put)


def collapse_repeats(text):
    """Defuse Whisper repetition loops ("okay okay okay…", "好好好好…") without
    harming real speech. Greedy decoding (beam_size 1 on partials) and the
    no-fallback temperature setting both make these loops more likely, and a
    looped caption reads as high-confidence so the no_speech/logprob filter
    misses it. Natural emphasis (a word or short phrase doubled) is preserved."""
    ws = text.split()
    # Immediately-repeated phrases (longest first) collapse to one copy.
    for n in (4, 3, 2):
        i, out = 0, []
        while i < len(ws):
            if i + 2 * n <= len(ws) and ws[i:i + n] == ws[i + n:i + 2 * n]:
                out.extend(ws[i:i + n])
                j = i + n
                while j + n <= len(ws) and ws[j:j + n] == ws[i:i + n]:
                    j += n
                i = j
            else:
                out.append(ws[i])
                i += 1
        ws = out
    # A single word repeated in a row: keep at most two ("very very" is fine).
    capped = []
    for w in ws:
        if len(capped) >= 2 and w == capped[-1] == capped[-2]:
            continue
        capped.append(w)
    text = " ".join(capped)
    # Spaceless loops (mostly CJK): same char x4+, or a 2-8 char chunk x3+.
    text = re.sub(r"([一-鿿])\1{3,}", r"\1", text)
    text = re.sub(r"(.{2,8}?)\1{2,}", r"\1", text)
    return text.strip()


def is_hallucinated(text, max_ratio=3.0, min_gate_len=24):
    """Confident, repetitive hallucinations (looped phrases, name-salad on
    music/applause) compress far more than real speech, yet decode as
    high-confidence so the no_speech/logprob segment filter misses them.
    Bytes/compressed-bytes is script-uniform, so CJK loops score as high as
    English ones; genuine captions sit well under max_ratio. Module-level so it
    is unit-testable without loading models (see test_gates.py)."""
    b = text.encode("utf-8")
    if len(b) < min_gate_len:
        return False
    return len(b) / max(1, len(zlib.compress(b))) > max_ratio


def _friendly_load_error(exc, model_key):
    """Turn a model-load failure into operator-actionable text for the panel.
    Out-of-memory (a small GPU + a big model, or other apps hogging RAM) is the
    common one on a constrained machine — say what to actually do about it."""
    msg = str(exc)
    if any(s in msg.lower()
           for s in ("out of memory", "mkl_malloc", "alloc", "cuda", "cublas", "cudnn")):
        return (f"Out of memory loading the '{model_key}' model. Close other apps, "
                f"or pick a smaller model (e.g. 'small'), then press Start again. [{msg}]")
    return msg


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
            # A provisional partial carrying live (still-forming) translations,
            # then the crisp final a beat later — mirrors the real pipeline.
            await out_queue.put({
                "type": "partial", "lang": lang, "original": original,
                "translations": [{"lang": t, "text": x} for t, x in translations],
            })
            await asyncio.sleep(0.9)
            await out_queue.put({
                "type": "final", "lang": lang, "original": original,
                "translations": [{"lang": t, "text": x} for t, x in translations],
            })
            await asyncio.sleep(4.5)


def build_pipeline_threads(loop, controller, stop_event, gen, asr, translator, vad_model,
                           device, source_file=None):
    """Build the segmentation/ASR/translation closures around the supplied,
    already-loaded models and start the worker threads. Returns (threads, stream)
    where stream is the live sounddevice InputStream (or None for --file). The
    verified pipeline logic (hallucination gate, repeat collapse, pre-ASR speech
    gate, language routing, translate_all) is unchanged — only the loop guards
    were adapted so the threads observe stop_event for clean shutdown."""
    import numpy as np
    import torch
    from silero_vad import VADIterator

    audio_cfg = CONFIG["audio"]
    vad_cfg = CONFIG["vad"]
    sr = audio_cfg["sample_rate"]
    chunk_size = 512

    blocklist = [s.lower() for s in CONFIG["filter"]["blocklist"]]
    # Stand-alone filler that Whisper hallucinates on near-silence ("thank you",
    # "you", "多謝"). Matched against the WHOLE transcript only (never a substring),
    # so real multi-word speech containing these words is never dropped.
    standalone = [s.strip().lower() for s in CONFIG["filter"].get("standalone_blocklist", [])]

    def is_junk(text):
        t = text.strip().lower()
        if len(t.strip(".,!?。，！？ ")) == 0:
            return True
        # Whole-transcript exact match against the stand-alone filler list.
        if t.strip(".,!?。，！？ ") in standalone or t in standalone:
            return True
        # Reject hallucinated echoes of the prompt's framing (e.g. "婚禮致辭",
        # "wedding speeches and toasts") via the blocklist — NOT a substring check
        # against the whole prompt, which would now swallow real utterances that
        # are just a guest's name listed in the prompt.
        return any(b in t for b in blocklist)

    # Thresholds for the module-level is_hallucinated() (hoisted for testability).
    max_ratio = CONFIG["filter"].get("max_compression_ratio", 3.0)
    min_gate_len = CONFIG["filter"].get("min_gate_len", 24)

    import collections
    import time as _time
    # Bounded: hold ~4s of audio. If the segmenter falls behind (A8 stall), the
    # deque drops the OLDEST chunk automatically so we keep the freshest audio
    # rather than growing without limit and projecting stale captions.
    chunk_queue = collections.deque(maxlen=max(1, int(4 * sr / chunk_size)))
    chunk_lock = threading.Condition()

    def feed_chunk(chunk):
        with chunk_lock:
            chunk_queue.append(chunk)
            chunk_lock.notify()
        # Liveness stamp for the watchdog: prove audio is still flowing.
        controller.last_chunk_ts = _time.monotonic()

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
    lang_cfg = CONFIG["languages"]

    def translate_all(lang, original, partial=False):
        targets = lang_cfg["targets"][lang]
        # On a throwaway partial, Cantonese skips Vietnamese: VI pivots through
        # the English translation (a second sequential NLLB call) and the final
        # will show all three anyway. English partials stay full — both targets
        # come from one batched call, so dropping VI saves nothing there.
        if partial and lang != "en":
            targets = [t for t in targets if t == "en"] or targets[:1]
        if lang == "en":
            # Targets are independent of each other, so batch them in one call.
            tgt_codes = [lang_cfg["nllb_tgt"][t] for t in targets]
            outs = translator.translate_multi(original, "eng_Latn", tgt_codes)
            return [{"lang": t, "text": o} for t, o in zip(targets, outs)]
        # Sequential: Vietnamese pivots through the English translation, so
        # English must be produced first.
        translations, en_text = [], None
        for tgt in targets:
            src_text, src_code = original, lang_cfg["nllb_src"][lang]
            if lang_cfg["pivot_through_english"] and tgt != "en" and en_text:
                src_text, src_code = en_text, "eng_Latn"
            out = translator.translate(src_text, src_code, lang_cfg["nllb_tgt"][tgt])
            if tgt == "en":
                en_text = out
            translations.append({"lang": tgt, "text": out})
        return translations

    def asr_worker():
        import time as _time
        while not stop_event.is_set():
            with job_lock:
                while not final_jobs and not partial_slot:
                    # Bounded wait so the loop can observe stop_event instead of
                    # blocking forever on a quiet mic.
                    job_lock.wait(0.5)
                    if stop_event.is_set():
                        return
                if final_jobs:
                    kind, (audio, utt_id) = "final", final_jobs.pop(0)
                else:
                    kind, (audio, utt_id) = "partial", (
                        partial_slot["audio"], partial_slot["utt_id"])
                    partial_slot.clear()

            # One bad utterance must not silently kill this daemon (it has no
            # supervisor; faulthandler only catches native crashes). Isolate the
            # whole transcribe/route/translate/emit body and keep looping.
            try:
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
                # Run the compression-ratio gate on the RAW transcript: a loop
                # ("Airplane music"x30) compresses far above max_ratio, but
                # collapse_repeats would squash it to one copy first and hide the
                # signal, so the gate has to fire BEFORE collapsing.
                if is_hallucinated(text, max_ratio, min_gate_len):
                    continue
                raw = text
                text = collapse_repeats(text)
                if not text or is_junk(text):
                    continue
                # Heavy shrinkage is itself a loop signal: if collapse_repeats
                # removed >80% of the text, it was a repetition loop (5x+ repeats).
                # A doubled/tripled emphatic phrase ("we love you we love you")
                # only shrinks to ~50%/33%, so 0.2 catches loops without eating it.
                if len(text) < 0.2 * len(raw):
                    continue
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
                    # Show translations forming live during the speech. But if a
                    # final for this (or the next) utterance is already queued, skip
                    # the partial's NLLB work — the final's own translations are
                    # milliseconds away and must not wait behind a throwaway partial.
                    with job_lock:
                        final_waiting = bool(final_jobs)
                    if final_waiting:
                        # A worker that was mid-transcribe when a stop/switch landed
                        # must not project a stale or cross-pipeline caption.
                        if stop_event.is_set():
                            continue
                        emit_threadsafe(loop, {
                            "type": "partial", "lang": lang, "original": original})
                        continue
                    translations = translate_all(lang, original, partial=True)
                    if stop_event.is_set():
                        continue
                    emit_threadsafe(loop, {
                        "type": "partial", "lang": lang, "original": original,
                        "translations": translations})
                    continue

                translations = translate_all(lang, original)
                elapsed = _time.time() - t0
                print(f"[{lang}] ({elapsed:.1f}s) {original}  =>  "
                      + "  |  ".join(t["text"] for t in translations))
                # Same guard on the final: a worker outliving its pipeline must not
                # emit or write controller state for a torn-down generation.
                if stop_event.is_set():
                    continue
                emit_threadsafe(loop, {
                    "type": "final", "lang": lang,
                    "original": original, "translations": translations,
                })
                # Reflect live activity in /api/state; broadcast a status on a
                # language flip so the panel's detected-language indicator updates.
                controller.note_final(lang, original, gen)
                utt_langs.pop(utt_id, None)
            except Exception:
                traceback.print_exc()
                continue

    def segmenter():
        import collections
        import time as _time

        vad = VADIterator(
            vad_model, threshold=vad_cfg["threshold"], sampling_rate=sr,
            min_silence_duration_ms=vad_cfg["min_silence_ms"], speech_pad_ms=0)
        # Second, independent silero instance JUST for per-chunk probability.
        # VADIterator's vad_model is recurrent/stateful and advances its own state
        # each chunk; calling that same object again here would double-advance it
        # and break detection. A dedicated model scores without disturbing VAD. We
        # never reset its states — continuous running avoids per-utterance cold-start.
        score_vad = controller.score_vad
        preroll = collections.deque(
            maxlen=max(1, int(vad_cfg["preroll_ms"] / 1000 * sr / chunk_size)))
        buf, speaking, start_t, last_partial = [], False, 0.0, 0.0
        # Pre-ASR speech gate counters over the SPOKEN region (preroll excluded):
        # fraction of voiced frames separates real speech from music/applause that
        # VADIterator otherwise brackets into an "utterance" Whisper hallucinates on.
        voiced, total = 0, 0
        min_speech_ratio = vad_cfg.get("min_speech_ratio", 0.40)
        min_speech_s = vad_cfg.get("min_speech_s", 0.5)
        energy_floor = vad_cfg.get("energy_floor", 0.0)

        def is_speech_segment():
            if total == 0:
                return False
            if total * chunk_size / sr < min_speech_s:      # too short to be a real line
                return False
            return voiced / total >= min_speech_ratio        # enough voiced frames (rejects music/applause)

        while not stop_event.is_set():
            with chunk_lock:
                while not chunk_queue:
                    # Bounded wait so the loop can observe stop_event between chunks.
                    chunk_lock.wait(0.5)
                    if stop_event.is_set():
                        return
                chunk = chunk_queue.popleft()
            # mic_reader keeps filling chunk_queue regardless, so losing this
            # thread is worse than losing asr_worker — isolate per-chunk failures
            # and reset running state so a mid-utterance error doesn't wedge VAD.
            try:
                t = torch.from_numpy(chunk)
                event = vad(t)
                prob = score_vad(t, sr).item()  # same tensor; score_vad is the separate instance

                if speaking:
                    buf.append(chunk)
                    total += 1
                    voiced += 1 if prob >= vad_cfg["threshold"] else 0
                if event and "start" in event:
                    speaking = True
                    utt_counter["n"] += 1
                    buf = list(preroll) + [chunk]
                    start_t = last_partial = _time.time()
                    # Count only the start chunk; preroll is pre-speech.
                    total = 1
                    voiced = 1 if prob >= vad_cfg["threshold"] else 0
                elif event and "end" in event:
                    speaking = False
                    # Gate is finals-only and fails safe by dropping: a non-speech
                    # segment submits nothing, so the last good caption stays up.
                    if buf:
                        audio = np.concatenate(buf)
                        if is_speech_segment() and (
                                energy_floor <= 0
                                or np.sqrt(np.mean(audio ** 2)) >= energy_floor):
                            submit("final", audio, utt_counter["n"])
                    buf = []
                elif speaking:
                    now = _time.time()
                    if now - start_t > vad_cfg["max_utterance_s"]:
                        if buf:
                            audio = np.concatenate(buf)
                            if is_speech_segment() and (
                                    energy_floor <= 0
                                    or np.sqrt(np.mean(audio ** 2)) >= energy_floor):
                                submit("final", audio, utt_counter["n"])
                        utt_counter["n"] += 1
                        buf, start_t, last_partial = [], now, now
                        voiced = total = 0  # next sub-segment counts fresh
                    elif (vad_cfg["partial_interval_s"] > 0
                          and now - last_partial > vad_cfg["partial_interval_s"]
                          and len(buf) * chunk_size > sr):
                        # Partials are throwaway/cosmetic and drive the live "forming"
                        # preview + the topbar text, so they are NOT speech-gated:
                        # gating them would freeze the on-screen preview through a
                        # soft real sentence. A music/applause partial is still caught
                        # downstream (is_junk / is_hallucinated / the client garbage
                        # guard), and its FINAL is dropped by is_speech_segment anyway.
                        submit("partial", np.concatenate(buf), utt_counter["n"])
                        last_partial = now
                if not speaking:
                    preroll.append(chunk)
            except Exception:
                traceback.print_exc()
                # Re-sync VADIterator's internal `triggered` flag with our Python
                # `speaking` flag after an error; otherwise VAD can stay latched
                # "on" while we think we're idle (or vice-versa). score_vad is left
                # un-reset by design (continuous running avoids cold-start).
                vad.reset_states()
                speaking, buf, voiced, total = False, [], 0, 0

    # Seed liveness so the watchdog doesn't fire before the first chunk arrives.
    controller.last_chunk_ts = _time.monotonic()

    def watchdog():
        # Surfaces a silent stall (mic disconnect, or a wedged segmenter that
        # stopped pulling chunks) as an explicit error state instead of a frozen
        # "running" panel. Observes stop_event so it exits cleanly on teardown.
        # Started during build (state still "loading"), so it only ACTS once the
        # pipeline has committed to "running"; until then it just waits.
        while not stop_event.is_set():
            if stop_event.wait(1.0):
                return
            if controller.state != "running":
                continue
            if _time.monotonic() - controller.last_chunk_ts > 4.0:
                if stop_event.is_set():
                    return
                secs = int(_time.monotonic() - controller.last_chunk_ts)
                with controller._lock:
                    controller.state = "error"
                    controller.error = (
                        f"No audio for {secs}s — mic may be disconnected")
                controller._broadcast()
                # stop() joins this very thread; call it from a throwaway thread so
                # the watchdog can return immediately instead of joining itself.
                threading.Thread(target=controller.stop, daemon=True).start()
                return

    threads = [
        threading.Thread(target=asr_worker, daemon=True),
        threading.Thread(target=segmenter, daemon=True),
    ]
    # The liveness watchdog only makes sense for the live mic: --file intentionally
    # stops feeding when the WAV ends and leaves the last caption on screen, so a
    # silence watchdog there would wrongly force an error state.
    if source_file is None:
        threads.append(threading.Thread(target=watchdog, daemon=True))
    for th in threads:
        th.start()

    if source_file:
        def file_feeder():
            import time as _time
            import wave
            try:
                with wave.open(str(source_file), "rb") as wf:
                    if wf.getframerate() != sr or wf.getnchannels() != 1:
                        raise RuntimeError(
                            f"{source_file} must be mono {sr} Hz WAV "
                            f"(got {wf.getnchannels()}ch {wf.getframerate()} Hz)")
                    raw = wf.readframes(wf.getnframes())
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                print(f"Streaming {source_file} ({len(audio) / sr:.1f}s) through the pipeline...")
                for i in range(0, len(audio), chunk_size):
                    if stop_event.is_set():
                        return
                    chunk = audio[i:i + chunk_size]
                    if len(chunk) < chunk_size:
                        chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
                    feed_chunk(chunk)
                    _time.sleep(chunk_size / sr)
                for _ in range(int(sr / chunk_size)):
                    if stop_event.is_set():
                        return
                    feed_chunk(np.zeros(chunk_size, dtype=np.float32))
                    _time.sleep(chunk_size / sr)
                print("File finished. Captions remain on screen; Ctrl+C to exit.")
            except Exception as e:
                # A daemon thread's SystemExit/exception is silently swallowed, which
                # would strand the controller at state="running" with no captions.
                # Surface a bad/short WAV (or any feeder error) as an explicit error.
                traceback.print_exc()
                with controller._lock:
                    controller.state = "error"
                    controller.error = f"File playback failed: {e}"
                controller._broadcast()

        feeder = threading.Thread(target=file_feeder, daemon=True)
        feeder.start()
        threads.append(feeder)
        return threads, None

    import sounddevice as sd
    stream = sd.InputStream(
        samplerate=sr, channels=1, dtype="float32",
        blocksize=chunk_size, device=device)
    stream.start()

    # Blocking reads on our own thread, NOT a PortAudio callback: cffi callbacks
    # fired from PortAudio's native audio thread intermittently crash the whole
    # process on Windows (access violation in _cffi_backend, see Event Viewer).
    # Python -> C blocking reads avoid that path entirely.
    def mic_reader():
        nonlocal stream
        overflow_warned = 0.0
        while not stop_event.is_set():
            try:
                data, overflowed = stream.read(chunk_size)
            except Exception:
                # The stream was stopped/closed out from under us during shutdown;
                # don't spew a PortAudio traceback to the console — just exit.
                if stop_event.is_set():
                    break
                traceback.print_exc()
                # A transient device hiccup (USB re-enumerate, sample-rate glitch)
                # shouldn't permanently kill capture. Try a bounded REOPEN with the
                # same parameters; only give up (and surface an error) if every
                # attempt fails.
                reopened = False
                for _ in range(3):
                    if stop_event.is_set():
                        return
                    try:
                        stream.stop()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
                    _time.sleep(0.3)
                    try:
                        new_stream = sd.InputStream(
                            samplerate=sr, channels=1, dtype="float32",
                            blocksize=chunk_size, device=device)
                        new_stream.start()
                    except Exception:
                        continue
                    stream = new_stream
                    # Keep the controller's ref in sync so stop() closes the LIVE
                    # stream, not the dead one we just discarded.
                    with controller._lock:
                        if controller.stream is not None:
                            controller.stream = stream
                    print("[audio] microphone stream reopened", file=sys.stderr)
                    reopened = True
                    break
                if reopened:
                    continue
                with controller._lock:
                    controller.state = "error"
                    controller.error = (
                        "Microphone disconnected or audio read failed — "
                        "press Restart.")
                controller._broadcast()
                break
            if overflowed:
                # Rate-limit so a sustained overflow doesn't flood the console.
                now = _time.monotonic()
                if now - overflow_warned > 1.0:
                    print("[audio] input overflow", file=sys.stderr)
                    overflow_warned = now
            feed_chunk(data[:, 0].copy())

    reader = threading.Thread(target=mic_reader, daemon=True)
    reader.start()
    threads.append(reader)
    device_name = sd.query_devices(stream.device)["name"]
    print(f"Listening on input device: {device_name}")
    return threads, stream


class PipelineController:
    """Module-level singleton that owns the live pipeline. The web server boots
    without any pipeline; the operator drives start/stop/restart from the browser.
    The heavy ML libs/models are loaded lazily in a background thread on the first
    start(), so the control panel (and --demo) work with no torch/models present.

    The model-independent translator + Silero VAD models are loaded once and
    cached across restarts; only the Whisper ASR is reloaded when the model key
    changes (or any restart, since CTranslate2/faster-whisper VRAM is freed)."""

    def __init__(self, loop):
        self.loop = loop
        self._lock = threading.Lock()
        self.state = "idle"            # idle | loading | running | error
        self.model = None              # current model key
        self.device = None             # current device index (None == system default)
        self.device_name = None
        self.detected_lang = None      # "en" | "yue" | None
        self.last_caption = None       # {"lang": str, "original": str} | None
        self.error = None
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.stream = None
        # Monotonically-increasing token (guarded by _lock). Every start() bumps it;
        # the background loader captures its value and only COMMITS its freshly-built
        # pipeline if the token still matches at the end of the load — otherwise a
        # stop()/restart landed mid-load and the build is stale and must be discarded.
        self.generation = 0
        self._loader = None            # in-flight background load thread (or None)
        self.last_chunk_ts = 0.0       # liveness stamp for the mic watchdog
        # Cached, model-INDEPENDENT objects (loaded once, reused across restarts).
        self.translator = None
        self.vad_model = None          # VADIterator's stateful Silero instance
        self.score_vad = None          # second, dedicated per-chunk scorer
        self.asr = None

    # ---- status helpers ---------------------------------------------------

    def status_msg(self):
        with self._lock:
            return {
                "type": "status",
                "state": self.state,
                "model": self.model,
                "device_name": self.device_name,
                "detected_lang": self.detected_lang,
            }

    def state_dict(self):
        with self._lock:
            return {
                "state": self.state,
                "model": self.model,
                "device": self.device,
                "device_name": self.device_name,
                "detected_lang": self.detected_lang,
                "last_caption": dict(self.last_caption) if self.last_caption else None,
                "error": self.error,
            }

    def _broadcast(self):
        emit_threadsafe(self.loop, self.status_msg())

    def note_final(self, lang, original, gen):
        """Called by asr_worker on every emitted final. Updates live state and
        broadcasts a status only when the detected language actually changes. The
        gen check ensures a worker that outlived its pipeline can't corrupt the
        state of a newer generation."""
        with self._lock:
            if gen != self.generation:
                return
            changed = lang != self.detected_lang
            self.detected_lang = lang
            self.last_caption = {"lang": lang, "original": original}
        if changed:
            self._broadcast()

    # ---- lifecycle --------------------------------------------------------

    def _resolve_device_name(self, device):
        try:
            import sounddevice as sd
            idx = device if device is not None else sd.default.device[0]
            return sd.query_devices(idx)["name"]
        except Exception:
            return None

    def start(self, model_key, device, source_file=None):
        """(Re)start the pipeline. Returns immediately; the heavy model load and
        thread startup happen on a background thread. If already running/loading,
        the current pipeline (incl. any in-flight load) is torn down first."""
        if model_key not in WHISPER_MODELS:
            raise ValueError(f"unknown model: {model_key}")
        if model_key == "cantonese" and not cantonese_available():
            raise ValueError("Cantonese model not found; run convert_cantonese.py first")
        # Re-entrant: tear down any existing pipeline (and JOIN any in-flight loader)
        # before starting the new one. stop() is fully self-synchronised and does its
        # long joins outside the lock, so it must be called WITHOUT _lock held.
        # Joining the old loader here is what makes model loads single-flight (a
        # second start() can't begin loading a 2nd Whisper until the first finishes).
        # "error" is included: an errored pipeline can still have live worker threads
        # (e.g. mic_reader broke but segmenter/asr_worker keep running) to tear down.
        if self.state in ("running", "loading", "error"):
            self.stop()
        with self._lock:
            # Bump the generation: any straggler thread/loader from a prior gen will
            # now see gen != self.generation and refuse to commit or write state.
            self.generation += 1
            gen = self.generation
            stop_event = threading.Event()
            self.stop_event = stop_event
            self.state = "loading"
            self.model = model_key
            self.device = device
            self.device_name = self._resolve_device_name(device)
            self.detected_lang = None
            self.error = None
            loader = threading.Thread(
                target=self._load_and_run,
                args=(gen, stop_event, model_key, device, source_file),
                daemon=True,
            )
            self._loader = loader
        self._broadcast()
        loader.start()

    def _load_and_run(self, gen, stop_event, model_key, device, source_file):
        asr = None
        threads = None
        stream = None
        try:
            # Heavy imports happen HERE, never at module top, so the web server
            # can boot and serve the control panel with no torch/models present.
            try:
                from silero_vad import load_silero_vad  # noqa: F401

                from asr import create_asr
                from translate import CaptionTranslator
            except ImportError as e:
                raise RuntimeError(
                    f"Missing dependency: {e.name}. "
                    "Run: pip install -r requirements-windows.txt (or -mac.txt), "
                    "then python setup_models.py."
                )

            # Cache the model-independent translator + Silero VAD once.
            if self.translator is None:
                print("Loading translation model...")
                self.translator = CaptionTranslator(
                    ROOT / CONFIG["mt"]["model_dir"], CONFIG["mt"])
            if self.vad_model is None:
                print("Loading Silero VAD...")
                self.vad_model = load_silero_vad()
                self.score_vad = load_silero_vad()

            # Build the ASR for the chosen model key (reuse WHISPER_MODELS + _resolve).
            mlx_model, fw_model = WHISPER_MODELS[model_key]
            asr_cfg = dict(CONFIG["asr"])
            asr_cfg["mlx_model"] = _resolve(mlx_model)
            asr_cfg["fw_model"] = _resolve(fw_model)
            print(f"Loading ASR model: {model_key} ({asr_cfg['mlx_model']})...")
            asr = create_asr(asr_cfg)
            print("Models loaded.")

            if stop_event.is_set():     # stopped while loading
                self._abort_stale(stop_event, asr, None, None)
                return

            # Build threads + open the mic stream BEFORE committing. Pass gen so the
            # workers stamp the right generation on any state write.
            threads, stream = build_pipeline_threads(
                self.loop, self, stop_event, gen, asr, self.translator,
                self.vad_model, device, source_file=source_file)

            # COMMIT POINT: publish the built pipeline ONLY if we're still the
            # current generation and no stop landed during the build. asr is
            # assigned here (not earlier) so a stale load never pins/leaks a model.
            with self._lock:
                if gen != self.generation or stop_event.is_set():
                    stale = True
                else:
                    stale = False
                    self.asr = asr
                    self.threads = threads
                    self.stream = stream
                    self.state = "running"
            if stale:
                # A stop()/restart landed mid-build; the just-built pipeline is
                # orphaned. Tear it down without touching controller state (a newer
                # generation owns it now).
                self._abort_stale(stop_event, asr, threads, stream)
                return
            self._broadcast()
        except Exception as e:
            traceback.print_exc()
            # Clean up anything we managed to open before failing.
            self._abort_stale(stop_event, asr, threads, stream)
            with self._lock:
                # Don't clobber a newer generation's state with this stale error.
                if gen == self.generation:
                    self.state = "error"
                    self.error = _friendly_load_error(e, model_key)
                    self.asr = None
                    do_broadcast = True
                else:
                    do_broadcast = False
            gc.collect()
            if do_broadcast:
                self._broadcast()

    def _abort_stale(self, stop_event, asr, threads, stream):
        """Tear down a pipeline that was built (or partially built) for a generation
        that's no longer current. Closes the stream and joins the just-built threads
        so no orphaned mic stream / worker survives. Never touches controller state.
        Sets the build's OWN stop_event so its workers exit even on the exception
        path (where stop() may not have run)."""
        stop_event.set()
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        for th in (threads or []):
            th.join(timeout=5.0)
        del asr
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

    def stop(self):
        """Stop the pipeline cleanly. Safe to call when already idle. Signals the
        stop_event, tears down the audio stream, joins the worker threads AND any
        in-flight loader with a timeout (never hangs), drops the ASR so
        CTranslate2/faster-whisper frees its VRAM, and KEEPS the translator + VAD
        models cached for fast restart."""
        with self._lock:
            if self.state == "idle":
                return
            stop_event = self.stop_event
            stream = self.stream
            threads = self.threads
            loader = self._loader
            self.stream = None
            self.threads = []
        stop_event.set()
        # Stopping the stream first unblocks mic_reader's blocking read(); the
        # reader catches the resulting error and exits because stop_event is set.
        # Done OUTSIDE the lock — these can block, and api/state etc. must stay
        # responsive during a teardown.
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        for th in threads:
            th.join(timeout=5.0)
        # Join the loader with a GENEROUS timeout: create_asr can take many seconds,
        # and a short timeout would let a second start() begin loading a 2nd Whisper
        # model concurrently (the observed OOM). This makes model loads single-flight.
        if loader is not None and loader is not threading.current_thread():
            loader.join(timeout=30.0)
        # A still-running worker's closure pins `asr`, so freeing it now would be a
        # no-op (and risks freeing a model the worker is mid-transcribe with). Only
        # drop our ref once the workers have actually exited.
        alive = [th for th in threads if th.is_alive()]
        with self._lock:
            # Don't stomp a newer generation: if start() already bumped past us and
            # set up a fresh load, leave its state/asr alone.
            if loader is None or loader is self._loader:
                self.asr = None
                self.detected_lang = None
                # Preserve an error state (and its message): the watchdog/mic-failure
                # path sets state="error" then calls stop() to tear down — clearing
                # it to "idle" here would wipe the operator-facing reason. A clean
                # stop (state was running/loading) goes to idle as usual.
                if self.state != "error":
                    self.state = "idle"
                    self.error = None
        if alive:
            print(f"[stop] {len(alive)} worker(s) still alive after timeout; "
                  "dropped controller ref anyway (best-effort).", file=sys.stderr)
        gc.collect()   # free faster-whisper / CTranslate2 VRAM held by the ASR
        try:
            import torch
            torch.cuda.empty_cache()   # return freed CUDA blocks to the driver
        except Exception:
            pass
        self._broadcast()


# ---- HTTP API handlers ----------------------------------------------------


async def api_options(request):
    models = [
        {"key": k, "label": MODEL_LABELS.get(k, k), "available": model_available(k)}
        for k in WHISPER_MODELS
    ]
    devices = []
    default_device = CONFIG["audio"].get("device")
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                devices.append({"index": i, "name": d["name"]})
    except Exception as e:
        traceback.print_exc()
        print(f"[api/options] could not list audio devices: {e}", file=sys.stderr)
    return web.json_response({
        "models": models,
        "devices": devices,
        "default_device": default_device,
    })


async def api_state(request):
    return web.json_response(controller.state_dict())


async def api_start(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    model_key = body.get("model")
    device = body.get("device")
    if model_key not in WHISPER_MODELS:
        return web.json_response(
            {"ok": False, "error": f"unknown model: {model_key}"}, status=400)
    if device is not None:
        try:
            device = int(device)
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "device must be an integer or null"}, status=400)
    try:
        # start() can block: it tears down + joins any running pipeline (and any
        # in-flight loader) before spawning the new background loader. Run it OFF
        # the event loop so the broadcaster, /api/state, and other clients stay
        # responsive during a restart/switch instead of freezing for the join window.
        await asyncio.get_running_loop().run_in_executor(
            None, controller.start, model_key, device)
    except Exception as e:
        with controller._lock:
            controller.state = "error"
            controller.error = str(e)
        controller._broadcast()
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    return web.json_response({"ok": True, "state": "loading"})


async def api_stop(request):
    # stop() blocks on thread joins (up to ~5s/worker + the loader); offload it so
    # the event loop keeps serving status/captions during teardown.
    await asyncio.get_running_loop().run_in_executor(None, controller.stop)
    return web.json_response({"ok": True, "state": "idle"})


# Which Whisper model the headless --file path uses: the --model choice isn't
# meaningful there (no panel), so fall back to config's model. config.json stores
# the resolved repo/name, not a key; map it back to a key, defaulting to turbo.
def _config_default_model_key():
    fw = CONFIG["asr"].get("fw_model")
    for key, (_, fw_name) in WHISPER_MODELS.items():
        if fw_name == fw:
            return key
    return "turbo"


CONFIG_DEFAULT_MODEL = _config_default_model_key()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="scripted captions, no models")
    parser.add_argument("--file", metavar="WAV",
                        help="stream a mono 16 kHz WAV through the real pipeline instead of the mic")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--model", choices=list(WHISPER_MODELS),
                        help="Whisper model size (smaller = less lag, less accurate). "
                             "If given (and not --demo/--file), auto-start that model "
                             "on the default device at boot. Default: control panel.")
    args = parser.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    # Validate a --model choice up front (esp. cantonese needs its converted folder)
    # so a power-user quick-launch fails loudly here rather than silently in the
    # background loader.
    if args.model == "cantonese" and not cantonese_available():
        sys.exit("Cantonese model not found. Run the converter first:\n"
                 "  python convert_cantonese.py   (or double-click 'Convert Cantonese')")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    global out_queue, controller
    # Bounded: back-pressure against a stalled broadcaster; emit_threadsafe drops
    # the oldest (disposable partial) on overflow rather than growing without limit.
    out_queue = asyncio.Queue(maxsize=256)
    controller = PipelineController(loop)

    app = web.Application()
    app.router.add_get("/", control_page)
    app.router.add_get("/display", display_page)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/options", api_options)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/start", api_start)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_static("/fonts/", ROOT / "display" / "fonts")

    host, port = CONFIG["server"]["host"], CONFIG["server"]["port"]
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, host, port).start())
    loop.create_task(broadcaster())

    if args.demo:
        print("DEMO MODE — scripted captions, no microphone or models in use.")
        loop.create_task(demo_feed())
    elif args.file:
        # Headless WAV streaming through the real pipeline (uses config defaults).
        print(f"Streaming file through the pipeline (headless): {args.file}")
        controller.start(CONFIG_DEFAULT_MODEL, CONFIG["audio"].get("device"),
                         source_file=args.file)
    elif args.model:
        # Power-user quick-launch: auto-start the chosen model on the default mic.
        print(f"Auto-starting pipeline with model: {args.model}")
        controller.start(args.model, CONFIG["audio"].get("device"))
    else:
        print("Server ready. Open the control panel to start the pipeline.")

    print(f"Control panel: http://localhost:{port}/")
    print(f"Projector:     http://localhost:{port}/display  (open full-screen)")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        controller.stop()


if __name__ == "__main__":
    main()
