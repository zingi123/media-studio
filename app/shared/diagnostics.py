"""
System diagnostics: what is REALLY installed / configured / reachable / downloaded / tested on this PC.

Every component gets ONE status label (the strictest truth) plus a list of checks, each saying exactly what was verified:
  INSTALLED | NOT INSTALLED | CONNECTED | NOT CONNECTED | MODEL AVAILABLE | MODEL MISSING | COMPATIBILITY ERROR | NOT TESTED
(CONFIGURED / NOT CONFIGURED are added only for credentials: a key being present is not the same as it working.)
A provider is CONNECTED only after a real test request succeeded in THIS session. SDK installed != connected.

Heavy checks (importing torch / torchaudio / pyannote / whisper) run in a separate process (_probe.py).
"""
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import data_dir

INSTALLED, NOT_INSTALLED, CONNECTED, NOT_CONNECTED = "INSTALLED", "NOT INSTALLED", "CONNECTED", "NOT CONNECTED"
MODEL_OK, MODEL_MISSING, COMPAT, NOT_TESTED = "MODEL AVAILABLE", "MODEL MISSING", "COMPATIBILITY ERROR", "NOT TESTED"
CONFIGURED, NOT_CONFIGURED = "CONFIGURED", "NOT CONFIGURED"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    level: str = "info"            # ok | warn | error | info


@dataclass
class Component:
    name: str
    status: str
    summary: str
    level: str = "info"
    checks: list = field(default_factory=list)


# ------------------------------------------------------------------ probe (separate process)
def run_probe(timeout=240, python=None):
    """Run _probe.py with the app's interpreter. -> dict. On crash/timeout: {'probe_error': ...} (the app is unaffected)."""
    script = Path(__file__).with_name("_probe.py")
    try:
        r = subprocess.run([python or sys.executable, str(script)], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", creationflags=_NOWIN)
    except subprocess.TimeoutExpired:
        return {"probe_error": f"The environment probe did not finish within {timeout}s (a library may be hanging while importing)."}
    except OSError as e:
        return {"probe_error": f"Could not start the probe: {e}"}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        tail = (r.stderr or r.stdout or "").strip()[-400:]
        return {"probe_error": f"The probe process failed (exit code {r.returncode}) - a native library probably crashed while importing. {tail}"}


# ------------------------------------------------------------------ requirement analysis (pure functions)
def _canon(n):
    return re.sub(r"[-_.]+", "-", n).lower()


def check_requirements(requires, installed):
    """Evaluate declared requirements (Requires-Dist strings) against installed versions.
    -> [{requirement, name, installed, ok, specifier}] for every unconditional requirement that is installed or missing."""
    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError:
        return [{"requirement": "", "name": "packaging", "installed": None, "ok": None, "specifier": "",
                 "note": "the 'packaging' package is not installed - requirement analysis skipped"}]
    out = []
    for line in requires or []:
        try:
            r = Requirement(line)
        except Exception:
            continue
        if r.marker is not None:
            try:
                if not r.marker.evaluate({"extra": ""}):
                    continue                              # extras / other platforms / other Python versions
            except Exception:
                continue
        have = installed.get(_canon(r.name))
        ok = None
        if have is not None:
            try:
                ok = r.specifier.contains(Version(have), prereleases=True) if str(r.specifier) else True
            except Exception:
                ok = None
        out.append({"requirement": line.split(";")[0].strip(), "name": r.name, "installed": have, "ok": ok, "specifier": str(r.specifier)})
    return out


def conflicts(requires, installed):
    return [c for c in check_requirements(requires, installed) if c.get("ok") is False]


def dependents_violations(dependents, targets):
    """Which installed packages would break if torch-family packages were changed to `targets` ({'torch':'2.8.0',...})?"""
    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError:
        return []
    bad = []
    for d in dependents or []:
        try:
            r = Requirement(d["requirement"])
            t = targets.get(_canon(r.name))
            if t and str(r.specifier) and not r.specifier.contains(Version(t.split("+")[0]), prereleases=True):
                bad.append({**d, "target": f"{r.name}=={t}"})
        except Exception:
            continue
    return bad


def _minor(v):
    m = re.match(r"(\d+)\.(\d+)", v or "")
    return (int(m[1]), int(m[2])) if m else None


# ------------------------------------------------------------------ local, no-network checks
def ffmpeg_info():
    from .ffmpeg_util import find_ffmpeg
    exe = find_ffmpeg()
    if not exe:
        return None
    ver = ""
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15, creationflags=_NOWIN)
        ver = (r.stdout.splitlines() or [""])[0]
    except Exception as e:  # noqa
        return {"exe": exe, "ok": False, "version": f"cannot run: {e}", "on_path": False}
    import shutil
    return {"exe": exe, "ok": bool(ver), "version": ver, "on_path": shutil.which("ffmpeg") is not None}


