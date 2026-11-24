from pathlib import Path
from .update_panel import UpdatePanel
from datetime import datetime
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QPushButton,
                               QAbstractItemView, QHeaderView, QFormLayout, QLineEdit, QSpinBox, QCheckBox, QFileDialog,
                               QPlainTextEdit, QMessageBox, QComboBox, QGroupBox)
from ..shared.hardware import hardware_report, tool_report
from ..shared.utils import fmt_size, kind_of, open_path, VIDEO_EXT, IMAGE_EXT, AUDIO_EXT


def _table(cols):
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _ts(x):
    return datetime.fromtimestamp(x).strftime("%Y-%m-%d %H:%M") if x else ""


class LibraryPage(QWidget):
    send_converter = Signal(str)
    send_translator = Signal(str)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<h2>Media Library</h2>Everything you downloaded appears here. Send a file straight to "
                           "the Converter or the Translator."))
        self.t = _table(["Title", "Type", "Source", "Size", "Added", "Path"])
        self.t.setColumnWidth(0, 300)
        v.addWidget(self.t, 1)
        r = QHBoxLayout()
        for txt, f in (("Refresh", self.refresh), ("Index Download Folder", self.index_folder), ("Open File", self.open_file),
                       ("Open Folder", self.open_folder), ("Send to Converter", lambda: self._send(self.send_converter)),
                       ("Send to Translator", lambda: self._send(self.send_translator)),
                       ("Remove from Library", self.remove)):
            b = QPushButton(txt)
            b.clicked.connect(f)
            r.addWidget(b)
        v.addLayout(r)
        self.t.doubleClicked.connect(self.open_file)
        self.refresh()

    def refresh(self):
        rows = self.ctx.db.library()
        self.t.setRowCount(len(rows))
        for i, d in enumerate(rows):
            for k, val in enumerate([d["title"], d["kind"], d["source"], fmt_size(d["size"]), _ts(d["created"]), d["path"]]):
                self.t.setItem(i, k, QTableWidgetItem(str(val)))

    def _path(self):
        r = self.t.currentRow()
        return self.t.item(r, 5).text() if r >= 0 else None

    def index_folder(self):
        root = Path(self.ctx.settings.get("download_root"))
        n = 0
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in (VIDEO_EXT | IMAGE_EXT | AUDIO_EXT):
                self.ctx.db.add_library(str(p), p.stem, kind_of(p), p.parent.parent.name)
                n += 1
        self.refresh()

    def open_file(self):
        p = self._path()
        if p and Path(p).exists():
            open_path(p)
        elif p:
            QMessageBox.information(self, "Missing", "This file no longer exists.")

    def open_folder(self):
        p = self._path()
        if p:
            open_path(Path(p).parent)

    def _send(self, sig):
        p = self._path()
        if p and Path(p).exists():
            sig.emit(p)

    def remove(self):
        p = self._path()
        if p:
            self.ctx.db.remove_library(p)   # only removes the entry, never the file
            self.refresh()


