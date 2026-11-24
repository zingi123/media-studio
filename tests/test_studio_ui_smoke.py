"""Smoke test of the Qt layer against a FAKE Qt (tests/qt_stub.py): proves the wiring (Style tabs <-> on-screen objects, undo,
timeline editing, export with the mixer and NO automatic Demucs) runs without errors. It cannot show how anything looks or feels in real Qt.
Skipped automatically when the real PySide6 is installed."""
import os
import sys
import types
import wave
from pathlib import Path

import numpy as np


class SkipTest(Exception):
    pass


def _tone(path, f, dur, sr=24000):
    t = np.arange(int(dur * sr)) / sr
    x = (np.sin(2 * np.pi * f * t) * 0.4 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(x.tobytes())


def _video(path):
    import subprocess, shutil
    ff = shutil.which("ffmpeg")
    if not ff:
        raise SkipTest("no ffmpeg")
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=4", "-f", "lavfi", "-i",
                    "sine=frequency=330:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)


def _page(tmp):
    try:
        import PySide6.QtWidgets  # noqa
        if not getattr(sys.modules["PySide6.QtWidgets"], "__file__", None) is None:
            raise SkipTest("real PySide6 present")
    except ImportError:
        pass
    sys.path.insert(0, str(Path(__file__).parent))
    import qt_stub
    qt_stub.install()
    from app.shared.settings import Settings
    from app.ui.translator_page import TranslatorPage
    ctx = types.SimpleNamespace(settings=Settings(), db=None, downloads=None)
    pg = TranslatorPage(ctx)
    pg._run = lambda fn, *a, done=None, alert=False, on_fail=None, **k: done(fn(*a, progress=None, cancel=lambda: False, **k)) if done else fn(*a, progress=None, cancel=lambda: False, **k)
    return pg


def test_style_tabs_sync_undo_timeline_and_export_without_auto_demucs():
    tmp = Path(os.environ["MEDIASTUDIO_HOME"]) / "smoke"
    tmp.mkdir(parents=True, exist_ok=True)
    vid = tmp / "clip.mp4"
    _video(vid)
    pg = _page(tmp)
    from app.translator.subtitles import Cue
    from PIL import Image
    pg.load_video(str(vid))
    assert pg.canvas.src_w == 640 and abs(pg.vid_dur - 4.0) < 0.2

    # --- Style tabs create / drive the on-screen objects
    pg.blur_btn.setChecked(True)
    b = pg.scene.primary("blur")
    assert b is not None and b.enabled and abs(b.cy - (0.70 + 0.055)) < 1e-6
    pg.b_top.setValue(10)
    assert abs(pg.scene.primary("blur").rect()[1] - 0.10) < 1e-6
    logo = tmp / "logo.png"; Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(logo)
    pg._logo_set(str(logo))
    lg = pg.scene.primary("logo")
    assert lg is not None and lg.enabled and lg.cx > 0.5 and lg.cy < 0.3          # 'Top Right'
    pg.l_size.setValue(30)
    assert abs(pg.scene.primary("logo").w - 0.30) < 1e-6
    pg.sub_mode.setCurrentIndex(1)
    sub = pg.scene.primary("subtitle")
    assert sub is not None and sub.enabled and sub.cy > 0.5
    # --- edit on screen -> Style tab shows it
    lg.cx, lg.cy, lg.w = 0.25, 0.5, 0.4
    pg._st_edited_on_canvas(lg.id)
    assert pg.l_size.value() == 40
    pg.l_size.setValue(35)                                                          # manual placement is kept
    assert abs(pg.scene.primary("logo").cx - 0.25) < 1e-6
    # --- add / duplicate / delete objects + undo
    pg._st_add("text")
    n0 = len(pg.scene.items)
    txt = [i for i in pg.scene.items if i.kind == "text"][0]
    pg._st_action("duplicate", txt.id)
    assert len(pg.scene.items) == n0 + 1
    pg._st_undo()
    assert len(pg.scene.items) == n0
    pg._st_redo()
    assert len(pg.scene.items) == n0 + 1

    # --- dialogue + timeline editing
    vo = []
    for i in range(3):
        p = tmp / f"v{i}.wav"; _tone(p, 300 + 100 * i, 0.8); vo.append(p)
    pg.cues = [Cue(0.2 + i * 1.2, 1.2 + i * 1.2, f"原{i}", f"ខ្មែរ {i} words here", audio=str(vo[i])) for i in range(3)]
    pg.refresh()
    pg.tl.playhead = 0.7
    pg.tl.sel_cues = {0}
    pg.tl.split_at_playhead()
    assert len(pg.cues) == 4 and pg.cues[0].end == pg.cues[1].start == 0.7 and pg.cues[0].audio == ""
    pg._st_undo()
    assert len(pg.cues) == 3 and pg.cues[0].audio
    pg.tl.sel_cues = {0, 1}
    pg.tl.merge_selected()
    assert len(pg.cues) == 2
    pg._st_undo()
    assert len(pg.cues) == 3

    # --- voice sync: edited line is the only one to regenerate
    from app.studio import dialogue as D
    for c in pg.cues:
        D.mark_generated(c)
    assert D.dirty_rows(pg.cues) == []
    pg.cues[1].translation = "changed text"
    assert D.dirty_rows(pg.cues) == [1]

    # --- mixer + export: Demucs must NOT run
    import app.translator.separation as SEP
    SEP.separate = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Demucs was started by Export"))
    pg.cues[1].translation = "ខ្មែរ 1 words here"
    pg.mixer.channels["khmer_voice"].volume = 80
    pg.out_dir = str(tmp / "out")
    pg.export_mode.setCurrentIndex(3)
    pg.export()
    outs = list((tmp / "out").glob("*_khmer_dub*.mp4"))
    assert outs, pg.log.toPlainText()
    from app.shared.ffmpeg_util import probe
    info = probe(outs[0])
    assert info["has_audio"] and info["width"] == 640
    # the project is saved
    pg.studio_save()
    from app.studio.model import load_project, project_file
    from app.shared.paths import work_dir
    sc, mx, extra = load_project(project_file(work_dir("clip")))
    assert sc.primary("logo") is not None and mx.channels["khmer_voice"].volume == 80