def hf_cache_dir():
    for k in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(k):
            return Path(os.environ[k])
    return Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface")) / "hub"


def hf_model_cached(repo, marker_file=None):
    """True when a snapshot of `repo` exists in the local Hugging Face cache (no network)."""
    d = hf_cache_dir() / ("models--" + repo.replace("/", "--")) / "snapshots"
    if not d.is_dir():
        return False
    for snap in d.iterdir():
        if marker_file is None and any(snap.iterdir()):
            return True
        if marker_file and (snap / marker_file).exists():
            return True
    return False


def whisper_model_file(model, backend):
    if backend == "openai-whisper":
        root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "whisper"
        return (root / f"{model}.pt").exists()
    return hf_model_cached(f"Systran/faster-whisper-{model}")


# ------------------------------------------------------------------ session test results (never persisted as CONNECTED)
SESSION = {}


def session_get(key):
    return SESSION.get(key)


def run_provider_tests(pid, settings=None):
    """Network / SDK / local-server tests for one provider; results are stored for THIS session."""
    from ..ai import providers as P
    p = P.get_provider(pid, settings)
    res = {"time": time.time()}
    res["conn"] = p.test_connection()
    if pid == "huggingface":
        res["request"] = res["conn"]
    elif res["conn"].ok or (pid == "ollama" and p.model):
        res["request"] = p.test_request() if (pid != "ollama" or res["conn"].ok) else None
    else:
        res["request"] = None
    SESSION[pid] = res
    return res


def run_audio_test(wav, hf_token="", model=None, device="cpu", whisper_model="base", language=None, seconds=60, progress=None, settings=None):
    """The REAL test: Whisper + Pyannote on a user-supplied audio file (first `seconds`). Results are recorded.
    -> dict(ok, whisper={...}, diarization={...}, errors=[...])"""
    from ..translator import speech_recognition as SR
    from ..translator import diarization as D
    import numpy as np, tempfile, wave
    res = {"ok": False, "errors": [], "time": time.time(), "file": str(wav)}
    a, sr = D.read_wav_mono(wav) if str(wav).lower().endswith(".wav") else (None, None)
    if a is None:
        from .ffmpeg_util import run_ffmpeg
        tmp = Path(tempfile.mkdtemp()) / "t.wav"
        run_ffmpeg(["-y", "-i", str(wav), "-t", str(seconds), "-vn", "-ac", "1", "-ar", "16000", str(tmp)])
        wav = tmp; a, sr = D.read_wav_mono(wav)
    if sr != 16000 or len(a) > seconds * sr:
        a = a[: int(seconds * sr)]
        if sr == 16000:
            tmp = Path(tempfile.mkdtemp()) / "t16.wav"
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes((a * 32767).astype(np.int16).tobytes())
            wav = tmp
        else:
            res["errors"].append(f"audio is {sr} Hz - use a 16 kHz WAV or a media file (FFmpeg converts it)."); SESSION["audio_test"] = res; return res
    try:
        t0 = time.time()
        segs, lang, dur = SR.run_asr(wav, whisper_model, language, device, progress=progress)
        words = sum(len(s.words) for s in segs)
        res["whisper"] = {"segments": len(segs), "words": words, "language": lang, "seconds": round(time.time() - t0, 1),
                          "audio_seconds": round(dur, 1), "sample": " ".join(s.text for s in segs)[:80],
                          "timestamps_ok": all(s.end >= s.start for s in segs)}
        if not words:
            res["errors"].append("Whisper returned no words for this audio.")
    except Exception as e:  # noqa
        from . import secrets
        res["errors"].append(f"Whisper: {secrets.redact(e)}")
    try:
        t0 = time.time()
        r = D.diarize(wav, hf_token, model or D.DEFAULT_MODEL, device, cache_dir=None)
        res["diarization"] = {"speakers": r.speakers(), "turns": len(r.turns), "overlaps": len(r.overlaps()),
                              "seconds": round(time.time() - t0, 1), "device": device}
    except Exception as e:  # noqa
        from . import secrets
        res["errors"].append(f"Pyannote: {secrets.redact(e)}")
    res["ok"] = not res["errors"]
    SESSION["audio_test"] = res
    if res["ok"]:
        try:
            (data_dir() / "env_tests.json").write_text(json.dumps({"audio_test": {k: v for k, v in res.items() if k != "errors"}}, default=str), encoding="utf-8")
        except OSError:
            pass
    return res


