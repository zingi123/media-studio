from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox, QDoubleSpinBox,
                               QSpinBox, QTableWidget, QTableWidgetItem, QFileDialog, QMessageBox, QProgressBar, QHeaderView)
from ..shared.utils import fmt_time, parse_time, unique_path
from ..translator import subtitles as S
from ..translator.ocr_subtitles import extract
from .workers import Worker


class OcrPage(QWidget):  # ទំព័រដាច់ដោយឡែក: Video → OCR → តារាងកែបាន → SRT
    def __init__(self, ctx):
        super().__init__()
        self.cues, self.video, self.worker, self._loading = [], "", None, False  # ស្ថានភាព
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<h2>Subtitle OCR</h2>Video → OCR → edit → SRT"))
        r = QHBoxLayout()
        self.vpath = QLineEdit(); self.vpath.setReadOnly(True)
        b = QPushButton("Import Video"); b.clicked.connect(self.pick)  # ជ្រើសវីដេអូ
        r.addWidget(self.vpath, 1); r.addWidget(b); v.addLayout(r)
        r = QHBoxLayout()
        self.lang = QComboBox(); self.lang.addItems(["ch", "en"])  # ភាសា OCR
        self.top = QSpinBox(); self.top.setRange(0, 95); self.top.setValue(65); self.top.setSuffix("% top")  # ចាប់ពីកម្ពស់
        self.bot = QSpinBox(); self.bot.setRange(5, 100); self.bot.setValue(100); self.bot.setSuffix("% bottom")  # ដល់កម្ពស់
        self.step = QDoubleSpinBox(); self.step.setRange(0.1, 2.0); self.step.setSingleStep(0.05); self.step.setValue(0.25); self.step.setSuffix(" s")  # ចន្លោះ sampling
        for lbl, w in (("Language", self.lang), ("Subtitle area", self.top), ("", self.bot), ("Sample every", self.step)):
            r.addWidget(QLabel(lbl)); r.addWidget(w)
        for t, f in (("Run OCR", self.run), ("Cancel", lambda: self.worker and self.worker.cancel())):
            b = QPushButton(t); b.clicked.connect(f); r.addWidget(b)
        v.addLayout(r)
        self.table = QTableWidget(0, 4); self.table.setHorizontalHeaderLabels(["No.", "Start", "End", "Text"])  # តារាងកែបាន
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._edited); v.addWidget(self.table, 1)
        r = QHBoxLayout()
        for t, f in (("Add Line", self.add), ("Delete Line(s)", self.delete), ("Export SRT", self.export)):
            b = QPushButton(t); b.clicked.connect(f); r.addWidget(b)
        v.addLayout(r)
        self.bar = QProgressBar(); v.addWidget(self.bar)
        self.msg = QLabel(""); v.addWidget(self.msg)

    def pick(self):
        f, _ = QFileDialog.getOpenFileName(self, "Video", "", "Video (*.mp4 *.mkv *.mov *.avi *.webm *.flv *.m4v)")
        if f:
            self.video = f; self.vpath.setText(f)

    def refresh(self):  # បង្ហាញ cues ក្នុងតារាង
        self._loading = True
        self.table.setRowCount(len(self.cues))
        for r, c in enumerate(self.cues):
            for k, t in enumerate((str(r + 1), fmt_time(c.start), fmt_time(c.end), c.text)):
                it = QTableWidgetItem(t)
                if k == 0:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, k, it)
        self._loading = False

    def _edited(self, it):  # កែក្នុងតារាង → cue
        if self._loading:
            return
        c, k = self.cues[it.row()], it.column()
        try:
            if k == 1: c.start = parse_time(it.text())
            elif k == 2: c.end = parse_time(it.text())
            elif k == 3: c.text = it.text()
        except ValueError:
            QMessageBox.warning(self, "Time", "Use HH:MM:SS,mmm"); self.refresh()

    def run(self):
        if not self.video:
            QMessageBox.information(self, "OCR", "Import a video first."); return
        if self.worker and self.worker.isRunning():
            return
        if self.top.value() >= self.bot.value():
            QMessageBox.warning(self, "OCR", "Top must be smaller than bottom."); return
        self.msg.setText("Running... (first run may load the OCR model)")
        w = Worker(extract, self.video, top=self.top.value() / 100, bottom=self.bot.value() / 100,
                   step=self.step.value(), lang=self.lang.currentText())  # រត់ក្នុង thread
        w.progress.connect(lambda p: self.bar.setValue(p if isinstance(p, int) else 0))
        w.done.connect(lambda c: (setattr(self, "cues", c), self.refresh(), self.msg.setText(f"✓ {len(c)} subtitles. Edit, then export.")))
        w.failed.connect(lambda m: self.msg.setText("✗ " + m))
        self.worker = w; w.start()

    def add(self):
        r = self.table.currentRow(); b = self.cues[r].end if 0 <= r < len(self.cues) else 0
        self.cues.insert(r + 1 if r >= 0 else len(self.cues), S.Cue(b, b + 2, "")); self.refresh()

    def delete(self):
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            del self.cues[r]
        self.refresh()

    def export(self):
        if not self.cues:
            return
        self.table.setCurrentCell(-1, -1)  # ยืนยันការកែដែលកំពុងធ្វើ
        start = str((Path(self.video).parent if self.video else Path.home()) / (Path(self.video).stem + "_ocr.srt" if self.video else "ocr.srt"))
        f, _ = QFileDialog.getSaveFileName(self, "Export SRT", start, "SRT (*.srt)")
        if f:
            Path(f).write_text(S.to_srt(sorted(self.cues, key=lambda c: c.start), "text"), encoding="utf-8")  # លេខរៀងថ្មីដោយស្វ័យប្រវត្តិ
            self.msg.setText("Saved " + f)
