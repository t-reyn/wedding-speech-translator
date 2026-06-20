"""One-time model download + conversion. Run after installing requirements.

Downloads/builds, in order:
  1. NLLB model (config.json -> mt.hf_model) converted to CTranslate2 int8
  2. Whisper prefetch, so first run at the venue is instant
  3. Smoke-tests the translation pipeline in both directions

NLLB is fetched with a hand-rolled resumable downloader (urllib + Range requests)
rather than huggingface_hub's snapshot_download: on this network, large single-file
models (e.g. nllb-200-distilled-1.3B's ~5 GB pytorch_model.bin) reproducibly hung
huggingface_hub at 0 B/s for many minutes with no exception. A short per-request
socket timeout plus Range-based resume turns stalls into quick, automatic retries.
"""

import json
import platform
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
_MT = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["mt"]
NLLB_HF = _MT.get("hf_model", "facebook/nllb-200-distilled-1.3B")
NLLB_DIR = ROOT / _MT["model_dir"]

HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{file}"
HF_API = "https://huggingface.co/api/models/{repo}"
SOCKET_TIMEOUT = 30


def _fetch_json(url, retries=10):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=SOCKET_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as e:
            print(f"    retry {attempt + 1}/{retries} fetching {url} ({e})")
            time.sleep(2)
    raise RuntimeError(f"Failed to fetch {url}")


def _download_with_resume(url, dest, retries=100):
    """GET url -> dest, resuming via Range requests after stalls/drops."""
    tmp = dest.with_name(dest.name + ".part")
    last_logged_mb = -1
    for attempt in range(retries):
        existing = tmp.stat().st_size if tmp.exists() else 0
        req = urllib.request.Request(url)
        if existing:
            req.add_header("Range", f"bytes={existing}-")
        try:
            with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as resp:
                resumed = existing and resp.status == 206
                mode = "ab" if resumed else "wb"
                downloaded = existing if resumed else 0
                content_len = resp.headers.get("Content-Length")
                total = (downloaded + int(content_len)) if content_len else None
                with open(tmp, mode) as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        mb = downloaded // (1 << 20)
                        if mb != last_logged_mb and mb % 25 == 0:
                            last_logged_mb = mb
                            if total:
                                print(f"    {dest.name}: {downloaded/total*100:5.1f}%"
                                      f"  ({downloaded // (1<<20)} / {total // (1<<20)} MB)")
                            else:
                                print(f"    {dest.name}: {downloaded // (1<<20)} MB")
            tmp.rename(dest)
            return
        except (urllib.error.URLError, OSError) as e:
            print(f"    {dest.name}: retry {attempt + 1}/{retries} after {e}")
            time.sleep(2)
    raise RuntimeError(f"Failed to download {url} after {retries} retries")


def download_repo(repo, dest_dir):
    skip = {".gitattributes"}
    info = _fetch_json(HF_API.format(repo=repo))
    files = [s["rfilename"] for s in info["siblings"]
             if s["rfilename"] not in skip and not s["rfilename"].endswith(".md")]
    dest_dir.mkdir(parents=True, exist_ok=True)
    for fname in files:
        dest = dest_dir / fname
        if dest.exists() and not (dest.with_name(dest.name + ".part")).exists():
            continue
        print(f"  fetching {fname}...")
        _download_with_resume(HF_RESOLVE.format(repo=repo, file=fname), dest)
    return dest_dir


def build_nllb():
    marker = NLLB_DIR / "source_model.txt"
    if (NLLB_DIR / "model.bin").exists() and marker.exists() \
            and marker.read_text(encoding="utf-8").strip() == NLLB_HF:
        print(f"NLLB ({NLLB_HF}) already converted at {NLLB_DIR}")
        return
    print(f"Downloading + converting {NLLB_HF} (one-time, can take a while)...")
    from ctranslate2.converters import TransformersConverter
    from transformers import AutoTokenizer
    src_dir = ROOT / "models" / "_src" / NLLB_HF.replace("/", "--")
    download_repo(NLLB_HF, src_dir)
    NLLB_DIR.parent.mkdir(exist_ok=True)
    TransformersConverter(str(src_dir)).convert(str(NLLB_DIR), quantization="int8", force=True)
    AutoTokenizer.from_pretrained(str(src_dir)).save_pretrained(str(NLLB_DIR))
    marker.write_text(NLLB_HF, encoding="utf-8")
    shutil.rmtree(src_dir, ignore_errors=True)
    print(f"NLLB ready at {NLLB_DIR}")


def prefetch_whisper():
    import json
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["asr"]
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        from huggingface_hub import snapshot_download
        print(f"Prefetching {cfg['mlx_model']}...")
        snapshot_download(cfg["mlx_model"])
    else:
        try:
            from faster_whisper.utils import download_model
            print(f"Prefetching faster-whisper {cfg['fw_model']}...")
            download_model(cfg["fw_model"])
        except ImportError:
            print("faster-whisper not installed; skipping Whisper prefetch.")
    print("Whisper model cached.")


def smoke_test():
    import json
    from translate import CaptionTranslator
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    langs = cfg["languages"]
    t = CaptionTranslator(NLLB_DIR, cfg["mt"])
    en = "Please raise your glasses and join me in a toast to the happy couple."
    zh = "多謝大家今晚抽空嚟同我哋一齊慶祝。"
    print("\nSmoke test:")
    print(f"  EN->ZH  {t.translate(en, 'eng_Latn', langs['nllb_tgt']['yue'])}")
    print(f"  EN->VI  {t.translate(en, 'eng_Latn', langs['nllb_tgt']['vi'])}")
    print(f"  YUE->EN {t.translate(zh, langs['nllb_src']['yue'], 'eng_Latn')}")
    print("\nAll set. Run: python server.py")


if __name__ == "__main__":
    build_nllb()
    prefetch_whisper()
    smoke_test()
