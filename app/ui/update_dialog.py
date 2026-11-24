"""Update alert: checks GitHub Releases in the background and offers a one-click 'Update now'."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
                               QProgressBar, QMessageBox, QApplication)

from ..shared.app_updater import (check_github, apply_from_url, restart_app, rollback_last, current_version)
from .workers import Worker


class UpdateDialog(QDialog):
    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info
        self.setWindowTitle("MediaStudio — Update available")
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        t = QLabel(f"<b>មានជំនាន់ថ្មី {info['version']}</b> &nbsp;(ឥឡូវនេះ {info['current']})")
        t.setStyleSheet("font-size:15px;")
        v.addWidget(t)
        self.notes = QPlainTextEdit(info.get("notes") or "(no release notes)")
        self.notes.setReadOnly(True)
        self.notes.setFixedHeight(130)
        v.addWidget(self.notes)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.hide()
        v.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        r = QHBoxLayout()
        r.addStretch()
        self.later = QPushButton("Later")
        self.later.clicked.connect(self.reject)
        self.go = QPushButton("Update now")
        self.go.setDefault(True)
        self.go.clicked.connect(self.start)
        r.addWidget(self.later)
        r.addWidget(self.go)
        v.addLayout(r)

    def start(self):
        self.go.setEnabled(False)
        self.later.setEnabled(False)
        self.bar.show()
        self.status.setText("កំពុងទាញយក ...")
        self._w = Worker(apply_from_url, self.info["url"], sha256_url=self.info.get("sha256_url", ""))
        self._w.progress.connect(lambda p: self.bar.setValue(int(p)))
        self._w.done.connect(self._ok)
        self._w.failed.connect(self._fail)
        self._w.start()

    def _ok(self, msg):
        self.bar.setValue(100)
        self.status.setText("Update រួចរាល់។ កំពុងបើកកម្មវិធីឡើងវិញ ...")
        QTimer.singleShot(900, self._restart)

    def _restart(self):
        restart_app()
        QApplication.quit()

    def _fail(self, err):
        self.bar.hide()
        self.status.setText("Update មិនបានសម្រេច៖\n" + err)
        self.later.setEnabled(True)
        self.go.setEnabled(True)
        self.go.setText("Try again")


class UpdateController:
    """Silent check ~4s after start; manual check from the header button."""

    def __init__(self, window):
        self.win = window
        self._busy = False
        QTimer.singleShot(4000, lambda: self.check(silent=True))

    def check(self, silent=False):
        if self._busy:
            return
        self._busy = True
        self._w = Worker(check_github)
        self._w.done.connect(lambda info: self._checked(info, silent))
        self._w.failed.connect(lambda e: self._failed(e, silent))
        self._w.start()

    def _checked(self, info, silent):
        self._busy = False
        if info.get("available"):
            UpdateDialog(info, self.win).exec()
        elif not silent:
            QMessageBox.information(self.win, "Update", f"អ្នកកំពុងប្រើជំនាន់ចុងក្រោយ ({current_version()}) ហើយ។")

    def _failed(self, err, silent):
        self._busy = False
        if not silent:
            QMessageBox.warning(self.win, "Update", err)
