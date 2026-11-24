from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QComboBox,
                               QPushButton, QHeaderView, QPlainTextEdit)
from ..translator.voice_presets import CATALOG, default_preset, resolve


def all_types():
    return [n for s in CATALOG.values() for items in s.values() for n in items]


def preset_text(p):
    if not p:
        return "-"
    return f"{p['base']} | pitch {p['pitch']:+d}% | speed {p['speed']:+d}% | formant {p['formant']:+d}%" + (f" | {p['fx']}" if p["fx"] else "")


class VoiceDetectDialog(QDialog):
    """Review detected speakers; the user may correct any Voice Type before it is applied."""

    def __init__(self, result, saved, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Detected Voice Types - review"); self.resize(900, 420)
        self.saved, self.sp, self.chosen = saved, result["speakers"], {}
        v = QVBoxLayout(self)
        v.addWidget(QLabel("AI estimates only (not certain). Correct any row, then Apply. No audio is generated here."))
        self.t = QTableWidget(len(self.sp), 6)
        self.t.setHorizontalHeaderLabels(["Speaker ID", "Voice Type", "Confidence", "Preset settings", "Speech (s)", "Evidence"])
        self.t.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.t.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.combos = []
        for r, s in enumerate(self.sp):
            for k, val in ((0, s["id"]), (2, f"{s['confidence']:.0%}"), (4, str(s["seconds"])), (5, s["note"])):
                it = QTableWidgetItem(val); it.setFlags(it.flags() & ~Qt.ItemIsEditable); self.t.setItem(r, k, it)
            cb = QComboBox(); cb.addItems(["Unknown"] + all_types()); cb.setCurrentText(resolve(s["type"]))
            cb.currentTextChanged.connect(lambda _t, row=r: self._upd(row))
            self.t.setCellWidget(r, 1, cb); self.combos.append(cb)
            self._upd(r)
        self.t.setColumnWidth(0, 90); self.t.setColumnWidth(1, 160); self.t.setColumnWidth(2, 90); self.t.setColumnWidth(4, 80)
        v.addWidget(self.t, 1)
        if result["notes"]:
            n = QPlainTextEdit("\n".join("• " + x for x in result["notes"])); n.setReadOnly(True); n.setFixedHeight(70); v.addWidget(n)
        r = QHBoxLayout(); r.addStretch()
        b = QPushButton("Cancel"); b.clicked.connect(self.reject); r.addWidget(b)
        b = QPushButton("APPLY TO LINES"); b.setStyleSheet("font-weight:bold"); b.clicked.connect(self._apply); r.addWidget(b)
        v.addLayout(r)

    def _upd(self, row):  # preset shown = saved preset if user saved one, else catalog preset (never invented)
        n = self.combos[row].currentText()
        p = None if n == "Unknown" else (self.saved.get(n) or default_preset(n))
        it = QTableWidgetItem(preset_text(p)); it.setFlags(it.flags() & ~Qt.ItemIsEditable); self.t.setItem(row, 3, it)

    def _apply(self):
        self.chosen = {s["id"]: self.combos[i].currentText() for i, s in enumerate(self.sp)}
        self.accept()