def last_audio_test_note():
    try:
        d = json.loads((data_dir() / "env_tests.json").read_text(encoding="utf-8"))["audio_test"]
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(d["time"]))
    except Exception:
        return ""


# ------------------------------------------------------------------ component builders
def _step(probe, name):
    return (probe.get("steps") or {}).get(name)


def _pkg(probe, name):
    return (probe.get("packages") or {}).get(name)


def build_components(probe, settings=None, session=None, local=None):
    """probe: run_probe() result. -> [Component]. `local` lets tests inject ffmpeg/ollama/hf-cache info."""
    session = SESSION if session is None else session
    L = local or {}
    from . import secrets as SEC
    g = (settings.get if settings else (lambda k: ""))
    comps = []
    if "probe_error" in probe:
        comps.append(Component("Python Environment", COMPAT, probe["probe_error"], "error", [Check("probe", COMPAT, probe["probe_error"], "error")]))
        return comps + _non_probe_components(probe, settings, session, L)

    py, steps = probe["python"], probe.get("steps", {})
    # ---------------- Python
    ok_ver = (3, 10) <= tuple(int(x) for x in py["version"].split(".")[:2]) <= (3, 12)
    venv = Path(py["virtual_env"] or py["prefix"]).name if py["in_venv"] else ""
    comps.append(Component("Python Environment", INSTALLED if ok_ver else COMPAT,
        f"Python {py['version']} - " + (f"virtual environment '{venv}'" if py["in_venv"] else "NOT inside a virtual environment"),
        "ok" if ok_ver and py["in_venv"] else "warn",
        [Check("Interpreter", INSTALLED, py["executable"], "info"),
         Check("Version", INSTALLED if ok_ver else COMPAT, f"{py['version']} ({'supported 3.10-3.12' if ok_ver else 'not verified for PyTorch/pyannote; 3.10-3.12 recommended'})", "ok" if ok_ver else "warn"),
         Check("Virtual environment", INSTALLED if py["in_venv"] else NOT_INSTALLED, venv or "none - packages are installed globally", "ok" if py["in_venv"] else "warn"),
         Check("Platform", INSTALLED, f"{py['platform']} {py['machine']}", "info")]))

    # ---------------- PyTorch
    t = steps.get("torch", {})
    tv = _pkg(probe, "torch")
    if not tv and not t.get("ok"):
        comps.append(Component("PyTorch", NOT_INSTALLED, "torch is not installed (optional: needed for speaker detection / Whisper)", "warn",
                               [Check("Package", NOT_INSTALLED, "pip metadata has no 'torch'", "warn")]))
    elif not t.get("ok"):
        comps.append(Component("PyTorch", COMPAT, f"torch {tv} is installed but import / CPU test FAILED: {t.get('error')}", "error",
                               [Check("Import + CPU tensor test", COMPAT, t.get("error", ""), "error")]))
    else:
        cpu_only = not t.get("cuda_build")
        comps.append(Component("PyTorch", INSTALLED, f"torch {t['version']} imports; CPU matrix test OK ({t['cpu_matmul_ms']} ms)", "ok",
            [Check("Import + CPU tensor test", INSTALLED, f"256x256 matmul in {t['cpu_matmul_ms']} ms, {t['threads']} CPU threads", "ok"),
             Check("Build type", INSTALLED, "CPU-only build" if cpu_only else f"CUDA {t['cuda_build']} build", "info")]))

    # ---------------- TorchAudio
    ta, tav = steps.get("torchaudio", {}), _pkg(probe, "torchaudio")
    if not tav and not ta.get("ok"):
        comps.append(Component("TorchAudio", NOT_INSTALLED, "torchaudio is not installed (needed by pyannote.audio)", "warn",
                               [Check("Package", NOT_INSTALLED, "pip metadata has no 'torchaudio'", "warn")]))
    else:
        checks, level, status = [], "ok", INSTALLED
        if ta.get("skipped"):
            status, level = COMPAT, "error"
            checks.append(Check("Runtime test", COMPAT, "skipped because torch does not import", "error"))
        elif not ta.get("ok"):
            status, level = COMPAT, "error"
            checks.append(Check("Import + resample/MelSpectrogram test", COMPAT, ta.get("error", ""), "error"))
        else:
            checks.append(Check("Import + resample/MelSpectrogram test", INSTALLED, f"torchaudio {ta['version']}: native ops work", "ok"))
        tm, am = _minor(t.get("version") or tv), _minor(ta.get("version") or tav)
        if tm and am and tm != am:
            checks.append(Check("Version match with torch", COMPAT if status != INSTALLED else INSTALLED,
                f"torch {tv} vs torchaudio {tav}: minor versions differ. torchaudio's compiled parts are built for ONE torch version; "
                "this is the most common cause of crashes/'undefined symbol' errors." + (" The basic runtime test above passed, but other functions may still fail." if status == INSTALLED else ""),
                "warn"))
            level = "warn" if level == "ok" else level
        cf = conflicts((probe.get("requires") or {}).get("torchaudio"), probe.get("installed", {}))
        for c in cf:
            checks.append(Check("pip metadata", COMPAT if status != INSTALLED else INSTALLED, f"torchaudio declares '{c['requirement']}' but {c['name']} {c['installed']} is installed", "warn"))
            level = "warn" if level == "ok" else level
        comps.append(Component("TorchAudio", status, f"torchaudio {tav}: " + ("works in the runtime test" if status == INSTALLED else "does NOT work with the installed torch") +
                               (" - version mismatch with torch (see details)" if any(c.level == "warn" for c in checks) else ""), level, checks))

    # ---------------- Whisper
    comps.append(_whisper_component(probe, settings, g, L))
    # ---------------- OCR
    comps += _ocr_components(probe)
    # ---------------- Pyannote
    comps.append(_pyannote_component(probe, settings, g, session, L))
    comps += _non_probe_components(probe, settings, session, L)
    return comps


