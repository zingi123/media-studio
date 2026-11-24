"""
Zero-shot voice cloning with VoxCPM2 (OpenBMB). NO training / fine-tuning happens here.

A "cloned voice" is just: a cleaned reference recording + a small JSON file. VoxCPM2 reads the reference at
generation time (reference_wav_path = isolated-reference cloning, no transcript needed) and speaks any text
(Khmer is one of its 30 supported languages) in that timbre.

Storage (permanent, outside any project - removed ONLY by delete_voice()):
    <MediaStudio home>/cloned_voices/<id>/voice.json      nickname, name key, hash, durations ...
    <MediaStudio home>/cloned_voices/<id>/reference.wav   cleaned mono 16 kHz reference (what the model hears)
    <MediaStudio home>/cloned_voices/<id>/original.<ext>  untouched copy of the imported file

Requires (only for cloning): an NVIDIA GPU with CUDA, `pip install voxcpm` (pulls torch). See requirements file.
"""
import hashlib
import json
import re
import shutil
import threading
import time
import uuid
import wave
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from ..shared.ffmpeg_util import Cancelled, probe, run_ffmpeg
from ..shared.paths import data_dir

PREFIX = "Clone - "                    # key of every cloned voice: "Clone - <nickname>" (never collides with catalog names)
MODEL_ID = "openbmb/VoxCPM2"
REF_SR = 16000                         # VoxCPM2 encodes reference audio at 16 kHz
ALLOWED_EXT = (".mp3", ".wav")
MIN_SEC, MAX_SEC = 2.0, 30.0           # shorter = too little timbre; longer is trimmed (slower + more VRAM, no benefit)
MAX_BYTES = 200 * 1024 * 1024
MIN_VRAM_GB = 6.0                      # VoxCPM2 is a 2B model; below this we warn (it may still run)
NICK_MAX = 30
CFG_VALUE, STEPS = 2.0, 10             # VoxCPM2 defaults


class CloneError(Exception):
    """Problem the user can understand and act on (bad file, duplicate, missing GPU...)."""


class CloneFatal(CloneError):
    """Retrying will not help (no GPU, model not installed, voice deleted, out of GPU memory)."""


# ------------------------------------------------------------------ storage
def clone_root() -> Path:
    p = data_dir() / "cloned_voices"
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_clone_name(name) -> bool:
    return str(name or "").startswith(PREFIX)


def _write_json(path: Path, data: dict):          # atomic: a crash never leaves a half-written voice.json
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def list_voices() -> list:
    """All saved cloned voices (valid ones only), oldest first. Each = dict from voice.json + 'dir'."""
    out = []
    root = clone_root()
    for d in sorted(root.iterdir()):
        j = d / "voice.json"
        if not (d.is_dir() and j.exists() and (d / "reference.wav").exists()):
            continue
        try:
            v = json.loads(j.read_text(encoding="utf-8"))
            v["dir"] = str(d)
            out.append(v)
        except Exception:
            continue                                # damaged file: skip, never delete automatically
    out.sort(key=lambda v: v.get("created", 0))
    return out


def get_voice(name_or_id):
    for v in list_voices():
        if name_or_id in (v.get("name"), v.get("id")):
            return v
    return None


def reference_path(name_or_id) -> str:
    v = get_voice(name_or_id)
    if not v:
        raise CloneFatal(f"Cloned voice '{name_or_id}' no longer exists (it was deleted). Choose another Voice Type.")
    return str(Path(v["dir"]) / "reference.wav")


def clean_nick(nick) -> str:
    return re.sub(r"\s+", " ", str(nick or "")).strip()[:NICK_MAX]


def name_for(nick) -> str:
    return PREFIX + clean_nick(nick)


def sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_duplicate(nick=None, ref_hash=None):
    """Existing voice with the same nickname (case-insensitive) or the same reference recording, else None."""
    key = name_for(nick).lower() if nick else None
    for v in list_voices():
        if key and v.get("name", "").lower() == key:
            return v
        if ref_hash and v.get("ref_sha256") == ref_hash:
            return v
    return None


