"""Deterministic regression + tuning harness for the hallucination gates.

Runs with NO models — it exercises the TEXT-side defenses directly (the real
functions imported from server.py, so this can't drift from production) so you
can tweak the thresholds in config.json and instantly re-check that real speech
is kept while loops and stand-alone fillers are dropped:

    python test_gates.py              # run the gate suite (exit 1 on a regression)
    python test_gates.py --make-noise # write synthetic non-speech WAVs to test_clips/

The synthetic clips let you sanity-check the AUDIO-side VAD speech-ratio gate
without a recording — feed one through the real pipeline and confirm it produces
NO captions:

    python server.py --file test_clips/white_noise.wav
    python server.py --file test_clips/sine_tone.wav      # "music-ish"

Synthetic noise is a floor, not a substitute: replace these with a REAL
music/applause clip from the venue's PA when you can, and tune
vad.min_speech_ratio / vad.energy_floor against it.
"""
import json
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# Import the REAL gate logic so this harness can never drift from production.
sys.path.insert(0, str(ROOT))
from server import collapse_repeats, is_hallucinated  # noqa: E402

FILT = CONFIG["filter"]
MAX_RATIO = FILT.get("max_compression_ratio", 3.0)
MIN_GATE = FILT.get("min_gate_len", 24)
STANDALONE = {s.strip().lower() for s in FILT.get("standalone_blocklist", [])}
_PUNCT = ".,!?。，！？ "


def _is_standalone_filler(t):  # mirrors server.py is_junk's whole-transcript check
    t = t.strip().lower()
    return t.strip(_PUNCT) in STANDALONE or t in STANDALONE


def would_drop(text):
    """Replicates asr_worker's text-side drop decision for a transcript: the
    raw-compression gate, the stand-alone filler match, and the >80%-shrink loop
    signal. (Does not model the substring blocklist — that's for the prompt-echo
    hallucinations, not relevant to threshold tuning.)"""
    if is_hallucinated(text, MAX_RATIO, MIN_GATE):
        return True
    if _is_standalone_filler(text):
        return True
    collapsed = collapse_repeats(text)
    if not collapsed:
        return True
    return len(collapsed) < 0.2 * len(text)


# (label, transcript, expected_drop)
CASES = [
    ("loop: airplane music",     "Airplane music " * 30, True),
    ("loop: CJK phrase x15",     "多謝大家" * 15,          True),
    ("loop: single word x40",    "okay " * 40,            True),
    ("filler: bare 'you'",       "you",                   True),
    ("filler: 'Okay.'",          "Okay.",                 True),
    ("real: EN sentence",        "Tonight we celebrate two families becoming one.", False),
    ("real: Cantonese sentence", "我第一次見到佢嗰陣就知道佢係一個好特別嘅人", False),
    ("real: emphasis x2",        "we love you we love you", False),
    ("real: emphasis x3",        "thank you thank you thank you", False),
    ("real: 'Thank you.' toast", "Thank you.",            False),  # deliberately NOT blocklisted
    ("real: short with names",   "Reynold and Ellese, congratulations.", False),
]


def run_suite():
    print(f"thresholds: max_compression_ratio={MAX_RATIO}  min_gate_len={MIN_GATE}")
    print(f"standalone_blocklist={sorted(STANDALONE)}\n")
    print(f"{'expect':6} {'got':6} {'':4} case")
    failures = 0
    for label, text, expect in CASES:
        got = would_drop(text)
        ok = got == expect
        failures += not ok
        print(f"{'DROP' if expect else 'KEEP':6} {'DROP' if got else 'KEEP':6} "
              f"{'ok' if ok else 'FAIL':4} {label}")
    print()
    if failures:
        print(f"{failures} regression(s): a gate change is dropping real speech or "
              "letting a hallucination through. Adjust config.json and re-run.")
        sys.exit(1)
    print("All gate cases pass.")


def make_noise():
    import math
    import random
    import struct
    import wave

    sr = CONFIG["audio"]["sample_rate"]
    out = ROOT / "test_clips"
    out.mkdir(exist_ok=True)
    dur = 6.0
    n = int(sr * dur)

    def write(name, samples):
        with wave.open(str(out / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(b"".join(
                struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples))
        print(f"  wrote {out / name}  ({dur:.0f}s mono {sr} Hz)")

    rnd = random.Random(0)
    write("white_noise.wav", (rnd.uniform(-0.3, 0.3) for _ in range(n)))
    write("sine_tone.wav", (0.3 * math.sin(2 * math.pi * 440 * i / sr) for i in range(n)))
    write("silence.wav", (0.0 for _ in range(n)))
    write("applause_like.wav",
          (rnd.uniform(-1.0, 1.0) * 0.4 * (0.5 + 0.5 * math.sin(2 * math.pi * 3 * i / sr))
           for i in range(n)))
    print("\nFeed each through the pipeline and confirm it produces NO captions, e.g.:")
    print("  python server.py --file test_clips/white_noise.wav")
    print("If a clip DOES caption, raise vad.min_speech_ratio (or vad.energy_floor) in config.json.")


if __name__ == "__main__":
    if "--make-noise" in sys.argv:
        make_noise()
    else:
        run_suite()