def _whisper_component(probe, settings, g, L):
    ow, fw = _pkg(probe, "openai-whisper"), _pkg(probe, "faster-whisper")
    steps = probe.get("steps", {})
    model = (g("whisper_model") if settings else "") or "small"
    checks = []
    if not ow and not fw:
        return Component("Whisper", NOT_INSTALLED, "No Whisper package installed (pip install openai-whisper  or  faster-whisper)", "warn",
                         [Check("openai-whisper", NOT_INSTALLED, "", "warn"), Check("faster-whisper", NOT_INSTALLED, "", "info")])
    worst, usable = INSTALLED, []
    for label, ver, key in (("openai-whisper", ow, "openai_whisper"), ("faster-whisper", fw, "faster_whisper")):
        if not ver:
            continue
        s = steps.get(key, {})
        if s.get("ok"):
            checks.append(Check(label, INSTALLED, f"{ver}: imports OK", "ok")); usable.append(label)
        else:
            checks.append(Check(label, COMPAT, f"{ver} installed but import failed: {s.get('error', '?')}", "error")); worst = COMPAT
    if not usable:
        return Component("Whisper", COMPAT, "Whisper is installed but cannot be imported", "error", checks)
    backend = usable[0] if "faster-whisper" not in usable else "faster-whisper"
    have = L.get("whisper_model_cached")
    if have is None:
        have = whisper_model_file(model, backend)
    checks.append(Check(f"Model '{model}' ({backend})", MODEL_OK if have else MODEL_MISSING,
                        "downloaded in the local cache" if have else "not downloaded yet - it downloads on first use (needs internet, a few hundred MB to several GB)", "ok" if have else "warn"))
    at = SESSION.get("audio_test")
    if at and at.get("whisper"):
        w = at["whisper"]
        checks.append(Check("Transcription test", INSTALLED if "Whisper" not in " ".join(at.get("errors", [])) else COMPAT,
                            f"{w['words']} words, language {w['language']} in {w['seconds']}s for {w['audio_seconds']}s of audio (CPU speed measured)", "ok"))
    else:
        checks.append(Check("Transcription test (Chinese audio)", NOT_TESTED, "run 'Test Whisper + Pyannote with an audio file' in System Status" + (f"; last success {last_audio_test_note()}" if last_audio_test_note() else ""), "info"))
    status = MODEL_OK if have else MODEL_MISSING
    summary = f"{', '.join(usable)} installed; model '{model}' " + ("downloaded" if have else "missing") + "; transcription NOT TESTED" if not (at and at.get("whisper")) else f"{', '.join(usable)} installed; model '{model}' " + ("downloaded" if have else "missing") + "; transcription tested"
    return Component("Whisper", status, summary, "ok" if have else "warn", checks)