def save_voice(nick, prepared: dict) -> dict:
    """Persist a prepared reference (see prepare_reference) as a new cloned voice. Raises CloneError on duplicates."""
    nick = clean_nick(nick)
    if not nick:
        raise CloneError("Enter a Nickname for the cloned voice.")
    dup = find_duplicate(nick, prepared["sha256"])
    if dup:
        why = "that Nickname" if dup["name"].lower() == name_for(nick).lower() else "this same recording"
        raise CloneError(f"A cloned voice already uses {why}: '{dup['name']}'. Nothing was overwritten.")
    ref, src = Path(prepared["wav"]), Path(prepared["source"])
    if not ref.exists() or not src.exists():
        raise CloneError("The prepared audio is missing - import the file again.")
    vid = uuid.uuid4().hex[:12]
    d = clone_root() / vid
    d.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(ref, d / "reference.wav")
        shutil.copy2(src, d / ("original" + src.suffix.lower()))
        meta = {"id": vid, "nick": nick, "name": name_for(nick), "created": time.time(), "engine": "voxcpm2",
                "model": MODEL_ID, "orig_name": src.name, "ref_sha256": prepared["sha256"],
                "duration": round(prepared["duration"], 2), "ref_sr": REF_SR, "trimmed": bool(prepared.get("trimmed"))}
        _write_json(d / "voice.json", meta)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)         # only the folder we just created
        raise
    meta["dir"] = str(d)
    return meta


def delete_voice(name_or_id) -> bool:
    """Permanently remove ONE cloned voice (reference, original copy, json). Never touches other voices."""
    if is_in_use(name_or_id):
        raise CloneError("This voice is being used by Generate Voice right now. Wait until it finishes, then delete it.")
    v = get_voice(name_or_id)
    if not v:
        return False
    d = Path(v["dir"]).resolve()
    if d.parent != clone_root().resolve():            # safety: only ever delete a direct child of cloned_voices/
        raise CloneError("Refusing to delete a folder outside the cloned voices storage.")
    try:
        shutil.rmtree(d)
    except OSError as e:
        raise CloneError(f"Could not delete the voice files (is the sample still playing?): {e}") from e
    for f in (data_dir() / "work" / "voice_demo").glob(f"clone_{v['id']}_*.wav"):   # cached demos of THIS voice only
        try:
            f.unlink()
        except OSError:
            pass
    return True


# ------------------------------------------------------------------ in-use tracking (blocks delete during generation)
_use_lock = threading.Lock()
_in_use = {}


@contextmanager
def in_use(names):
    names = [n for n in set(names) if is_clone_name(n)]
    with _use_lock:
        for n in names:
            _in_use[n] = _in_use.get(n, 0) + 1
    try:
        yield
    finally:
        with _use_lock:
            for n in names:
                _in_use[n] = _in_use.get(n, 1) - 1
                if _in_use[n] <= 0:
                    _in_use.pop(n, None)


def is_in_use(name_or_id) -> bool:
    v = get_voice(name_or_id)
    keys = {name_or_id} | ({v["name"], v["id"]} if v else set())
    with _use_lock:
        return any(k in _in_use for k in keys)


# ------------------------------------------------------------------ audio preparation
def _sniff(path: Path) -> str:
    """Real format from the first bytes (the extension alone can lie)."""
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    return ""


def validate_source(path) -> Path:
    p = Path(path)
    if not p.is_file():
        raise CloneError("File not found.")
    if p.suffix.lower() not in ALLOWED_EXT:
        raise CloneError("Only MP3 and WAV files are supported.")
    size = p.stat().st_size
    if size < 2048:
        raise CloneError("The file is empty or too small to be audio.")
    if size > MAX_BYTES:
        raise CloneError(f"The file is larger than {MAX_BYTES // 1024 // 1024} MB.")
    kind = _sniff(p)
    if not kind:
        raise CloneError("This file is not a valid MP3 or WAV recording (wrong or damaged format).")
    return p