class HistoryPage(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<h2>Download History</h2>"))
        self.t = _table(["When", "Status", "Site", "Title", "Size", "Path / URL"])
        v.addWidget(self.t, 1)
        r = QHBoxLayout()
        for txt, f in (("Refresh", self.refresh), ("Open Folder", self.open_folder), ("Clear History", self.clear)):
            b = QPushButton(txt)
            b.clicked.connect(f)
            r.addWidget(b)
        v.addLayout(r)
        self.refresh()

    def refresh(self):
        rows = self.ctx.db.history()
        self.t.setRowCount(len(rows))
        for i, d in enumerate(rows):
            for k, val in enumerate([_ts(d["created"]), d["status"], d["platform"], d["title"], fmt_size(d["size"]),
                                     d["path"] or d["url"]]):
                self.t.setItem(i, k, QTableWidgetItem(str(val)))

    def open_folder(self):
        r = self.t.currentRow()
        if r >= 0:
            p = Path(self.t.item(r, 5).text())
            if p.exists():
                open_path(p.parent)

    def clear(self):
        if QMessageBox.question(self, "Clear", "Clear the history list? (Downloaded files are not deleted.)") == QMessageBox.Yes:
            self.ctx.db.clear_history()
            self.refresh()


from ..shared import secrets as _sec


class SettingsPage(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<h2>Settings</h2>"))
        f = QFormLayout()
        s = ctx.settings
        self.f = {}
        self.own = set()   # fields with their own Update / Clear buttons

        def line(key, label, browse=None, secret=False, buttons=False):
            e = QLineEdit(str(s.get(key) or ""))
            if secret:
                e.setEchoMode(QLineEdit.Password)
            row = QHBoxLayout()
            row.addWidget(e)
            if browse:
                b = QPushButton("Browse")
                b.clicked.connect(lambda: self._browse(e, browse))
                row.addWidget(b)
            if buttons:                      # own Update (save) + Clear (delete) buttons; not part of the common Save
                b = QPushButton("Update"); b.clicked.connect(lambda _=False, k=key, t=label: self.update_key(k, t)); row.addWidget(b)
                b = QPushButton("Clear"); b.clicked.connect(lambda _=False, k=key, t=label: self.clear_key(k, t)); row.addWidget(b)
                self.own.add(key)
            f.addRow(label, row)
            self.f[key] = e

        line("download_root", "Download folder", "dir")
        line("ffmpeg_path", "FFmpeg path (blank = auto)", "file")
        line("cookies_file", "Cookies file (cookies.txt, for sites that need login)", "file", buttons=True)
        line("khmer_font_path", "Khmer font file (.ttf) for burn-in subtitles", "file")
        line("gemini_key", "Gemini API key (optional)", secret=True)
        line("gemini_model", "Gemini model")
        line("claude_key", "Claude API key (optional)", secret=True)
        line("claude_model", "Claude model")
        line("ollama_url", "Ollama server URL")
        line("ollama_model", "Ollama model (must be downloaded)")
        line("hf_token", "Hugging Face token (speaker detection + online voice clone)", secret=True, buttons=True)
        self.par = QSpinBox(); self.par.setRange(1, 8); self.par.setValue(int(s.get("parallel_downloads")))
        f.addRow("Parallel downloads (applies after restart)", self.par)
        self.hw = QCheckBox("Prefer GPU video encoding (falls back to CPU)"); self.hw.setChecked(bool(s.get("hw_accel")))
        f.addRow("", self.hw)
        v.addLayout(f)
        r = QHBoxLayout()
        b = QPushButton("Save"); b.clicked.connect(self.save); r.addWidget(b)
        b = QPushButton("Check Tools && Hardware"); b.clicked.connect(self.check); r.addWidget(b)
        r.addStretch()
        v.addLayout(r)
        self.out = QPlainTextEdit(); self.out.setReadOnly(True)
        v.addWidget(self.out, 1)
        v.addWidget(UpdatePanel())
        v.addWidget(QLabel(f"API keys and tokens are stored in: {_sec.backend()} (never in settings.json). Cookies path is saved in settings.json. "
                           "Use Translator > Speakers & AI > AI Providers to test connections."))

    def _browse(self, edit, kind):
        p = QFileDialog.getExistingDirectory(self, "Folder") if kind == "dir" else QFileDialog.getOpenFileName(self, "File")[0]
        if p:
            edit.setText(p)

    def update_key(self, key, title):
        """Update = save THIS field only."""
        v = self.f[key].text().strip()
        if not v:
            self.out.setPlainText(f"{title}: the box is empty. Type a value, or press Clear to remove the saved one.")
            return
        self.ctx.settings.set(key, v); self.ctx.settings.save()
        self.out.setPlainText(f"Updated: {title}.")

    def clear_key(self, key, title):
        """Clear = delete THIS field's saved value (box + settings.json)."""
        self.f[key].clear()
        self.ctx.settings.set(key, ""); self.ctx.settings.save()
        if key == "hf_token":
            import os
            os.environ.pop("HF_TOKEN", None)
        self.out.setPlainText(f"Cleared: {title}.")

    def save(self):
        for k, e in self.f.items():
            if k in self.own:                # Cookies file / Hugging Face token use their own Update / Clear buttons
                continue
            self.ctx.settings.set(k, e.text().strip())
        self.ctx.settings.set("parallel_downloads", self.par.value())
        self.ctx.settings.set("hw_accel", self.hw.isChecked())
        self.ctx.settings.save()
        self.out.setPlainText("Saved.")

    def check(self):
        self.out.setPlainText(hardware_report() + "\n\n" + tool_report() + "\n\nGuide: Whisper 'tiny/base' run on most PCs "
                              "(CPU); 'small' needs ~8 GB RAM; 'medium/large' want 16 GB+ RAM or an NVIDIA GPU. "
                              "Local NLLB translation needs ~4 GB RAM free.")


class MoreSettingsPage(QWidget):
    """More Settings: Hugging Face token + Cookies, each with its own Update and Clear button."""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<h2>More Settings</h2>Type a value and click <b>Update</b> to save it. "
                           "Click <b>Clear</b> to remove the saved value."))
        self.f = {}
        for key, title, hint, secret, browse in (
            ("hf_token", "Hugging Face token", "hf_xxxxxxxxxxxxxxxx  (speaker detection + online voice clone)", True, False),
            ("cookies_file", "Cookies", "Path to cookies.txt (for sites that need login)", False, True),
        ):
            box = QGroupBox(title)
            h = QHBoxLayout(box)
            e = QLineEdit(str(ctx.settings.get(key) or ""))
            e.setPlaceholderText(hint)
            if secret:
                e.setEchoMode(QLineEdit.Password)
            h.addWidget(e, 1)
            if secret:
                show = QPushButton("Show"); show.setCheckable(True)
                show.toggled.connect(lambda on, ed=e, b=show: (ed.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password),
                                                                b.setText("Hide" if on else "Show")))
                h.addWidget(show)
            if browse:
                b = QPushButton("Browse"); b.clicked.connect(lambda _=False, ed=e: self._browse(ed)); h.addWidget(b)
            b = QPushButton("Update"); b.clicked.connect(lambda _=False, k=key, t=title: self.update_key(k, t)); h.addWidget(b)
            b = QPushButton("Clear"); b.clicked.connect(lambda _=False, k=key, t=title: self.clear_key(k, t)); h.addWidget(b)
            v.addWidget(box)
            self.f[key] = e
        self.status = QLabel("")
        v.addWidget(self.status)
        v.addStretch(1)
        v.addWidget(QLabel(f"Tokens are stored in: {_sec.backend()} (never in settings.json)."))

    def _browse(self, edit):
        p = QFileDialog.getOpenFileName(self, "Choose cookies file", "", "Text files (*.txt);;All files (*)")[0]
        if p:
            edit.setText(p)

    def update_key(self, key, title):
        v = self.f[key].text().strip()
        if not v:
            self.status.setText(f"{title}: the box is empty. Type a value, or press Clear to remove the saved one.")
            return
        self.ctx.settings.set(key, v); self.ctx.settings.save()
        if key == "hf_token":
            import os
            os.environ["HF_TOKEN"] = v
        self.status.setText(f"Updated: {title}.")

    def clear_key(self, key, title):
        self.f[key].clear()
        self.ctx.settings.set(key, ""); self.ctx.settings.save()
        if key == "hf_token":
            import os
            os.environ.pop("HF_TOKEN", None)
        self.status.setText(f"Cleared: {title}.")


