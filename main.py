"""Launch the MediaStudio desktop application.

Run with: python main.py            (development)
Packaged: MediaStudio.exe loads the replaceable app code from the 'code' folder next to the exe.
"""
from __future__ import annotations
import sys
import traceback
from pathlib import Path


def _setup_paths() -> None:
    """Packaged build: the app code lives in <exe folder>\\code\\app (so the in-app updater can replace it)."""
    if getattr(sys, "frozen", False):
        code = Path(sys.executable).resolve().parent / "code"
        if code.is_dir() and str(code) not in sys.path:
            sys.path.insert(0, str(code))


def _log_crash(text: str) -> None:
    """A windowed .exe has no console, so write startup errors to a file the user can open."""
    try:
        p = Path.home() / "MediaStudio" / "crash.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def _stdlib_hints() -> None:
    """Never executed. Lets PyInstaller see standard-library modules that only the external 'app' code uses
    (app is excluded from the exe, so its stdlib imports are not analysed otherwise)."""
    import asyncio, base64, collections, concurrent.futures, contextlib, copy, csv, ctypes  # noqa: F401
    import dataclasses, datetime, difflib, functools, gc, glob, hashlib, importlib  # noqa: F401
    import importlib.metadata, json, math, os, pathlib, platform, queue, random, re  # noqa: F401
    import shutil, sqlite3, statistics, struct, subprocess, tempfile, threading, time  # noqa: F401
    import traceback, types, unicodedata, urllib.error, urllib.parse, urllib.request  # noqa: F401
    import uuid, wave, zipfile  # noqa: F401


def main() -> int:
    _setup_paths()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        msg = "MediaStudio requires PySide6. Install the project's desktop dependencies.\n" + str(exc)
        _log_crash(msg)
        if sys.stderr:
            print(msg, file=sys.stderr)
        return 2
    try:
        from app.context import Context
        from app.shared.settings import Settings
        from app.shared.db import DB
        from app.downloader.download_manager import DownloadManager
        from app.ui.main_window import MainWindow
        app = QApplication(sys.argv)
        settings = Settings()
        db = DB()
        downloads = DownloadManager(db, settings)
        window = MainWindow(Context(settings=settings, db=db, downloads=downloads))
        window.show()
        return app.exec()
    except Exception:
        err = traceback.format_exc()
        _log_crash(err)
        try:
            from PySide6.QtWidgets import QApplication as _QA, QMessageBox
            _qa = _QA.instance() or _QA(sys.argv)
            QMessageBox.critical(None, "MediaStudio", "MediaStudio failed to start.\n\n" + err[-1500:]
                                 + "\n\n(Saved to ~/MediaStudio/crash.log)")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())