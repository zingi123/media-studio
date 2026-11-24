"""GUI smoke test for STEP 1 (needs PySide6). NOT RUN by the author - PySide6 was unavailable where this was written.
Run on your machine from the project root, with the same Python you run MediaStudio with:

    QT_QPA_PLATFORM=offscreen python tests/gui_smoke_step1.py        (Windows PowerShell: $env:QT_QPA_PLATFORM="offscreen")

It builds the real TranslatorPage, then checks the accordion and the preview fitting at several window sizes / aspect ratios.
Create the Context the same way your launcher does; if your launcher builds it differently, edit make_ctx().
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication

FAILS = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def make_ctx():
    from types import SimpleNamespace
    from app.shared.settings import Settings
    try:
        s = Settings()
    except TypeError:
        s = Settings(os.path.join(os.getcwd(), "_smoke_settings.json"))
    return SimpleNamespace(settings=s, db=None, downloads=None)


def pump(n=5):
    for _ in range(n):
        QCoreApplication.processEvents()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from app.ui.main_window import MainWindow
    w = MainWindow(make_ctx()); w.show(); pump()
    t = w.trans

    # ---- accordion ----
    def open_keys(): return [k for k, b in t.boxes.items() if b.btn.isChecked()]
    check(open_keys() == ["media"], "start: only Media is open")
    for key in ("script", "translate", "voice", "style", "tools", "export", "media"):
        t.rail_btns[key].click(); pump()
        check(open_keys() == [key], f"rail click '{key}' -> only '{key}' open (got {open_keys()})")
        check(all(t.boxes[k].inner.isVisibleTo(t.left_scroll) == (k == key) for k in t.boxes), f"'{key}': only its body is visible")
    t.rail_btns["media"].click(); pump()
    check(open_keys() == [], "click the active rail button again -> collapsed")
    t.boxes["voice"].btn.click(); pump(); t.boxes["speakers"].btn.click(); pump()
    check(open_keys() == ["speakers"], "header clicks also behave as accordion")
    check(len(t.boxes) == 8 and len({id(b) for b in t.boxes.values()}) == 8, "no panels duplicated")
    check(t.rail_btns["voice"].property("active") is False, "rail highlight follows closing panel")

    # ---- preview fit ----
    cv = t.canvas
    t.pv_stack.setCurrentIndex(1)
    for (sw, sh) in ((1920, 1080), (1080, 1920), (1000, 1000), (1440, 1080), (2560, 1080)):
        cv.set_source(sw, sh, 10.0)
        for (ww, wh) in ((1500, 920), (1120, 700), (1800, 1000), (1200, 900), (1120, 1000)):
            w.resize(ww, wh); pump(10)
            r = cv.mapFromScene(cv.sceneRect().topLeft())  # noqa (keeps the view alive)
            tl, br = cv.mapFromScene(0, 0), cv.mapFromScene(cv.sw, cv.sh)
            fw, fh = br.x() - tl.x(), br.y() - tl.y()
            vp = cv.viewport().rect()
            tag = f"{sw}x{sh} in window {ww}x{wh}"
            check(abs((fw / fh) - (cv.sw / cv.sh)) < 0.01, f"{tag}: displayed aspect == frame aspect ({fw}x{fh})")
            check(tl.x() >= 0 and tl.y() >= 0 and br.x() <= vp.width() and br.y() <= vp.height(), f"{tag}: whole frame inside the viewport")
            check(not cv.horizontalScrollBar().isVisible() and not cv.verticalScrollBar().isVisible(), f"{tag}: no scrollbars while fitted")
            c = vp.center(); check(abs((tl.x() + br.x()) / 2 - c.x()) <= 2 and abs((tl.y() + br.y()) / 2 - c.y()) <= 2, f"{tag}: centred")
            bar = t.transport
            kids = [bar._w[k] for k in ("time", "back", "play", "fwd", "seek", "extra")]
            rects = [k.geometry() for k in kids]
            overlap = any(a.intersects(b2) for i, a in enumerate(rects) for b2 in rects[i + 1:])
            check(not overlap, f"{tag}: transport controls do not overlap ({'wide' if bar._wide else 'narrow'} layout)")
            check(bar.width() >= bar.minimumSizeHint().width(), f"{tag}: transport bar has its minimum width")

    # ---- existing buttons still wired ----
    for name in ("big_btn", "insp_btn", "tl_btn"):
        b = getattr(t, name); b.click(); pump(); b.click(); pump()
    check(True, "Big view / Inspector / Timeline toggles clickable without error")
    w.show_workspace(1); pump(); w.show_workspace(0); pump()
    check(True, "workspace tabs switch without error")

    print("\nRESULT:", "ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
