"""Demucs (voice / music separation).

Works from source (run.bat) AND from the packaged MediaStudio.exe.  In the exe `sys.executable` is MediaStudio.exe itself, so
`python -m demucs` would just start another MediaStudio and die silently (empty log).  Therefore:
  1. not frozen            -> sys.executable -m demucs
  2. frozen                -> a REAL python that has demucs (setting `demucs_python`, .venv311 / python / venv folders near the exe,
                              `py -3.11`, python on PATH) ...
  3. otherwise (or if 2 fails to find one) -> run demucs INSIDE the exe (demucs.separate.main) when the exe contains it.
"""
import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from ..shared.ffmpeg_util import Cancelled, run_ffmpeg
from ..shared.paths import work_dir, app_root

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PYS = (".venv311/Scripts/python.exe", ".venv/Scripts/python.exe", "venv/Scripts/python.exe",
        "python/python.exe", "runtime/python.exe", "python311/python.exe")


def _has_demucs(py):
    try:
        r = subprocess.run([str(py), "-c", "import demucs"], capture_output=True, timeout=60, creationflags=_NOWIN)
        return r.returncode == 0
    except Exception:  # noqa
        return False


def find_python(setting=""):
    """A python interpreter that can import demucs, or None. In source mode that is simply sys.executable."""
    if not getattr(sys, "frozen", False):
        return sys.executable if importlib.util.find_spec("demucs") is not None else None
    cands = []
    if setting:
        cands.append(Path(setting))
    here = Path(sys.executable).resolve().parent
    for base in [here, *list(here.parents)[:3], Path.cwd()]:
        cands += [base / rel for rel in _PYS]
    for c in cands:
        if c.exists() and _has_demucs(c):
            return str(c)
    for name in ("python", "python3"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            c = Path(d) / (name + ".exe")
            if d and c.exists() and "WindowsApps" not in str(c) and _has_demucs(c):
                return str(c)
    return None


def _args(out, wav, device):
    # -j 1: one worker (less RAM on small PCs)
    return ["--two-stems=vocals", "-n", "htdemucs", "-d", device, "-j", "1", "-o", str(out), str(wav)]


def _run_inprocess(args, log, cancel):
    """demucs inside this process (exe that contains demucs). A thread, so STOP ALL still answers."""
    err = {}

    def job():
        try:
            from demucs.separate import main as demucs_main
            demucs_main(args)
        except SystemExit as e:  # argparse / demucs call sys.exit
            if e.code not in (0, None):
                err["e"] = f"demucs exited with code {e.code}"
        except BaseException as e:  # noqa
            err["e"] = f"{type(e).__name__}: {e}"
    t = threading.Thread(target=job, daemon=True)
    t.start()
    while t.is_alive():
        if cancel and cancel():
            raise Cancelled()
        time.sleep(0.3)
    if err:
        Path(log).write_text(err["e"], encoding="utf-8")
        return 1
    return 0


def separate(video, device="cpu", progress=None, cancel=None, python=""):  # បំបែកសំឡេង → ត្រឡប់ no_vocals.wav
    w = work_dir(Path(video).stem)  # ថតការងារ
    wav, out = w / "orig.wav", w / "demucs"  # ឯកសារ audio ដើម និងថតលទ្ធផល
    target = out / "htdemucs" / "orig" / "no_vocals.wav"  # ទីតាំង music+SFX
    if target.exists():  # មាន cache ហើយ
        return str(target)  # មិនបំបែកម្តងទៀត
    frozen = bool(getattr(sys, "frozen", False))
    py = find_python(python)
    inproc = False
    if py is None:
        if frozen and importlib.util.find_spec("demucs") is not None:
            inproc = True
        else:
            raise RuntimeError(
                "Demucs is not available. " + (
                    "MediaStudio.exe does not contain it and no Python with demucs was found. Install it with:  "
                    "<your .venv311>\\Scripts\\python -m pip install demucs  (the app finds .venv311 next to the exe), "
                    "or set `demucs_python` in settings to that python.exe." if frozen else
                    "Install it with: pip install demucs") + " Auto Dub continues without it.")
    run_ffmpeg(["-y", "-i", video, "-vn", "-ac", "2", "-ar", "44100", wav], cancel=cancel)  # ទាញ audio ដើម
    out.mkdir(parents=True, exist_ok=True)
    log = out / "demucs.log"  # ទុក output ដើម្បីចៀសវាង pipe ពេញ
    if inproc:
        rc = _run_inprocess(_args(out, wav, device), log, cancel)
    else:
        cmd = [py, "-m", "demucs", *_args(out, wav, device)]
        with open(log, "w", encoding="utf-8", errors="replace") as lf:
            try:
                p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, creationflags=_NOWIN)  # រត់ demucs
            except Exception as e:  # noqa
                raise RuntimeError(f"Cannot start Demucs: {e}") from e  # មិនអាចរត់
            while p.poll() is None:  # រង់ចាំ ហើយពិនិត្យ STOP
                if cancel and cancel():
                    p.kill(); p.wait()  # សម្លាប់ process
                    raise Cancelled()
                time.sleep(0.3)
        rc = p.returncode
    if rc != 0 or not target.exists():  # បរាជ័យ
        tail = log.read_text(encoding="utf-8", errors="replace")[-500:].strip() if log.exists() else ""
        if not tail:  # silent death = crash / out of memory / CPU without the needed instructions
            tail = (f"(no output, exit code {rc}) - Demucs crashed, most likely out of memory or an unsupported CPU. "
                    "Untick Demucs: the dubbing works without it.")
        raise RuntimeError("Demucs failed:\n" + tail)  # បង្ហាញកំហុស
    if progress:
        progress(100)  # ចប់
    return str(target)  # ផ្លូវ no_vocals
