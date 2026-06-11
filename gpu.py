"""Windows-only: add the nvidia-*-cu12 cuBLAS/cuDNN DLL dirs to the search path
so CTranslate2 (faster-whisper + NLLB) can run on the GPU. The pip wheel ships
the DLLs but not on PATH. No-op on the Mac/MLX target, which uses no CUDA.
"""

import os
import sys

_done = False


def enable_cuda_dlls():
    global _done
    if _done or sys.platform != "win32":
        _done = True
        return
    _done = True
    try:
        import nvidia
    except ImportError:
        return
    # nvidia is a PEP 420 namespace package: __file__ is None, use __path__.
    bins = []
    for base in list(getattr(nvidia, "__path__", [])):
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            binp = os.path.join(base, sub, "bin")
            if os.path.isdir(binp):
                bins.append(binp)
    for binp in bins:
        try:
            os.add_dll_directory(binp)
        except OSError:
            pass
    # add_dll_directory alone isn't honoured by CTranslate2's transitive DLL
    # load; prepending PATH is what actually makes cublas64_12.dll resolve.
    if bins:
        os.environ["PATH"] = os.pathsep.join(bins) + os.pathsep + os.environ.get("PATH", "")
