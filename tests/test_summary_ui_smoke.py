"""Smoke test of the Movie Summary Qt layer against the FAKE Qt (tests/qt_stub.py): proves the buttons are wired to the controller
and the whole flow runs without errors, and that the main window still builds the Translator when the new page is broken.
It cannot show how anything looks or feels in real Qt. Skipped when the real PySide6 is installed."""
import json
import os
import sys
import types
from pathlib import Path

import conftest  # noqa


class SkipTest(Exception):
    pass


def _stub():
    try:
        import PySide6.QtWidgets  # noqa
        if getattr(sys.modules["PySide6.QtWidgets"], "__file__", None) is not None:
            raise SkipTest("real PySide6 present")
    except ImportError:
        pass
    sys.path.insert(0, str(Path(__file__).parent))
    import qt_stub
    qt_stub.install()


def test_page_runs_the_whole_flow_through_its_buttons():
    _stub()
    from test_summary_engine import make_video, make_tone, FakeProvider, probe
    from app.summary.controller import SummaryController
    import app.ui.summary_page as SP
    import app.summary.voices as V

    tmp = Path(os.environ["MEDIASTUDIO_HOME"]) / "sum_smoke"
    pg = SP.SummaryPage(types.SimpleNamespace(), controller=SummaryController(root=tmp))
    pg._run = lambda fn, *a, done=None, **k: done(fn(*a, progress=None, cancel=lambda: False, **k)) if done else fn(*a, progress=None, cancel=lambda: False, **k)
    pg._beats_text = lambda: "0-2 hero wakes\n2-4 stranger knocks\n4-6 door opens"
    pg._provider = lambda: FakeProvider([json.dumps([{"id": i, "km": "ក ខ"} for i in (1, 2, 3)])])
    pg._voice_choice = lambda: ("edge", "km-KH-SreymomNeural")
    old_edge_synth = V.edge_synth
    V.edge_synth = lambda voice: (lambda text, out_path, emotion="Neutral", speed=1.0: make_tone(out_path, 0.8))

    pg._open(make_video())
    pg.thr.setValue(30); pg.minlen.setValue(0.5); pg.maxlen.setValue(60)
    pg.detect_scenes()
    assert len(pg.ctl.project.scenes) == 3
    pg.load_beats()
    pg.generate_script()
    assert [g.km for g in pg.ctl.project.segments] == ["ក ខ"] * 3
    pg.narrate()
    assert all(g.dur > 0 for g in pg.ctl.project.segments)
    pg.plan()
    assert len(pg.ctl.project.clips) == 3

    out = tmp / "page_out.mp4"
    class FD:
        @staticmethod
        def getSaveFileName(*a, **k):
            return str(out), ""
    SP.QFileDialog = FD
    pg.export()
    assert out.exists() and abs(probe(out)["duration"] - sum(g.dur for g in pg.ctl.project.segments)) < 0.3

    # table edits go to the controller
    class Item:
        def __init__(self, r, c, t): self._r, self._c, self._t = r, c, t
        def row(self): return self._r
        def column(self): return self._c
        def text(self): return self._t
    pg.table.item = lambda r, c: Item(r, c, "1")
    pg._refresh = lambda: None
    pg._cell_changed(Item(0, 2, "ថ្មី"))
    assert pg.ctl.project.segment(1).km == "ថ្មី"
    pg._cell_changed(Item(0, 4, "2 🔒"))
    assert pg.ctl.project.segment(1).scene_id == 2 and pg.ctl.project.segment(1).locked
    V.edge_synth = old_edge_synth


def test_main_window_survives_a_broken_summary_page_and_keeps_the_translator():
    _stub()
    import app.ui.summary_page as SP
    from app.shared.settings import Settings
    from app.ui.main_window import MainWindow
    real = SP.SummaryPage

    def boom(*a, **k):
        raise RuntimeError("simulated Qt failure")
    SP.SummaryPage = boom
    try:
        win = MainWindow(types.SimpleNamespace(settings=Settings(), db=None, downloads=None))
    finally:
        SP.SummaryPage = real
    assert win.summary is None and win.trans is not None                  # Translator is intact
    win.show_workspace(1); win.show_workspace(0)
    # closeEvent is not exercised: the fake Qt has no QMainWindow.closeEvent for the original `super().closeEvent` line
