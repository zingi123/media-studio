"""Inspector (selected object + video frame) and the canvas tool bar.  They edit the model objects directly and emit signals;
the page decides about history, canvas refresh and the Style-tab sync."""
from PySide6.QtCore import Qt, Signal, QTimer, QEvent
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QSlider,
                               QCheckBox, QComboBox, QPlainTextEdit, QColorDialog, QScrollArea, QFrame, QGroupBox, QButtonGroup)

from ..studio.model import FRAME_PRESETS, FIT_MODES


def _dspin(lo, hi, step=1.0, dec=1, suffix=""):
    s = QDoubleSpinBox(); s.setRange(lo, hi); s.setSingleStep(step); s.setDecimals(dec)
    if suffix:
        s.setSuffix(suffix)
    return s


class StudioToolsPanel(QWidget):
    """The "Tools" group of the left panel (between Style and Export): every editing tool in one place.
    (The old floating copy over the video was removed: the video stays clean.)"""
    toolSelected = Signal(str)
    addRequested = Signal(str)
    fitView = Signal()
    zoomView = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        lay = QVBoxLayout(self); lay.setContentsMargins(10, 8, 10, 10); lay.setSpacing(6)

        def title(t):
            l = QLabel(t); l.setObjectName("muted"); lay.addWidget(l)

        def grid(items, checkable=False):
            g = QGridLayout(); g.setSpacing(6); out = {}
            for n, (key, text, tip, fn) in enumerate(items):
                b = QPushButton(text); b.setToolTip(tip); b.setFixedHeight(32); b.setCheckable(checkable)
                b.clicked.connect(lambda _=False, k=key, f=fn: f(k)); g.addWidget(b, n // 2, n % 2); out[key] = b
            lay.addLayout(g); return out

        title("TOOLS")
        self.group = QButtonGroup(self); self.group.setExclusive(True)
        self.tools = grid((("select", "↖ Select", "Click an object on the video to select and edit it", self.toolSelected.emit),
                           ("picture", "✥ Picture", "Drag = move the picture inside the frame, wheel = zoom the picture", self.toolSelected.emit),
                           ("blur", "▭ Blur area", "Drag a rectangle on the video to blur that area", self.toolSelected.emit)), True)
        for b in self.tools.values():
            self.group.addButton(b)
        self.tools["select"].setChecked(True)
        title("ADD")
        grid((("logo", "+ Logo", "Add a logo", self.addRequested.emit), ("watermark", "+ Watermark", "Add a watermark", self.addRequested.emit),
              ("subtitle", "+ Khmer subtitle", "Add the Khmer subtitle", self.addRequested.emit), ("text", "+ Text", "Add a text object", self.addRequested.emit)))
        title("VIEW")
        grid((("out", "−  Zoom out", "Zoom the view out", lambda _k: self.zoomView.emit(1 / 1.2)),
              ("in", "+  Zoom in", "Zoom the view in", lambda _k: self.zoomView.emit(1.2)),
              ("fit", "Fit view", "Fit the frame into the window", lambda _k: self.fitView.emit())))

    def set_tool(self, key):
        b = self.tools.get(key)
        if b is not None:
            b.setChecked(True)


class InspectorPanel(QWidget):
    itemEdited = Signal(str)          # id of the item whose values changed
    frameEdited = Signal()
    editStarted = Signal()            # before a value is written (history)
    action = Signal(str)              # duplicate | delete | front | back | font | pick_image | auto_fit | reset_picture
    setTime = Signal(str)             # "start" | "end"  -> page sets it to the playhead

    def __init__(self, parent=None):
        super().__init__(parent)
        self.item, self.frame, self._busy, self._color = None, None, False, "#FFFFFF"
        self._mirror_cb = None
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setFrameShape(QFrame.NoFrame); outer.addWidget(sc)
        body = QWidget(); body.setObjectName("plain"); sc.setWidget(body)
        bl = QVBoxLayout(body); bl.setContentsMargins(6, 6, 6, 6); bl.setSpacing(8)

        # ---------- object ----------
        self.obj_box = QGroupBox("Selected object"); g = QGridLayout(self.obj_box); g.setVerticalSpacing(5)
        self.kind_lbl = QLabel("Nothing selected"); self.kind_lbl.setObjectName("cardTitle")
        g.addWidget(self.kind_lbl, 0, 0, 1, 4)
        self.enabled = QCheckBox("Visible"); self.enabled.toggled.connect(lambda v: self._set("enabled", bool(v)))
        g.addWidget(self.enabled, 1, 0, 1, 2)
        self.pick_btn = QPushButton("Change image..."); self.pick_btn.clicked.connect(lambda: self.action.emit("pick_image"))
        g.addWidget(self.pick_btn, 1, 2, 1, 2)
        self.x = _dspin(-50, 150, 1, 1, " %"); self.y = _dspin(-50, 150, 1, 1, " %")
        self.w = _dspin(0.5, 300, 1, 1, " %"); self.h = _dspin(0.5, 300, 1, 1, " %")
        self.rot = _dspin(-180, 180, 1, 1, " °")
        for r, (lbl, wd, f) in enumerate((("X (centre)", self.x, "cx"), ("Y (centre)", self.y, "cy"), ("Width", self.w, "w"), ("Height", self.h, "h"),
                                          ("Rotation", self.rot, "rot"))):
            g.addWidget(QLabel(lbl), 2 + r, 0, 1, 2); g.addWidget(wd, 2 + r, 2, 1, 2)
            if f == "rot":
                wd.valueChanged.connect(lambda v: self._set("rot", float(v)))
            else:
                wd.valueChanged.connect(lambda v, f=f: self._set(f, float(v) / 100.0))
        self.op = QSlider(Qt.Horizontal); self.op.setRange(0, 100); self.op.setValue(100)
        self.op_lbl = QLabel("100%"); self.op.valueChanged.connect(lambda v: (self.op_lbl.setText(f"{v}%"), self._set("opacity", v / 100.0)))
        g.addWidget(QLabel("Opacity"), 7, 0, 1, 2); g.addWidget(self.op, 7, 2); g.addWidget(self.op_lbl, 7, 3)
        self.t_start = _dspin(0, 100000, 0.1, 2, " s"); self.t_end = _dspin(0, 100000, 0.1, 2, " s")
        self.t_start.setSpecialValueText("0 (start)"); self.t_end.setSpecialValueText("end of video")
        self.t_start.valueChanged.connect(lambda v: self._set("start", float(v)))
        self.t_end.valueChanged.connect(lambda v: self._set("end", float(v)))
        g.addWidget(QLabel("Show from"), 8, 0); g.addWidget(self.t_start, 8, 1, 1, 2)
        b = QPushButton("= ▶"); b.setFixedWidth(40); b.setToolTip("Use the playhead time"); b.clicked.connect(lambda: self.setTime.emit("start")); g.addWidget(b, 8, 3)
        g.addWidget(QLabel("Show until"), 9, 0); g.addWidget(self.t_end, 9, 1, 1, 2)
        b = QPushButton("= ▶"); b.setFixedWidth(40); b.setToolTip("Use the playhead time"); b.clicked.connect(lambda: self.setTime.emit("end")); g.addWidget(b, 9, 3)
        # text
        self.text_box = QWidget(); tg = QGridLayout(self.text_box); tg.setContentsMargins(0, 0, 0, 0); tg.setVerticalSpacing(5)
        self.text = QPlainTextEdit(); self.text.setFixedHeight(54); self.text.setPlaceholderText("Khmer text")
        self.text.textChanged.connect(lambda: self._set("text", self.text.toPlainText()))
        tg.addWidget(self.text, 0, 0, 1, 4)
        self.font_btn = QPushButton("Font..."); self.font_btn.clicked.connect(lambda: self.action.emit("font")); tg.addWidget(self.font_btn, 1, 0, 1, 2)
        self.size = QSpinBox(); self.size.setRange(8, 400); self.size.setSuffix(" px"); self.size.valueChanged.connect(lambda v: self._set("size", int(v)))
        tg.addWidget(self.size, 1, 2, 1, 2)
        self.color_btn = QPushButton("Colour"); self.color_btn.clicked.connect(self._pick_color); tg.addWidget(self.color_btn, 2, 0)
        self.outline = QCheckBox("Outline"); self.outline.toggled.connect(lambda v: self._set("outline", bool(v))); tg.addWidget(self.outline, 2, 1)
        self.bold = QCheckBox("Bold"); self.bold.toggled.connect(lambda v: self._set("bold", bool(v))); tg.addWidget(self.bold, 2, 2)
        self.scroll = QCheckBox("Running text"); self.scroll.toggled.connect(lambda v: self._set("scroll", bool(v))); tg.addWidget(self.scroll, 3, 0, 1, 2)
        self.speed = _dspin(2, 200, 2, 0, " %/s"); self.speed.setToolTip("Frame widths per second (x100)")
        self.speed.valueChanged.connect(lambda v: self._set("scroll_speed", float(v) / 100.0)); tg.addWidget(self.speed, 3, 2, 1, 2)
        g.addWidget(self.text_box, 10, 0, 1, 4)
        # blur
        self.blur_box = QWidget(); bg = QHBoxLayout(self.blur_box); bg.setContentsMargins(0, 0, 0, 0)
        bg.addWidget(QLabel("Blur level"))
        self.blur = QSlider(Qt.Horizontal); self.blur.setRange(0, 100); self.blur_lbl = QLabel("60%")
        self.blur.valueChanged.connect(lambda v: (self.blur_lbl.setText(f"{v}%"), self._set("blur_percent", int(v))))
        bg.addWidget(self.blur, 1); bg.addWidget(self.blur_lbl)
        g.addWidget(self.blur_box, 11, 0, 1, 4)
        # actions
        row = QHBoxLayout()
        for key, text in (("duplicate", "Copy"), ("delete", "Delete"), ("front", "▲"), ("back", "▼")):
            b = QPushButton(text); b.clicked.connect(lambda _=False, k=key: self.action.emit(k))
            if len(text) == 1:
                b.setFixedWidth(34)
            if key == "delete":
                b.setObjectName("danger")
            row.addWidget(b)
        g.addLayout(row, 12, 0, 1, 4)
        bl.addWidget(self.obj_box)

        # ---------- frame ----------
        fb = QGroupBox("Video frame"); fg = QGridLayout(fb); fg.setVerticalSpacing(5)
        self.preset = QComboBox(); self.preset.addItems(list(FRAME_PRESETS))
        self.preset.currentTextChanged.connect(lambda v: self._fset("preset", v))
        self.cw = QSpinBox(); self.cw.setRange(16, 8192); self.cw.valueChanged.connect(lambda v: self._fset("custom_w", int(v)))
        self.ch = QSpinBox(); self.ch.setRange(16, 8192); self.ch.valueChanged.connect(lambda v: self._fset("custom_h", int(v)))
        self.fit = QComboBox(); self.fit.addItems(["Fit (whole picture)", "Fill (crop to frame)", "Stretch"])
        self.fit.currentIndexChanged.connect(lambda i: self._fset("fit", FIT_MODES[i]))
        fg.addWidget(QLabel("Frame"), 0, 0); fg.addWidget(self.preset, 0, 1, 1, 3)
        fg.addWidget(QLabel("Custom"), 1, 0); fg.addWidget(self.cw, 1, 1); fg.addWidget(QLabel("×"), 1, 2); fg.addWidget(self.ch, 1, 3)
        fg.addWidget(QLabel("Picture"), 2, 0); fg.addWidget(self.fit, 2, 1, 1, 3)
        self.auto = QCheckBox("Auto Fit on import"); self.auto.toggled.connect(lambda v: self._fset("auto_fit", bool(v)))
        ab = QPushButton("Auto Fit now"); ab.clicked.connect(lambda: self.action.emit("auto_fit"))
        fg.addWidget(self.auto, 3, 0, 1, 2); fg.addWidget(ab, 3, 2, 1, 2)
        self.zoom = QSlider(Qt.Horizontal); self.zoom.setRange(10, 400); self.zoom.setValue(100); self.zoom_lbl = QLabel("100%")
        self.zoom.valueChanged.connect(lambda v: (self.zoom_lbl.setText(f"{v}%"), self._fset("zoom", v / 100.0)))
        fg.addWidget(QLabel("Zoom"), 4, 0); fg.addWidget(self.zoom, 4, 1, 1, 2); fg.addWidget(self.zoom_lbl, 4, 3)
        self.ox = _dspin(-200, 200, 1, 1, " %"); self.oy = _dspin(-200, 200, 1, 1, " %")
        self.ox.valueChanged.connect(lambda v: self._fset("off_x", v / 100.0)); self.oy.valueChanged.connect(lambda v: self._fset("off_y", v / 100.0))
        fg.addWidget(QLabel("Move X / Y"), 5, 0); fg.addWidget(self.ox, 5, 1, 1, 1); fg.addWidget(self.oy, 5, 2, 1, 2)
        self.crop = {}
        for k, (name, f) in enumerate((("Crop L", "crop_l"), ("Crop T", "crop_t"), ("Crop R", "crop_r"), ("Crop B", "crop_b"))):
            s = _dspin(0, 90, 1, 1, " %"); s.valueChanged.connect(lambda v, f=f: self._fset(f, v / 100.0))
            self.crop[f] = s
            fg.addWidget(QLabel(name), 6 + k // 2, 0 if k % 2 == 0 else 2); fg.addWidget(s, 6 + k // 2, 1 if k % 2 == 0 else 3)
        self.mirror = QCheckBox("Mirror (flip left-right)"); self.mirror.toggled.connect(self._mirror_toggled)
        fg.addWidget(self.mirror, 8, 0, 1, 3)
        rb = QPushButton("Reset picture"); rb.clicked.connect(lambda: self.action.emit("reset_picture")); fg.addWidget(rb, 8, 3)
        bl.addWidget(fb)
        bl.addStretch(1)
        self.set_item(None)

    # ---------- values <-> model ----------
    def _set(self, field, value):
        if self._busy or self.item is None:
            return
        self.editStarted.emit()
        setattr(self.item, field, value)
        self.item.clamp()
        self.itemEdited.emit(self.item.id)

    def _fset(self, field, value):
        if self._busy or self.frame is None:
            return
        self.editStarted.emit()
        setattr(self.frame, field, value)
        self.frameEdited.emit()

    def _mirror_toggled(self, v):
        if not self._busy:
            self.editStarted.emit()
            self.action.emit("mirror_on" if v else "mirror_off")

    def _pick_color(self):
        if self.item is None:
            return
        c = QColorDialog.getColor(Qt.white, self, "Colour")
        if c.isValid():
            self._color = c.name(); self._set("color", self._color); self._paint_color()

    def _paint_color(self):
        self.color_btn.setStyleSheet(f"background:{self._color};color:{'#000' if self._color.lower() in ('#ffffff', '#ffff00') else '#fff'}")

    def set_item(self, item):
        self.item = item
        self.refresh_item()

    def refresh_item(self):
        it = self.item
        self._busy = True
        on = it is not None
        for w in (self.enabled, self.x, self.y, self.w, self.h, self.rot, self.op, self.t_start, self.t_end):
            w.setEnabled(on)
        self.pick_btn.setVisible(on and it.kind in ("logo", "watermark"))
        self.text_box.setVisible(on and it.kind in ("text", "subtitle"))
        self.text.setVisible(on and it.kind == "text")
        self.scroll.setVisible(on and it.kind == "text"); self.speed.setVisible(on and it.kind == "text")
        self.blur_box.setVisible(on and it.kind == "blur")
        if not on:
            self.kind_lbl.setText("Nothing selected - click an object on the video")
        else:
            self.kind_lbl.setText(it.label() + ("  (Style-tab object)" if it.key else ""))
            self.enabled.setChecked(it.enabled)
            self.x.setValue(it.cx * 100); self.y.setValue(it.cy * 100); self.w.setValue(it.w * 100); self.h.setValue(it.h * 100)
            self.h.setEnabled(it.kind not in ("logo", "watermark"))
            self.rot.setValue(it.rot); self.rot.setEnabled(it.kind != "blur")
            self.op.setValue(int(round(it.opacity * 100)))
            self.t_start.setValue(it.start); self.t_end.setValue(max(0.0, it.end))
            self.text.setPlainText(it.text) if self.text.toPlainText() != it.text else None
            self.size.setValue(int(it.size)); self.outline.setChecked(it.outline); self.bold.setChecked(it.bold)
            self.scroll.setChecked(it.scroll); self.speed.setValue(it.scroll_speed * 100)
            self._color = it.color; self._paint_color()
            self.blur.setValue(int(it.blur_percent)); self.blur_lbl.setText(f"{int(it.blur_percent)}%")
        self._busy = False

    def set_frame(self, frame, mirror=False):
        self.frame = frame
        self._busy = True
        self.preset.setCurrentText(frame.preset)
        self.cw.setValue(frame.custom_w); self.ch.setValue(frame.custom_h)
        self.cw.setEnabled(frame.preset == "Custom"); self.ch.setEnabled(frame.preset == "Custom")
        self.fit.setCurrentIndex(FIT_MODES.index(frame.fit) if frame.fit in FIT_MODES else 0)
        self.auto.setChecked(frame.auto_fit)
        self.zoom.setValue(int(round(frame.zoom * 100))); self.zoom_lbl.setText(f"{int(round(frame.zoom * 100))}%")
        self.ox.setValue(frame.off_x * 100); self.oy.setValue(frame.off_y * 100)
        for f, s in self.crop.items():
            s.setValue(getattr(frame, f) * 100)
        self.mirror.setChecked(bool(mirror))
        self._busy = False
