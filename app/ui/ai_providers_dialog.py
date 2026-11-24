"""AI Providers: add / edit / test / remove keys, choose models and the provider per task.
Keys go to the OS credential store (shared.secrets) - never to settings.json, never into logs or status text.
A provider is called CONNECTED only after a real test request succeeded (see System Status)."""
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QLineEdit, QComboBox,
                               QPushButton, QFormLayout, QFileDialog, QMessageBox, QScrollArea, QWidget)
from ..ai import providers as P
from ..shared import secrets as SEC


class _Test(QThread):
    done = Signal(object)

    def __init__(self, fn):
        super().__init__(); self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:  # noqa
            self.done.emit(P.TestResult(False, SEC.redact(str(e))))


class AIProvidersDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("AI Providers"); self.resize(660, 720)
        self._threads = []
        outer = QVBoxLayout(self)
        sc = QScrollArea(); sc.setWidgetResizable(True); outer.addWidget(sc, 1)
        host = QWidget(); sc.setWidget(host); v = QVBoxLayout(host)
        v.addWidget(QLabel(f"Keys are stored in: <b>{SEC.backend()}</b>. Every provider is optional - the app works without them.<br>"
                           "A provider shows CONNECTED only in <b>System Status</b> after a real test request succeeds."))
        self._tasks(v)
        self._add("gemini", "Gemini", "gemini_key", "gemini_model", "Gemini API key", v)
        self._add("claude", "Claude", "claude_key", "claude_model", "Anthropic API key", v)
        self._add_hf(v)
        self._add_ollama(v)
        r = QHBoxLayout(); r.addStretch()
        b = QPushButton("Close"); b.clicked.connect(self.accept); r.addWidget(b); outer.addLayout(r)

    # ---------------------------------------------------------------- helpers
    def _box(self, title, v):
        g = QGroupBox(title); f = QFormLayout(g); v.addWidget(g)
        st = QLabel(""); st.setWordWrap(True); st.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return g, f, st

    def _key_edit(self, key):
        e = QLineEdit(); e.setEchoMode(QLineEdit.Password)
        e.setPlaceholderText("a key is saved - type to replace it" if self.s.get(key) else "not set")
        return e

    def _save_key(self, key, e, st):
        t = e.text().strip()
        if t:
            self.s.set(key, t); self.s.save(); e.clear(); e.setPlaceholderText("a key is saved - type to replace it")
            st.setText("Saved (not tested yet).")

    def _remove_key(self, key, e, st):
        if QMessageBox.question(self, "Remove", "Remove the saved credential from this PC?") == QMessageBox.Yes:
            self.s.set(key, ""); self.s.save(); e.clear(); e.setPlaceholderText("not set"); st.setText("Removed.")

    def _run(self, fn, st, after=None):
        st.setText("Testing...")
        t = _Test(fn); self._threads.append(t)
        def done(r):
            st.setText(("✓ " if r.ok else "✗ ") + SEC.redact(r.message))
            if after:
                after(r)
        t.done.connect(done); t.start()

    def _models_combo(self, cur):
        c = QComboBox(); c.setEditable(True); c.setCurrentText(cur or ""); return c

    def _fill(self, combo, models):
        cur = combo.currentText()
        combo.clear(); combo.addItems(models); combo.setCurrentText(cur if cur else (models[0] if models else ""))

    # ---------------------------------------------------------------- provider per task + transport
    def _tasks(self, v):
        g, f, st = self._box("Which provider for which task", v)
        self.tr = QComboBox(); self.sty = QComboBox()
        for lab, val in (("Gemini", "gemini"), ("Claude", "claude"), ("Ollama (local)", "ollama")):
            self.sty.addItem(lab, val)
        for lab, val in (("Gemini", "gemini"), ("Offline NLLB (lower quality)", "nllb"), ("Claude", "claude"), ("Ollama (local)", "ollama")):
            self.tr.addItem(lab, val)
        self.tr.setCurrentIndex(max(0, self.tr.findData(self.s.get("translate_provider"))))
        self.sty.setCurrentIndex(max(0, self.sty.findData(self.s.get("story_provider"))))
        self.tp = QComboBox()
        for lab, val in (("Automatic (official SDK if installed, else built-in HTTPS)", "auto"), ("Official SDK only", "sdk"), ("Built-in HTTPS only", "rest")):
            self.tp.addItem(lab, val)
        self.tp.setCurrentIndex(max(0, self.tp.findData(self.s.get("provider_transport"))))
        f.addRow("Chinese → Khmer translation", self.tr); f.addRow("Story / character summary", self.sty); f.addRow("Gemini/Claude connection", self.tp)
        def save():
            self.s.set("translate_provider", self.tr.currentData()); self.s.set("story_provider", self.sty.currentData())
            self.s.set("provider_transport", self.tp.currentData()); self.s.save(); st.setText("Saved.")
        b = QPushButton("Save choices"); b.clicked.connect(save); f.addRow(b); f.addRow(st)

    # ---------------------------------------------------------------- key based providers
    def _add(self, pid, title, kkey, mkey, label, v):
        g, f, st = self._box(title, v)
        sdk = P.get_provider(pid, self.s).sdk_version()
        f.addRow("Official SDK", QLabel(f"installed ({sdk})" if sdk else "not installed - the built-in HTTPS client is used"))
        e = self._key_edit(kkey); f.addRow(label, e)
        m = self._models_combo(self.s.get(mkey)); f.addRow("Model", m)
        row = QHBoxLayout()
        b = QPushButton("Save"); b.clicked.connect(lambda: (self._save_key(kkey, e, st), self.s.set(mkey, m.currentText().strip()), self.s.save()))
        t = QPushButton("Test connection + load models")
        def test():
            if e.text().strip():
                self._save_key(kkey, e, st)
            self.s.set(mkey, m.currentText().strip())
            self._run(lambda: P.get_provider(pid, self.s).test_connection(), st, lambda r: self._fill(m, r.models) if r.models else None)
        t.clicked.connect(test)
        t2 = QPushButton("Send test request")
        t2.clicked.connect(lambda: self._run(lambda: P.get_provider(pid, self.s).test_request(), st))
        rm = QPushButton("Remove key"); rm.clicked.connect(lambda: self._remove_key(kkey, e, st))
        for w in (b, t, t2, rm):
            row.addWidget(w)
        row.addStretch(); f.addRow(row); f.addRow(st)

    def _add_hf(self, v):
        g, f, st = self._box("Hugging Face (speaker detection / voice models)", v)
        e = self._key_edit("hf_token"); f.addRow("Token (READ)", e)
        mode = QComboBox()
        for lab, val in (("Pyannote community-1 (recommended, needs token)", "pyannote"),
                         ("Energy fallback - less accurate, no overlap detection", "fallback"),
                         ("Off - treat everything as one speaker", "off")):
            mode.addItem(lab, val)
        mode.setCurrentIndex(max(0, mode.findData(self.s.get("diar_mode"))))
        f.addRow("Speaker detection", mode)
        model = QLineEdit(str(self.s.get("diar_model") or "")); f.addRow("Pyannote model", model)
        row = QHBoxLayout()
        b = QPushButton("Save"); b.clicked.connect(lambda: (self._save_key("hf_token", e, st), self.s.set("diar_mode", mode.currentData()),
                                                            self.s.set("diar_model", model.text().strip()), self.s.save()))
        t = QPushButton("Test token + model access")
        def test():
            if e.text().strip():
                self._save_key("hf_token", e, st)
            self.s.set("diar_model", model.text().strip()); self.s.set("diar_mode", mode.currentData()); self.s.save()
            self._run(lambda: P.get_provider("huggingface", self.s).test_connection(), st)
        t.clicked.connect(test)
        rm = QPushButton("Remove token"); rm.clicked.connect(lambda: self._remove_key("hf_token", e, st))
        for w in (b, t, rm):
            row.addWidget(w)
        row.addStretch(); f.addRow(row); f.addRow(st)

    def _add_ollama(self, v):
        g, f, st = self._box("Ollama (local translation)", v)
        exe = QLineEdit(str(self.s.get("ollama_exe") or "")); exe.setPlaceholderText("auto-detected (PATH / %LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe)")
        f.addRow("ollama.exe path (optional)", exe)
        url = QLineEdit(str(self.s.get("ollama_url") or "")); f.addRow("Server URL", url)
        m = self._models_combo(self.s.get("ollama_model")); f.addRow("Model (downloaded)", m)
        def apply():
            self.s.set("ollama_exe", exe.text().strip()); self.s.set("ollama_url", url.text().strip()); self.s.set("ollama_model", m.currentText().strip())
        row = QHBoxLayout()
        b = QPushButton("Save"); b.clicked.connect(lambda: (apply(), self.s.save()))
        br = QPushButton("Browse..."); br.clicked.connect(lambda: exe.setText(QFileDialog.getOpenFileName(self, "ollama.exe", "", "Executable (*.exe);;All (*.*)")[0] or exe.text()))
        t = QPushButton("Detect Ollama + list models")
        def detect():
            apply()
            def work():
                s_ = P.get_provider("ollama", self.s).status()
                lines = [("exe: " + (f"{s_['exe']} ({s_['exe_source']}, v{s_['exe_version'] or '?'})" if s_["installed"] else "not found")),
                         "server: " + ("running" if s_["running"] else "not running"), "models: " + (", ".join(s_["models"]) or "none downloaded"),
                         "loaded in memory: " + (", ".join(s_["loaded"]) or "none"), s_["message"]]
                return P.TestResult(s_["running"] and bool(s_["models"]), "\n".join(lines), s_["models"])
            self._run(work, st, lambda r: self._fill(m, r.models) if r.models else None)
        t.clicked.connect(detect)
        t2 = QPushButton("Send test request")
        t2.clicked.connect(lambda: (apply(), self._run(lambda: P.get_provider("ollama", self.s).test_request(), st)))
        t3 = QPushButton("Start server")
        def start():
            apply()
            self._run(lambda: (lambda s_: P.TestResult(s_["running"], s_["message"], s_["models"]))(P.get_provider("ollama", self.s).start_server()), st)
        t3.clicked.connect(start)
        for w in (b, br, t, t2, t3):
            row.addWidget(w)
        row.addStretch(); f.addRow(row)
        adv = QLabel("To install a model use a terminal, e.g.  ollama pull qwen2.5:3b  (about 2 GB, needs ~4 GB free RAM). "
                     "MediaStudio never downloads a model by itself; check RAM and disk first.")
        adv.setWordWrap(True); f.addRow(adv); f.addRow(st)