def prepare_reference(src, work=None, progress=None, cancel=None) -> dict:
    """Validate + convert any MP3/WAV into the model's reference format (mono, 16 kHz, 16-bit WAV, silence-trimmed).
    The original file is only READ. Returns {'wav','source','sha256','duration','trimmed','warnings'}."""
    p = validate_source(src)
    say = progress or (lambda *_: None)
    say("Checking audio file...")
    info = probe(p)
    if not info["has_audio"] or info["duration"] <= 0:
        raise CloneError("No audio could be read from this file.")
    work = Path(work) if work else data_dir() / "work" / "clone_prepare"
    work.mkdir(parents=True, exist_ok=True)
    out = work / f"ref_{uuid.uuid4().hex[:8]}.wav"
    sil = "silenceremove=start_periods=1:start_threshold=-50dB"
    af = f"{sil},areverse,{sil},areverse"                            # trim silence at start and end
    args = ["-y", "-i", str(p), "-vn", "-ac", "1", "-ar", str(REF_SR)]       # stereo -> mono, resample
    if info["duration"] > MAX_SEC + 5:
        args += ["-t", str(MAX_SEC + 5)]                              # read at most a bit over the cap
    say("Converting to mono WAV (16 kHz)...")
    run_ffmpeg(args + ["-af", af, "-c:a", "pcm_s16le", str(out)], cancel=cancel)
    if cancel and cancel():
        raise Cancelled()
    with wave.open(str(out), "rb") as w:
        n, sr = w.getnframes(), w.getframerate()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    if len(pcm) == 0:
        raise CloneError("The recording is silent.")
    dur = len(pcm) / sr
    warns, trimmed = [], False
    if dur < MIN_SEC:
        raise CloneError(f"Too short: {dur:.1f}s of speech. Use at least {MIN_SEC:.0f} seconds of clear speech (5-15 s is ideal).")
    if dur > MAX_SEC:
        pcm, dur, trimmed = pcm[:int(MAX_SEC * sr)], MAX_SEC, True
        warns.append(f"Long recording: only the first {MAX_SEC:.0f} s are used as the reference.")
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    peak = float(np.max(np.abs(pcm)))
    if rms < 10 ** (-52 / 20):
        raise CloneError("The recording is almost silent. Use a louder, clearer recording.")
    if peak >= 0.999 and float(np.mean(np.abs(pcm) > 0.995)) > 0.01:
        warns.append("The recording is clipped (distorted); the clone may sound rough.")
    if rms < 10 ** (-38 / 20):
        warns.append("The recording is very quiet; the clone quality may suffer.")
    if peak < 0.3:                                                     # gentle peak normalisation to ~ -3 dBFS
        pcm = np.clip(pcm * (0.7 / max(peak, 1e-4)), -1.0, 1.0)
    with wave.open(str(out), "wb") as w:                              # write the checked / trimmed / normalised version
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((pcm * 32767.0).astype(np.int16).tobytes())
    say("Audio is ready.")
    return {"wav": str(out), "source": str(p), "sha256": sha256_of(p), "duration": dur, "trimmed": trimmed,
            "warnings": warns}


# ------------------------------------------------------------------ GPU / engine
def check_gpu() -> dict:
    """{'ok': bool, 'message': str, 'name': str, 'vram_gb': float}. Never raises."""
    if is_online():
        try:
            import gradio_client  # noqa: F401
        except Exception:
            return {"ok": False, "name": "Hugging Face", "vram_gb": 0.0, "online": True,
                    "message": "Hugging Face (Online) needs the 'gradio_client' package. Install it with:  pip install gradio_client   "
                               "then restart the app."}
        msg = f"Hugging Face (Online): Space '{_hf_space()}'. Your reference voice is uploaded to that Space."
        if not _hf_token():
            msg += " No Hugging Face token saved (Settings) - anonymous use has a very small quota."
        return {"ok": True, "name": "Hugging Face", "vram_gb": 0.0, "online": True, "message": msg}
    try:
        import torch
    except Exception:
        return {"ok": False, "name": "", "vram_gb": 0.0,
                "message": "Voice cloning needs PyTorch + VoxCPM2. Install them with:  pip install voxcpm   "
                           "(see requirements_voice_clone.txt), then restart the app."}
    try:
        if _device_pref() == "cpu" or not torch.cuda.is_available():
            # No (usable) GPU: run VoxCPM2 on the CPU instead. Works anywhere, just slower.
            ram = 0.0
            try:
                import psutil
                ram = psutil.virtual_memory().total / 1024 ** 3
            except Exception:
                pass
            msg = "No GPU found - using the CPU. Cloning works but is slow (can take minutes per line); keep the app open."
            if 0 < ram < 8:
                msg += f" Warning: only {ram:.1f} GB RAM, 16 GB is recommended."
            return {"ok": True, "name": "CPU", "vram_gb": 0.0, "cpu": True, "message": msg}
        pr = torch.cuda.get_device_properties(0)
        gb = pr.total_memory / 1024 ** 3
        msg = f"GPU: {pr.name} ({gb:.1f} GB)"
        if gb < MIN_VRAM_GB:
            msg += f" - warning: under {MIN_VRAM_GB:.0f} GB VRAM, cloning may run out of memory."
        return {"ok": True, "name": pr.name, "vram_gb": gb, "message": msg}
    except Exception as e:  # noqa
        return {"ok": False, "name": "", "vram_gb": 0.0, "message": f"Could not query the GPU: {e}"}


