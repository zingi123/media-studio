"""Local end-to-end downloader test (no internet): python tests/test_downloader_integration.py"""
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp())
os.environ["MEDIASTUDIO_HOME"] = str(tmp / "home")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa
from PIL import Image  # noqa
from app.shared.ffmpeg_util import find_ffmpeg  # noqa
from app.shared.db import DB  # noqa
from app.shared.settings import settings  # noqa
from app.downloader.download_manager import DownloadManager  # noqa
from app.downloader.models import Job  # noqa
from app.downloader.scanner import scan_url, ScanError  # noqa

srv_dir = tmp / "srv"
srv_dir.mkdir()
subprocess.run([find_ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc=s=160x120:r=15:d=2", "-pix_fmt", "yuv420p",
                str(srv_dir / "clip.mp4")], capture_output=True, check=True)
Image.new("RGB", (64, 64), "red").save(srv_dir / "pic.jpg")


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(srv_dir), **k)

    def log_message(self, *a):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"

app = QCoreApplication([])
db, s = DB(str(tmp / "t.db")), settings()
dm = DownloadManager(db, s)
events = []
dm.job_updated.connect(lambda j: events.append((j.item.title, j.status)))


def pump(cond, timeout=30):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.05)
    return cond()


# scan
items_v = scan_url(base + "/clip.mp4")
items_i = scan_url(base + "/pic.jpg")
assert items_v[0].category == "direct" and items_i[0].kind == "image", (items_v, items_i)
try:
    scan_url("https://example.com/nothing")
    raise SystemExit("FAIL: unsupported link accepted")
except ScanError as e:
    print("unsupported ->", e)

root = str(tmp / "dl")
from app.downloader.models import Item  # noqa
bad = Item(base + "/missing.mp4", "missing", "Other_Sources", "direct")
jobs = [Job(items_v[0], root, "Best"), Job(bad, root, "Best"), Job(items_i[0], root, "Best")]
for j in jobs:
    dm.submit(j)
assert pump(lambda: all(j.status in ("Done", "Failed") for j in jobs)), [j.status for j in jobs]
print([(j.item.title, j.status, j.error[:60]) for j in jobs])
assert [j.status for j in jobs] == ["Done", "Failed", "Done"], "one failure must not stop the queue"
assert Path(jobs[0].path).exists() and Path(jobs[2].path).exists()
print("files:", jobs[0].path, jobs[2].path)

# duplicate detection
dup = Job(items_v[0], root, "Best")
dm.submit(dup)
assert pump(lambda: dup.status in ("Duplicate", "Done", "Failed"))
assert dup.status == "Duplicate", dup.status
# forced duplicate must not overwrite
dup2 = Job(items_v[0], root, "Best", allow_duplicate=True)
dm.submit(dup2)
assert pump(lambda: dup2.status in ("Done", "Failed")), dup2.status
print("forced dup ->", dup2.status, dup2.path, dup2.error)
files = list((Path(root)).rglob("*.mp4"))
print("mp4 files:", [f.name for f in files])
assert len(files) == 2 and dup2.path != jobs[0].path, "forced duplicate must create a new file"
assert db.library() and db.history()

# cancel a queued job
c = Job(items_v[0], root, "Best", allow_duplicate=True)
dm.cancel(c)
dm.submit(c)
c.cancel_flag = True
pump(lambda: c.status in ("Cancelled", "Done"), 10)
print("cancel ->", c.status)
print("ALL OK")
