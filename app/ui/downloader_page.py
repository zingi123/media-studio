from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QSize, QUrl
from PySide6.QtGui import (
    QPixmap,
    QIcon,
    QGuiApplication,
    QDesktopServices,
)

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QScrollArea,
    QToolButton,
    QPlainTextEdit,
    QPushButton,
    QLabel,
    QSpinBox,
    QLineEdit,
    QFileDialog,
    QComboBox,
    QCheckBox,
    QTableWidget,
    QTableWidgetItem,
    QProgressBar,
    QHeaderView,
    QAbstractItemView,
    QMessageBox,
    QInputDialog,
)

from ..downloader.models import Job
from ..downloader.scanner import scan_url
from ..downloader.url_detector import detect, SOCIAL, DRAMA, ALL_SITES
from ..shared.utils import (
    fmt_duration,
    fmt_size,
    open_path,
    safe_name,
)
from .workers import Worker, ThumbTask, _ThumbSig


COLS = [
    "",
    "Preview",
    "Title",
    "Site",
    "Type",
    "Duration",
    "Status",
    "Progress",
]

QUALITIES = [
    "Best",
    "1080p",
    "720p",
    "480p",
    "Audio only (MP3)",
]


class DownloaderPage(QWidget):

    def __init__(self, ctx):
        super().__init__()

        self.ctx = ctx
        self.jobs = {}
        self.workers = []

        self.pool = QThreadPool.globalInstance()

        self.sig = _ThumbSig()
        self.sig.loaded.connect(self._thumb_loaded)

        ctx.downloads.job_updated.connect(self._job_updated)

        self._build()

    # ==================================================
    # UI BUILD
    # ==================================================

    def _build(self):

        v = QVBoxLayout(self)

        v.addWidget(
            QLabel(
                "<h2>Downloader - download a whole series from one link</h2>"
                "<b>1.</b> Choose or create a folder &nbsp; "
                "<b>2.</b> Open a website below, log in if needed, and copy the series link &nbsp; "
                "<b>3.</b> Paste it below &nbsp; "
                "<b>4.</b> Click <b>Scrap</b> to list all episodes &nbsp; "
                "<b>5.</b> Click <b>Download All</b>."
            )
        )

        # WEBSITE SHORTCUTS
        self._add_site_shortcuts(v)

        # URL INPUT
        self.urls = QPlainTextEdit()

        self.urls.setPlaceholderText(
            "Paste the series link from "
            + ", ".join(s.name for s in ALL_SITES)
            + " here"
        )

        self.urls.setFixedHeight(60)

        v.addWidget(self.urls)

        # URL BUTTONS
        r = QHBoxLayout()

        for text, fn in (
            ("Paste URL", self.paste),
            ("Clear", self.urls.clear),
        ):

            b = QPushButton(text)
            b.clicked.connect(fn)
            r.addWidget(b)

        r.addWidget(QLabel("Max items per link (0 = all):"))

        self.limit = QSpinBox()
        self.limit.setRange(0, 5000)
        self.limit.setValue(0)

        r.addWidget(self.limit)

        self.scan_btn = QPushButton("Scrap")
        self.scan_btn.setDefault(True)
        self.scan_btn.clicked.connect(self.scan)

        r.addWidget(self.scan_btn)
        r.addStretch()

        v.addLayout(r)

        # DOWNLOAD SETTINGS
        r = QHBoxLayout()

        r.addWidget(QLabel("Save to:"))

        self.folder = QLineEdit(
            self.ctx.settings.get("download_root")
        )

        r.addWidget(self.folder, 1)

        b = QPushButton("Browse")
        b.clicked.connect(self.browse)
        r.addWidget(b)

        b = QPushButton("New Folder...")
        b.clicked.connect(self.new_folder)
        r.addWidget(b)

        r.addWidget(QLabel("Quality:"))

        self.quality = QComboBox()
        self.quality.addItems(QUALITIES)

        r.addWidget(self.quality)

        self.captions = QCheckBox(
            "Save captions (.description)"
        )
        self.captions.setChecked(True)

        r.addWidget(self.captions)

        self.dups = QCheckBox("Allow duplicates")
        r.addWidget(self.dups)

        v.addLayout(r)

        self.flat = QCheckBox(
            "Save all files directly in this folder (no sub-folders)"
        )

        self.flat.setChecked(True)

        v.addWidget(self.flat)

        # DOWNLOAD TABLE
        self.table = QTableWidget(0, len(COLS))

        self.table.setHorizontalHeaderLabels(COLS)

        self.table.setSelectionBehavior(
            QAbstractItemView.SelectRows
        )

        self.table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )

        self.table.verticalHeader().setDefaultSectionSize(54)
        self.table.setIconSize(QSize(80, 48))

        h = self.table.horizontalHeader()

        h.setSectionResizeMode(2, QHeaderView.Stretch)

        self.table.setColumnWidth(0, 30)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(7, 140)

        v.addWidget(self.table, 1)

        # DOWNLOAD CONTROLS
        r = QHBoxLayout()

        buttons = [
            ("Download All", self.download_all),
            ("Select All", lambda: self._check_all(True)),
            ("Deselect All", lambda: self._check_all(False)),
            ("Remove Selected", self.remove_selected),
            ("Download Selected", self.download_selected),
            ("Pause", self.pause),
            ("Resume", self.resume),
            ("Cancel", self.cancel),
            ("Retry Failed", self.retry_failed),
            ("Clear Completed", self.clear_completed),
            ("Open Folder", self.open_folder),
        ]

        for text, fn in buttons:

            b = QPushButton(text)
            b.clicked.connect(fn)

            r.addWidget(b)

        v.addLayout(r)

        # OVERALL PROGRESS
        self.overall = QProgressBar()
        v.addWidget(self.overall)

        self.summary = QLabel("")
        v.addWidget(self.summary)

        # LOG
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(70)

        v.addWidget(self.log)

    # ==================================================
    # WEBSITE SHORTCUTS
    # ==================================================

    def _add_site_shortcuts(self, parent_layout):

        sections = [
            ("Social Networks", SOCIAL),
            ("Movie & Drama Platforms", DRAMA),
        ]

        # Empty sections (for example Social Networks) are not shown.
        sections = [(title, sites) for title, sites in sections if sites]

        if not sections:
            return

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFixedHeight(min(240, 40 + 34 * sum((len(s) + 2) // 3 for _, s in sections)))

        content = QWidget()

        grid = QGridLayout(content)

        grid.setContentsMargins(4, 4, 4, 4)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        row = 0

        for title, sites in sections:

            grid.addWidget(QLabel(f"<b>{title}</b>"), row, 0, 1, 3)

            row += 1

            for index, site in enumerate(sites):

                button = QToolButton()

                button.setText(site.name)

                button.setToolTip(
                    f"{site.features}\n{site.homepage}".strip()
                )

                button.setToolButtonStyle(
                    Qt.ToolButtonTextOnly
                )

                button.setMinimumHeight(28)
                button.setCursor(Qt.PointingHandCursor)

                button.clicked.connect(
                    lambda checked=False, url=site.homepage:
                    QDesktopServices.openUrl(QUrl(url))
                )

                grid.addWidget(
                    button,
                    row + index // 3,
                    index % 3
                )

            row += (len(sites) + 2) // 3

        area.setWidget(content)

        parent_layout.addWidget(
            QLabel("<b>Website Shortcuts (click to open in your browser)</b>")
        )

        parent_layout.addWidget(area)

    # ==================================================
    # HELPERS
    # ==================================================

    def paste(self):

        t = QGuiApplication.clipboard().text().strip()

        if t:

            cur = self.urls.toPlainText().rstrip()

            self.urls.setPlainText(
                (cur + "\n" if cur else "") + t
            )

    def browse(self):

        d = QFileDialog.getExistingDirectory(
            self,
            "Choose download folder",
            self.folder.text()
        )

        if d:
            self.folder.setText(d)

    def new_folder(self):

        name, ok = QInputDialog.getText(
            self,
            "New folder",
            "Folder name (e.g. the series name):"
        )

        if ok and name.strip():

            p = (
                Path(self.folder.text().strip() or ".")
                / safe_name(name.strip(), 80)
            )

            try:
                p.mkdir(parents=True, exist_ok=True)

            except OSError as e:

                QMessageBox.warning(
                    self,
                    "Folder",
                    f"Could not create the folder:\n{e}"
                )

                return

            self.folder.setText(str(p))

            self.log.appendPlainText(
                f"Folder ready: {p}"
            )

    def _row_of(self, uid):

        for r in range(self.table.rowCount()):

            if (
                self.table.item(r, 0)
                and self.table.item(r, 0).data(Qt.UserRole) == uid
            ):
                return r

        return -1

    def _uid(self, r):

        return self.table.item(r, 0).data(Qt.UserRole)

    def _check_all(self, on):

        for r in range(self.table.rowCount()):

            self.table.item(r, 0).setCheckState(
                Qt.Checked if on else Qt.Unchecked
            )

    def _selected_jobs(self, fallback_all=True):

        rows = sorted(
            {i.row() for i in self.table.selectedIndexes()}
        )

        uids = (
            [self._uid(r) for r in rows]
            or (
                [self._uid(r) for r in range(self.table.rowCount())]
                if fallback_all else []
            )
        )

        return [
            self.jobs[u]
            for u in uids
            if u in self.jobs
        ]

    # ==================================================
    # SCANNING
    # ==================================================

    def scan(self):

        lines = [
            l.strip()
            for l in self.urls.toPlainText().splitlines()
            if l.strip()
        ]

        if not lines:
            return

        cookies = (
            self.ctx.settings.get("cookies_file") or None
        )

        for url in lines:

            site = detect(url)

            if not site:

                self.log.appendPlainText(
                    "✗ Unsupported link. Only these sites work: "
                    + ", ".join(s.name for s in ALL_SITES)
                    + f"  ({url})"
                )

                continue

            self.log.appendPlainText(
                f"Scanning {site.name}: {url}"
            )

            w = Worker(
                scan_url,
                url,
                limit=self.limit.value(),
                cookies=cookies
            )

            w.done.connect(self._scan_done)

            w.failed.connect(
                lambda m, u=url:
                self.log.appendPlainText(f"✗ {m}")
            )

            w.finished.connect(
                lambda w=w:
                self.workers.remove(w)
                if w in self.workers else None
            )

            self.workers.append(w)
            w.start()

    def _scan_done(self, items):

        self.table.setUpdatesEnabled(False)

        for it in items:

            r = self.table.rowCount()

            self.table.insertRow(r)

            chk = QTableWidgetItem()

            chk.setFlags(
                Qt.ItemIsUserCheckable
                | Qt.ItemIsEnabled
                | Qt.ItemIsSelectable
            )

            chk.setCheckState(Qt.Checked)
            chk.setData(Qt.UserRole, it.uid)

            self.table.setItem(r, 0, chk)

            self.table.setItem(
                r, 1, QTableWidgetItem("")
            )

            title = (
                f"{it.episode:02d}. "
                if it.category == "drama" and it.episode
                else ""
            ) + it.title

            self.table.setItem(
                r, 2, QTableWidgetItem(title)
            )

            self.table.setItem(
                r, 3, QTableWidgetItem(it.site)
            )

            self.table.setItem(
                r, 4, QTableWidgetItem(it.kind)
            )

            self.table.setItem(
                r, 5, QTableWidgetItem(
                    fmt_duration(it.duration)
                )
            )

            self.table.setItem(
                r, 6, QTableWidgetItem("Ready")
            )

            pb = QProgressBar()

            self.table.setCellWidget(r, 7, pb)

            job = Job(it, "", "")
            job.status = "Ready"

            self.jobs[it.uid] = job

            if it.thumbnail:

                self.pool.start(
                    ThumbTask(
                        it.uid,
                        it.thumbnail,
                        self.sig
                    )
                )

        self.table.setUpdatesEnabled(True)

        self.log.appendPlainText(
            f"✓ Found {len(items)} item(s). "
            "Click Download All (or untick episodes you do not want "
            "and click Download Selected)."
        )

    def _thumb_loaded(self, uid, data):

        r = self._row_of(uid)

        pm = QPixmap()

        if r >= 0 and pm.loadFromData(data):

            self.table.item(r, 1).setIcon(
                QIcon(
                    pm.scaled(
                        80,
                        48,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation
                    )
                )
            )

    # ==================================================
    # DOWNLOAD QUEUE
    # ==================================================

    def download_all(self):

        self._check_all(True)
        self.download_selected()

    def download_selected(self):

        root = self.folder.text().strip()

        if not root:

            QMessageBox.warning(
                self,
                "Folder",
                "Choose a download folder first."
            )

            return

        Path(root).mkdir(parents=True, exist_ok=True)

        n = 0

        for r in range(self.table.rowCount()):

            if (
                self.table.item(r, 0).checkState()
                != Qt.Checked
            ):
                continue

            job = self.jobs[self._uid(r)]

            if job.status not in (
                "Ready",
                "Failed",
                "Cancelled",
                "Duplicate"
            ):
                continue

            job.root = root
            job.quality = self.quality.currentText()

            job.save_captions = self.captions.isChecked()
            job.allow_duplicate = self.dups.isChecked()

            job.flat = self.flat.isChecked()

            self.ctx.downloads.submit(job)

            n += 1

        if not n:

            self.log.appendPlainText(
                "Nothing to download (select items that are Ready)."
            )

    def _job_updated(self, job):

        r = self._row_of(job.item.uid)

        if r < 0:
            return

        txt = job.status

        if job.status == "Downloading" and job.speed:

            txt += f" ({fmt_size(job.speed)}/s)"

        if job.status == "Failed":

            txt = "Failed"

            self.table.item(r, 6).setToolTip(
                job.error
            )

            self.log.appendPlainText(
                f"✗ {job.item.title[:50]}: {job.error}"
            )

        self.table.item(r, 6).setText(txt)

        self.table.cellWidget(r, 7).setValue(
            job.progress
        )

        self._update_overall()

    def _update_overall(self):

        started = [
            j for j in self.jobs.values()
            if j.status != "Ready"
        ]

        if not started:
            return

        self.overall.setValue(
            int(
                sum(
                    j.progress if j.status != "Failed" else 100
                    for j in started
                ) / len(started)
            )
        )

        c = lambda s: sum(
            1 for j in started if j.status == s
        )

        size = sum(
            j.size for j in started
            if j.status == "Done"
        )

        self.summary.setText(
            f"Done: {c('Done')}  |  "
            f"Duplicate: {c('Duplicate')}  |  "
            f"Failed: {c('Failed')}  |  "
            f"Active: {c('Downloading') + c('Queued') + c('Paused')}  |  "
            f"Total size: {fmt_size(size)}"
        )

    # ==================================================
    # DOWNLOAD CONTROLS
    # ==================================================

    def pause(self):

        [
            self.ctx.downloads.pause(j)
            for j in self._selected_jobs()
        ]

    def resume(self):

        [
            self.ctx.downloads.resume(j)
            for j in self._selected_jobs()
        ]

    def cancel(self):

        [
            self.ctx.downloads.cancel(j)
            for j in self._selected_jobs()
        ]

    def retry_failed(self):

        for j in self.jobs.values():

            if j.status in ("Failed", "Cancelled"):

                self.ctx.downloads.submit(j)

    def remove_selected(self):

        rows = sorted(
            {i.row() for i in self.table.selectedIndexes()},
            reverse=True
        )

        for r in rows:

            uid = self._uid(r)

            j = self.jobs.get(uid)

            if j and j.status in (
                "Downloading",
                "Queued",
                "Paused"
            ):

                self.ctx.downloads.cancel(j)

            self.jobs.pop(uid, None)

            self.table.removeRow(r)

    def clear_completed(self):

        for r in range(
            self.table.rowCount() - 1,
            -1,
            -1
        ):

            uid = self._uid(r)

            if self.jobs[uid].status in (
                "Done",
                "Duplicate"
            ):

                self.jobs.pop(uid)

                self.table.removeRow(r)

    def open_folder(self):

        done = [
            j for j in self.jobs.values()
            if j.path and Path(j.path).exists()
        ]

        target = (
            Path(done[-1].path).parent
            if done
            else Path(self.folder.text())
        )

        target.mkdir(parents=True, exist_ok=True)

        open_path(target)