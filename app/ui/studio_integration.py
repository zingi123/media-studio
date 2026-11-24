"""StudioMixin: connects the new studio widgets (canvas, inspector, timeline, mixer) to TranslatorPage.

The page keeps its single source of truth for dialogue (`self.cues`); the studio adds `self.scene` (objects + frame),
`self.mixer`, `self.clips` (SFX / music) and ONE undo history for all of them.  The old Style tabs (Blur / Subtitle / Logo / Track)
stay and are kept in sync with the matching on-screen objects ("primary" objects).
"""
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QItemSelectionModel
from PySide6.QtWidgets import QFileDialog, QMessageBox, QTabWidget, QWidget, QVBoxLayout, QPushButton, QScrollArea, QFrame

from ..shared.paths import work_dir
from ..studio import audio as AU
from ..studio import dialogue as D
from ..studio import geometry as G
from ..studio import model as M
from ..studio import tracks as TR
from ..translator.voice_synchronizer import build_voice_track
from .mixer_panel import MixerPanel
from .studio_panels import StudioToolsPanel, InspectorPanel
from .studio_timeline import StudioTimeline
from .video_canvas import VideoCanvas
from .playback import PlaybackMixin


class StudioMixin(PlaybackMixin):
    # ------------------------------------------------------------------ state
    def _studio_init(self):
        self.scene = M.Scene()
        self.mixer = M.Mixer()
        self.clips = []                       # AudioClip (SFX / music)
        self.hist = TR.StudioHistory()
        self._sync_guard = False
        self._manual = set()                  # primary objects the user placed by hand (legacy position presets no longer override them)
        self._last_soft_capture = 0.0
        self._save_timer = None
        self.mixplayer = None                 # (STEP 2: not used any more - the mixer plays through the unified engine)
        self._mix_prev_vol = None
        self._video_fp = ""
        self._pb_init()                       # STEP 2: unified audio engine (needs self.mixer)

    # ------------------------------------------------------------------ widgets
    def _studio_make_canvas(self):
        if getattr(self, "canvas", None) is not None:      # already built (the left panel needs the Tools group first)
            return
        self.tools_panel = StudioToolsPanel()     # docked copy: the "Tools" group in the left panel
        self.canvas = VideoCanvas()
        self.canvas.set_model(self.scene)
        self.canvas.cue_text = lambda t: (lambda c: c.translation if c else "")(self._cue_at(t))
        self.inspector = InspectorPanel()
        self.inspector.set_frame(self.scene.frame, self.scene.mirror)
        cv, ins = self.canvas, self.inspector
        tp = self.tools_panel
        tp.toolSelected.connect(cv.set_tool)
        cv.toolChanged.connect(tp.set_tool)
        tp.addRequested.connect(self._st_add)
        tp.fitView.connect(cv.fit_view)
        tp.zoomView.connect(cv.zoom_view)
        cv.editStarted.connect(self._st_capture)
        cv.editFinished.connect(self._st_schedule_save)
        cv.objectSelected.connect(self._st_selected)
        cv.objectEdited.connect(self._st_edited_on_canvas)
        cv.blurAreaDrawn.connect(self._st_blur_drawn)
        cv.pictureEdited.connect(lambda: (ins.set_frame(self.scene.frame, self.scene.mirror), self._st_schedule_save()))
        cv.contextRequested.connect(lambda act, iid: self._st_action(act, iid))
        ins.editStarted.connect(self._st_capture_soft)
        ins.itemEdited.connect(self._st_edited_in_inspector)
        ins.frameEdited.connect(self._st_frame_edited)
        ins.action.connect(self._st_action)
        ins.setTime.connect(self._st_set_time)

    def _studio_make_timeline(self):
        self.tl = StudioTimeline(lambda: self.cues, lambda: self.clips)
        tl = self.tl
        tl.cueClicked.connect(self._st_cue_clicked)
        tl.seekRequested.connect(self._seek)
        tl.editStarted.connect(self._st_capture)
        tl.edited.connect(self._st_timeline_edited)
        tl.undoRequested.connect(self._st_undo)
        tl.redoRequested.connect(self._st_redo)
        tl.playPauseRequested.connect(self._toggle_play)
        tl.addAudioRequested.connect(self._st_add_audio)
        tl.regenerateRequested.connect(self.regen_edited)
        tl.voiceDeleteRequested.connect(self._st_delete_voice)
        tl.selectionChanged.connect(self._st_mirror_selection)
        self.mixer_panel = MixerPanel()
        self.mixer_panel.set_mixer(self.mixer)
        mp = self.mixer_panel
        mp.changed.connect(self._st_mixer_changed)
        mp.demucsRequested.connect(self.run_demucs_clicked)
        mp.listenRequested.connect(self._st_listen)
        mp.stopRequested.connect(lambda: self._pb_stop() if self.vplayer else None)
        self.bottom_tabs = QTabWidget()
        self.table.setMinimumHeight(40)                       # small minimums: the video above gets the height; the table scrolls
        mix_sc = QScrollArea(); mix_sc.setWidgetResizable(True); mix_sc.setFrameShape(QFrame.NoFrame); mix_sc.setWidget(mp)
        self.bottom_tabs.setMinimumHeight(70)
        self.bottom_tabs.addTab(self.table, "Dialogue table")
        self.bottom_tabs.addTab(mix_sc, "Audio mixer")
        return tl, self.bottom_tabs

    def _studio_voice_buttons(self, layout):
        """Extra buttons for the Voice box (called from _build_left)."""
        b = QPushButton("Regenerate edited lines"); b.setToolTip("Only the lines whose Khmer text / voice / emotion / speed changed")
        b.clicked.connect(self.regen_edited); layout.addWidget(b)
        b = QPushButton("Check voice timing"); b.setToolTip("Compare every generated voice with its time slot; offer to extend lines that are too short")
        b.clicked.connect(lambda: self._st_check_sync(True)); layout.addWidget(b)

    def _studio_export_buttons(self, layout):
        b = QPushButton("Run Demucs (separate original voice / music)"); b.setToolTip("Manual. Export never runs Demucs by itself; it uses the result of this button.")
        b.clicked.connect(self.run_demucs_clicked); layout.addWidget(b)
        self.demucs_state = QPushButton("Open Audio mixer"); self.demucs_state.clicked.connect(lambda: self.bottom_tabs.setCurrentIndex(1))
        layout.addWidget(self.demucs_state)

    # ------------------------------------------------------------------ wiring after the page is built
    def _studio_wire(self):
        self._pb_wire()                       # STEP 2: audio follows the video player state
        # legacy Style tabs  ->  primary objects
        L = self._legacy_to_scene
        for wd in (self.blur_pct,):
            wd.valueChanged.connect(lambda _=None: L(("blur",)))
        self.blur_btn.toggled.connect(lambda _=None: L(("blur",), ("blur",)))
        for wd in (self.b_top, self.b_h, self.b_left, self.b_w):
            wd.valueChanged.connect(lambda _=None: L(("blur",), ("blur",)))
        self.sub_mode.currentIndexChanged.connect(lambda _=None: L(("sub",)))
        for wd in (self.s_size, self.s_margin):
            wd.valueChanged.connect(lambda _=None: L(("sub",), ("sub",)))
        self.s_pos.currentIndexChanged.connect(lambda _=None: L(("sub",), ("sub",)))
        self.s_outline.toggled.connect(lambda _=None: L(("sub",)))
        self.s_color.clicked.connect(lambda: QTimer.singleShot(0, lambda: L(("sub",))))
        self.font_box.activated.connect(lambda _=None: QTimer.singleShot(0, lambda: L(("sub",))))
        self.logo_btn.toggled.connect(lambda _=None: L(("logo",), ("logo",)))
        self.l_size.valueChanged.connect(lambda _=None: L(("logo",), ("logo",)))
        self.l_op.valueChanged.connect(lambda _=None: L(("logo",)))
        self.l_pos.currentIndexChanged.connect(lambda _=None: L(("logo",), ("logo!",)))
        self.wm_btn.toggled.connect(lambda _=None: L(("wm",), ("wm",)))
        self.wm_size.valueChanged.connect(lambda _=None: L(("wm",), ("wm",)))
        self.wm_op.valueChanged.connect(lambda _=None: L(("wm",)))
        self.wm_pos.currentIndexChanged.connect(lambda _=None: L(("wm",), ("wm!",)))
        self.t_mirror.toggled.connect(lambda _=None: L(("mirror",)))
        # mixer <-> the two quick sliders of the Export box
        self.voice_vol.valueChanged.connect(lambda v: self._st_quick_slider("khmer_voice", v))
        self.bg_vol.valueChanged.connect(lambda v: self._st_quick_slider("orig_bg", v))
        self.keep_bg.setText("Include original voice / music (after Demucs)")
        self.keep_bg.setToolTip("Uses the stems made by the Demucs button. Export never starts Demucs itself.")
        self.video_hint_timer = None
        self._st_refresh_mixer_state()

    def _st_quick_slider(self, key, v):
        if self._sync_guard:
            return
        self.mixer.channels[key].volume = int(v)
        self.mixer_panel.set_mixer(self.mixer)
        self._st_schedule_save()

    # ------------------------------------------------------------------ history
    def _st_capture(self):
        self.hist.capture(self.cues, self.clips, self.scene)
        self._st_hist_buttons()

    def _st_capture_soft(self):
        """Inspector edits arrive value by value: one history step per burst."""
        now = time.time()
        if now - self._last_soft_capture > 0.8:
            self._st_capture()
        self._last_soft_capture = now

    def _st_hist_buttons(self):
        self.tl.set_history_state(self.hist.can_undo(), self.hist.can_redo())

    def _st_undo(self):
        ok, sc = self.hist.undo(self.cues, self.clips, self.scene)
        self._st_after_history(ok, sc)

    def _st_redo(self):
        ok, sc = self.hist.redo(self.cues, self.clips, self.scene)
        self._st_after_history(ok, sc)

    def _st_after_history(self, ok, sc):
        if not ok:
            return
        if sc is not None:
            self._st_set_scene(sc)
        self.refresh()
        self.tl.refresh()
        self._st_hist_buttons()
        self._pb_content_changed()
        self._st_schedule_save()

    def _st_set_scene(self, sc):
        self.scene = sc
        self.canvas.set_model(sc)
        self.inspector.set_frame(sc.frame, sc.mirror)
        self.inspector.set_item(None)
        self._pull_all_legacy()

    # ------------------------------------------------------------------ canvas / inspector events
    def _st_selected(self, iid):
        self.inspector.set_item(self.scene.get(iid))

    def _st_edited_on_canvas(self, iid):
        it = self.scene.get(iid)
        if it is not None and it.key:
            self._manual.add(it.id)
        self.inspector.refresh_item()
        self._pull_legacy(it)

    def _st_edited_in_inspector(self, iid):
        it = self.scene.get(iid)
        self.canvas.refresh_object(iid)
        if it is not None and it.key:
            self._manual.add(it.id)
        self._pull_legacy(it)
        self._st_schedule_save()

    def _st_frame_edited(self):
        self.canvas.relayout()
        self._st_schedule_save()

    def _st_blur_drawn(self, l, t, w, h):
        self._st_capture()
        it = self.scene.primary("blur")
        if it is None:
            it = self.scene.add(M.Item(kind="blur", key="blur"))
        it.enabled = True
        it.set_rect(l, t, w, h)
        self._manual.add(it.id)
        self.canvas.refresh_object(None); self.canvas.select(it.id)
        self._pull_legacy(it)
        self._st_schedule_save()

    def _st_add(self, kind):
        sc = self.scene
        self._st_capture()
        if kind in ("logo", "watermark"):
            f, _ = QFileDialog.getOpenFileName(self, "Image", "", "Images (*.png *.jpg *.jpeg *.webp)")
            if not f:
                return
            prim = sc.primary(kind) is None
            it = M.Item(kind=kind, key=M.PRIMARY[kind] if prim else "", path=f, w=0.15 if kind == "logo" else 0.2,
                        opacity=1.0 if kind == "logo" else 0.5, cx=0.85 if kind == "logo" else 0.8, cy=0.12 if kind == "logo" else 0.9)
            sc.add(it)
            if prim:
                self._set_legacy_path(kind, f)
        elif kind == "subtitle":
            if sc.primary("subtitle") is not None:
                it = sc.primary("subtitle"); it.enabled = True
                self.sub_mode.setCurrentIndex(1)
            else:
                self.sub_mode.setCurrentIndex(1)           # creates the primary subtitle object through the legacy sync
                it = sc.primary("subtitle")
        else:
            it = sc.add(M.Item(kind="text", text="អត្ថបទ · Text", cx=0.5, cy=0.15, w=0.7, h=0.1, size=64,
                               font_file=self.font_file, font_name=self._font_family(self.font_file) if Path(self.font_file).exists() else ""))
        if it is None:
            return
        self.canvas.refresh_object(None)
        self.canvas.select(it.id)
        self.inspector.set_item(it)
        self._pull_legacy(it)
        self._st_schedule_save()

    def _set_legacy_path(self, kind, path):
        self._sync_guard = True
        try:
            if kind == "logo":
                self.fx.logo.path = path; self.logo_name.setText(Path(path).name); self.logo_btn.setChecked(True)
            else:
                self.fx.track.wm_path = path; self.wm_name.setText(Path(path).name); self.wm_btn.setChecked(True)
                self._save_key("trk_wm_path", path)
        finally:
            self._sync_guard = False

    def _st_action(self, act, iid=None):
        it = self.scene.get(iid) if iid else self.canvas.selected()
        sc = self.scene
        if act in ("auto_fit", "reset_picture"):
            self._st_capture()
            f = sc.frame
            if act == "auto_fit":
                f.auto_choose(self.canvas.src_w or 1920, self.canvas.src_h or 1080)
            else:
                f.fit, f.zoom, f.off_x, f.off_y = "fit", 1.0, 0.0, 0.0
                f.crop_l = f.crop_t = f.crop_r = f.crop_b = 0.0
            self.inspector.set_frame(f, sc.mirror); self.canvas.relayout(); self._st_schedule_save()
            return
        if act in ("mirror_on", "mirror_off"):
            sc.mirror = act == "mirror_on"
            self._sync_guard = True
            try:
                self.t_mirror.setChecked(sc.mirror)
            finally:
                self._sync_guard = False
            self.canvas.relayout(); self._st_schedule_save()
            return
        if it is None:
            return
        if act == "pick_image":
            f, _ = QFileDialog.getOpenFileName(self, "Image", "", "Images (*.png *.jpg *.jpeg *.webp)")
            if f:
                self._st_capture(); it.path = f
                if it.key:
                    self._set_legacy_path("logo" if it.kind == "logo" else "watermark", f)
                self.canvas.refresh_object(it.id); self._st_schedule_save()
            return
        if act == "font":
            self._st_pick_font(it)
            return
        self._st_capture()
        if act == "duplicate":
            n = sc.duplicate(it.id)
            self.canvas.refresh_object(None)
            if n is not None:
                self.canvas.select(n.id); self.inspector.set_item(n)
        elif act == "delete":
            sc.remove(it.id)
            self.canvas.refresh_object(None); self.canvas.select(""); self.inspector.set_item(None)
            self._after_primary_removed(it)
        elif act in ("front", "back"):
            sc.move_order(it.id, 1 if act == "front" else -1); self.canvas.refresh_object(None)
        elif act == "toggle":
            it.enabled = not it.enabled; self.canvas.refresh_object(it.id); self.inspector.refresh_item(); self._pull_legacy(it)
        self._st_schedule_save()

    def _after_primary_removed(self, it):
        """Deleting a Style-tab object switches its Style-tab switch off (so it is not recreated)."""
        if not it.key:
            return
        self._sync_guard = True
        try:
            {"blur": lambda: self.blur_btn.setChecked(False), "sub": lambda: self.sub_mode.setCurrentIndex(0),
             "logo": lambda: self.logo_btn.setChecked(False), "wm": lambda: self.wm_btn.setChecked(False)}[it.key]()
        finally:
            self._sync_guard = False

    def _st_pick_font(self, it):
        f, _ = QFileDialog.getOpenFileName(self, "Khmer font (Unicode)", "", "Fonts (*.ttf *.otf)")
        if not f:
            return
        self._st_capture()
        it.font_file, it.font_name = f, self._font_family(f)
        self.canvas.refresh_object(it.id); self._st_schedule_save()

    def _st_set_time(self, which):
        it = self.canvas.selected()
        if it is None:
            return
        t = (self.vplayer.position() / 1000.0) if self.vplayer else self.tl.playhead
        self._st_capture()
        if which == "start":
            it.start = t
        else:
            it.end = max(t, it.start + 0.1)
        self.inspector.refresh_item(); self.canvas.refresh_object(it.id); self._st_schedule_save()

    # ------------------------------------------------------------------ legacy Style tabs <-> primary objects
    def _legacy_to_scene(self, kinds=("blur", "sub", "logo", "wm", "mirror"), geometry=()):
        if self._sync_guard or not hasattr(self, "canvas"):
            return
        self._sync_guard = True
        try:
            sc = self.scene
            geo = set(geometry)
            if "blur" in kinds:
                it, on = sc.primary("blur"), self.blur_btn.isChecked()
                new = it is None and on
                if new:
                    it = sc.add(M.Item(kind="blur", key="blur"))
                if it is not None:
                    it.enabled, it.blur_percent = on, self.blur_pct.value()
                    if new or "blur" in geo:
                        it.set_rect(self.b_left.value() / 100, self.b_top.value() / 100, self.b_w.value() / 100, self.b_h.value() / 100)
                        self._manual.discard(it.id)
            if "sub" in kinds:
                it, on = sc.primary("subtitle"), self.sub_mode.currentIndex() == 1
                new = it is None and on
                if new:
                    it = sc.add(M.Item(kind="subtitle", key="sub"))
                if it is not None:
                    it.enabled, it.size, it.color, it.outline = on, self.s_size.value(), self._color, self.s_outline.isChecked()
                    it.font_file = self.font_file
                    it.font_name = self._font_family(self.font_file) if Path(self.font_file).exists() else ""
                    if new or "sub" in geo:
                        it.w, it.h, it.cx = 0.9, max(0.06, it.size * 1.5 / 1080), 0.5
                        m, pos = self.s_margin.value() / 1080.0, self.s_pos.currentText()
                        it.cy = 1 - m - it.h / 2 if pos == "Bottom" else m + it.h / 2 if pos == "Top" else 0.5
                        self._manual.discard(it.id)
            for kind, key, btn, size, op, pos, path in (
                    ("logo", "logo", self.logo_btn, self.l_size, self.l_op, self.l_pos, self.fx.logo.path),
                    ("watermark", "wm", self.wm_btn, self.wm_size, self.wm_op, self.wm_pos, self.fx.track.wm_path)):
                if key not in kinds:
                    continue
                it = sc.primary(kind)
                on = btn.isChecked() and bool(path) and Path(path).exists()
                new = it is None and on
                if new:
                    it = sc.add(M.Item(kind=kind, key=key))
                if it is None:
                    continue
                it.enabled, it.path, it.opacity = on, path, op.value() / 100.0
                it.w = size.value() / 100.0
                self.canvas._ensure_aspect(it)
                force = (key + "!") in geo
                if new or force or (key in geo and it.id not in self._manual):
                    it.cx, it.cy = G.preset_position(pos.currentText(), it.w, it.h)
                    if force:
                        self._manual.discard(it.id)
            if "mirror" in kinds:
                sc.mirror = self.t_mirror.isChecked()
            self.canvas.relayout()
            self.canvas.rebuild_objects()
            self.inspector.set_item(self.canvas.selected())
            self.inspector.set_frame(sc.frame, sc.mirror)
        finally:
            self._sync_guard = False
        self._st_schedule_save()

    def _pull_legacy(self, it):
        """Object edited on screen -> the matching Style-tab widgets show the same numbers."""
        if it is None or not it.key or self._sync_guard:
            return
        self._sync_guard = True
        try:
            if it.key == "blur":
                l, t, w, h = it.rect()
                self.blur_pct.setValue(it.blur_percent); self.b_left.setValue(int(round(l * 100))); self.b_top.setValue(int(round(t * 100)))
                self.b_w.setValue(max(1, int(round(w * 100)))); self.b_h.setValue(max(1, int(round(h * 100))))
                self.blur_btn.setChecked(it.enabled)
            elif it.key == "sub":
                self.s_size.setValue(int(it.size)); self.s_outline.setChecked(it.outline)
                self.sub_mode.setCurrentIndex(1 if it.enabled else 0)
                self._color = it.color; self._paint_color()
                bottom_gap = (1 - it.cy - it.h / 2) * 1080
                pos = "Middle" if abs(it.cy - 0.5) < 0.12 else ("Top" if it.cy < 0.5 else "Bottom")
                self.s_pos.setCurrentText(pos)
                self.s_margin.setValue(int(max(0, round(bottom_gap if pos == "Bottom" else (it.cy - it.h / 2) * 1080 if pos == "Top" else 0))))
            elif it.key in ("logo", "wm"):
                size, op, btn = ((self.l_size, self.l_op, self.logo_btn) if it.key == "logo" else (self.wm_size, self.wm_op, self.wm_btn))
                size.setValue(max(size.minimum(), min(size.maximum(), int(round(it.w * 100)))))
                op.setValue(max(op.minimum(), min(op.maximum(), int(round(it.opacity * 100)))))
                btn.setChecked(it.enabled)
        finally:
            self._sync_guard = False

    def _pull_all_legacy(self):
        for it in self.scene.items:
            self._pull_legacy(it)
        self._sync_guard = True
        try:
            self.t_mirror.setChecked(self.scene.mirror)
        finally:
            self._sync_guard = False

    # ------------------------------------------------------------------ timeline events
    def _st_cue_clicked(self, i):
        self._st_mirror_selection()
        self._update_inspector(i)

    def _st_mirror_selection(self):
        """Table selection follows the timeline selection (without bouncing back)."""
        was, self._loading = self._loading, True
        try:
            sm = self.table.selectionModel()
            sm.clearSelection()
            for r in sorted(self.tl.sel_cues):
                if 0 <= r < self.table.rowCount():
                    sm.select(self.table.model().index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
            rows = sorted(self.tl.sel_cues)
            if rows and rows[0] < self.table.rowCount():
                self.table.setCurrentCell(rows[0], 0)
        finally:
            self._loading = was

    def _st_timeline_edited(self, what):
        self.refresh()
        self._st_mirror_selection()
        self.tl.refresh()
        self._st_hist_buttons()
        self._st_check_sync(False)
        self._st_refresh_mixer_state()
        self._pb_content_changed()             # timeline edits must invalidate the playback render cache
        self._st_schedule_save()

    def _st_add_audio(self, track):
        f, _ = QFileDialog.getOpenFileName(self, "Audio", "", "Audio (*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.opus *.mp4)")
        if not f:
            return
        try:
            k = AU.clip_from_file(f, track, start=self.tl.playhead)
        except Exception as e:  # noqa
            QMessageBox.warning(self, "Audio", str(e)); return
        self._st_capture()
        self.clips.append(k)
        self.tl.sel_clips = {k.id}
        self.tl.refresh(); self._st_refresh_mixer_state(); self._pb_content_changed(); self._st_schedule_save()

    def _st_delete_voice(self, rows):
        self._st_capture()
        for r in rows:
            if 0 <= r < len(self.cues):
                D.invalidate(self.cues[r])
        self.refresh(); self.tl.refresh(); self._pb_content_changed(); self._st_schedule_save()

    # ------------------------------------------------------------------ dialogue <-> voice sync
    def regen_edited(self):
        if not self._need_video():
            return
        self._collect_focus()
        rows = D.dirty_rows(self.cues)
        if not rows:
            self.log.appendPlainText("Nothing to regenerate: every line with Khmer text already has an up-to-date voice.")
            return
        self._st_capture()                   # generated audio is part of the same undoable dialogue state
        for r in rows:
            if D.has_audio(self.cues[r]):
                D.invalidate(self.cues[r])
        self.tl.refresh()
        self._pb_content_changed()
        self.tog["Process"].setChecked(True)
        self.log.appendPlainText(f"Regenerating {len(rows)} edited line(s): {[r + 1 for r in rows][:12]}")
        self._run(self._gen, rows, retry=True, done=self._gen_done)

    def _studio_after_generate(self):
        """Called from _gen_done: remember what each voice was generated from, measure it, check the timing."""
        for c in self.cues:
            if D.has_audio(c):
                D.mark_generated(c)
        D.measure(self.cues, force=True)
        self._st_check_sync(True)
        self.tl.refresh()
        self._pb_content_changed()
        self._st_schedule_save()

    def _st_check_sync(self, show=False):
        try:
            D.measure(self.cues)
            rep = D.check_sync(self.cues, self.vid_dur or None)
        except Exception as e:  # noqa
            if show:
                self.log.appendPlainText(f"Voice timing check failed: {e}")
            return
        self.tl.set_report({r["row"]: r["status"] for r in rep})
        n, bad = D.summarize(rep)
        if not show:
            return
        problems = [r for r in bad if r["status"] in ("long", "too_long")]
        if not problems:
            self.log.appendPlainText(f"✓ Voice timing: {n} line(s) checked, all voices fit their slots.")
            return
        for r in problems[:12]:
            self.log.appendPlainText(f"⚠ Line {r['row'] + 1}: voice {r['voice']:.1f}s, slot {r['slot']:.1f}s - {r['fix']}")
        if len(problems) > 12:
            self.log.appendPlainText(f"... and {len(problems) - 12} more")
        self.tog["Process"].setChecked(True)
        if QMessageBox.question(self, "Voice timing", f"{len(problems)} voice(s) are longer than their slot.\nExtend the End of those lines where "
                                "there is room (Start and the voice are not changed)?") == QMessageBox.Yes:
            self._st_capture()
            ch = D.extend_to_fit(self.cues, self.vid_dur or None)
            self.refresh(); self.tl.refresh(); self._st_check_sync(False); self._st_schedule_save()
            self.log.appendPlainText(f"Extended {len(ch)} line(s).")

    # ------------------------------------------------------------------ Demucs + mixer
    def run_demucs_clicked(self):
        if not self._need_video():
            return
        if AU.have_stems(self.video):
            self.log.appendPlainText("Demucs stems already exist for this video (vocals + background). Use the Audio mixer.")
            self.bottom_tabs.setCurrentIndex(1); self._st_refresh_mixer_state()
            return
        self.tog["Process"].setChecked(True)
        self.log.appendPlainText("Demucs separating the original audio... (first run downloads the model; can take several minutes)")
        self.mixer_panel.set_demucs("Demucs: running...", True)
        self._run(AU.run_demucs, self.video, self.device.currentText(), done=self._demucs_done,
                  on_fail=lambda m: self._st_refresh_mixer_state())

    def _demucs_done(self, res):
        self.log.appendPlainText("✓ Demucs finished: Original Vocals and Background are now in the Audio mixer.")
        self._st_refresh_mixer_state()
        self.tl.refresh()                     # expose the new Demucs Background lane
        self._pb_content_changed()
        self.bottom_tabs.setCurrentIndex(1)

    def _st_refresh_mixer_state(self):
        if not hasattr(self, "mixer_panel"):
            return
        have_stems = bool(self.video) and AU.have_stems(self.video)
        voiced = any(D.has_audio(c) for c in self.cues)
        has_video = bool(self.video)
        self.mixer_panel.set_available({"orig_vocals": has_video, "orig_bg": have_stems, "khmer_voice": voiced,
                                        "ai_voice": any(k.track == "ai_voice" for k in self.clips),
                                        "sfx": any(k.track == "sfx" for k in self.clips), "bgm": any(k.track == "bgm" for k in self.clips)})
        self.mixer_panel.set_demucs("Demucs: ready (vocals + background separated)" if have_stems else
                                    "Demucs: not run - press the button to split the original voice from the music", False)

    def _st_mixer_changed(self):
        self._sync_guard = True
        try:
            self.voice_vol.setValue(self.mixer.channels["khmer_voice"].volume)
            self.bg_vol.setValue(self.mixer.channels["orig_bg"].volume)
        finally:
            self._sync_guard = False
        self._pb_mixer_changed()              # STEP 2: volume / mute / solo are read live by the engine; enable/disable re-renders
        self._st_schedule_save()

    def _st_listen(self, channel):
        """'Play mix' / per-track ▶: the SAME playback as the main Play button (video + all tracks); channel = only that track audible.
        (STEP 2: the old separate 30 s preview player, which paused the video, was removed.)"""
        self._collect_focus()
        self._pb_listen(channel)

    def _st_stop_listen(self):
        """Called by 'Stop all' (processing): only ends a 'listen to one track' playback, never touches normal playback."""
        if self.mix_engine.listen_only:
            self.mix_engine.listen_only = None
            if self.vplayer and self._playing():
                self.vplayer.pause()

    # ------------------------------------------------------------------ Clear Cache
    def clear_cache_clicked(self):
        """Header button 'Clear Cache': deletes the work cache, the saved project history and the download history."""
        from ..shared import cache as C
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Clear Cache", "A step is still running. Press Stop first, then clear the cache.")
            return
        rep = C.report(self.ctx.db)
        lines = [f"•  {label}:  {C.human(b)}" for label, b in rep["items"]]
        lines.append(f"•  Download history list:  {rep['history_rows']} item(s)")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning); box.setWindowTitle("Clear Cache")
        box.setText(f"Delete the cache and all saved history?  ({C.human(rep['bytes'])})")
        box.setInformativeText("\n".join(lines) + "\n\nThe open video is kept, but Demucs stems, Khmer voice files, the saved script "
                               "and mixer / timeline settings are removed and must be made again.\n\n"
                               "NOT deleted: settings, API keys, cloned voices, AI models, downloaded and exported files.")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel); box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return
        try:                                     # release every file that is still open (mapped channel wavs, preview proxy)
            self._pb_shutdown()
        except Exception:  # noqa
            pass
        try:
            if self.vplayer:
                self.vplayer.stop(); self.vplayer.setSource(QUrl())
        except Exception:  # noqa
            pass
        if self._save_timer is not None:
            self._save_timer.stop()              # a pending autosave must not write the old project back
        self.tog["Process"].setChecked(True)
        self.log.appendPlainText("Clearing cache and history...")
        self._run(C.clear, self.ctx.db, done=self._clear_cache_done, alert=True,
                  on_fail=lambda m: self._clear_cache_reset())

    def _clear_cache_reset(self):
        """Forget the in-memory project too, otherwise it would be saved again (autosave / close) and the history would come back."""
        self.cues = []
        self.clips = []
        self.mixer = M.Mixer()
        self._pb_set_mixer(self.mixer)
        try:
            self.tl.peaks.cache.clear()
        except Exception:  # noqa
            pass
        if self.video:
            self.load_video(self.video)          # re-opens the video as a blank project (and re-creates the preview)
        else:
            self.refresh()
            self._st_refresh_mixer_state()

    def _clear_cache_done(self, res):
        from ..shared import cache as C
        self._clear_cache_reset()
        msg = f"Cache cleared: {C.human(res['bytes'])} freed ({res['files']} file(s)), {res['history_rows']} history item(s) removed."
        self.log.appendPlainText("✓ " + msg)
        if res["skipped"]:
            self.log.appendPlainText(f"{len(res['skipped'])} file(s) could not be deleted (in use by another program):")
            for x in res["skipped"][:10]:
                self.log.appendPlainText("   " + x)
            msg += f"\n\n{len(res['skipped'])} file(s) are in use by another program and were kept. Close it and press Clear Cache again."
        QMessageBox.information(self, "Clear Cache", msg)

    # ------------------------------------------------------------------ project persistence
    def _st_paths(self):
        d = work_dir(Path(self.video).stem)
        return M.project_file(d), d / "dialogue.json"

    def _st_schedule_save(self):
        if not self.video:
            return
        if self._save_timer is None:
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(900)
            self._save_timer.timeout.connect(self._st_save_now)
        self._save_timer.start()

    def _st_save_now(self):
        if not self.video:
            return
        try:
            proj, dlg = self._st_paths()
            M.save_project(proj, self.scene, self.mixer, {"clips": [k.to_dict() for k in self.clips], "video": self.video})
            if self.cues:
                D.save_dialogue(dlg, self.cues, self._video_fp)
        except Exception as e:  # noqa - saving must never break editing
            self.log.appendPlainText(f"(project not saved: {e})")

    def studio_save(self):
        self._st_save_now()

    def _studio_on_video(self, path, info):
        """Called by load_video: load the saved studio project of this video (or auto-fit a new one)."""
        from ..translator.project_state import fingerprint
        try:
            self._video_fp = fingerprint(path)
        except Exception:  # noqa
            self._video_fp = ""
        w, h, dur = info.get("width", 0), info.get("height", 0), info.get("duration", 0.0)
        proj, dlg = self._st_paths()
        existed = proj.exists()
        sc, mx, extra = M.load_project(proj)
        if existed:
            self.scene, self.mixer = sc, mx
            self._pb_set_mixer(self.mixer)
            self.clips = [AU.AudioClip.from_dict(x) for x in extra.get("clips", [])]
        else:
            keep_frame = self.scene.frame
            self.scene = M.Scene(); self.scene.frame = keep_frame      # the chosen frame (9:16 ...) carries over to the next video
            self.clips = []
        if self.scene.frame.auto_fit and not existed:
            self.scene.frame.auto_choose(w, h)
        self.hist = TR.StudioHistory()
        self.mix_engine.set_sources({}, 0.0, "")      # STEP 2: audio of the previous video must not stay loaded
        self.canvas.set_source(w, h, dur)
        self._st_set_scene(self.scene)
        self.mixer_panel.set_mixer(self.mixer)
        self._sync_guard = True
        try:
            self.voice_vol.setValue(self.mixer.channels["khmer_voice"].volume); self.bg_vol.setValue(self.mixer.channels["orig_bg"].volume)
        finally:
            self._sync_guard = False
        if not self.cues:
            old = D.load_dialogue(dlg, self._video_fp)
            if old:
                self.cues = old
                self.log.appendPlainText(f"Restored {len(old)} dialogue line(s) saved with this video.")
                self.refresh()
        self.tl.set_video(path)
        self.tl.set_duration(dur)
        self.tl.fit_all()
        self._st_hist_buttons()
        self._st_refresh_mixer_state()
