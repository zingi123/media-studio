"""Standalone environment probe. Run by diagnostics.run_probe() in a SEPARATE process (python -I-free, no app imports) so a broken
native library (e.g. a torch / torchaudio mismatch) can crash only the probe, never MediaStudio.
Prints ONE JSON document on stdout. Every step is wrapped: a failure is recorded with its exception, never raised."""
import importlib
import importlib.metadata as md
import json
import os
import platform
import re
import sys
import time

PKGS = ["torch", "torchaudio", "torchvision", "torchcodec", "pyannote.audio", "pyannote.core", "pyannote.pipeline", "pyannote.metrics",
        "openai-whisper", "faster-whisper", "ctranslate2", "huggingface_hub", "google-genai", "anthropic", "numpy", "scipy", "librosa",
        "soundfile", "numba", "speechbrain", "transformers", "tiktoken", "paddleocr", "paddlepaddle", "rapidocr-onnxruntime", "onnxruntime",
        "easyocr", "opencv-python", "PySide6", "edge-tts", "keyring", "psutil", "packaging", "imageio-ffmpeg", "yt-dlp", "funasr", "Pillow"]
ROOTS = ["pyannote.audio", "openai-whisper", "faster-whisper", "torchaudio", "speechbrain", "huggingface_hub"]
TORCH_FAMILY = {"torch", "torchaudio", "torchvision", "torchcodec"}

out = {"python": {"executable": sys.executable, "version": platform.python_version(), "implementation": platform.python_implementation(),
                  "platform": platform.platform(), "machine": platform.machine(), "prefix": sys.prefix, "base_prefix": getattr(sys, "base_prefix", sys.prefix),
                  "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix), "virtual_env": os.environ.get("VIRTUAL_ENV", "")},
       "packages": {}, "steps": {}, "requires": {}, "installed": {}, "dependents": []}


def step(name, fn):
    t0 = time.time()
    try:
        r = fn()
        out["steps"][name] = {"ok": True, "ms": int((time.time() - t0) * 1000), **(r if isinstance(r, dict) else {})}
    except BaseException as e:  # noqa
        out["steps"][name] = {"ok": False, "ms": int((time.time() - t0) * 1000), "error": f"{type(e).__name__}: {str(e)[:300]}"}


def ver(p):
    try:
        return md.version(p)
    except Exception:
        return None


for p in PKGS:
    out["packages"][p] = ver(p)

# ---- metadata: declared requirements of the key packages + every installed distribution that depends on torch*
def norm(n):
    return re.sub(r"[-_.]+", "-", n).lower()

for r in ROOTS:
    try:
        out["requires"][r] = md.requires(r) or []
    except Exception:
        out["requires"][r] = None
names = set()
for reqs in out["requires"].values():
    for line in reqs or []:
        m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line)
        if m:
            names.add(m.group(1))
for n in names:
    v = ver(n)
    if v:
        out["installed"][norm(n)] = v
for d in md.distributions():
    try:
        dn = d.metadata["Name"]
        for line in d.requires or []:
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*(.*)", line)
            if m and norm(m.group(1)) in TORCH_FAMILY and "extra ==" not in line:
                out["dependents"].append({"package": dn, "version": d.version, "requirement": line.strip()})
    except Exception:
        pass
for f in TORCH_FAMILY:
    v = ver(f)
    if v:
        out["installed"][f] = v

# ---- torch
def _torch():
    import torch
    t0 = time.time()
    x = torch.randn(256, 256)
    s = float((x @ x).sum())
    ms = int((time.time() - t0) * 1000)
    cuda = bool(torch.cuda.is_available())
    return {"version": torch.__version__, "cuda_build": torch.version.cuda, "cuda_available": cuda,
            "device": torch.cuda.get_device_name(0) if cuda else "", "threads": torch.get_num_threads(), "cpu_matmul_ms": ms,
            "vram_mb": int(torch.cuda.get_device_properties(0).total_memory / 2**20) if cuda else 0}
step("torch", _torch)

def _ta():
    import torch, torchaudio
    w = torch.randn(1, 16000)
    r = torchaudio.functional.resample(w, 16000, 8000)
    assert r.shape[-1] == 8000, f"resample returned {tuple(r.shape)}"
    torchaudio.transforms.MelSpectrogram(sample_rate=16000)(w)
    res = {"version": torchaudio.__version__}
    try:
        res["backends"] = list(torchaudio.list_audio_backends())
    except Exception as e:  # noqa - removed in newer torchaudio
        res["backends_note"] = f"{type(e).__name__}"
    return res
if out["steps"]["torch"]["ok"]:
    step("torchaudio", _ta)
else:
    out["steps"]["torchaudio"] = {"ok": False, "skipped": True, "error": "torch does not import"}

def _pya_import():
    from pyannote.audio import Pipeline  # noqa
    import pyannote.audio
    return {"version": getattr(pyannote.audio, "__version__", ver("pyannote.audio"))}
def _pya_audio():
    import torch
    from pyannote.audio.core.io import Audio
    a = Audio(sample_rate=16000, mono="downmix")
    wav, sr = a({"waveform": torch.randn(1, 32000), "sample_rate": 16000})
    return {"samples": int(wav.shape[-1]), "sr": int(sr)}
if out["packages"].get("pyannote.audio"):
    step("pyannote_import", _pya_import)
    if out["steps"]["pyannote_import"]["ok"]:
        step("pyannote_audio_io", _pya_audio)

def _whisper():
    import whisper
    from whisper.audio import log_mel_spectrogram  # noqa
    assert hasattr(whisper, "load_model")
    return {"version": getattr(whisper, "__version__", ver("openai-whisper")), "models": list(whisper.available_models())}
if out["packages"].get("openai-whisper"):
    step("openai_whisper", _whisper)

def _fw():
    import faster_whisper
    return {"version": getattr(faster_whisper, "__version__", ver("faster-whisper"))}
if out["packages"].get("faster-whisper"):
    step("faster_whisper", _fw)

for label, mod in (("google_genai", "google.genai"), ("anthropic_sdk", "anthropic"), ("huggingface_hub", "huggingface_hub")):
    pk = {"google_genai": "google-genai", "anthropic_sdk": "anthropic", "huggingface_hub": "huggingface_hub"}[label]
    if out["packages"].get(pk):
        step(label, lambda m=mod: (importlib.import_module(m), {})[1])

# ---- OCR packages: import only the light ones; paddle is imported lazily by the app, so only metadata is reported
if out["packages"].get("rapidocr-onnxruntime"):
    step("rapidocr", lambda: (importlib.import_module("rapidocr_onnxruntime"), {})[1])
if out["packages"].get("paddlepaddle") and out["packages"].get("paddleocr"):
    def _paddle():
        import paddle
        return {"paddle_version": paddle.__version__}
    step("paddle", _paddle)

# ---- machine
try:
    import psutil
    v = psutil.virtual_memory()
    out["machine"] = {"cpu_count": os.cpu_count(), "ram_total_mb": int(v.total / 2**20), "ram_available_mb": int(v.available / 2**20)}
except Exception:
    out["machine"] = {"cpu_count": os.cpu_count(), "ram_total_mb": None, "ram_available_mb": None}
print(json.dumps(out))
