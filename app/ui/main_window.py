"""MediaStudio — application shell with two independent workspaces: Translator and Khmer Movie Summary.

The Translator workspace is unchanged. Khmer Movie Summary is a separate page; if it cannot be created for any reason the
Translator still opens and that tab shows the reason. Other legacy pages (converter, downloader, dashboard and settings pages)
are intentionally not registered in the active navigation; their source files are retained so shared utilities keep working.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QFrame, QSizePolicy, QStackedWidget,
)
from .translator_page import TranslatorPage
from .theme import apply_theme, T


class MainWindow(QMainWindow):
    def __init__(self, ctx):
        super().__init__()
        apply_theme()
        self.setWindowTitle("MediaStudio — Translator")
        self.resize(1500, 920)
        self.setMinimumSize(1120, 700)

        root = QWidget()
        root.setObjectName("appRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Application header: one focused Translator workspace.
        header = QFrame()
        header.setObjectName("appHeader")
        header.setFixedHeight(int(T["header_h"]))
        h = QHBoxLayout(header)
        h.setContentsMargins(18, 0, 18, 0)
        h.setSpacing(18)

        brand = QLabel("✦  MediaStudio")
        brand.setObjectName("brand")
        h.addWidget(brand)

        divider = QFrame()
        divider.setObjectName("headerDivider")
        divider.setFrameShape(QFrame.VLine)
        h.addWidget(divider)

        self.tab_trans = QPushButton("▣  Translator")
        self.tab_sum = QPushButton("▶  Khmer Movie Summary")
        for i, b in enumerate((self.tab_trans, self.tab_sum)):
            b.setObjectName("navTab")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=i: self.show_workspace(k))
            h.addWidget(b)
        h.addStretch(1)

        self.hint = QLabel("AI VIDEO TRANSLATION  ·  KHMER DUBBING")
        self.hint.setObjectName("headerHint")
        h.addWidget(self.hint)
        self.upd_btn = QPushButton("⟳  Update")
        self.upd_btn.setToolTip("Check for a new version")
        h.addWidget(self.upd_btn)
        outer.addWidget(header)

        # Left workflow rail + the existing functional Translator page.
        body = QWidget()
        body.setObjectName("workspaceBody")
        row = QHBoxLayout(body)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(8)

        self.trans = TranslatorPage(ctx)
        self.trans.side.setObjectName("workflowRail")
        self.trans.side.setFixedWidth(int(T["rail_w"]))
        self.trans.side.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        row.addWidget(self.trans.side)
        row.addWidget(self.trans, 1)

        self.summary = None
        try:
            from .summary_page import SummaryPage
            self.summary = SummaryPage(ctx)
            second = self.summary
        except Exception as e:  # noqa - the Translator must never be blocked by the new module
            import traceback
            second = QLabel("Khmer Movie Summary could not be created:\n\n" + traceback.format_exc()[-1200:])
            second.setWordWrap(True)
            second.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        summary_body = QWidget()
        summary_body.setObjectName("workspaceBody")
        sl = QVBoxLayout(summary_body)
        sl.setContentsMargins(8, 8, 8, 8)
        sl.addWidget(second)

        self.stack = QStackedWidget()
        self.stack.addWidget(body)
        self.stack.addWidget(summary_body)
        outer.addWidget(self.stack, 1)
        self.show_workspace(0)

        self.setCentralWidget(root)

        try:                                    # update alert must never block the app
            from .update_dialog import UpdateController
            self._upd = UpdateController(self)
            self.upd_btn.clicked.connect(lambda: self._upd.check(silent=False))
        except Exception:
            self.upd_btn.hide()

    def show_workspace(self, index):
        self.stack.setCurrentIndex(index)
        self.tab_trans.setChecked(index == 0)
        self.tab_sum.setChecked(index == 1)
        self.setWindowTitle("MediaStudio — " + ("Translator" if index == 0 else "Khmer Movie Summary"))
        self.hint.setText("AI VIDEO TRANSLATION  ·  KHMER DUBBING" if index == 0 else "MOVIE RECAP  ·  KHMER NARRATION")

    def closeEvent(self, event):
        # Stop active background work before the window closes.
        try:
            self.trans.stop_all()
            self.trans._pb_shutdown()
        except Exception:
            pass
        try:
            self.trans.studio_save()        # scene / mixer / SFX clips / dialogue of the open video
        except Exception:
            pass
        try:
            if self.summary is not None:
                self.summary.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
