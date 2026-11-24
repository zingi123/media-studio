"""System Status / AI Environment - one honest panel. Every row says what was actually checked; nothing is green because a
package merely exists. Recheck = local checks (no network). Run connection tests = real requests (your choice, they may use
a few API tokens). Heavy imports run in a separate process (shared/_probe.py)."""
import time
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QLabel,
                               QPlainTextEdit, QHeaderView, QAbstractItemView, QFileDialog, QMessageBox, QSplitter)
from ..shared import diagnostics as DG, env_fix as EF, secrets as SEC
from ..shared.paths import data_dir

COLORS = {"ok": "#1f6f3d", "warn": "#8a6a00", "error": "#8a1f1f", "info": "#3a3f47"}


class _Job(QThread):
    done = Signal(object)

    def __init__(self, fn):
        super().__init__(); self.fn = fn

    def run(self):
        try:
            self.done.emit(("ok", self.fn()))
        except Exception as e:  # noqa
            self.done.emit(("err", SEC.redact(str(e))))


class SystemStatusDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("System Status - AI Environment"); self.resize(1000, 640)
        self.probe, self.comps, self.jobs = None, [], []
        v = QVBoxLayout(self)
        self.info = QLabel("Press Recheck. Local checks only - no network, no API tokens used.")
        self.info.setWordWrap(True); v.addWidget(self.info)
        sp = QSplitter(Qt.Vertical); v.addWidget(sp, 1)
        self.t = QTableWidget(0, 3); self.t.setHorizontalHeaderLabels(["Component", "Status", "What was actually checked"])
        self.t.setEditTriggers(QAbstractItemView.NoEditTriggers); self.t.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch); self.t.setColumnWidth(0, 200); self.t.setColumnWidth(1, 170)
        self.t.itemSelectionChanged.connect(self._detail); sp.addWidget(self.t)
        self.d = QPlainTextEdit(); self.d.setReadOnly(True); sp.addWidget(self.d); sp.setSizes([360, 200])
        r1 = QHBoxLayout(); r2 = QHBoxLayout()
        for row, items in ((r1, (("Recheck", self.recheck), ("Run connection tests (uses API requests)", self.run_tests),
                                 ("Test Whisper + Pyannote with an audio file...", self.audio_test), ("Start Ollama server", self.start_ollama))),
                           (r2, (("Compatibility plan...", self.plan), ("Copy report", self.copy), ("Save report...", self.save), ("Close", self.accept)))):
            for txt, fn in items:
                b = QPushButton(txt); b.clicked.connect(fn); row.addWidget(b)
            row.addStretch()
        v.addLayout(r1); v.addLayout(r2)
        self.recheck()

    # ---------------------------------------------------------------- jobs
    def _job(self, fn, after, msg):
        self.info.setText(msg)
        j = _Job(fn); self.jobs.append(j)
        def done(res):
            kind, val = res
            if kind == "err":
                self.info.setText("Failed: " + val); QMessageBox.warning(self, "System Status", val)
            else:
                after(val)
        j.done.connect(done); j.start()

    def recheck(self):
        def work():
            return DG.collect(self.s)
        def after(res):
            self.comps, self.probe = res
            self._fill()
            self.info.setText("Rechecked " + time.strftime("%H:%M:%S") + ". Rows marked NOT TESTED have not been proven to work yet.")
        self._job(work, after, "Checking the environment (importing PyTorch/Pyannote in a separate process can take a minute)...")

    def _fill(self):
        sel = self.t.currentRow()
        self.t.setRowCount(len(self.comps))
        for r, c in enumerate(self.comps):
            it0, it1, it2 = QTableWidgetItem(c.name), QTableWidgetItem(c.status), QTableWidgetItem(c.summary)
            it1.setBackground(QColor(COLORS.get(c.level, "#3a3f47"))); it1.setForeground(QColor("#ffffff"))
            it2.setToolTip(c.summary)
            for k, it in enumerate((it0, it1, it2)):
                self.t.setItem(r, k, it)
        if self.comps:
            self.t.selectRow(max(0, sel))
        self._detail()

    def _detail(self):
        r = self.t.currentRow()
        if r < 0 or r >= len(self.comps):
            self.d.setPlainText(""); return
        c = self.comps[r]
        self.d.setPlainText(f"{c.name} - {c.status}\n{c.summary}\n\n" + "\n".join(f"• {k.name}: {k.status}\n    {k.detail}" for k in c.checks))

    def run_tests(self):
        if QMessageBox.question(self, "Connection tests", "This sends a tiny real request to every provider that has a key (Gemini, Claude, Hugging Face) "
                                "and a test generation to Ollama if a model is selected. It may use a few API tokens. Continue?") != QMessageBox.Yes:
            return
        def work():
            out = {}
            for pid in ("huggingface", "gemini", "claude", "ollama"):
                try:
                    from ..ai.providers import get_provider
                    p = get_provider(pid, self.s)
                    if pid not in ("ollama",) and not p.configured()[0]:
                        continue
                    DG.run_provider_tests(pid, self.s); out[pid] = True
                except Exception as e:  # noqa
                    out[pid] = SEC.redact(str(e))
            return out
        self._job(work, lambda _: self.recheck(), "Running connection tests...")

    def audio_test(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose a short audio/video file with speech (ideally 2+ speakers, Chinese)", "", "Media (*.wav *.mp3 *.m4a *.mp4 *.mkv *.mov);;All (*.*)")
        if not f:
            return
        def work():
            return DG.run_audio_test(f, self.s.get("hf_token"), self.s.get("diar_model") or None, "cpu", self.s.get("whisper_model") or "base", seconds=60, settings=self.s)
        def after(res):
            lines = [f"Result: {'OK' if res['ok'] else 'FAILED'}"]
            if res.get("whisper"):
                w = res["whisper"]; lines.append(f"Whisper: {w['words']} words, language {w['language']}, {w['seconds']}s for {w['audio_seconds']}s audio.\nSample: {w['sample']}")
            if res.get("diarization"):
                d = res["diarization"]; lines.append(f"Pyannote: {len(d['speakers'])} speaker(s), {d['turns']} turn(s), {d['overlaps']} overlap(s) in {d['seconds']}s.")
            lines += res.get("errors", [])
            QMessageBox.information(self, "Audio test", "\n\n".join(lines))
            self.recheck()
        self._job(work, after, "Running Whisper and Pyannote on the first 60 seconds (CPU: this can take several minutes; the first run downloads models)...")

    def start_ollama(self):
        def work():
            from ..ai.providers import get_provider
            return get_provider("ollama", self.s).start_server()
        self._job(work, lambda st: (QMessageBox.information(self, "Ollama", st["message"]), self.recheck()), "Starting the Ollama server...")

    def plan(self):
        if not self.probe:
            return
        plan = EF.build_plan(self.probe)
        txt = EF.format_plan(plan)
        box = QMessageBox(self); box.setWindowTitle("Compatibility plan"); box.setText(txt[:3500])
        box.setInformativeText("Nothing is changed by this window. To apply an option: run  python verify_env.py --fix  in the terminal "
                               "(it saves a backup, validates with a dry run, and asks you to confirm).")
        box.exec()

    def _report(self):
        return DG.format_report(self.comps, self.probe)

    def copy(self):
        QGuiApplication.clipboard().setText(self._report()); self.info.setText("Report copied (it contains no keys or tokens).")

    def save(self):
        f, _ = QFileDialog.getSaveFileName(self, "Save report", str(data_dir() / "environment_report.txt"), "Text (*.txt)")
        if f:
            open(f, "w", encoding="utf-8").write(self._report()); self.info.setText(f"Saved {f}")