_gpu_lock = threading.RLock()          # one VoxCPM2 job at a time (one GPU, one loaded model)
_clone_job = threading.Lock()          # only ONE clone job at a time, across every dialog
_engine = None


def try_begin_clone_job() -> bool:
    return _clone_job.acquire(blocking=False)


def end_clone_job():
    try:
        _clone_job.release()
    except RuntimeError:
        pass


def _setting(key, default=""):
    try:
        from ..shared.settings import settings
        v = settings().get(key)
        return default if v in (None, "") else v
    except Exception:
        return default


def _device_pref() -> str:
    """'auto' (default: GPU if present, else CPU) | 'cpu' | 'cuda' | 'hf' (Hugging Face online) via settings 'voxcpm_device'."""
    v = str(_setting("voxcpm_device", "auto")).lower()
    return v if v in ("cpu", "cuda", "hf") else "auto"


def is_online() -> bool:
    return _device_pref() == "hf"


def set_device(dev: str):
    """Choose the clone engine ('cpu' | 'cuda' | 'hf'), save it, and drop a loaded local model if the engine changed."""
    from ..shared.settings import settings
    dev = dev if dev in ("cpu", "cuda", "hf") else "auto"
    old = _device_pref()
    s = settings(); s.set("voxcpm_device", dev); s.save()
    if old != dev:
        unload_engine()


def _use_cuda() -> bool:
    try:
        import torch
        return _device_pref() in ("auto", "cuda") and torch.cuda.is_available()
    except Exception:
        return False


def _steps() -> int:
    """Diffusion steps: fewer = faster. Default 10 on GPU, 6 on CPU (override with settings 'voxcpm_steps')."""
    try:
        return max(2, min(30, int(_setting("voxcpm_steps", STEPS if _use_cuda() else 6))))
    except Exception:
        return STEPS


def _model_source() -> str:
    try:
        from ..shared.settings import settings
        s = settings()
        p = str(s.get("voxcpm_model") or "").strip()
        tok = str(s.get("hf_token") or "").strip()
        import os
        if tok:
            os.environ["HF_TOKEN"] = tok                 # always the CURRENT token (Update in Settings takes effect now)
        else:
            os.environ.pop("HF_TOKEN", None)             # Clear in Settings really removes it
        return p or MODEL_ID
    except Exception:
        return MODEL_ID


def _free_gpu():
    try:
        import gc, torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def unload_engine():
    global _engine
    with _gpu_lock:
        _engine = None
        _free_gpu()


def _translate_error(e: Exception) -> CloneError:
    name, msg = e.__class__.__name__, str(e)
    if "OutOfMemory" in name or "out of memory" in msg.lower():
        _free_gpu()
        return CloneFatal("The GPU ran out of memory. Close other GPU programs (games, browsers with video, other AI "
                          "tools) and try again, or use a GPU with more VRAM.")
    if isinstance(e, CloneError):
        return e
    low = msg.lower()
    if is_online():
        if any(k in low for k in ("quota", "exceeded", "rate limit", "too many requests", "429", "zerogpu")):
            return CloneFatal("Hugging Face quota is used up (or too many requests). Wait for the quota to reset, save a "
                              "Hugging Face token in Settings (a logged-in account gets more quota), or switch the engine "
                              "to CPU / GPU.")
        if any(k in low for k in ("401", "403", "unauthorized", "invalid token", "forbidden", "invalid credentials")):
            return CloneFatal("Hugging Face refused the token. Check or Update the Hugging Face token in Settings.")
        if any(k in low for k in ("404", "not found", "no api", "cannot find a function")):
            return CloneFatal(f"The Hugging Face Space or its API was not found ('{_hf_space()}'). Check the 'hf_space' / "
                              f"'hf_api_name' values in settings.json. Details: {msg[:200]}")
        if any(k in low for k in ("sleeping", "building", "runtime error", "paused", "starting")):
            return CloneError("The Hugging Face Space is not running right now (sleeping, building or stopped). Try again in a minute.")
        if any(k in low for k in ("connect", "timed out", "timeout", "network", "name resolution", "unreachable")):
            return CloneError(f"Could not reach Hugging Face. Check your internet connection. ({name})")
    return CloneError(f"{name}: {msg}" if msg else name)