def _ocr_components(probe):
    out = []
    pad, pp = _pkg(probe, "paddleocr"), _pkg(probe, "paddlepaddle")
    st = probe.get("steps", {}).get("paddle", {})
    if pad and pp:
        if st.get("ok"):
            out.append(Component("PaddleOCR", INSTALLED, f"paddleocr {pad} + paddlepaddle {pp} import OK (not used by the default OCR engine; select it in Settings > OCR engine)", "ok",
                                 [Check("paddleocr", INSTALLED, pad, "ok"), Check("paddlepaddle import", INSTALLED, st.get("paddle_version", ""), "ok"), Check("OCR run on an image", NOT_TESTED, "not tested", "info")]))
        else:
            out.append(Component("PaddleOCR", COMPAT, f"paddlepaddle import failed: {st.get('error', '?')}", "error", [Check("paddlepaddle import", COMPAT, st.get("error", ""), "error")]))
    elif pad:
        out.append(Component("PaddleOCR", COMPAT, f"paddleocr {pad} installed but 'paddlepaddle' (its runtime) is missing: pip install paddlepaddle", "error", [Check("paddlepaddle", NOT_INSTALLED, "", "error")]))
    else:
        out.append(Component("PaddleOCR", NOT_INSTALLED, "Not installed (optional). The default OCR engine is RapidOCR (CPU, ONNX) - see 'OCR engines'.", "info", [Check("paddleocr", NOT_INSTALLED, "", "info")]))
    rapid, easy, ort = _pkg(probe, "rapidocr-onnxruntime"), _pkg(probe, "easyocr"), _pkg(probe, "onnxruntime")
    rs = probe.get("steps", {}).get("rapidocr", {})
    checks = [Check("RapidOCR", (INSTALLED if rs.get("ok") else COMPAT) if rapid else NOT_INSTALLED,
                    (f"{rapid}, imports OK" if rs.get("ok") else f"{rapid}: {rs.get('error', '')}") if rapid else "pip install rapidocr-onnxruntime", "ok" if rs.get("ok") else "warn"),
              Check("EasyOCR", INSTALLED if easy else NOT_INSTALLED, easy or "optional (Japanese/Korean/Thai/Vietnamese)", "info"),
              Check("onnxruntime", INSTALLED if ort else NOT_INSTALLED, ort or "", "info")]
    good = bool(rapid and rs.get("ok")) or bool(easy)
    out.append(Component("OCR engines (RapidOCR / EasyOCR)", INSTALLED if good else NOT_INSTALLED,
                         "RapidOCR works (CPU)" if rapid and rs.get("ok") else ("no OCR engine installed: pip install rapidocr-onnxruntime" if not good else "EasyOCR installed"),
                         "ok" if good else "warn", checks))
    return out


def _pyannote_component(probe, settings, g, session, L):
    pv = _pkg(probe, "pyannote.audio")
    steps = probe.get("steps", {})
    if not pv:
        return Component("Pyannote", NOT_INSTALLED, "pyannote.audio is not installed (pip install pyannote.audio==4.0.7)", "warn", [Check("Package", NOT_INSTALLED, "", "warn")])
    checks = [Check("Version", INSTALLED if pv.split(".")[0] == "4" else COMPAT, f"{pv}" + ("" if pv.split(".")[0] == "4" else " - community-1 needs >= 4.0"), "ok" if pv.split(".")[0] == "4" else "error")]
    status, level = INSTALLED, "ok"
    imp, io = steps.get("pyannote_import", {}), steps.get("pyannote_audio_io", {})
    if not imp.get("ok"):
        status, level = COMPAT, "error"
        checks.append(Check("Import (Pipeline)", COMPAT, imp.get("error", "?"), "error"))
    else:
        checks.append(Check("Import (Pipeline)", INSTALLED, "from pyannote.audio import Pipeline works", "ok"))
        if io.get("ok"):
            checks.append(Check("Audio pipeline (in-memory waveform)", INSTALLED, "pyannote's audio loader accepts a torch waveform", "ok"))
        else:
            status, level = COMPAT, "error"
            checks.append(Check("Audio pipeline (in-memory waveform)", COMPAT, io.get("error", "?"), "error"))
    cf = conflicts((probe.get("requires") or {}).get("pyannote.audio"), probe.get("installed", {}))
    for c in cf:
        checks.append(Check("pip metadata", COMPAT if status == COMPAT else INSTALLED, f"pyannote.audio declares '{c['requirement']}' but {c['name']} {c['installed']} is installed", "warn"))
        level = "warn" if level == "ok" else level
    repo = (g("diar_model") if settings else "") or "pyannote/speaker-diarization-community-1"
    cached = L.get("hf_cached")
    if cached is None:
        cached = hf_model_cached(repo)
    checks.append(Check(f"Model {repo}", MODEL_OK if cached else MODEL_MISSING,
                        "snapshot found in the local Hugging Face cache" if cached else "not downloaded yet - needs a valid token + accepted terms, then downloads on first use", "ok" if cached else "warn"))
    at = session.get("audio_test")
    if at and at.get("diarization"):
        d = at["diarization"]
        checks.append(Check("Diarization test on real audio", INSTALLED, f"{len(d['speakers'])} speaker(s), {d['turns']} turn(s), {d['overlaps']} overlap(s) in {d['seconds']}s on {d['device']}", "ok"))
        tested = True
    else:
        why = next((e for e in (at or {}).get("errors", []) if e.startswith("Pyannote")), "")
        checks.append(Check("Diarization test on real audio", COMPAT if why else NOT_TESTED, why or "not tested - use 'Test Whisper + Pyannote with an audio file'" + (f" (last success {last_audio_test_note()})" if last_audio_test_note() else ""), "error" if why else "info"))
        tested = False
    if status == COMPAT:
        return Component("Pyannote", COMPAT, "pyannote.audio is installed but does not work here: " + next(c.detail for c in checks if c.status == COMPAT)[:160], "error", checks)
    if tested:
        return Component("Pyannote", INSTALLED, "installed and diarization succeeded on real audio in this session", level, checks)
    return Component("Pyannote", NOT_TESTED if cached else MODEL_MISSING,
                     f"installed; model {'cached locally' if cached else 'not downloaded'}; diarization NOT TESTED on real audio",
                     level if level != "ok" else "warn", checks)


