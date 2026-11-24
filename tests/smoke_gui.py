"""Headless GUI smoke test: python tests/test_gui_smoke.py (needs tests/test_downloader_integration.py's server idea)"""
import http.server, os, subprocess, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp())
os.environ["MEDIASTUDIO_HOME"] = str(tmp / "home")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from app.shared.ffmpeg_util import find_ffmpeg
from app.shared.db import DB
from app.shared.settings import settings
from app.context import Context
from app.downloader.download_manager import DownloadManager
from app.ui.main_window import MainWindow

srv = tmp / "srv"; srv.mkdir()
subprocess.run([find_ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc=s=160x120:r=15:d=2", "-pix_fmt", "yuv420p", str(srv / "a.mp4")], capture_output=True, check=True)
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(s, *a, **k): super().__init__(*a, directory=str(srv), **k)
    def log_message(s, *a): pass
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{httpd.server_address[1]}/a.mp4"

app = QApplication([]); s = settings(); db = DB(str(tmp / "t.db"))
w = MainWindow(Context(s, db, DownloadManager(db, s)))
page = w.stack.widget(1)
page.folder.setText(str(tmp / "dl"))
page.urls.setPlainText(url + "\nhttps://example.com/x")
def pump(c, t=30):
    st = time.time()
    while not c() and time.time() - st < t: app.processEvents(); time.sleep(0.05)
    return c()
page.scan()
assert pump(lambda: page.table.rowCount() == 1), page.log.toPlainText()
assert "Unsupported" in page.log.toPlainText()
page.download_selected()
assert pump(lambda: page.table.item(0, 6).text() == "Done"), page.table.item(0, 6).text()
print("status:", page.table.item(0, 6).text(), "|", page.summary.text())
page.clear_completed(); assert page.table.rowCount() == 0
w.lib.refresh(); assert w.lib.t.rowCount() == 1
# converter page: add file, inspect, run convert
conv = w.conv
f = next((tmp / "dl").rglob("*.mp4"))
conv.add_paths([f]); conv.inspect(conv.files.item(0))
conv.vh.setCurrentText("60"); conv.run()
assert pump(lambda: conv.start.isEnabled() and "Finished" in conv.log.toPlainText()), conv.log.toPlainText()
print(conv.log.toPlainText())
print("GUI SMOKE OK")
