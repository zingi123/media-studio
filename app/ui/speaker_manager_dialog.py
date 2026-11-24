"""Speaker Manager dialog - thin UI over translator.speakers.SpeakerBook (all rules live in the backend)."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QLabel,
                               QInputDialog, QMessageBox, QHeaderView, QAbstractItemView, QPlainTextEdit)
from ..translator.speakers import SpeakerBook, SpeakerError


class SpeakerManagerDialog(QDialog):
    changed = Signal()                     # rows / voices changed -> the page refreshes its table

    def __init__(self, book: SpeakerBook, cues, voices: dict, selected_rows=(), suggestions=(), preview_cb=None, save_cb=None, parent=None):
        """voices: {label: voice id} (edge-tts voices from khmer_tts.VOICES).  preview_cb(speaker_id) plays a sample."""
        super().__init__(parent)
        self.book, self.cues, self.voices, self.sel = book, cues, voices, list(selected_rows)
        self.preview_cb, self.save_cb = preview_cb, save_cb
        self.setWindowTitle("Speaker Manager"); self.resize(900, 460)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("One character = one voice for the whole video. Speakers are never merged automatically - "
                           "similar voices are only suggested below."))
        self.t = QTableWidget(0, 7)
        self.t.setHorizontalHeaderLabels(["ID", "Name", "Rows", "Gender", "Voice", "Locked", "Notes"])
        self.t.setSelectionBehavior(QAbstractItemView.SelectRows); self.t.setSelectionMode(QAbstractItemView.SingleSelection)
        self.t.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.t.itemChanged.connect(self._edited)
        v.addWidget(self.t, 1)
        self.sug = QPlainTextEdit(); self.sug.setReadOnly(True); self.sug.setFixedHeight(60); v.addWidget(self.sug)
        self.suggestions = list(suggestions)
        for rows in ((("Rename", self.rename), ("Merge into...", self.merge), ("Split selected rows", self.split),
                      ("Reassign selected rows to...", self.reassign)),
                     (("Lock / Unlock", self.toggle_lock), ("Set voice...", self.set_voice), ("Preview voice", self.preview),
                      ("Apply voices to all rows", self.apply_voices), ("Save", self.save))):
            r = QHBoxLayout()
            for txt, fn in rows:
                b = QPushButton(txt); b.clicked.connect(fn); r.addWidget(b)
            r.addStretch(); v.addLayout(r)
        r = QHBoxLayout(); r.addStretch(); b = QPushButton("Close"); b.clicked.connect(self.accept); r.addWidget(b); v.addLayout(r)
        self.reload()

    # ---------------------------------------------------------------- helpers
    def reload(self):
        self.book.sync_from_cues(self.cues)
        self.t.blockSignals(True)
        self.t.setRowCount(len(self.book.speakers))
        for r, (sid, sp) in enumerate(self.book.speakers.items()):
            vals = [sid, sp.name, str(len(self.book.rows_of(sid, self.cues))), sp.gender or "-",
                    next((k for k, x in self.voices.items() if x == sp.voice), sp.voice or "-"), "🔒" if sp.locked else "", sp.notes]
            for k, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if k not in (1, 6):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.t.setItem(r, k, it)
        self.t.blockSignals(False)
        lines = [f"• {a} and {b} sound similar ({c:.0%}) - merge only if you know they are the same person." for a, b, c in self.suggestions]
        lines += [f"• {a} and {b} sound similar ({c:.0%}) - merge only if you know they are the same person."
                  for a, b, c in self.book.suggest_merges() if (a, b, c) not in self.suggestions]
        self.sug.setPlainText("\n".join(lines) or "No merge suggestions.")

    def _sid(self):
        r = self.t.currentRow()
        if r < 0:
            QMessageBox.information(self, "Speaker", "Select a speaker first."); return None
        return self.t.item(r, 0).text()

    def _do(self, fn):
        try:
            res = fn()
        except SpeakerError as e:
            QMessageBox.warning(self, "Speaker Manager", str(e)); return None
        self.reload(); self.changed.emit(); return res

    def _pick(self, title, exclude=None):
        ids = [i for i in self.book.speakers if i != exclude]
        labels = [f"{i}  ({self.book.get(i).name})" if self.book.get(i).name else i for i in ids]
        txt, ok = QInputDialog.getItem(self, "Speaker Manager", title, labels, 0, False)
        return ids[labels.index(txt)] if ok else None

    def _edited(self, it):
        sid = self.t.item(it.row(), 0).text()
        if it.column() == 1:
            self._do(lambda: self.book.rename(sid, it.text()))
        elif it.column() == 6:
            self.book.get(sid).notes = it.text()

    # ---------------------------------------------------------------- actions
    def rename(self):
        sid = self._sid()
        if not sid:
            return
        n, ok = QInputDialog.getText(self, "Rename speaker", f"Name for {sid}:", text=self.book.get(sid).name)
        if ok:
            self._do(lambda: self.book.rename(sid, n))

    def merge(self):
        sid = self._sid()
        dst = self._pick(f"Merge {sid} INTO:", exclude=sid) if sid else None
        if sid and dst and QMessageBox.question(self, "Merge", f"All rows of {sid} become {dst}. Continue?") == QMessageBox.Yes:
            self._do(lambda: self.book.merge(sid, dst, self.cues))

    def split(self):
        sid = self._sid()
        if sid:
            n = self._do(lambda: self.book.split(sid, self.sel, self.cues))
            if n:
                QMessageBox.information(self, "Split", f"The selected rows now belong to the new speaker '{n}'.")

    def reassign(self):
        if not self.sel:
            QMessageBox.information(self, "Reassign", "Select rows in the translation table first, then reopen this window."); return
        dst = self._pick("Move the selected rows to:")
        if dst:
            self._do(lambda: self.book.reassign(self.sel, dst, self.cues))

    def toggle_lock(self):
        sid = self._sid()
        if sid:
            sp = self.book.get(sid); (self.book.unlock if sp.locked else self.book.lock)(sid); self.reload()

    def set_voice(self):
        sid = self._sid()
        if not sid:
            return
        labels = list(self.voices)
        txt, ok = QInputDialog.getItem(self, "Voice", f"Khmer voice for {sid}:", labels, 0, False)
        if ok:
            gender = "female" if "female" in txt.lower() or "sreymom" in self.voices[txt].lower() else "male"
            self._do(lambda: self.book.set_voice(sid, voice=self.voices[txt], gender=gender))

    def preview(self):
        sid = self._sid()
        if not sid:
            return
        if not self.book.get(sid).voice:
            QMessageBox.information(self, "Preview", "Choose a voice for this speaker first (Set voice...)."); return
        if self.preview_cb:
            self.preview_cb(sid)

    def apply_voices(self):
        n = self.book.apply_to_cues(self.cues); self.changed.emit()
        QMessageBox.information(self, "Voices", f"{n} row(s) updated. Regenerate their audio to hear the change.")

    def save(self):
        if self.save_cb:
            self.save_cb()