def _provider_component(pid, label, settings, session, local):
    """Gemini / Claude: SDK, key, connection, model, test request - separately."""
    from ..ai import providers as P
    p = P.get_provider(pid, settings) if settings is not None else None
    sdk = p.sdk_version() if p else None
    key = bool(p.key) if p else False
    res = session.get(pid)
    checks = [Check("SDK", INSTALLED if sdk else NOT_INSTALLED, f"{p.sdk_dist} {sdk}" if sdk else f"{p.sdk_dist if p else ''} not installed - the built-in HTTPS client is used instead", "ok" if sdk else "info"),
              Check("API key", CONFIGURED if key else NOT_CONFIGURED, "a key is saved in the credential store (its validity is NOT known yet)" if key else "no key saved - add one in AI Providers", "info" if key else "warn")]
    if not key:
        checks += [Check("Connection", NOT_CONNECTED, "needs an API key", "warn"), Check("Model", NOT_TESTED, "", "info"), Check("Test request", NOT_TESTED, "", "info")]
        return Component(label, NOT_CONNECTED, "No API key configured" + (" (SDK installed)" if sdk else ""), "warn", checks)
    if not res:
        checks += [Check("Connection", NOT_TESTED, "no network test run in this session", "info"), Check("Model", NOT_TESTED, p.model, "info"), Check("Test request", NOT_TESTED, "no test request sent", "info")]
        return Component(label, NOT_TESTED, "Key configured - connection NOT TESTED yet (press 'Run connection tests')", "warn", checks)
    conn, req = res["conn"], res.get("request")
    checks.append(Check("Connection", CONNECTED if conn.models else NOT_CONNECTED, conn.message, "ok" if conn.models else "error"))
    if conn.models:
        has = (p.model in conn.models)
        checks.append(Check("Model", MODEL_OK if has else MODEL_MISSING, f"'{p.model}' " + ("is listed by the provider" if has else "is NOT in the provider's model list"), "ok" if has else "error"))
    else:
        checks.append(Check("Model", NOT_TESTED, "could not list models", "info"))
    if req is None:
        checks.append(Check("Test request", NOT_TESTED, "skipped (connection failed)", "info"))
        return Component(label, NOT_CONNECTED, conn.message[:200], "error", checks)
    checks.append(Check("Test request", CONNECTED if req.ok else NOT_CONNECTED, req.message, "ok" if req.ok else "error"))
    if req.ok:
        return Component(label, CONNECTED, "Authenticated and a real test request succeeded (" + time.strftime("%H:%M", time.localtime(res["time"])) + ")", "ok", checks)
    return Component(label, NOT_CONNECTED, req.message[:200], "error", checks)


