from PySide6.QtCore import QThread, Signal, QRunnable, QObject
import threading
from ..shared.ffmpeg_util import Cancelled


class Worker(QThread):
    """Run fn(*args, progress=cb, cancel=cb, **kw) off the GUI thread."""
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, fn, *args, **kw):
        super().__init__()
        self.fn, self.args, self.kw = fn, args, kw
        self._cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()

    def cancel(self):
        """Request cancellation and release a paused worker so it can stop safely."""
        self._cancel.set()
        self._resume.set()

    def pause(self):
        """Pause at the next cooperative checkpoint in the active pipeline."""
        if not self._cancel.is_set():
            self._resume.clear()

    def resume(self):
        self._resume.set()

    @property
    def is_paused(self):
        return not self._resume.is_set() and not self._cancel.is_set()

    def _checkpoint(self):
        # Existing pipeline stages already poll the cancel callback. Waiting here
        # turns those safe checkpoints into pause/resume points without blocking Qt.
        self._resume.wait()
        return self._cancel.is_set()

    def run(self):
        try:
            r = self.fn(*self.args, progress=self.progress.emit, cancel=self._checkpoint, **self.kw)
            self.done.emit(r)
        except Cancelled:
            self.failed.emit("Cancelled")
        except Exception as e:  # noqa
            self.failed.emit(str(e) or e.__class__.__name__)


class _ThumbSig(QObject):
    loaded = Signal(str, bytes)


class ThumbTask(QRunnable):
    def __init__(self, key, url, sig):
        super().__init__()
        self.key, self.url, self.sig = key, url, sig

    def run(self):
        import urllib.request
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=10).read()
            self.sig.loaded.emit(self.key, data)
        except Exception:
            pass
