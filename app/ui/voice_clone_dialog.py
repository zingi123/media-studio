from pathlib import Path
from PySide6.QtCore import Qt, QUrl, QSize
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QFileDialog,
                               QProgressBar, QFormLayout, QMessageBox, QListWidgetItem, QWidget)
from ..shared.utils import open_path
from ..translator import voice_clone as VC
from ..translator.voice_presets import DEMO_TEXT
from .voice_type_dialog import AddVoiceTypeDialog, _KEEP
from .workers import Worker

RED = "background:#c0392b;color:white;font-weight:bold"        # same red as the existing STOP ALL button
CLONE_KEY = "\u0000cloned-voices"                              # list-item key of the "Cloned Voices" category


def _gpu_job(progress=None, cancel=None):                      # runs off the GUI thread (importing torch is slow)
    return VC.check_gpu()


def _prepare_job(path, progress=None, cancel=None):
    return VC.prepare_reference(path, progress=progress, cancel=cancel)


def _clone_job(wav, text, progress=None, cancel=None):         # the single clone job; releases the global lock itself
    try:
        return VC.clone_demo(wav, text, progress=progress, cancel=cancel)
    finally:
        VC.end_clone_job()


def confirm_delete_clone(parent, name, in_use=None, before=None) -> bool:
    """Ask 'Confirm / Cancel', then permanently delete that ONE cloned voice. True only if it was deleted."""
    busy = in_use or VC.is_in_use
    if busy(name):
        QMessageBox.warning(parent, "Delete Voice", f"'{name}' is being used by Generate Voice right now.\n"
                            "Wait until it finishes, then delete it.")
        return False
    box = QMessageBox(QMessageBox.Warning, "Delete cloned voice",
                      f"Delete '{name}' permanently?\n\nIts reference audio and saved data will be removed. "
                      "Other voices are not affected. This cannot be undone.", QMessageBox.NoButton, parent)
    ok = box.addButton("Confirm", QMessageBox.AcceptRole)
    cancel = box.addButton("Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    if box.clickedButton() is not ok:
        return False
    if before:
        before()
    try:
        VC.delete_voice(name)
    except VC.CloneError as e:
        QMessageBox.warning(parent, "Delete Voice", str(e))
        return False
    return True


class CloneVoiceDialog(QDialog):
    """Import MP3/WAV -> prepare -> preview -> START CLONE (VoxCPM2 zero-shot, GPU) -> hear it -> SAVE with a Nickname."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clone Voice - VoxCPM2 zero-shot"); self.resize(520, 520)
        self.prepared = self.sample = self.result_voice = None
        self.gpu_ok, self.busy, self.worker, self.player = False, False, None, None
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Zero-shot cloning (no training). Import a clean recording of ONE speaker, 5-15 seconds, "
                           "no music or noise. MP3 or WAV."))
        self.gpu = QLabel("Checking GPU..."); self.gpu.setWordWrap(True); v.addWidget(self.gpu)
        r = QHBoxLayout()
        self.imp = QPushButton("Import Audio (MP3 / WAV)"); self.imp.clicked.connect(self._import)
        self.fname = QLabel("(no file)"); self.fname.setWordWrap(True)
        r.addWidget(self.imp); r.addWidget(self.fname, 1); v.addLayout(r)
        self.info = QLabel(""); self.info.setWordWrap(True); v.addWidget(self.info)
        r = QHBoxLayout()
        self.play_ref = QPushButton("\u25B6  Play reference"); self.play_ref.clicked.connect(lambda: self._play(self.prepared and self.prepared["wav"]))
        self.stop_btn = QPushButton("\u25A0  Stop"); self.stop_btn.clicked.connect(self._stop)
        r.addWidget(self.play_ref); r.addWidget(self.stop_btn); r.addStretch(); v.addLayout(r)
        f = QFormLayout()
        self.nick = QLineEdit(); self.nick.setMaxLength(VC.NICK_MAX); self.nick.setPlaceholderText("Nickname e.g. Sophea")
        self.text = QLineEdit(DEMO_TEXT); self.text.setToolTip("Khmer sentence the cloned voice will say as a test")
        f.addRow("Nickname", self.nick); f.addRow("Test text", self.text); v.addLayout(f)
        self.bar = QProgressBar(); self.bar.setFixedHeight(14); self.bar.setTextVisible(False); self.bar.setRange(0, 1); v.addWidget(self.bar)
        self.msg = QLabel(""); self.msg.setWordWrap(True); v.addWidget(self.msg)
        v.addStretch()
        r = QHBoxLayout()
        self.start = QPushButton("START CLONE"); self.start.setStyleSheet("font-weight:bold"); self.start.clicked.connect(self._start)
        self.play_out = QPushButton("\u25B6  Play cloned sample"); self.play_out.clicked.connect(lambda: self._play(self.sample))
        self.save = QPushButton("SAVE CLONED VOICE"); self.save.setStyleSheet("font-weight:bold"); self.save.clicked.connect(self._save)
        self.cancel_job = QPushButton("Cancel job"); self.cancel_job.clicked.connect(self._cancel_job)
        close = QPushButton("Close"); close.clicked.connect(self.reject)
        for b in (self.start, self.play_out, self.save, self.cancel_job, close):
            r.addWidget(b)
        v.addLayout(r)
        self._sync()
        self._run(_gpu_job, done=self._gpu_done, label="Checking GPU...")

    # ---------- state ----------
    def _sync(self):
        b = self.busy
        self.imp.setEnabled(not b)
        self.nick.setEnabled(not b); self.text.setEnabled(not b)
        self.play_ref.setEnabled(bool(self.prepared) and not b)
        self.start.setEnabled(bool(self.prepared) and self.gpu_ok and not b)
        self.play_out.setEnabled(bool(self.sample) and not b)
        self.save.setEnabled(bool(self.sample) and not b)
        self.cancel_job.setEnabled(b)
        self.bar.setRange(0, 0 if b else 1)                     # indeterminate "busy" bar while working

    def _status(self, text, error=False):
        self.msg.setStyleSheet("color:#c0392b" if error else "")
        self.msg.setText(text)

    def _run(self, fn, *a, done=None, label="", failed=None):
        w = self.worker = Worker(fn, *a)
        self.busy = True; self._sync(); self._status(label)
        w.progress.connect(lambda p: self._status(p) if isinstance(p, str) else None)
        w.done.connect(lambda r: self._finished(done, r))
        w.failed.connect(lambda m: self._finished(failed or self._failed, m, ok=False))
        w.start()

    def _finished(self, cb, arg, ok=True):
        self.busy = False; self._sync()
        if cb:
            cb(arg)

    def _failed(self, m):
        self._status("Cancelled." if m == "Cancelled" else "\u2717 " + m, error=(m != "Cancelled"))

    # ---------- GPU ----------
    def _gpu_done(self, g):
        self.gpu_ok = g["ok"]
        self.gpu.setText(("\u2713 " if g["ok"] else "\u26A0 ") + g["message"])
        self.gpu.setStyleSheet("" if g["ok"] else "color:#c0392b")
        self._status("" if g["ok"] else "Cloning is unavailable on this computer. You can still import and listen to audio.")
        self._sync()

    # ---------- import / prepare ----------
    def _import(self):
        f, _ = QFileDialog.getOpenFileName(self, "Reference voice", "", "Audio (*.mp3 *.wav)")
        if f:
            self.load_file(f)

    def load_file(self, f):
        self._drop_prepared()
        self.fname.setText(Path(f).name); self.info.setText("")
        self._run(_prepare_job, f, done=self._prepared, label="Checking and converting audio...")

    def _drop_prepared(self):
        self._stop()
        if self.prepared:
            Path(self.prepared["wav"]).unlink(missing_ok=True)
        self.prepared = self.sample = None

    def _prepared(self, p):
        dup = VC.find_duplicate(None, p["sha256"])
        if dup:
            Path(p["wav"]).unlink(missing_ok=True)
            self.info.setText(""); self._status(f"\u2717 This recording is already cloned as '{dup['name']}'. "
                                                "Pick it from Cloned Voices instead.", error=True)
            self._sync(); return
        self.prepared = p
        lines = [f"Ready: {p['duration']:.1f} s, mono, {VC.REF_SR // 1000} kHz WAV (the original file is untouched)."] + [
            "\u26A0 " + w for w in p["warnings"]]
        self.info.setText("\n".join(lines))
        self._status("Press \u25B6 Play reference to check it, then START CLONE.")
        self._sync()

    # ---------- clone ----------
    def _start(self):
        if not (self.prepared and self.gpu_ok) or self.busy:
            return
        nick = VC.clean_nick(self.nick.text())
        if nick and VC.find_duplicate(nick):
            self._status(f"\u2717 A cloned voice named '{VC.name_for(nick)}' already exists. Choose another Nickname.", error=True)
            return
        if not VC.try_begin_clone_job():
            self._status("\u2717 Another clone is already running. Wait until it finishes.", error=True); return
        self.sample = None
        self._run(_clone_job, self.prepared["wav"], self.text.text().strip() or DEMO_TEXT, done=self._cloned,
                  label="Cloning... (first time the model loads - this can take a while)")

    def _cloned(self, path):
        self.sample = path
        self._status("\u2713 Clone finished. Play the sample; if you like it, set a Nickname and press SAVE CLONED VOICE.")
        self._sync()
        self._play(path)

    def _cancel_job(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel(); self._status("Cancelling... it stops at the next safe point.")

    # ---------- save ----------
    def _save(self):
        if not (self.prepared and self.sample):
            return
        nick = VC.clean_nick(self.nick.text())
        if not nick:
            self._status("\u2717 Enter a Nickname first.", error=True); self.nick.setFocus(); return
        try:
            self.result_voice = VC.save_voice(nick, self.prepared)
        except VC.CloneError as e:
            self._status("\u2717 " + str(e), error=True); return
        except Exception as e:  # noqa
            self._status(f"\u2717 Could not save the voice: {e}", error=True); return
        QMessageBox.information(self, "Clone Voice", f"Saved '{self.result_voice['name']}'.\n"
                                "It stays until you delete it yourself.")
        self.accept()

    # ---------- playback ----------
    def _play(self, path):
        if not path:
            return
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            if not self.player:
                self.player = QMediaPlayer(self); self.audio_out = QAudioOutput(self)
                self.player.setAudioOutput(self.audio_out)
            self.player.stop(); self.player.setSource(QUrl.fromLocalFile(str(path))); self.player.play()
        except Exception:
            open_path(path)

    def _stop(self):
        if self.player:
            self.player.stop()

    def done(self, r):                                          # closing: stop sound, cancel + park a running thread
        self._stop()
        w = self.worker
        if w and w.isRunning():
            w.cancel()
            try:
                w.done.disconnect(); w.failed.disconnect(); w.progress.disconnect()
            except (RuntimeError, TypeError):
                pass
            _KEEP.append(w); w.finished.connect(lambda: _KEEP.remove(w) if w in _KEEP else None)
        if self.prepared:                                        # temp reference is only needed until it is saved (a copy is stored)
            Path(self.prepared["wav"]).unlink(missing_ok=True)
        super().done(r)


class AddVoiceTypeCloneDialog(AddVoiceTypeDialog):
    """The existing Add Voice Type wizard + a CLONE VOICE button + a 'Cloned Voices' list (each with DELETE).
    Everything else is inherited unchanged from AddVoiceTypeDialog."""
    CLONE_LABEL = "\u2605  Cloned Voices"

    def __init__(self, selected, parent=None, nicks=None, in_use=None):
        self.deleted, self.new_clone, self._in_use = [], None, in_use
        super().__init__(selected, parent, nicks)
        row = self.layout().itemAt(self.layout().count() - 1).layout()     # the bottom button row of the base dialog
        self.clone_btn = QPushButton("CLONE VOICE"); self.clone_btn.setMinimumHeight(30)
        self.clone_btn.setStyleSheet("font-weight:bold"); self.clone_btn.clicked.connect(self._clone)
        row.addWidget(self.clone_btn)                                      # after the existing buttons

    # ---- list: add the "Cloned Voices" category next to the original categories ----
    def _show(self, step):
        if step == "clones":
            self._show_clones(); return
        for w in (*self.setting_widgets, self.nick):                        # may have been disabled by a cloned voice
            w.setEnabled(True)
        super()._show(step)
        if step == "cat":
            it = QListWidgetItem(f"{self.CLONE_LABEL}  ({len(VC.list_voices())})")
            it.setData(Qt.UserRole, CLONE_KEY)
            self.list.insertItem(0, it)

    def _pick(self, it):
        n = it.data(Qt.UserRole)
        if self.step == "cat" and n == CLONE_KEY:
            self._go("clones")
        elif self.step == "clones":
            if n:
                self.name = n; self._go("review")
        else:
            super()._pick(it)

    def _show_clones(self):
        self.step = "clones"
        self.queue, self.playing_all = [], False
        self.all_btn.setText(self.ALL_TXT); self.all_btn.setVisible(False)
        self.list.clear(); self.list.setVisible(True); self.form.setVisible(False)
        self.add.setVisible(False); self.back.setEnabled(bool(self.hist))
        self.title.setText("Choose Cloned Voice  (DELETE removes a cloned voice permanently)")
        vs = VC.list_voices()
        if not vs:
            it = QListWidgetItem("(no cloned voices yet - press CLONE VOICE)"); it.setFlags(Qt.NoItemFlags)
            self.list.addItem(it); return
        for v in vs:
            it = QListWidgetItem(); it.setData(Qt.UserRole, v["name"]); it.setSizeHint(QSize(0, 36))
            self.list.addItem(it)
            self.list.setItemWidget(it, self._row(v["name"]))

    def _row(self, name):
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(6, 2, 6, 2)
        lb = QLabel(self._label(name)); lb.setAttribute(Qt.WA_TransparentForMouseEvents)   # clicks fall through to the list
        d = QPushButton("DELETE"); d.setStyleSheet(RED); d.setToolTip("Delete this cloned voice permanently")
        d.clicked.connect(lambda _=False, n=name: self._delete(n))
        h.addWidget(lb, 1); h.addWidget(d)
        return w

    def _label(self, n):
        return n + ("  (added)" if n in self.selected else "") if VC.is_clone_name(n) else super()._label(n)

    def _delete(self, name):
        if confirm_delete_clone(self, name, self._in_use, before=lambda: self.player and self.player.stop()):
            self.selected.pop(name, None); self.deleted.append(name)
            self._show_clones()

    # ---- review / demo / add for a cloned voice ----
    def _review(self):
        if not VC.is_clone_name(self.name):
            super()._review(); return
        v = VC.get_voice(self.name) or {}
        self.title.setText(f"Review: {self.name}"); self.demo_msg.setText(
            "Cloned voice: the sound comes from its saved reference recording (the GPU is used for the demo).")
        if self.player:
            self.player.stop()
        for w in (*self.setting_widgets, self.nick):
            w.setEnabled(False)
        self.nick.setText(v.get("nick", ""))
        self.add.setText("UPDATE" if self.name in self.selected else "ADD TO SELECTED VOICE TYPES")

    def _demo(self):
        if not VC.is_clone_name(self.name):
            super()._demo(); return
        if self.worker and self.worker.isRunning():
            return
        self.demo_btn.setEnabled(False); self.demo_btn.setText("Generating...")
        self.demo_msg.setText("Generating with VoxCPM2 (the first time the model loads - this can take a while)...")
        w = self.worker = Worker(VC.voice_demo_file, self.name, self.demo_text.text().strip() or DEMO_TEXT)
        w.done.connect(self._demo_done); w.failed.connect(self._clone_demo_fail); w.start()

    def _clone_demo_fail(self, msg):
        self._demo_reset(); self.demo_msg.setText(f"Demo failed: {msg}")

    def _add(self):
        if VC.is_clone_name(self.name):
            self.result_item = (self.name, VC.preset_for()); self.accept()
        else:
            super()._add()

    # ---- CLONE VOICE ----
    def _clone(self):
        if self.worker and self.worker.isRunning():
            return
        self._stop_sound()
        d = CloneVoiceDialog(self)
        d.exec()
        if d.result_voice:
            self.new_clone = d.result_voice["name"]
            self.hist = []; self._show("cat"); self._go("clones")           # show the new voice, ready to select

    def _stop_sound(self):
        if self.player:
            self.player.stop()
        self.queue, self.playing_all = [], False
