"""STEP 1 logic tests - need NO Qt. Run from the project root:  python tests/test_step1_logic.py -v"""
import importlib.util, os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):          # load a module file directly (app/ui/__init__ and app/studio/__init__ import nothing Qt)
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


accordion = _load("accordion", "app/ui/accordion.py")
geometry = _load("geometry", "app/studio/geometry.py")


class FakeSignal:
    def __init__(self): self._slots = []
    def connect(self, f): self._slots.append(f)
    def emit(self, *a):
        for f in list(self._slots): f(*a)


class FakeBtn:                  # behaves like a checkable QToolButton: setChecked emits toggled only on a real change
    def __init__(self, on=False): self._on = on; self.toggled = FakeSignal()
    def isChecked(self): return self._on
    def setChecked(self, v):
        v = bool(v)
        if v != self._on:
            self._on = v; self.toggled.emit(v)
    def click(self): self.setChecked(not self._on)


class FakeBox:
    def __init__(self, name, on=False): self.name = name; self.btn = FakeBtn(on)


KEYS = ("media", "script", "translate", "voice", "style", "tools", "export")


def make(open_key="media"):
    boxes = {k: FakeBox(k, k == open_key) for k in KEYS}
    return boxes, accordion.AccordionGroup(list(boxes.values()))


def opened(boxes): return [k for k, b in boxes.items() if b.btn.isChecked()]


class AccordionTests(unittest.TestCase):
    def test_start_state_one_open(self):
        boxes, _ = make(); self.assertEqual(opened(boxes), ["media"])

    def test_open_another_closes_previous(self):
        boxes, g = make(); boxes["voice"].btn.click(); self.assertEqual(opened(boxes), ["voice"])

    def test_every_pair_switch_leaves_exactly_one_open(self):
        for a in KEYS:
            for b in KEYS:
                if a == b: continue
                boxes, g = make(a); boxes[b].btn.click()
                self.assertEqual(opened(boxes), [b], (a, b))

    def test_click_active_header_collapses(self):
        boxes, g = make(); boxes["media"].btn.click(); self.assertEqual(opened(boxes), []); self.assertIsNone(g.open_item())

    def test_rail_toggle_opens_then_collapses(self):
        boxes, g = make()
        self.assertTrue(g.toggle(boxes["style"])); self.assertEqual(opened(boxes), ["style"])
        self.assertFalse(g.toggle(boxes["style"])); self.assertEqual(opened(boxes), [])

    def test_rail_switch_sequence(self):
        boxes, g = make()
        for k in ("script", "translate", "voice", "style", "tools", "export", "media", "export"):
            g.toggle(boxes[k]); self.assertLessEqual(len(opened(boxes)), 1)
            self.assertEqual(opened(boxes), [k])

    def test_reopen_after_collapse(self):
        boxes, g = make(); boxes["media"].btn.click(); boxes["media"].btn.click(); self.assertEqual(opened(boxes), ["media"])

    def test_multiple_open_at_start_is_normalised(self):
        boxes = {k: FakeBox(k, True) for k in KEYS}; accordion.AccordionGroup(list(boxes.values()))
        self.assertEqual(len(opened(boxes)), 1)

    def test_rail_highlight_signal_fires_for_closed_panel(self):   # rail buttons listen to toggled(False) of the panel that closes
        boxes, g = make(); seen = []
        boxes["media"].btn.toggled.connect(lambda on: seen.append(on))
        boxes["script"].btn.click(); self.assertEqual(seen, [False])

    def test_no_items_lost_or_duplicated(self):
        boxes, g = make(); self.assertEqual(len(g.items), len(KEYS)); self.assertEqual(len({id(i) for i in g.items}), len(KEYS))


class FitTests(unittest.TestCase):
    ASPECTS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1000, 1000), "4:3": (1440, 1080),
               "21:9": (2560, 1080), "3:4": (1080, 1440), "tiny": (2, 2), "tall 1:5": (200, 1000)}
    VIEWS = [(800, 450), (400, 700), (1200, 300), (300, 1200), (1000, 1000), (160, 90), (2560, 1440), (33, 29)]

    def test_fits_inside_with_gutter_and_keeps_aspect(self):
        for name, (fw, fh) in self.ASPECTS.items():
            for vw, vh in self.VIEWS:
                k = geometry.fit_scale(vw, vh, fw, fh)
                if min(vw, vh) > 2 * geometry.FIT_MARGIN_PX + 1:
                    self.assertLessEqual(fw * k, vw - 2 * geometry.FIT_MARGIN_PX + 1e-6, (name, vw, vh))
                    self.assertLessEqual(fh * k, vh - 2 * geometry.FIT_MARGIN_PX + 1e-6, (name, vw, vh))
                # one scale for both axes => displayed ratio == source ratio exactly (no stretch)
                self.assertAlmostEqual((fw * k) / (fh * k), fw / fh, places=9)

    def test_fills_the_limiting_axis(self):                      # as large as possible: touches the limiting side
        m = 2 * geometry.FIT_MARGIN_PX
        k = geometry.fit_scale(800, 450, 1920, 1080)              # 16:9 video in a 800x450 view: height limits
        self.assertAlmostEqual(1080 * k, 450 - m, places=6)
        k = geometry.fit_scale(1200, 300, 1920, 1080)             # very wide view: height limits again
        self.assertAlmostEqual(1080 * k, 300 - m, places=6)
        k = geometry.fit_scale(400, 700, 1080, 1920)              # 9:16 video in a 400x700 view: width limits
        self.assertAlmostEqual(1080 * k, 400 - m, places=6)
        self.assertLess(1920 * k, 700 - m)

    def test_degenerate_sizes_do_not_crash(self):
        for args in ((0, 0, 1000, 562), (100, 0, 1000, 562), (800, 450, 0, 0), (-5, 10, 10, 10)):
            self.assertEqual(geometry.fit_scale(*args), 1.0)
        self.assertGreater(geometry.fit_scale(10, 10, 1000, 1000), 0)     # viewport smaller than the gutter

    def test_monotonic_on_window_resize(self):                   # growing the window never shrinks the picture
        prev = 0
        for w in range(200, 2000, 37):
            k = geometry.fit_scale(w, int(w * 0.6), 1920, 1080); self.assertGreaterEqual(k, prev - 1e-12); prev = k


if __name__ == "__main__":
    unittest.main()
