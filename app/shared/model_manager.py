"""
Model manager - CPU-first, memory-aware.

 * Models are loaded ON DEMAND (`get`) and cached; by default only ONE heavy model stays in memory (loading a second
   heavy model first unloads the others) - several big models are never held at once unless workers > 1 is configured.
 * `unload` / `unload_all` free a model safely (gc + CUDA cache if CUDA exists). External caches (pyannote pipeline,
   speaker embedder, emotion models) register an unloader so unload_all() reaches them too.
 * Memory check before loading: ok / warn / block, from the real available RAM (psutil, Windows API or /proc/meminfo).
 * Time estimates are given ONLY from measurements made on this machine (record_speed) - never guessed.
"""
import gc
import json
import os
import threading
import time
from pathlib import Path

from .paths import data_dir

# Approximate memory needs of OpenAI Whisper models in MB (the official README VRAM figures; CPU RAM is similar). Rough guide only.
WHISPER_MB = {"tiny": 1000, "base": 1000, "small": 2000, "medium": 5000, "large": 10000, "large-v2": 10000, "large-v3": 10000, "turbo": 6000}
PYANNOTE_MB = 2000          # rough: segmentation + embedding models + clustering on CPU
BLOCK_RATIO = 0.7           # available RAM < 0.7 x need  -> refuse to load (likely out-of-memory)
WARN_RATIO = 1.3            # available RAM < 1.3 x need  -> warn


class InsufficientMemory(RuntimeError):
    pass


def total_ram_mb():
    return _mem()[0]


def available_ram_mb():
    return _mem()[1]


def _mem():
    """(total_mb, available_mb) - (None, None) when it cannot be read."""
    try:
        import psutil
        v = psutil.virtual_memory()
        return v.total / 2**20, v.available / 2**20
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            s = MS(); s.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return s.ullTotalPhys / 2**20, s.ullAvailPhys / 2**20
        d = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")[0], line.split(":")[1].split()[0]
            d[k] = int(v) / 1024
        return d["MemTotal"], d.get("MemAvailable", d.get("MemFree"))
    except Exception:
        return None, None


def check_memory(need_mb, avail_mb=None, what="model"):
    """-> (level, message) with level 'ok' | 'warn' | 'block' | 'unknown'."""
    avail = available_ram_mb() if avail_mb is None else avail_mb
    if not need_mb:
        return "ok", ""
    if avail is None:
        return "unknown", f"Free RAM could not be read; {what} needs about {need_mb / 1024:.1f} GB."
    if avail < need_mb * BLOCK_RATIO:
        return "block", (f"Not enough free RAM for {what}: about {need_mb / 1024:.1f} GB needed, {avail / 1024:.1f} GB free. "
                         "Close other programs or choose a smaller model.")
    if avail < need_mb * WARN_RATIO:
        return "warn", (f"{what} needs about {need_mb / 1024:.1f} GB and only {avail / 1024:.1f} GB is free - "
                        "it may be slow or the PC may start swapping.")
    return "ok", ""


class ModelManager:
    def __init__(self, max_heavy=1):
        self.max_heavy = max_heavy
        self._m = {}                       # key -> {"obj","heavy","mb","t"}
        self._ext = {}                     # name -> unloader()
        self._lock = threading.RLock()
        self.warn = None                   # optional callback(str)

    def set_max_heavy(self, n):
        self.max_heavy = max(1, int(n))

    def register_external(self, name, unloader):
        self._ext[name] = unloader

    def loaded(self):
        return sorted(self._m)

    def get(self, key, loader, est_mb=0, heavy=True, avail_mb=None):
        """Return the cached model, or load it (after the memory check and after unloading other heavy models)."""
        with self._lock:
            if key in self._m:
                self._m[key]["t"] = time.time()
                return self._m[key]["obj"]
            if heavy:
                while sum(1 for v in self._m.values() if v["heavy"]) >= self.max_heavy:
                    victim = min((k for k, v in self._m.items() if v["heavy"]), key=lambda k: self._m[k]["t"])
                    self.unload(victim)
                self._run_external()            # also free caches the manager does not own (pyannote, embedder...)
                level, msg = check_memory(est_mb, avail_mb, f"model '{key if isinstance(key, str) else key[1]}'")
                if level == "block":
                    raise InsufficientMemory(msg)
                if level in ("warn", "unknown") and msg and self.warn:
                    self.warn(msg)
            obj = loader()
            self._m[key] = {"obj": obj, "heavy": heavy, "mb": est_mb, "t": time.time()}
            return obj

    def _run_external(self):
        for fn in list(self._ext.values()):
            try:
                fn()
            except Exception:
                pass

    def unload(self, key):
        with self._lock:
            ent = self._m.pop(key, None)
        del ent
        self._free()

    def unload_all(self):
        with self._lock:
            self._m.clear()
        self._run_external()
        self._free()

    @staticmethod
    def _free():
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


MM = ModelManager()


# ------------------------------------------------------------------ CPU configuration
def configure_cpu(threads=0):
    """Apply the configured CPU thread count to PyTorch (0 = leave PyTorch's default). Returns the value in use or None."""
    try:
        import torch
        if threads and threads > 0:
            torch.set_num_threads(int(threads))
        return torch.get_num_threads()
    except Exception:
        return None


def pick_device(requested="auto"):
    """'cpu' unless CUDA is really usable. requested: 'auto' | 'cpu' | 'cuda' (cuda falls back to cpu with a reason)."""
    reason = ""
    cuda = False
    try:
        import torch
        cuda = bool(torch.cuda.is_available())
        if not cuda and requested == "cuda":
            reason = "CUDA was requested but this PyTorch build/hardware has no CUDA - using CPU."
    except Exception:
        if requested == "cuda":
            reason = "CUDA was requested but PyTorch is not importable - using CPU."
    if requested == "cpu":
        return "cpu", ""
    return ("cuda" if cuda else "cpu"), reason


# ------------------------------------------------------------------ measured speed -> estimates (never guessed)
def _perf_file():
    return data_dir() / "perf.json"


def record_speed(task, key, audio_seconds, wall_seconds):
    """Remember how long `task` took per second of media on THIS machine (e.g. task='whisper', key='small|cpu')."""
    if audio_seconds <= 0 or wall_seconds <= 0:
        return
    try:
        d = json.loads(_perf_file().read_text(encoding="utf-8")) if _perf_file().exists() else {}
    except Exception:
        d = {}
    e = d.setdefault(f"{task}:{key}", {"n": 0, "factor": 0.0})
    e["factor"] = (e["factor"] * e["n"] + wall_seconds / audio_seconds) / (e["n"] + 1)     # running mean: seconds per media second
    e["n"] += 1
    _perf_file().write_text(json.dumps(d, indent=1), encoding="utf-8")


def estimate_seconds(task, key, audio_seconds):
    """-> estimated seconds, or None when this machine has no measurement yet (no guessing)."""
    try:
        e = json.loads(_perf_file().read_text(encoding="utf-8")).get(f"{task}:{key}")
    except Exception:
        return None
    return round(e["factor"] * audio_seconds) if e and e["n"] else None
