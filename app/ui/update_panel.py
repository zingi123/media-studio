from PySide6.QtWidgets import (QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QPlainTextEdit, QFileDialog, QMessageBox, QApplication)

from ..shared.app_updater import (apply_update, apply_from_url, rollback_last, restart_app, is_frozen)
from .workers import Worker


class UpdatePanel(QGroupBox):
    """Apply new code without rebuilding: choose the update ZIP (or paste a link) and click."""

    def __init__(self, parent=None):
        super().__init__("App Update (apply the code you developed)", parent)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Choose the update ZIP file (it contains the 'app' folder), then click Update. "
                           "A backup is made automatically so you can Undo."))
        r = QHBoxLayout()
        self.zip_btn = QPushButton("Update from ZIP file...")
        self.zip_btn.clicked.connect(self.pick_zip)
        r.addWidget(self.zip_btn)
        self.undo_btn = QPushButton("Undo last update")
        self.undo_btn.clicked.connect(lambda: self._run(rollback_last))
        r.addWidget(self.undo_btn)
        r.addStretch()
        v.addLayout(r)
        r = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setPlaceholderText("or paste a direct link to the update .zip (https://...)")
        r.addWidget(self.link, 1)
        self.link_btn = QPushButton("Update from link")
        self.link_btn.clicked.connect(self.from_link)
        r.addWidget(self.link_btn)
        v.addLayout(r)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFixedHeight(90)
        v.addWidget(self.out)
        if is_frozen():
            for b in (self.zip_btn, self.undo_btn, self.link_btn):
                b.setEnabled(False)
            self.out.setPlainText("This is the packaged .exe, which cannot replace its own code.\n"
                                  "Start the app with run.bat (or the desktop shortcut) to use Update, "
                                  "or rebuild the .exe with make_app.bat.")

    def pick_zip(self):
        p, _ = QFileDialog.getOpenFileName(self, "Choose update ZIP", "", "ZIP files (*.zip)")
        if p:
            self._run(apply_update, p)

    def from_link(self):
        if self.link.text().strip():
            self._run(apply_from_url, self.link.text().strip())

    def _run(self, fn, *args):
        for b in (self.zip_btn, self.undo_btn, self.link_btn):
            b.setEnabled(False)
        self.out.setPlainText("Working ... please wait.")
        self._w = Worker(fn, *args)
        self._w.done.connect(self._ok)
        self._w.failed.connect(self._fail)
        self._w.start()

    def _enable(self):
        for b in (self.zip_btn, self.undo_btn, self.link_btn):
            b.setEnabled(True)

    def _fail(self, msg):
        self.out.setPlainText(str(msg))
        self._enable()

    def _ok(self, msg):
        self.out.setPlainText(str(msg))
        self._enable()
        if QMessageBox.question(self, "Restart", "Restart the app now to use the new code?") == QMessageBox.Yes:
            restart_app()
            QApplication.instance().quit()