"""Run on the target Windows/Python 3.11 environment before calling MediaStudio production-ready.

Usage: python tests/windows_runtime_check.py
This is a diagnostic only: it does not install packages, download models, or modify project settings.
"""
from __future__ import annotations
import importlib.util
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def report(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'CHECK'} | {label}" + (f" | {detail}" if detail else ""))


def main() -> int:
    print("=== MediaStudio Windows Runtime Diagnostics ===")
    report("Windows", platform.system() == "Windows", platform.platform())
    report("Python 3.11", sys.version_info[:2] == (3, 11), sys.version.split()[0])

    try:
        import PySide6
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
        fmt = QAudioFormat()
        fmt.setSampleRate(44100)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        dev = QMediaDevices.defaultAudioOutput()
        report("PySide6", True, getattr(PySide6, "__version__", "installed"))
        report("Qt Multimedia", True)
        report("Default audio device", not dev.isNull(), dev.description() if not dev.isNull() else "No output device")
        report("44.1 kHz stereo PCM", (not dev.isNull()) and dev.isFormatSupported(fmt))
        report("QAudioSink", QAudioSink is not None)
    except Exception as exc:
        report("PySide6 / Qt Multimedia", False, str(exc))

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    report("FFmpeg", bool(ffmpeg), ffmpeg or "Not on PATH; configure MediaStudio Settings")
    report("FFprobe", bool(ffprobe), ffprobe or "Not on PATH (MediaStudio has an FFmpeg probe fallback)")

    for package in ("numpy", "demucs", "torch"):
        spec = importlib.util.find_spec(package)
        report(package, spec is not None, "installed" if spec else "not installed (feature may be unavailable)")

    try:
        from app.ui.main_window import MainWindow  # noqa: F401
        report("MainWindow import", True)
    except Exception as exc:
        report("MainWindow import", False, repr(exc))

    print("\nCHECK items are environment diagnostics, not automatic failures. Run the GUI smoke test and verify a real export/playback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