def _non_probe_components(probe, settings, session, L):
    from ..ai import providers as P
    comps = []
    comps.append(_hf_component(probe, settings, session))
    comps.append(_provider_component("gemini", "Gemini", settings, session, L))
    comps.append(_provider_component("claude", "Claude", settings, session, L))
    comps.append(_ollama_component(settings, session, L))
    comps.append(_ffmpeg_component(L))
    m = probe.get("machine") or {}
    t = (probe.get("steps") or {}).get("torch", {})
    ram, free = m.get("ram_total_mb"), m.get("ram_available_mb")
    cl = [Check("CPU", INSTALLED, f"{m.get('cpu_count', os.cpu_count())} logical cores", "info"),
          Check("RAM", INSTALLED if ram else NOT_TESTED, f"{ram / 1024:.1f} GB total, {free / 1024:.1f} GB free" if ram else "install 'psutil' to read RAM", "ok" if (ram or 0) >= 8000 else "warn")]
    if ram and ram < 8000:
        cl.append(Check("Advice", INSTALLED, "under 8 GB: use Whisper tiny/base/small, one model at a time (default)", "warn"))
    comps.append(Component("CPU", INSTALLED, f"{m.get('cpu_count', os.cpu_count())} cores" + (f", {ram / 1024:.1f} GB RAM" if ram else ""), "ok" if (ram or 0) >= 8000 else "warn", cl))
    if t.get("ok") and t.get("cuda_available"):
        comps.append(Component("GPU/CUDA", INSTALLED, f"CUDA available: {t.get('device')} ({t.get('vram_mb', 0) / 1024:.1f} GB VRAM)", "ok",
                               [Check("CUDA", INSTALLED, f"torch built for CUDA {t.get('cuda_build')}; device {t.get('device')}", "ok")]))
    else:
        why = ("this PyTorch is a CPU-only build" if t.get("ok") and not t.get("cuda_build") else
               ("no usable CUDA device was found" if t.get("ok") else "PyTorch is not importable, so CUDA cannot be checked"))
        comps.append(Component("GPU/CUDA", NOT_INSTALLED, f"CUDA not available - {why}. Everything runs on the CPU.", "info",
                               [Check("CUDA", NOT_INSTALLED, why, "info"), Check("Fallback", INSTALLED, "CPU processing is used automatically; CUDA will be used only if it becomes available", "ok")]))
    return comps


def _hf_component(probe, settings, session):
    sdk = _pkg(probe, "huggingface_hub")
    key = bool(settings.get("hf_token")) if settings else False
    res = session.get("huggingface")
    checks = [Check("huggingface_hub", INSTALLED if sdk else NOT_INSTALLED, sdk or "not installed (the app does not need it for access checks)", "ok" if sdk else "info"),
              Check("Token", CONFIGURED if key else NOT_CONFIGURED, "saved (validity not known yet)" if key else "no token saved", "info" if key else "warn")]
    if not key:
        return Component("Hugging Face", NOT_CONNECTED, "No token configured", "warn", checks + [Check("Authentication", NOT_CONNECTED, "needs a token", "warn"), Check("Model access", NOT_TESTED, "", "info")])
    if not res:
        return Component("Hugging Face", NOT_TESTED, "Token configured - authentication and model access NOT TESTED yet", "warn",
                         checks + [Check("Authentication", NOT_TESTED, "", "info"), Check("Model access", NOT_TESTED, "", "info")])
    r = res["conn"]
    checks.append(Check("Authentication + model access", CONNECTED if r.ok else NOT_CONNECTED, r.message, "ok" if r.ok else "error"))
    return Component("Hugging Face", CONNECTED if r.ok else NOT_CONNECTED, r.message[:200], "ok" if r.ok else "error", checks)