def get_engine(progress=None, cancel=None):
    """Load VoxCPM2 once (first run downloads ~5 GB of weights). Raises CloneFatal when it cannot work."""
    global _engine
    say = progress or (lambda *_: None)
    with _gpu_lock:
        if _engine is not None:
            return _engine
        g = check_gpu()
        if not g["ok"]:
            raise CloneFatal(g["message"])
        try:
            from voxcpm import VoxCPM
        except ImportError as e:
            raise CloneFatal("VoxCPM2 is not installed. Run:  pip install voxcpm   (needs Python 3.10-3.12, "
                             "PyTorch 2.5+ with CUDA 12+). Then restart the app.") from e
        except Exception as e:  # noqa
            raise CloneFatal(f"VoxCPM2 could not be imported: {e}") from e
        if cancel and cancel():
            raise Cancelled()
        src = _model_source()
        dev = "cuda" if _use_cuda() else "cpu"
        say(f"Loading VoxCPM2 model on the {'GPU' if dev == 'cuda' else 'CPU'}... "
            f"(first time: loading/downloading the model can take several minutes)")
        try:
            if dev == "cpu":
                import os, torch
                torch.set_num_threads(max(1, os.cpu_count() or 1))     # use every core
            try:
                _engine = VoxCPM.from_pretrained(src, load_denoiser=False, optimize=False, device=dev)
            except TypeError:                                          # older voxcpm without the device argument
                _engine = VoxCPM.from_pretrained(src, load_denoiser=False, optimize=False)
        except Exception as e:  # noqa
            _free_gpu()
            te = _translate_error(e)
            if isinstance(te, CloneFatal):               # e.g. out of GPU memory while loading
                raise te from e
            raise CloneFatal(f"Could not load the VoxCPM2 model ({src}): {te}. Check your internet connection "
                             f"for the first download, and free disk space (~5 GB).") from e
        say("Model loaded.")
        return _engine


# ------------------------------------------------------------------ Hugging Face (online)
def _hf_token() -> str:
    return str(_setting("hf_token", "")).strip()


def _hf_space() -> str:
    return str(_setting("hf_space", "openbmb/VoxCPM-Demo")).strip()


def _pick_endpoint(client, api_name=""):
    """Choose the Space endpoint + its parameter list from client.view_api(). `api_name` (settings) wins when set."""
    info = client.view_api(return_format="dict") or {}
    eps = (info.get("named_endpoints") or {})
    if api_name:
        key = api_name if api_name.startswith("/") else "/" + api_name
        if key not in eps:
            raise CloneFatal(f"The Space has no API named '{key}'. Available: {', '.join(eps) or 'none'}.")
        return key, eps[key].get("parameters") or []
    def score(item):
        """Best endpoint = one that takes a text box AND an audio file and is named like a generator.
        Judged by component TYPE (not label) so helper endpoints (ASR / toggles) never win."""
        name, e = item
        ps = e.get("parameters") or []
        comps = [str(p.get("component") or "").lower() for p in ps]
        has_audio = any("audio" in c for c in comps)
        has_text = any(c in ("textbox", "text", "textarea") for c in comps)
        n = name.lower()
        s = (3 if (has_audio and has_text) else 0) + (1 if has_audio else 0) + (1 if has_text else 0)
        if any(k in n for k in ("generate", "synth", "tts", "clone", "predict", "run_tts")):
            s += 4
        if any(k in n for k in ("asr", "toggle", "transcri", "recogni", "_on_", "example", "clear", "reset")):
            s -= 6
        return (s, comps.count("textbox") + comps.count("text"), len(ps))
    key, e = max(eps.items(), key=score)
    return key, e.get("parameters") or []