class DashboardPage(QWidget):
    go = Signal(int)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<h1>All-in-One Media Studio</h1>"))
        r = QHBoxLayout()
        for txt, idx in (("Downloader", 1), ("Converter", 2), ("Translator && Khmer Dubbing", 3), ("More Settings", 4)):
            b = QPushButton(txt)
            b.setMinimumHeight(70)
            b.clicked.connect(lambda _=False, i=idx: self.go.emit(i))
            r.addWidget(b)
        v.addLayout(r)
        self.stats = QLabel()
        v.addWidget(self.stats)
        v.addWidget(QLabel("<b>Recent activity</b>"))
        self.t = _table(["When", "Status", "Site", "Title"])
        v.addWidget(self.t, 1)

    def refresh(self):
        h = self.ctx.db.history(200)
        c = lambda s: sum(1 for x in h if x["status"] == s)  # noqa
        self.stats.setText(f"Downloads - done: {c('Done')}   failed: {c('Failed')}   |   Library items: {len(self.ctx.db.library())}")
        rows = h[:12]
        self.t.setRowCount(len(rows))
        for i, d in enumerate(rows):
            for k, val in enumerate([_ts(d["created"]), d["status"], d["platform"], d["title"]]):
                self.t.setItem(i, k, QTableWidgetItem(str(val)))