def _ollama_component(settings, session, L):
    from ..ai import providers as P
    st = L.get("ollama")
    if st is None:
        st = P.get_provider("ollama", settings).status() if settings is not None else P.OllamaProvider().status()
    model = (settings.get("ollama_model") if settings else "") or ""
    checks = [Check("Executable", INSTALLED if st["installed"] else NOT_INSTALLED,
                    (f"{st['exe']} (found via {st['exe_source']}; version {st['exe_version'] or '?'})" + ("" if st["exe_source"] == "PATH" else " - not on PATH, the app uses the full path")) if st["installed"] else "ollama.exe not found (PATH, standard locations, configured path)", "ok" if st["installed"] else "warn"),
              Check("Server process", CONNECTED if st["running"] else NOT_CONNECTED, f"answering at version {st['version']}" if st["running"] else "no server answering on the configured URL (start it from this panel or run 'ollama serve')", "ok" if st["running"] else "warn"),
              Check("API connection", CONNECTED if st["api_ok"] else NOT_CONNECTED, "model list request succeeded" if st["api_ok"] else "could not read /api/tags", "ok" if st["api_ok"] else "warn")]
    have = bool(st["models"])
    checks.append(Check("Installed models", MODEL_OK if have else MODEL_MISSING, ", ".join(st["models"]) if have else "Ollama is installed, but no local model is available.", "ok" if have else "warn"))
    sel_ok = bool(model) and (model in st["models"] or f"{model}:latest" in st["models"])
    checks.append(Check(f"Selected model '{model or '-'}'", MODEL_OK if sel_ok else MODEL_MISSING, "downloaded" if sel_ok else ("no model selected" if not model else f"not downloaded - run: ollama pull {model}"), "ok" if sel_ok else "warn"))
    loaded = [m for m in st["loaded"]]
    res = session.get("ollama")
    req = res.get("request") if res else None
    checks.append(Check("Loaded in memory", INSTALLED if loaded else NOT_TESTED, ", ".join(loaded) if loaded else "no model is currently loaded (Ollama loads one on the first request)", "info"))
    if req is not None:
        checks.append(Check("Test generation", CONNECTED if req.ok else NOT_CONNECTED, req.message, "ok" if req.ok else "error"))
    else:
        checks.append(Check("Test generation", NOT_TESTED, "no test request sent", "info"))
    if req is not None and req.ok:
        return Component("Ollama", CONNECTED, f"Ready: model '{model}' answered a test request", "ok", checks)
    if not st["installed"] and not st["running"]:
        return Component("Ollama", NOT_INSTALLED, st["message"], "info", checks)
    if not st["running"]:
        return Component("Ollama", NOT_CONNECTED, st["message"], "warn", checks)
    if not have:
        return Component("Ollama", MODEL_MISSING, st["message"], "warn", checks)
    if not sel_ok:
        return Component("Ollama", MODEL_MISSING, f"Server running, but the selected model '{model}' is not downloaded" if model else "Server running with models, but none is selected", "warn", checks)
    return Component("Ollama", NOT_TESTED, f"Server running, model '{model}' downloaded - test generation NOT RUN yet", "warn", checks)


def _ffmpeg_component(L):
    f = L.get("ffmpeg") if "ffmpeg" in L else ffmpeg_info()
    if not f:
        return Component("FFmpeg", NOT_INSTALLED, "FFmpeg not found: winget install Gyan.FFmpeg  (or put ffmpeg.exe in the 'bin' folder, or set its path in Settings)", "error",
                         [Check("ffmpeg", NOT_INSTALLED, "not on PATH, not in 'bin', no bundled imageio-ffmpeg", "error")])
    if not f["ok"]:
        return Component("FFmpeg", COMPAT, f"found at {f['exe']} but it cannot run: {f['version']}", "error", [Check("ffmpeg -version", COMPAT, f["version"], "error")])
    return Component("FFmpeg", INSTALLED, f["version"][:80], "ok" if f["on_path"] else "warn",
                     [Check("Executable", INSTALLED, f["exe"], "ok"), Check("ffmpeg -version", INSTALLED, f["version"], "ok"),
                      Check("On PATH", INSTALLED if f["on_path"] else NOT_INSTALLED,
                            "yes" if f["on_path"] else "no - MediaStudio finds it anyway; Whisper receives audio in memory so it does not need PATH", "ok" if f["on_path"] else "info")])


# ------------------------------------------------------------------ public entry points
def collect(settings=None, probe=None):
    probe = probe if probe is not None else run_probe()
    return build_components(probe, settings), probe


def format_report(components, probe=None):
    lines = [f"MediaStudio environment report - {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    if probe and "python" in probe:
        p = probe["python"]
        lines += [f"Python: {p['version']}  {p['executable']}", f"Virtual env: {p['virtual_env'] or ('(sys.prefix) ' + p['prefix'] if p['in_venv'] else 'none')}",
                  "", "Installed packages (pip metadata):"]
        lines += [f"  {k:<22} {v}" for k, v in sorted((probe.get('packages') or {}).items()) if v]
        lines.append("")
    for c in components:
        lines.append(f"[{c.status}] {c.name}: {c.summary}")
        for k in c.checks:
            lines.append(f"      - {k.name}: {k.status} - {k.detail}")
    lines += ["", "No API keys or tokens are included in this report."]
    from . import secrets
    return secrets.redact("\n".join(lines))
