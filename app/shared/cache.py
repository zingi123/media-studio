"""Clear Cache: delete everything MediaStudio stored while working, so the app starts clean.  No Qt here - testable.

DELETED
  work/          every per-video work folder: Demucs stems, rendered tracks, Khmer voice (TTS) files, previews, scan results,
                 and the saved studio project / dialogue of each video (the "history" of what you did to a video)
  voice_demo/    voice test files
  database       the download-history list (the downloaded files themselves are NOT deleted)
  memory         waveform peak cache

NEVER TOUCHED
  settings, API keys / tokens, cloned voices, Movie Summary projects and voice profiles, downloaded AI models,
  the Media Library index, and every file you exported or downloaded.
"""
import os
from pathlib import Path

from .paths import data_dir


def targets():
    """-> [(label, folder)] in the order they are shown to the user."""
    d = data_dir()
    return [("Work cache and saved project history (stems, voice files, previews)", d / "work"),
            ("Voice demo files", d / "voice_demo")]


def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def report(db=None):
    """What a clear would remove right now: {'items': [(label, bytes)], 'bytes': total, 'history_rows': n}."""
    items = [(label, folder_size(p) if p.exists() else 0) for label, p in targets()]
    rows = db.history_count() if db is not None else 0
    return {"items": items, "bytes": sum(b for _l, b in items), "history_rows": rows}


def _remove_children(folder, skipped, cancel=None):
    """Delete everything INSIDE `folder` (the folder itself stays).  Returns (files_deleted, bytes_freed).
    A file that cannot be deleted (open in another program, Windows lock) is recorded in `skipped`, never raises."""
    files = freed = 0
    folder = Path(folder)
    if not folder.exists():
        return 0, 0
    for root, dirs, names in os.walk(folder, topdown=False):
        if cancel and cancel():
            break
        for n in names:
            p = os.path.join(root, n)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            try:
                os.chmod(p, 0o666)
            except OSError:
                pass
            try:
                os.remove(p)
                files += 1
                freed += size
            except OSError as e:
                skipped.append(f"{p}  ({e.__class__.__name__})")
        for dn in dirs:
            dp = os.path.join(root, dn)
            try:
                if os.path.islink(dp):
                    os.unlink(dp)
                else:
                    os.rmdir(dp)                      # only succeeds when empty (i.e. everything inside was deleted)
            except OSError:
                pass
    return files, freed


def clear(db=None, progress=None, cancel=None):
    """Delete the cache.  Returns {'files', 'bytes', 'history_rows', 'skipped': [...]}.  Safe to call from a worker thread."""
    skipped = []
    files = freed = 0
    t = targets()
    for i, (_label, p) in enumerate(t):
        f, b = _remove_children(p, skipped, cancel)
        files += f
        freed += b
        if progress:
            progress(int(100 * (i + 1) / (len(t) + 1)))
    rows = db.clear_history() if db is not None else 0
    try:                                                  # in-memory waveform peaks of files that no longer exist
        from ..studio import audio as AU
        AU.clear_peak_cache()
    except Exception:  # noqa - never block the clear
        pass
    if progress:
        progress(100)
    return {"files": files, "bytes": freed, "history_rows": rows, "skipped": skipped}