def _build_args(params, text, reference):
    """Map the endpoint's parameters onto (text, reference audio). Everything else gets the Space's own default.

    Matching uses the parameter NAME (e.g. 'text_input', 'control_instruction', 'reference_wav_path_input').
    The visible label is only a fallback: a label such as 'Target Text (start with (instruction) ...)' must not
    hide the real text box.
    """
    from gradio_client import handle_file
    skip = ("prompt", "reference", "transcript", "control", "instruction")
    pname = lambda p: str(p.get("parameter_name") or "").lower()  # noqa
    plabel = lambda p: str(p.get("label") or "").lower()          # noqa
    comp_of = lambda p: str(p.get("component") or p.get("type", {}).get("type", "") if isinstance(p.get("type"), dict) else p.get("component") or "").lower()  # noqa

    def is_text_comp(p):
        return comp_of(p) in ("textbox", "text", "textarea") or "textbox" in comp_of(p)

    def is_audio(p):
        c = comp_of(p)
        return c == "audio" or "audio" in c or "wav" in pname(p) or "audio" in plabel(p)

    # which parameter takes the text: prefer the one whose NAME says text and is not a prompt/control box
    text_idx = None
    for i, p in enumerate(params):
        if is_text_comp(p) and not is_audio(p) and not any(k in pname(p) for k in skip) and "text" in pname(p):
            text_idx = i; break
    if text_idx is None:
        for i, p in enumerate(params):
            if is_text_comp(p) and not is_audio(p) and not any(k in (pname(p) or plabel(p)) for k in skip):
                text_idx = i; break
    if text_idx is None:                                            # last resort: the first text box of all
        for i, p in enumerate(params):
            if is_text_comp(p) and not is_audio(p):
                text_idx = i; break
    # which parameter takes the reference voice: the first audio input
    ref_idx = next((i for i, p in enumerate(params) if is_audio(p)), None)

    if text_idx is None or ref_idx is None:
        seen = "; ".join(f"{pname(p) or '?'}({comp_of(p) or '?'})" for p in params) or "none"
        raise CloneFatal("Could not match this Space's inputs to (text + reference audio). "
                         f"Inputs found: {seen}. Set 'hf_space' / 'hf_api_name' in settings.json to a Space/endpoint "
                         "that takes text and a reference voice.")
    args = []
    for i, p in enumerate(params):
        if i == ref_idx:
            args.append(handle_file(reference))
        elif i == text_idx:
            args.append(text)
        elif p.get("parameter_has_default"):
            args.append(p.get("parameter_default"))
        else:
            args.append(None)
    return args


def _first_audio_path(res) -> str:
    """Gradio returns a path, a (sr, ...) tuple, a dict {'path':..}, or a list of these - find the audio file."""
    if isinstance(res, (str, Path)):
        return str(res)
    if isinstance(res, dict):
        return str(res.get("path") or res.get("name") or "")
    if isinstance(res, (list, tuple)):
        for x in res:
            f = _first_audio_path(x)
            if f and Path(f).exists():
                return f
    return ""


def _synthesize_online(text, reference, out_tmp, progress=None, cancel=None):
    """Speak `text` in the voice of `reference` on a Hugging Face Space -> mono WAV written to out_tmp."""
    say = progress or (lambda *_: None)
    try:
        from gradio_client import Client
    except Exception as e:  # noqa
        raise CloneFatal("Hugging Face (Online) needs the 'gradio_client' package:  pip install gradio_client") from e
    space, tok = _hf_space(), _hf_token()
    say(f"Hugging Face: connecting to {space}...")
    try:
        try:
            client = Client(space, token=tok or None, verbose=False)
        except TypeError:                                              # older gradio_client uses hf_token=
            client = Client(space, hf_token=tok or None, verbose=False)
        api, params = _pick_endpoint(client, str(_setting("hf_api_name", "")).strip())
        args = _build_args(params, text, reference)
        say("Hugging Face: generating voice online...")
        job = client.submit(*args, api_name=api)
        while not job.done():
            if cancel and cancel():
                try:
                    job.cancel()
                finally:
                    raise Cancelled()
            time.sleep(0.3)
        res = job.result()
    except Cancelled:
        raise
    except Exception as e:  # noqa
        raise _translate_error(e) from e
    src = _first_audio_path(res)
    if not src or not Path(src).exists():
        raise CloneError("Hugging Face returned no audio file.")
    run_ffmpeg(["-y", "-i", src, "-vn", "-ac", "1", "-c:a", "pcm_s16le", str(out_tmp)], cancel=cancel)   # any format -> WAV


