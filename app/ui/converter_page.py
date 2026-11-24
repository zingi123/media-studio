import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QLineEdit, QFileDialog, QProgressBar,
                               QMenu, QMessageBox, QPlainTextEdit, QFormLayout, QStackedWidget, QGroupBox)
from ..converter.frame_extractor import extract_frames
from ..converter.image_converter import convert_image
from ..converter.video_converter import convert_video, QUALITY_CRF
from ..converter.video_splitter import split_video
from ..converter.video_merger import merge_videos, plan_order, MODES as MERGE_MODES
from ..shared.ffmpeg_util import probe
from ..shared.utils import kind_of, open_path, natural_key, VIDEO_EXT, IMAGE_EXT
from .workers import Worker

MODES = ["Convert videos", "Convert images", "Split video (create short)", "Extract frames",
         "Merge videos (Episode 1 → N into ONE video)"]


def _ranges(nums):  # [4, 6, 7, 8, 9] -> "4, 6-9"
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def run_batch(tasks, parallel, progress=None, cancel=None):
    """tasks: list of (index, callable(progress_cb, cancel) -> result). Failures never stop the batch."""
    results = {}

    def one(idx, fn):
        try:
            if cancel and cancel():
                return idx, "Cancelled", None
            progress(("file", idx, 0, "Working..."))
            r = fn(lambda p: progress(("file", idx, p, f"{p}%")), cancel)
            progress(("file", idx, 100, "Done"))
            return idx, "Done", r
        except Exception as e:  # noqa
            progress(("file", idx, 0, "Failed: " + str(e).splitlines()[0][:80]))
            return idx, "Failed", str(e)

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        futs = [ex.submit(one, i, f) for i, f in tasks]
        for f in futs:
            i, st, r = f.result()
            results[i] = (st, r)
    return results


