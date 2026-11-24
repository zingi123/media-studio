"""Launch the MediaStudio desktop application.

Run with: python main.py
"""
from __future__ import annotations
import sys

def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        print("MediaStudio requires PySide6. Install the project's desktop dependencies.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2
    from app.context import Context
    from app.shared.settings import Settings
    from app.shared.db import DB
    from app.downloader.download_manager import DownloadManager
    from app.ui.main_window import MainWindow
    app = QApplication(sys.argv)
    settings = Settings()
    db = DB()
    downloads = DownloadManager(db, settings)
    window = MainWindow(Context(settings=settings, db=db, downloads=downloads))
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
