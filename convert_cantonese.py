"""Convert a Cantonese-specialised Whisper fine-tune into the format this
machine's Whisper engine needs, so it can be selected with `--model cantonese`.

  - macOS (Apple Silicon)  -> MLX format  -> models/whisper-cantonese-mlx/
  - Windows / Linux        -> CTranslate2 -> models/whisper-cantonese-ct2/

Run once per machine (needs wifi; downloads ~1.6 GB the first time):

    python convert_cantonese.py

The default model is a bilingual EN+Cantonese turbo fine-tune (same speed as the
stock turbo, MIT licensed). Override with --model <hf-repo-id> to try another.
After it finishes, start captions and pick the Cantonese model:

    python server.py --model cantonese          (Windows)
    Start Captions -> choose "6) cantonese"      (Mac)

To go back to the stock model, just pick turbo again — nothing here is destructive.
"""

import argparse
import platform
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
# Bilingual (Cantonese + English, code-switch aware) turbo fine-tune. Keeps the
# same large-v3-turbo backbone as the stock model, so latency is unchanged.
DEFAULT_MODEL = "JackyHoCL/whisper-large-v3-turbo-cantonese-yue-english"
MLX_DIR = ROOT / "models" / "whisper-cantonese-mlx"
CT2_DIR = ROOT / "models" / "whisper-cantonese-ct2"
MLX_EXAMPLES = ROOT / "models" / "_mlx-examples"


def build_ct2(model_id, force):
    """faster-whisper (Windows/Linux) loads a CTranslate2 model directory."""
    if (CT2_DIR / "model.bin").exists() and not force:
        print(f"Cantonese CT2 model already present at {CT2_DIR}")
        return
    from ctranslate2.converters import TransformersConverter

    from setup_models import download_repo  # resilient resumable downloader
    src = ROOT / "models" / "_src" / model_id.replace("/", "--")
    print(f"Downloading {model_id} ...")
    download_repo(model_id, src)
    print("Converting to CTranslate2 (int8) ...")
    # faster-whisper needs the tokenizer + feature-extractor config beside the
    # weights. copy_files is a *constructor* arg (not a convert() arg), and it
    # errors on a missing file, so only list what the download actually contains.
    wanted = ["tokenizer.json", "preprocessor_config.json", "tokenizer_config.json",
              "special_tokens_map.json", "vocabulary.json", "vocab.json",
              "merges.txt", "normalizer.json", "added_tokens.json"]
    copy_files = [f for f in wanted if (src / f).exists()]
    TransformersConverter(str(src), copy_files=copy_files).convert(
        str(CT2_DIR), quantization="int8", force=True)
    print(f"Done. Cantonese CT2 model at {CT2_DIR}")


def build_mlx(model_id, force):
    """mlx-whisper (Apple Silicon) loads an MLX weights directory. The canonical
    converter lives in the mlx-examples repo; clone it shallowly and run it."""
    if (MLX_DIR / "weights.npz").exists() and not force:
        print(f"Cantonese MLX model already present at {MLX_DIR}")
        return
    if not MLX_EXAMPLES.exists():
        print("Fetching the MLX Whisper converter (one-time)...")
        subprocess.run(
            ["git", "clone", "--depth", "1",
             "https://github.com/ml-explore/mlx-examples", str(MLX_EXAMPLES)],
            check=True)
    convert_py = MLX_EXAMPLES / "whisper" / "convert.py"
    print(f"Converting {model_id} to MLX (float16) — downloads ~1.6 GB first run...")
    subprocess.run(
        [sys.executable, str(convert_py),
         "--torch-name-or-path", model_id,
         "--mlx-path", str(MLX_DIR),
         "--dtype", "float16"],
        check=True, cwd=str(MLX_EXAMPLES / "whisper"))
    print(f"Done. Cantonese MLX model at {MLX_DIR}")


def main():
    parser = argparse.ArgumentParser(description="Convert a Cantonese Whisper model.")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Hugging Face repo id of the fine-tune to convert.")
    parser.add_argument("--force", action="store_true",
                        help="Re-convert even if a converted model already exists.")
    args = parser.parse_args()

    is_apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"
    print(f"Source model: {args.model}")
    if is_apple_silicon:
        build_mlx(args.model, args.force)
    else:
        build_ct2(args.model, args.force)
    print("\nAll set. Select it with:  python server.py --model cantonese")


if __name__ == "__main__":
    main()