class ConverterPage(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.worker = None
        self.setAcceptDrops(True)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<h2>Converter</h2>Drag & drop files or folders below. Right-click a file to inspect it "
                           "or create short clips. (No crop/rotate - by design.)"))
        self.files = QListWidget()
        self.files.setSelectionMode(QListWidget.ExtendedSelection)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.files, 1)
        r = QHBoxLayout()
        for t, f in (("Add Files", self.add_files), ("Add Folder", self.add_folder), ("Remove Selected", self.remove),
                     ("Clear", self.files.clear)):
            b = QPushButton(t)
            b.clicked.connect(f)
            r.addWidget(b)
        r.addStretch()
        v.addLayout(r)

        r = QHBoxLayout()
        r.addWidget(QLabel("Task:"))
        self.mode = QComboBox()
        self.mode.addItems(MODES)
        r.addWidget(self.mode)
        r.addWidget(QLabel("Output folder (blank = next to source):"))
        self.out = QLineEdit()
        r.addWidget(self.out, 1)
        b = QPushButton("Browse")
        b.clicked.connect(lambda: self.out.setText(QFileDialog.getExistingDirectory(self, "Output") or self.out.text()))
        r.addWidget(b)
        v.addLayout(r)

        self.stack = QStackedWidget()
        self.mode.currentIndexChanged.connect(self.stack.setCurrentIndex)
        v.addWidget(self.stack)
        # 0 video
        w = QGroupBox("Video options")
        f = QFormLayout(w)
        self.vfmt = QComboBox(); self.vfmt.addItems(["mp4", "mkv", "mov", "webm", "avi"])
        self.vh = QComboBox(); self.vh.addItems(["Original", "2160", "1440", "1080", "720", "480", "360"])
        self.vfps = QComboBox(); self.vfps.addItems(["Original", "60", "30", "25", "24", "15"])
        self.vq = QComboBox(); self.vq.addItems(list(QUALITY_CRF))
        self.vq.setCurrentText("Medium")
        self.vhw = QCheckBox("Use GPU encoding if available (falls back to CPU)")
        self.vhw.setChecked(bool(ctx.settings.get("hw_accel")))
        for lbl, wd in (("Format", self.vfmt), ("Height (px)", self.vh), ("Frame rate", self.vfps),
                        ("Quality / compression", self.vq), ("", self.vhw)):
            f.addRow(lbl, wd)
        self.stack.addWidget(w)
        # 1 image
        w = QGroupBox("Image options")
        f = QFormLayout(w)
        self.ifmt = QComboBox(); self.ifmt.addItems(["jpg", "png", "webp"])
        self.iside = QSpinBox(); self.iside.setRange(0, 20000); self.iside.setSpecialValueText("Original")
        self.iq = QSpinBox(); self.iq.setRange(1, 100); self.iq.setValue(90)
        f.addRow("Format", self.ifmt); f.addRow("Max side (px)", self.iside); f.addRow("Quality", self.iq)
        self.stack.addWidget(w)
        # 2 split
        w = QGroupBox("Split options")
        f = QFormLayout(w)
        self.smode = QComboBox(); self.smode.addItems(["Fixed duration", "Random range"])
        self.smin = QDoubleSpinBox(); self.smin.setRange(1, 3600); self.smin.setValue(20); self.smin.setSuffix(" s")
        self.smax = QDoubleSpinBox(); self.smax.setRange(1, 3600); self.smax.setValue(35); self.smax.setSuffix(" s")
        f.addRow("Mode", self.smode); f.addRow("Duration / minimum", self.smin); f.addRow("Maximum (random only)", self.smax)
        self.stack.addWidget(w)
        # 3 frames
        w = QGroupBox("Frame extraction")
        f = QFormLayout(w)
        self.fcount = QSpinBox(); self.fcount.setRange(1, 1000); self.fcount.setValue(10)
        self.fint = QDoubleSpinBox(); self.fint.setRange(0, 3600); self.fint.setSpecialValueText("Off")
        self.ffmt = QComboBox(); self.ffmt.addItems(["jpg", "png", "webp"])
        f.addRow("Number of frames", self.fcount); f.addRow("...or every N seconds", self.fint); f.addRow("Format", self.ffmt)
        self.stack.addWidget(w)
        # 4 merge
        w = QGroupBox("Merge options - order = the number in each file name (Episode 001, Part 002, EP3 ... any amount)")
        f = QFormLayout(w)
        self.mname = QLineEdit(); self.mname.setPlaceholderText("blank = automatic, e.g. Part 001-045")
        self.mmode = QComboBox(); self.mmode.addItems(MERGE_MODES)
        self.mmode.setCurrentText(str(ctx.settings.get("merge_mode")))
        self.mmode.currentTextChanged.connect(lambda t: (ctx.settings.set("merge_mode", t), ctx.settings.save()))
        self.mmode.setToolTip("Auto: no re-encoding when all files match, otherwise only the different ones are re-encoded.\n"
                              "Fast: never re-encode (refuses if the files differ).  Safe: re-encode everything (slow).")
        self.mchap = QCheckBox("Add a chapter for every episode (jump between episodes in the player)")
        self.mchap.setChecked(bool(ctx.settings.get("merge_chapters")))
        self.mchap.toggled.connect(lambda x: (ctx.settings.set("merge_chapters", bool(x)), ctx.settings.save()))
        self.msort = QPushButton("Sort list by episode number"); self.msort.clicked.connect(self.sort_list)
        f.addRow("Output name", self.mname); f.addRow("Method", self.mmode); f.addRow("", self.mchap); f.addRow("", self.msort)
        self.stack.addWidget(w)

        r = QHBoxLayout()
        r.addWidget(QLabel("Files at once:"))
        self.par = QSpinBox(); self.par.setRange(1, 8); self.par.setValue(1)
        r.addWidget(self.par)
        self.delorig = QCheckBox("Delete originals after success (asks to confirm)")
        r.addWidget(self.delorig)
        r.addStretch()
        self.start = QPushButton("Start")
        self.start.clicked.connect(self.run)
        self.stopb = QPushButton("Cancel")
        self.stopb.clicked.connect(lambda: self.worker and self.worker.cancel())
        self.openb = QPushButton("Open Output Folder")
        self.openb.clicked.connect(self.open_out)
        for b in (self.start, self.stopb, self.openb):
            r.addWidget(b)
        v.addLayout(r)
        self.bar = QProgressBar()
        v.addWidget(self.bar)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setFixedHeight(80)
        v.addWidget(self.log)
        self.last_dir = None

    # ----- file list -----
    def add_paths(self, paths):
        have = {self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())}
        for p in paths:
            p = Path(p)
            cand = [x for x in p.rglob("*") if x.is_file()] if p.is_dir() else [p]
            for x in sorted(cand, key=natural_key):
                if kind_of(x) in ("video", "image") and str(x) not in have:
                    it = QListWidgetItem(f"[{kind_of(x)}] {x.name}")
                    it.setData(Qt.UserRole, str(x))
                    it.setToolTip(str(x))
                    self.files.addItem(it)
                    have.add(str(x))

    def add_files(self):
        exts = " ".join("*" + e for e in sorted(VIDEO_EXT | IMAGE_EXT))
        fs, _ = QFileDialog.getOpenFileNames(self, "Add files", "", f"Media ({exts})")
        self.add_paths(fs)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add folder")
        if d:
            self.add_paths([d])

    def sort_list(self):  # list order = episode order (by the number in the file name)
        paths = [self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())]
        vids = [p for p in paths if kind_of(p) == "video"]
        order = plan_order(vids)["order"] + [p for p in paths if kind_of(p) != "video"]
        self.files.clear()
        self.add_paths(order)
        self.log.appendPlainText(f"Sorted {len(vids)} video(s) by episode number.")

    def _run_merge(self):
        paths = [self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())]
        paths = [p for p in paths if kind_of(p) == "video"]
        if len(paths) < 2:
            QMessageBox.information(self, "Merge", "Add at least 2 video files (all your episodes) to merge.")
            return
        plan = plan_order(paths)
        first, last = Path(plan["order"][0]).name, Path(plan["order"][-1]).name
        self.log.appendPlainText(f"Merge order: {first}  →  {last}  ({len(paths)} files, sorted by episode number)")
        warn = []
        if plan["missing"]:
            warn.append("Missing episode number(s): " + _ranges(plan["missing"]))
        if plan["dupes"]:
            warn.append("Duplicate episode number(s): " + _ranges(plan["dupes"]))
        if plan["unnumbered"]:
            warn.append("No number found in: " + ", ".join(plan["unnumbered"][:5]) + " (they are placed last)")
        if warn:
            self.log.appendPlainText("⚠ " + " | ".join(warn))
            if QMessageBox.question(self, "Check the list", "\n".join(warn) + "\n\nMerge anyway?") != QMessageBox.Yes:
                return
        out_root = self.out.text().strip()
        out_dir = (Path(out_root) if out_root else Path(plan["order"][0]).parent) / "Merged"
        self.last_dir = out_dir
        self.bar.setValue(0)
        self.start.setEnabled(False)
        w = Worker(merge_videos, paths, out_dir, self.mname.text(), self.mmode.currentText(), self.mchap.isChecked())
        w.progress.connect(lambda p: self.bar.setValue(p) if isinstance(p, int) else self.log.appendPlainText(str(p)))
        w.done.connect(self._merge_done)
        w.failed.connect(lambda m: (self.log.appendPlainText("✗ " + m), self.start.setEnabled(True)))
        self.worker = w
        w.start()

    def _merge_done(self, r):
        self.log.appendPlainText(f"✓ Merged {r['files']} files into ONE video: {r['out']}"
                                 + (f"  ({r['reencoded']} file(s) re-encoded)" if r["reencoded"] else "  (no re-encoding)"))
        self.start.setEnabled(True)

    def remove(self):
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls()])

    def _menu(self, pos):
        it = self.files.itemAt(pos)
        if not it:
            return
        path = it.data(Qt.UserRole)
        m = QMenu(self)
        a1 = m.addAction("Inspect (resolution / FPS)")
        a2 = m.addAction("Create short video...") if kind_of(path) == "video" else None
        a3 = m.addAction("Remove")
        act = m.exec(self.files.mapToGlobal(pos))
        if act == a1:
            self.inspect(it)
        elif a2 and act == a2:
            self.files.clearSelection()
            it.setSelected(True)
            self.mode.setCurrentIndex(2)
            self.log.appendPlainText("Set Split options, then press Start (only the selected file will be split).")
        elif act == a3:
            self.files.takeItem(self.files.row(it))

    def inspect(self, it):
        path = it.data(Qt.UserRole)
        try:
            if kind_of(path) == "video":
                i = probe(path)
                txt = (f"{i['width']}x{i['height']}  |  {i['fps']:.2f} fps  |  {i['duration']:.1f}s  |  "
                       f"{i['vcodec']}  |  {Path(path).suffix[1:]}  |  {os.path.getsize(path) / 1e6:.1f} MB")
            else:
                from PIL import Image
                with Image.open(path) as im:
                    txt = f"{im.width}x{im.height}  |  {im.format}  |  {os.path.getsize(path) / 1e6:.2f} MB"
            self.log.appendPlainText(f"{Path(path).name}: {txt}")
        except Exception as e:  # noqa
            self.log.appendPlainText(f"{Path(path).name}: cannot read ({e})")

    # ----- run -----
    def _paths(self):
        sel = [i.data(Qt.UserRole) for i in self.files.selectedItems()]
        return sel or [self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())]

    def run(self):
        mode = self.mode.currentIndex()
        if mode == 4:
            return self._run_merge()
        want = "image" if mode == 1 else "video"
        paths = [p for p in self._paths() if kind_of(p) == want]
        if not paths:
            QMessageBox.information(self, "Converter", f"Add at least one {want} file for this task.")
            return
        if self.delorig.isChecked() and mode in (0, 1):
            if QMessageBox.question(self, "Delete originals?",
                                    f"{len(paths)} original file(s) will be PERMANENTLY deleted after each "
                                    "successful conversion. Continue?") != QMessageBox.Yes:
                return
        out_root = self.out.text().strip() or None
        tasks = []
        for i, p in enumerate(paths):
            od = Path(out_root) if out_root else Path(p).parent
            if mode == 0:
                h = 0 if self.vh.currentText() == "Original" else int(self.vh.currentText())
                fps = 0 if self.vfps.currentText() == "Original" else float(self.vfps.currentText())
                fn = lambda pr, c, p=p, od=od, h=h, fps=fps: convert_video(  # noqa
                    p, od / "Converted", self.vfmt.currentText(), h, fps, self.vq.currentText(),
                    hw=self.vhw.isChecked(), progress=pr, cancel=c)
            elif mode == 1:
                fn = lambda pr, c, p=p, od=od: convert_image(  # noqa
                    p, od / "Converted", self.ifmt.currentText(), self.iside.value(), self.iq.value(), pr, c)
            elif mode == 2:
                m = "fixed" if self.smode.currentIndex() == 0 else "random"
                fn = lambda pr, c, p=p, m=m: split_video(  # noqa
                    p, out_root or Path(p).parent, m, self.smin.value(), self.smax.value(), progress=pr, cancel=c)
            else:
                fn = lambda pr, c, p=p: extract_frames(  # noqa
                    p, out_root or Path(p).parent, self.fcount.value(), self.fint.value(), self.ffmt.currentText(), pr, c)
            tasks.append((i, fn))
        self.cur_paths, self.cur_mode = paths, mode
        self.last_dir = Path(out_root) if out_root else Path(paths[0]).parent
        self.bar.setValue(0)
        self.pct = {}
        self.start.setEnabled(False)
        self.worker = Worker(run_batch, tasks, self.par.value())
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(lambda m: (self.log.appendPlainText("✗ " + m), self.start.setEnabled(True)))
        self.worker.start()

    def _progress(self, t):
        _, idx, pct, msg = t
        self.pct[idx] = pct
        self.bar.setValue(int(sum(self.pct.values()) / len(self.cur_paths)))
        if msg in ("Done",) or msg.startswith("Failed"):
            self.log.appendPlainText(f"{Path(self.cur_paths[idx]).name}: {msg}")

    def _done(self, results):
        ok = sum(1 for s, _ in results.values() if s == "Done")
        self.log.appendPlainText(f"Finished: {ok} succeeded, {len(results) - ok} failed/cancelled.")
        if self.delorig.isChecked() and self.cur_mode in (0, 1):
            for i, (s, _) in results.items():
                if s == "Done":
                    try:
                        os.remove(self.cur_paths[i])
                    except OSError as e:
                        self.log.appendPlainText(f"Could not delete {self.cur_paths[i]}: {e}")
        self.start.setEnabled(True)

    def open_out(self):
        if self.last_dir and self.last_dir.exists():
            open_path(self.last_dir)