# ------------------------------------------------------------------ synthesis
def _clean_text(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t.lstrip("(（").strip()          # a leading "(" would be read by VoxCPM2 as a voice-style instruction


def synthesize_clone(text, out_wav, reference, speed=1.0, progress=None, cancel=None, style="") -> str:
    """Speak `text` (Khmer) in the voice of `reference` (path of a reference.wav) -> WAV file at out_wav."""
    t = _clean_text(text)
    if not t:
        raise ValueError("Empty text")
    style = re.sub(r"[()（）]", " ", str(style or "")).strip()
    if style:                                                         # NEW: emotion / delivery, e.g. "angry, voice breaking"
        t = f"({style}) {t}"
    if not Path(reference).exists():
        raise CloneFatal("The reference audio of this cloned voice is missing.")
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_wav.with_suffix(".part.wav")
    if is_online():                                                   # Hugging Face Space: no local model, no GPU lock
        _synthesize_online(t, str(reference), tmp, progress, cancel)
    else:
        with _gpu_lock:
            eng = get_engine(progress, cancel)
            try:
                chunks = []
                for ch in eng.generate_streaming(text=t, reference_wav_path=str(reference), cfg_value=CFG_VALUE,
                                                 inference_timesteps=_steps()):
                    if cancel and cancel():                           # streaming lets STOP / Cancel work mid-line
                        raise Cancelled()
                    chunks.append(np.asarray(ch, dtype=np.float32))
                if not chunks:
                    raise RuntimeError("the model produced no audio")
                wav = np.concatenate(chunks)
                sr = int(eng.tts_model.sample_rate)
            except Cancelled:
                raise
            except Exception as e:  # noqa
                raise _translate_error(e) from e
            finally:
                _free_gpu()
        pcm = (np.clip(wav, -1.0, 1.0) * 32767.0).astype(np.int16)
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(pcm.tobytes())
    if abs(float(speed) - 1.0) > 0.01:                                # honour the page's "Speech speed" box
        run_ffmpeg(["-y", "-i", str(tmp), "-filter:a", f"atempo={min(2.0, max(0.5, float(speed))):.3f}",
                    "-c:a", "pcm_s16le", str(out_wav)], cancel=cancel)
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(out_wav)
    if not out_wav.exists() or out_wav.stat().st_size < 1000:
        raise CloneError("The cloned voice produced no audio.")
    return str(out_wav)


def clone_demo(prepared_wav, text, out_name="clone_preview", progress=None, cancel=None) -> str:
    """Speak `text` with a reference that is not saved yet (used by the CLONE VOICE panel)."""
    out = data_dir() / "work" / "voice_demo" / f"{out_name}_{uuid.uuid4().hex[:6]}.wav"
    return synthesize_clone(text, out, prepared_wav, 1.0, progress, cancel)


def voice_demo_file(name, text, progress=None, cancel=None) -> str:
    """Demo of a SAVED cloned voice (cached per voice + text)."""
    v = get_voice(name)
    if not v:
        raise CloneFatal(f"Cloned voice '{name}' no longer exists.")
    key = hashlib.md5((v["id"] + "|" + _clean_text(text)).encode("utf-8")).hexdigest()[:10]
    out = data_dir() / "work" / "voice_demo" / f"clone_{v['id']}_{key}.wav"
    if out.exists() and out.stat().st_size > 1000:
        return str(out)
    return synthesize_clone(text, out, Path(v["dir"]) / "reference.wav", 1.0, progress, cancel)


def preset_for(nick="") -> dict:
    """Valid Voice Type preset for a cloned voice so the existing save/load code (voice_presets) keeps it.
    The effect values are neutral and ignored: the sound comes from the reference recording."""
    return {"base": "P", "pitch": 0, "speed": 0, "formant": 0, "fx": "", "nick": ""}
