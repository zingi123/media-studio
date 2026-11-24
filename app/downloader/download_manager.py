import queue
from glob import escape as glob_escape
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, unquote
from PySide6.QtCore import QObject, Signal

from ..shared.ffmpeg_util import find_ffmpeg
from ..shared.utils import unique_path, kind_of
from .adapters import get_adapter
from .errors import friendly_error
from .organizer import base_name, target_dir
from .url_detector import detect, DIRECT

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


class _Cancel(Exception):
    pass


def format_for(quality: str):
    q = quality.lower()
    if q.startswith("audio"):
        return "ba/b"
    if q.startswith("best"):
        return "bv*+ba/b"
    h = "".join(c for c in q.split("p")[0] if c.isdigit())
    return f"bv*[height<={h}]+ba/b[height<={h}]/b" if h else "bv*+ba/b"


class DownloadManager(QObject):
    job_updated = Signal(object)

    def __init__(self, db, settings):
        super().__init__()
        self.db, self.settings = db, settings
        self.q = queue.Queue()
        self.workers = []
        self._lock = threading.Lock()

    # ---------- public API ----------
    def submit(self, job):
        job.status, job.progress, job.error, job.cancel_flag = "Queued", 0, "", False
        job.run_evt.set()
        self._ensure_workers()
        self.q.put(job)
        self.job_updated.emit(job)

    def pause(self, job):
        if job.status in ("Downloading", "Queued"):
            job.run_evt.clear()
            job.status = "Paused"
            self.job_updated.emit(job)

    def resume(self, job):
        if job.status == "Paused":
            job.status = "Downloading" if job.progress else "Queued"
            job.run_evt.set()
            self.job_updated.emit(job)

    def cancel(self, job):
        if job.status in ("Queued", "Downloading", "Paused"):
            job.cancel_flag = True
            job.run_evt.set()
            if job.status != "Downloading":
                job.status = "Cancelled"
                self.job_updated.emit(job)

    # ---------- workers ----------
    def _ensure_workers(self):
        n = max(1, int(self.settings.get("parallel_downloads") or 1))
        with self._lock:
            while len(self.workers) < n:
                t = threading.Thread(target=self._loop, daemon=True)
                t.start()
                self.workers.append(t)

    def _loop(self):
        while True:
            job = self.q.get()
            try:
                if job.cancel_flag:
                    job.status = "Cancelled"
                    self.job_updated.emit(job)
                    continue
                self._run(job)
            except Exception as e:  # noqa - one failure must never stop the queue
                job.status, job.error = "Failed", friendly_error(e)
                self.db.add_history(job.item.url, job.item.title, job.item.site, "Failed")
                self.job_updated.emit(job)

    def _run(self, job):
        item = job.item
        if not job.allow_duplicate:
            existing = self.db.completed_path(item.url)
            if existing:
                job.status, job.path, job.progress = "Duplicate", existing, 100
                self.job_updated.emit(job)
                return
        job.status = "Downloading"
        self.job_updated.emit(job)
        out_dir = Path(job.root) if getattr(job, "flat", False) else target_dir(job.root, item)
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            clean = item.url.lower().split("?")[0]
            if item.kind == "image" and clean.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                self._direct_file(job, out_dir)
            else:
                self._ytdlp(job, out_dir)
        except _Cancel:
            job.status = "Cancelled"
            self.job_updated.emit(job)
            return
        p = Path(job.path)
        job.size = p.stat().st_size if p.exists() else 0
        job.status, job.progress = "Done", 100
        self.db.add_history(item.url, item.title, item.site, "Done", job.path, job.size)
        if p.exists():
            self.db.add_library(job.path, item.title, kind_of(p), item.site)
        self.job_updated.emit(job)

    def _wait_or_cancel(self, job):
        job.run_evt.wait()
        if job.cancel_flag:
            raise _Cancel()

    def _ytdlp(self, job, out_dir):
        import yt_dlp
        item = job.item
        base = base_name(item)
        n = 0
        while any(out_dir.glob(glob_escape(base) + ".*")):   # never reuse/overwrite an existing name
            n += 1
            base = f"{base_name(item)}_{n}"
        tmpl = str(out_dir).replace("%", "%%") + "/" + base.replace("%", "%%") + ".%(ext)s"
        audio_only = job.quality.lower().startswith("audio")

        def hook(d):
            self._wait_or_cancel(job)
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    job.progress = min(99, int(d.get("downloaded_bytes", 0) * 100 / total))
                job.speed, job.eta = d.get("speed") or 0, d.get("eta")
                self.job_updated.emit(job)

        opts = {"outtmpl": tmpl, "format": format_for(job.quality), "quiet": True, "no_warnings": True,
                "noprogress": True, "progress_hooks": [hook], "overwrites": False, "continuedl": True,
                "retries": 3, "fragment_retries": 3, "windowsfilenames": True, "noplaylist": True,
                "writedescription": bool(job.save_captions), "socket_timeout": 30}
        if not audio_only:
            opts["merge_output_format"] = "mp4"
        else:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
        ff = find_ffmpeg()
        if ff:
            opts["ffmpeg_location"] = ff
        ck = self.settings.get("cookies_file")
        if ck:
            opts["cookiefile"] = ck
        site = detect(item.url) or DIRECT
        opts.update(get_adapter(site).ydl_options(item))
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(item.url, download=True)
            if info is None:
                raise RuntimeError("Download returned nothing")
            rd = (info.get("requested_downloads") or [{}])[0]
            path = rd.get("filepath") or y.prepare_filename(info)
        if audio_only and not Path(path).exists():
            path = str(Path(path).with_suffix(".mp3"))
        job.path = path

    def _direct_file(self, job, out_dir):
        item = job.item
        name = Path(unquote(urlparse(item.url).path)).name or (base_name(item) + ".jpg")
        dest = unique_path(out_dir / name)
        part = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(item.url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                try:
                    self._wait_or_cancel(job)
                except _Cancel:
                    f.close()
                    part.unlink(missing_ok=True)
                    raise
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    job.progress = min(99, int(got * 100 / total))
                    self.job_updated.emit(job)
        part.replace(dest)
        job.path = str(dest)