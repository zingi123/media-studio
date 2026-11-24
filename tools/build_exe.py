"""Build dist/MediaStudio/ :  MediaStudio.exe (Python + libraries) + code/app (replaceable by the updater).
Run through build_exe.bat inside the project venv (.venv311)."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import gen_deps  # noqa: E402

# Packages NOT bundled. PaddleOCR is optional (default OCR = RapidOCR) and paddle's libpaddle.pyd crashes
# PyInstaller's analysis on CPUs without the needed instructions (0xc000001d). The rest only slow the build.
EXCLUDE = ["paddle", "paddleocr", "paddlex", "tensorflow", "tensorboard", "easyocr", "tkinter", "pytest"]


def excluded(mod):
    return any(mod == e or mod.startswith(e + ".") for e in EXCLUDE)


def installed(mod):
    try:
        return importlib.util.find_spec(mod.split(".")[0]) is not None
    except Exception:
        return False


def main():
    mods = [m for m in sorted(set(gen_deps.third_party()) | gen_deps.EXTRA) if installed(m) and not excluded(m)]
    missing = [m for m in gen_deps.third_party() if not installed(m)]
    if missing:
        print("NOTE: imported by the code but not installed in this venv (skipped):", ", ".join(missing))
    print("NOTE: excluded from the build:", ", ".join(EXCLUDE))
    cmd = [sys.executable, "-m", "PyInstaller", str(ROOT / "main.py"), "--name", "MediaStudio", "--noconfirm", "--clean",
           "--windowed", "--exclude-module", "app", "--paths", str(ROOT),
           "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build")]
    for e in EXCLUDE:
        cmd += ["--exclude-module", e]
    ico = ROOT / "assets" / "logo.ico"
    if ico.exists():
        cmd += ["--icon", str(ico)]
    for m in mods:
        cmd += ["--hidden-import", m]
        if m in gen_deps.COLLECT_ALL:
            cmd += ["--collect-all", m]
    print("PyInstaller ...")
    subprocess.check_call(cmd, cwd=ROOT)

    out = ROOT / "dist" / "MediaStudio"
    code = out / "code"
    if code.exists():
        shutil.rmtree(code)
    shutil.copytree(ROOT / "app", code / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for d in ("assets", "bin"):
        if (ROOT / d).exists():
            shutil.copytree(ROOT / d, out / d, dirs_exist_ok=True)
    if not (out / "bin" / "ffmpeg.exe").exists():
        print("WARNING: put ffmpeg.exe into the 'bin' folder, then run build_exe.bat again.")
    print("DONE ->", out)


if __name__ == "__main__":
    main()