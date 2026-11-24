"""OCR vs speech review: rows where the burned-in subtitle differs from what Whisper heard. Nothing is replaced until you decide."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QLabel,
                               QHeaderView, QAbstractItemView)
from ..translator import ocr_fix as OF
from ..shared.utils import fmt_time


class OcrReviewDialog(QDialog):
    changed = Signal()

    def __init__(self, cues, parent=None):
        super().__init__(parent)
        self.cues = cues
        self.setWindowTitle("Review: speech (Whisper) vs burned-in subtitle (OCR)"); self.resize(900, 460)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Timing always stays Whisper's. Choose which TEXT is right for each row; unresolved rows keep the speech text."))
        self.t = QTableWidget(0, 5); self.t.setHorizontalHeaderLabels(["Row", "Time", "Speech (Whisper)", "Subtitle (OCR)", "Similarity"])
        self.t.setEditTriggers(QAbstractItemView.NoEditTriggers); self.t.setSelectionBehavior(QAbstractItemView.SelectRows)
        h = self.t.horizontalHeader(); h.setSectionResizeMode(2, QHeaderView.Stretch); h.setSectionResizeMode(3, QHeaderView.Stretch)
        v.addWidget(self.t, 1)
        r = QHBoxLayout()
        for txt, fn in (("Use subtitle text", lambda: self._decide(True)), ("Keep speech text", lambda: self._decide(False)),
                        ("Accept all similar (≥ 80%)", self._all_similar), ("Close", self.accept)):
            b = QPushButton(txt); b.clicked.connect(fn); r.addWidget(b)
        r.addStretch(); v.addLayout(r)
        self.rows = []
        self.reload()

    def reload(self):
        self.rows = [i for i, c in enumerate(self.cues) if c.text_conflict]
        self.t.setRowCount(len(self.rows))
        for r, i in enumerate(self.rows):
            c = self.cues[i]
            for k, val in enumerate((str(i + 1), f"{fmt_time(c.start)} → {fmt_time(c.end)}", c.text, c.ocr_text, f"{c.ocr_sim:.0%}")):
                self.t.setItem(r, k, QTableWidgetItem(val))
        self.setWindowTitle(f"Review: speech vs subtitle - {len(self.rows)} difference(s) left")

    def _decide(self, use_ocr):
        r = self.t.currentRow()
        if r >= 0:
            OF.resolve(self.cues[self.rows[r]], use_ocr); self.reload(); self.changed.emit()

    def _all_similar(self):
        OF.accept_all_similar(self.cues, 0.8); self.reload(); self.changed.emit()
