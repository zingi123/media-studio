from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QWidget, QFormLayout, QComboBox, QSpinBox, QLineEdit)
from ..shared.utils import open_path
from ..translator.voice_presets import CATALOG, GROUPS, LIM, default_preset, display_name, demo_file, DEMO_TEXT
from .workers import Worker

_KEEP = []  # thread ដែលកំពុងរត់ពេលបិទផ្ទាំង (កុំឱ្យ crash)


class AddVoiceTypeDialog(QDialog):
    """Category → Subcategory → (Group: Boy/Girl/Man/...) → Voice (Boy 1/2/3) → Review → ADD.
    គ្រប់សំឡេងកែ setting បាន (Base/Pitch/Speed/Formant/Effects) ហើយរក្សាទុក។ Back/Cancel supported."""

    ALL_TXT = "\u25B6  DEMO ALL"  # អក្សរប៊ូតុង

    def __init__(self, selected, parent=None, nicks=None):
        super().__init__(parent)
        self.nicks = nicks or {}  # Nickname ដែលបានរក្សាទុកជាអចិន្ត្រៃយ៍
        self.selected, self.result_item = selected, None  # ដែលបានជ្រើសរួច / លទ្ធផល (ឈ្មោះ, preset)
        self.cat = self.sub = self.group = self.name = ""  # ជម្រើសបច្ចុប្បន្ន
        self.hist, self.step = [], "cat"  # ប្រវត្តិជំហាន សម្រាប់ Back
        self.setWindowTitle("Add Voice Type"); self.resize(440, 470)  # ចំណងជើង និងទំហំ
        v = QVBoxLayout(self)
        self.title = QLabel(); v.addWidget(self.title)  # ចំណងជើងជំហាន
        self.list = QListWidget(); self.list.itemClicked.connect(self._pick); v.addWidget(self.list, 1)  # បញ្ជីជម្រើស
        self.form = QWidget(); fl = QFormLayout(self.form)  # ទម្រង់ review
        self.nick = QLineEdit(); self.nick.setPlaceholderText("Nickname (optional) e.g. Sophea"); self.nick.setMaxLength(30)  # ឈ្មោះហៅ
        fl.addRow("Nickname", self.nick)
        self.base = QComboBox(); self.base.addItems(["P", "S"])  # P=Piseth S=Sreymom
        self.pitch, self.speed, self.formant = QSpinBox(), QSpinBox(), QSpinBox()  # តម្លៃលេខ
        for sp in (self.pitch, self.speed, self.formant):
            sp.setRange(*LIM); sp.setSuffix("%")  # ជួរ -90..100
        self.fx = QLineEdit(); self.fx.setPlaceholderText("e.g. Reverb, Echo")  # effects ដាក់ក្បៀស
        self.setting_widgets = (self.base, self.pitch, self.speed, self.formant, self.fx)
        for lbl, w in (("Base (P/S)", self.base), ("Pitch", self.pitch), ("Speed", self.speed),
                       ("Formant", self.formant), ("Effects", self.fx)):
            fl.addRow(lbl, w)  # បន្ថែមជួរ
        self.demo_text = QLineEdit(DEMO_TEXT); self.demo_text.setToolTip("Type any Khmer sentence to hear this voice")
        fl.addRow("Demo text", self.demo_text)
        self.demo_btn = QPushButton("\u25B6  DEMO - hear this voice"); self.demo_btn.setMinimumHeight(30)
        self.demo_btn.setStyleSheet("font-weight:bold"); self.demo_btn.clicked.connect(self._demo)
        fl.addRow(self.demo_btn)
        self.demo_msg = QLabel(); self.demo_msg.setWordWrap(True); fl.addRow(self.demo_msg)
        self.player = self.worker = None
        v.addWidget(self.form)
        r = QHBoxLayout()
        self.back = QPushButton("Back"); self.back.clicked.connect(self._back)  # ត្រឡប់ក្រោយ
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)  # បោះបង់
        self.add = QPushButton("ADD TO SELECTED VOICE TYPES"); self.add.clicked.connect(self._add)  # បន្ថែម
        self.all_btn = QPushButton(self.ALL_TXT); self.all_btn.clicked.connect(self._demo_all)  # លេងទាំងអស់
        self.queue, self.playing_all, self.cur = [], False, ""  # ជួរសំឡេងដែលត្រូវលេង / ស្ថានភាព / ឈ្មោះបច្ចុប្បន្ន
        for b in (self.all_btn, self.back, cancel, self.add):
            r.addWidget(b)
        v.addLayout(r)
        self._show("cat")  # ចាប់ផ្តើមពីប្រភេទ

    def _items(self):  # បញ្ជីឈ្មោះសម្រាប់ជំហាន "type"
        return list(GROUPS[self.sub][self.group]) if self.group else list(CATALOG[self.cat][self.sub])

    def _label(self, n):  # ឈ្មោះក្នុងបញ្ជី + nickname + សម្គាល់ជាន់គ្នា
        return display_name(n, self.selected.get(n) or {"nick": self.nicks.get(n, "")}) + ("  (added)" if n in self.selected else "")

    def _show(self, step):  # បង្ហាញជំហាន
        self.step = step
        self.queue, self.playing_all = [], False; self.all_btn.setText(self.ALL_TXT); self.all_btn.setEnabled(True)  # បញ្ឈប់ការលេងទាំងអស់
        self.all_btn.setVisible(step != "review")  # បង្ហាញគ្រប់ជំហាន លើកលែង review
        self.list.clear()  # សម្អាតបញ្ជី
        self.form.setVisible(step == "review"); self.list.setVisible(step != "review")  # form ឬ list
        self.add.setVisible(step == "review"); self.back.setEnabled(bool(self.hist))  # ប៊ូតុង
        if step == "cat":
            self.title.setText("Choose Category"); items = list(CATALOG)  # ប្រភេទ
        elif step == "sub":
            self.title.setText("Choose Subcategory"); items = list(CATALOG[self.cat])  # Human/Fantasy
        elif step == "group":
            self.title.setText("Choose Human Type"); items = list(GROUPS[self.sub])  # Boy/Girl/Man/Woman/...
        elif step == "type":
            self.title.setText(f"Choose Voice — {self.group}" if self.group else "Choose Voice Type"); items = self._items()  # Boy 1/2/3
        else:
            self._review(); return
        for n in items:
            it = QListWidgetItem(self._label(n) if step == "type" else n)
            it.setData(Qt.UserRole, n)
            self.list.addItem(it)

    def _review(self):  # បង្ហាញ setting + Nickname
        self.title.setText(f"Review: {self.name}"); self.demo_msg.setText("")
        if self.player:
            self.player.stop()
        p = self.selected.get(self.name) or default_preset(self.name)  # preset ដែលបានរក្សា ឬ preset ដើម
        self.base.setCurrentText(p["base"]); self.pitch.setValue(p["pitch"])  # បំពេញទម្រង់
        self.speed.setValue(p["speed"]); self.formant.setValue(p["formant"]); self.fx.setText(p["fx"])
        self.nick.setText(p.get("nick") or self.nicks.get(self.name, ""))  # Nickname ពីមុន
        self.add.setText("UPDATE" if self.name in self.selected else "ADD TO SELECTED VOICE TYPES")  # កែ ឬបន្ថែម
        self.nick.setFocus()

    def _cur_preset(self):  # តម្លៃបច្ចុប្បន្នក្នុងទម្រង់
        return {"base": self.base.currentText(), "pitch": self.pitch.value(), "speed": self.speed.value(),
                "formant": self.formant.value(), "fx": self.fx.text().strip()}

    def _demo(self):  # ចុច DEMO → បង្កើត (ក្នុង thread) រួចលេង
        if self.worker and self.worker.isRunning():
            return
        self.demo_btn.setEnabled(False); self.demo_btn.setText("Generating...")
        self.demo_msg.setText("")
        w = self.worker = Worker(demo_file, self._cur_preset(), self.demo_text.text().strip() or DEMO_TEXT, self.name)
        w.done.connect(self._demo_done); w.failed.connect(self._demo_fail); w.start()

    def _demo_reset(self):
        self.demo_btn.setEnabled(True); self.demo_btn.setText("\u25B6  DEMO - hear this voice")

    def _demo_done(self, path):
        self._demo_reset()
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            if not self.player:
                self.player = QMediaPlayer(self); self.audio_out = QAudioOutput(self)
                self.player.setAudioOutput(self.audio_out)
            self.player.stop(); self.player.setSource(QUrl.fromLocalFile(path)); self.player.play()
            self.demo_msg.setText(f"Playing: {self.name}")
        except Exception:
            open_path(path)  # គ្មាន QtMultimedia → បើកដោយ player ក្នុងប្រព័ន្ធ

    def _demo_fail(self, msg):
        self._demo_reset()
        self.demo_msg.setText(f"Demo failed: {msg}  (edge-tts needs internet and ffmpeg)")

    def _scope(self):  # ឈ្មោះសំឡេងទាំងអស់ក្រោមជំហានបច្ចុប្បន្ន
        if self.step == "type":
            return self._items()  # បញ្ជីបច្ចុប្បន្ន
        subs = CATALOG if self.step == "cat" else {self.cat: CATALOG[self.cat]} if self.step == "sub" else {self.cat: {self.sub: CATALOG[self.cat][self.sub]}}
        return [n for s in subs.values() for items in s.values() for n in items]  # ប្រភេទទាំងអស់ / ក្រុមរងទាំងអស់

    def _demo_all(self):  # ចុច DEMO ALL → លេងគ្រប់សំឡេងក្នុងបញ្ជី តាមលំដាប់
        if self.worker and self.worker.isRunning():
            return
        self.queue = list(enumerate(self._scope()))  # (ជួរ, ឈ្មោះ)
        self.playing_all = True; self._next()  # ចាប់ផ្តើម

    def _next(self):  # បង្កើត + លេងសំឡេងបន្ទាប់
        if not (self.playing_all and self.queue):  # អស់ហើយ
            self.playing_all = False; self.all_btn.setText(self.ALL_TXT); self.all_btn.setEnabled(True); return
        i, n = self.queue.pop(0); self.cur = n  # ឈ្មោះដែលកំពុងលេង
        if self.step == "type":
            self.list.setCurrentRow(i)  # បន្លិចក្នុងបញ្ជី
        self.all_btn.setEnabled(False); self.all_btn.setText(f"Generating {n}...")
        p = self.selected.get(n) or default_preset(n)  # preset (ដែលបានកែ ឬដើម)
        w = self.worker = Worker(demo_file, p, self.demo_text.text().strip() or DEMO_TEXT, n)
        w.done.connect(self._all_play); w.failed.connect(self._all_fail); w.start()

    def _all_play(self, path):  # លេងឯកសារ រួចទៅបន្ទាប់ពេលចប់
        from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
        if not self.player:
            self.player = QMediaPlayer(self); self.audio_out = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_out)
            self.player.mediaStatusChanged.connect(lambda s: self.playing_all and s == QMediaPlayer.EndOfMedia and self._next())  # ចប់ → បន្ទាប់
        self.all_btn.setText(f"Playing: {self.cur}")
        self.player.stop(); self.player.setSource(QUrl.fromLocalFile(path)); self.player.play()

    def _all_fail(self, msg):  # fail → ឈប់ ហើយបង្ហាញសារ
        self.playing_all = False; self.all_btn.setEnabled(True); self.all_btn.setText(self.ALL_TXT)
        self.title.setText(f"Demo failed: {msg}")

    def done(self, r):  # បិទផ្ទាំង → បញ្ឈប់សំឡេង / thread
        if self.player:
            self.player.stop()
        w = self.worker
        if w and w.isRunning():
            w.done.disconnect(); w.failed.disconnect()
            _KEEP.append(w); w.finished.connect(lambda: _KEEP.remove(w) if w in _KEEP else None)
        super().done(r)

    def _go(self, step):  # ទៅជំហានបន្ទាប់ ហើយចងចាំ
        self.hist.append(self.step); self._show(step)

    def _after_sub(self):  # Human → ជ្រើស Group មុន; ផ្សេងទៀត → បញ្ជីត្រង់
        self.group = ""
        self._go("group" if self.sub in GROUPS else "type")

    def _pick(self, it):  # ចុចលើជម្រើស
        n = it.data(Qt.UserRole)
        if self.step == "cat":
            self.cat = n
            if len(CATALOG[n]) == 1:  # Animal ទៅបញ្ជីត្រង់
                self.sub = next(iter(CATALOG[n])); self._after_sub()
            else:
                self._go("sub")
        elif self.step == "sub":
            self.sub = n; self._after_sub()
        elif self.step == "group":
            self.group = n; self._go("type")
        elif self.step == "type":
            self.name = n; self._go("review")

    def _back(self):
        if self.hist:
            self._show(self.hist.pop())  # ត្រឡប់ជំហានមុន

    def _add(self):
        p = self._cur_preset(); p["nick"] = self.nick.text().strip()  # setting + Nickname (ទទេបាន)
        self.result_item = (self.name, p)
        self.accept()
