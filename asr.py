"""Pluggable Whisper backends: mlx-whisper on Apple Silicon, faster-whisper elsewhere.

Both expose transcribe(audio, language, initial_prompt, final) -> (text, detected_language).
Whisper's built-in translate task is deliberately not used: large-v3-turbo was trained
without it, so translation happens in a separate NLLB stage (see translate.py).
"""

import numpy as np

from gpu import enable_cuda_dlls

enable_cuda_dlls()


class MlxWhisperASR:
    def __init__(self, cfg):
        import mlx_whisper
        self.mlx_whisper = mlx_whisper
        self.model = cfg["mlx_model"]
        self.beam_final = cfg["beam_size_final"]
        self.mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32),
                                    path_or_hf_repo=self.model)

    def transcribe(self, audio, language=None, initial_prompt=None, final=True):
        result = self.mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.model,
            language=language, initial_prompt=initial_prompt,
            condition_on_previous_text=False, fp16=True)
        segments = [s for s in result["segments"]
                    if not (s["no_speech_prob"] > 0.6 and s["avg_logprob"] < -1.0)]
        text = " ".join(s["text"].strip() for s in segments)
        return text.strip(), result.get("language")


class FasterWhisperASR:
    def __init__(self, cfg):
        from faster_whisper import WhisperModel
        self.beam_final = cfg["beam_size_final"]
        self.model = None
        cuda_compute = cfg.get("cuda_compute_type", "int8_float16")
        if cfg.get("device", "auto") in ("auto", "cuda"):
            try:
                model = WhisperModel(cfg["fw_model"], device="cuda", compute_type=cuda_compute)
                # CUDA construction can succeed even when the cuBLAS/cuDNN runtime
                # DLLs are missing; the failure only surfaces on the first encode.
                # Probe with silence so we can fall back to CPU before going live.
                list(model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
                self.model = model
                print("Whisper: using CUDA (GPU).")
            except Exception as e:
                print(f"Whisper: CUDA unavailable ({type(e).__name__}); falling back to CPU.")
        if self.model is None:
            self.model = WhisperModel(cfg["fw_model"], device="cpu", compute_type="int8")
            print("Whisper: using CPU (int8).")

    def transcribe(self, audio, language=None, initial_prompt=None, final=True):
        segments, info = self.model.transcribe(
            audio, language=language, initial_prompt=initial_prompt,
            beam_size=self.beam_final if final else 1,
            condition_on_previous_text=False, vad_filter=False)
        segments = [s for s in segments
                    if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
        text = " ".join(s.text.strip() for s in segments)
        return text.strip(), info.language


def create_asr(cfg):
    backend = cfg.get("backend", "auto")
    if backend in ("auto", "mlx"):
        try:
            return MlxWhisperASR(cfg)
        except ImportError:
            if backend == "mlx":
                raise
    return FasterWhisperASR(cfg)
