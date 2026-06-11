"""NLLB-200 translation via CTranslate2, with OpenCC Traditional-Chinese normalisation.

English -> zho_Hant (Standard Written Chinese, Traditional — what HK subtitles use).
Cantonese speech -> Whisper Chinese transcript -> yue_Hant-sourced NLLB -> English.
All Chinese shown on screen is normalised to HK Traditional via OpenCC, because
Whisper sometimes emits Simplified characters for Cantonese audio.
"""

import re

from gpu import enable_cuda_dlls

enable_cuda_dlls()

import ctranslate2
from transformers import AutoTokenizer

try:
    from opencc import OpenCC
    _opencc = OpenCC("s2hk")
except Exception:
    from opencc import OpenCC
    _opencc = OpenCC("s2t")


class CaptionTranslator:
    def __init__(self, model_dir, cfg):
        self.translator = self._load(str(model_dir), cfg.get("device", "auto"))
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
        self.beam_size = cfg.get("beam_size", 2)
        # Names to keep verbatim through translation (longest first so full
        # names match before bare first names). Each becomes an "XXnnXX"
        # placeholder NLLB passes through untouched, restored after decoding.
        names = sorted((cfg.get("protect_names") or []), key=len, reverse=True)
        self._shields = [
            (re.compile(r"\b" + re.escape(n) + r"\b", re.IGNORECASE), n)
            for n in names]

    @staticmethod
    def _load(model_dir, device):
        if device in ("auto", "cuda"):
            try:
                t = ctranslate2.Translator(model_dir, device="cuda", compute_type="auto")
                print("Translator: using CUDA (GPU).")
                return t
            except Exception as e:
                if device == "cuda":
                    raise
                print(f"Translator: CUDA unavailable ({type(e).__name__}); using CPU.")
        return ctranslate2.Translator(model_dir, device="cpu", compute_type="auto")

    def translate(self, text, src_lang, tgt_lang):
        text, restores = self._shield(text)
        self.tokenizer.src_lang = src_lang
        tokens = self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode(text))
        results = self.translator.translate_batch(
            [tokens], target_prefix=[[tgt_lang]],
            beam_size=self.beam_size, max_decoding_length=256)
        out_tokens = results[0].hypotheses[0]
        if out_tokens and out_tokens[0] == tgt_lang:
            out_tokens = out_tokens[1:]
        out = self.tokenizer.decode(
            self.tokenizer.convert_tokens_to_ids(out_tokens),
            skip_special_tokens=True)
        if tgt_lang.endswith("_Hant") or tgt_lang.endswith("_Hans"):
            out = _opencc.convert(out)
        for token, name in restores:
            out = out.replace(token, name)
        return out.strip()

    def _shield(self, text):
        restores = []
        for rx, name in self._shields:
            if rx.search(text):
                token = f"XX{len(restores):02d}XX"
                text = rx.sub(token, text)
                restores.append((token, name))
        return text, restores

    def to_traditional(self, text):
        return _opencc.convert(text)
