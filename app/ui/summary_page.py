"""Khmer Movie Summary workspace (Qt view). ALL behaviour lives in app/summary/controller.py (tested without Qt);
this page only reads widgets, calls the controller, and shows the result.

Movie Summary: import -> scene detection -> optional Gemini visual descriptions -> story beats -> Khmer script -> narration -> scene matching -> sync -> export. The Translator Studio timeline remains a separate shared editor; this page adds a focused scene/beat review workflow.
"""
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit, QLineEdit, QSpinBox,
                               QDoubleSpinBox, QComboBox, QCheckBox, QTableWidget, QTableWidgetItem, QListWidget, QProgressBar,
                               QFileDialog, QMessageBox, QScrollArea, QSplitter, QHeaderView, QInputDialog, QSlider)

from ..summary import voices as V, sync as SY, web_import as WI
from ..summary.controller import SummaryController
from ..summary.voice_profiles import VoiceProfileStore
from ..translator.khmer_tts import VOICES
from .translator_widgets import SectionBox
from .workers import Worker

COLS = ["#", "Story beat", "Khmer narration (editable)", "Style", "Scene (edit = lock)", "Match", "Audio", "Status"]
ICON = {"info": "ℹ", "warning": "⚠", "error": "✖"}


class SummaryPage(QWidget):
    def __init__(self, ctx=None, controller=None):
        super().__init__()
        self.ctx = ctx
        self.ctl = controller or SummaryController()
        self.worker = None
        self._filling = False
        self.profile_store = VoiceProfileStore()
        self._web_job = self._web_report = None                   # job/លទ្ធផលពិនិត្យ Link បច្ចុប្បន្ន
        self._build()
        dm = getattr(ctx, "downloads", None)                       # Download Manager ដែលមានស្រាប់
        if dm is not None:
            dm.job_updated.connect(self._web_job_updated)         # ទទួលវឌ្ឍនភាព download

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        lv.setSpacing(8)
        lv.addWidget(SectionBox("1  Movie", self._movie_box(), "Import and scene detection", "▣", open=True))
        lv.addWidget(SectionBox("2  Story beats", self._beats_box(), "What happens, in story order", "☰", open=True))
        lv.addWidget(SectionBox("3  Khmer script", self._script_box(), "AI recap narration from the beats", "✎"))
        lv.addWidget(SectionBox("4  Narration voice", self._voice_box(), "edge-tts or your cloned voice", "♪"))
        lv.addWidget(SectionBox("5  Plan and export", self._export_box(), "Scene matching, sync, final video", "▶"))
        lv.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(left)
        sc.setMinimumWidth(380)
        split.addWidget(sc)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        rv.setSpacing(8)
        self._build_preview(rv)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._cell_changed)
        rv.addWidget(self.table, 3)
        self.scenes = QTableWidget(0, 3)
        self.scenes.setHorizontalHeaderLabels(["Scene", "Time", "What is visible (editable, helps matching)"])
        self.scenes.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.scenes.itemChanged.connect(self._caption_changed)
        rv.addWidget(self.scenes, 2)
        self.timeline = QTableWidget(0, 6)
        self.timeline.setHorizontalHeaderLabels(["#", "Scene", "Source in–out", "Output in–out", "Speed", "Freeze"])
        self.timeline.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        rv.addWidget(QLabel("Summary EDL timeline (review view)"))
        rv.addWidget(self.timeline, 1)
        self.issues = QListWidget()
        rv.addWidget(self.issues, 1)
        row = QHBoxLayout()
        self.status = QLabel("Open a movie to begin.")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.cancel_btn = QPushButton("Stop")
        self.cancel_btn.clicked.connect(self.stop_all)
        row.addWidget(self.status, 1)
        row.addWidget(self.bar, 1)
        row.addWidget(self.cancel_btn)
        rv.addLayout(row)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)

    def _build_preview(self, layout):
        """Embedded source-movie preview; Qt Multimedia is optional and failure is non-fatal."""
        from PySide6.QtWidgets import QFrame
        frame = QFrame()
        pv = QVBoxLayout(frame)
        pv.setContentsMargins(4, 4, 4, 4)
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtMultimediaWidgets import QVideoWidget
            self.preview_video = QVideoWidget(frame)
            self.preview_video.setMinimumHeight(180)
            self.preview_player = QMediaPlayer(self)
            self.preview_audio = QAudioOutput(self)
            self.preview_player.setAudioOutput(self.preview_audio)
            self.preview_player.setVideoOutput(self.preview_video)
            pv.addWidget(self.preview_video, 1)
            controls = QHBoxLayout()
            self.preview_play = QPushButton("Play / Pause")
            self.preview_play.clicked.connect(self._toggle_preview)
            self.preview_seek = QSlider(Qt.Horizontal)
            self.preview_seek.setRange(0, 0)
            self.preview_seek.sliderMoved.connect(self.preview_player.setPosition)
            self.preview_player.positionChanged.connect(self.preview_seek.setValue)
            self.preview_player.durationChanged.connect(lambda d: self.preview_seek.setRange(0, d))
            controls.addWidget(self.preview_play)
            controls.addWidget(self.preview_seek, 1)
            pv.addLayout(controls)
        except Exception as e:
            self.preview_player = None
            pv.addWidget(QLabel("Video preview unavailable: Qt Multimedia could not initialize."))
        layout.addWidget(frame, 1)

    def _toggle_preview(self):
        if not self.preview_player:
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if self.preview_player.playbackState() == QMediaPlayer.PlayingState:
            self.preview_player.pause()
        else:
            self.preview_player.play()

    def _set_preview_source(self, path):
        if self.preview_player:
            self.preview_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
            self.preview_player.pause()
            self.preview_player.setPosition(0)

    def _box(self):
        w = QWidget()
        w.setObjectName("plain")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.setSpacing(8)
        return w, lay

    def _movie_box(self):
        w, lay = self._box()
        b = QPushButton("Import Video")                            # ជម្រើស A: ឯកសារពីកុំព្យូទ័រ
        b.clicked.connect(self.open_movie)
        lay.addWidget(b)
        lay.addWidget(QLabel("or paste a web link:"))              # ជម្រើស B: Link
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Movie / episode link (authorised content only)")
        lay.addWidget(self.url_edit)
        wr = QHBoxLayout()
        self.check_btn = QPushButton("Check link")
        self.check_btn.clicked.connect(self.check_link)
        self.web_btn = QPushButton("Import from Web Link")
        self.web_btn.setEnabled(False)                             # បើកតែបន្ទាប់ពី Link ពិនិត្យរួច
        self.web_btn.clicked.connect(self.import_web)
        self.web_cancel = QPushButton("Cancel")
        self.web_cancel.setEnabled(False)
        self.web_cancel.clicked.connect(self.cancel_web)
        for x in (self.check_btn, self.web_btn, self.web_cancel):
            wr.addWidget(x)
        lay.addLayout(wr)
        self.episode_box = QComboBox()                             # ជ្រើស 1 ភាគ (បើ Link ជាបញ្ជីភាគ)
        self.episode_box.setVisible(False)
        lay.addWidget(self.episode_box)
        self.link_note = QLabel("")
        self.link_note.setWordWrap(True)
        lay.addWidget(self.link_note)
        self.info = QLabel("No movie")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        lay.addWidget(QLabel("Use only footage you are authorised to use."))
        r = QHBoxLayout()
        self.thr = QSpinBox(); self.thr.setRange(5, 95); self.thr.setValue(30); self.thr.setSuffix(" % cut sensitivity")
        self.minlen = QDoubleSpinBox(); self.minlen.setRange(0.2, 30); self.minlen.setValue(1.0); self.minlen.setSuffix(" s min")
        self.maxlen = QDoubleSpinBox(); self.maxlen.setRange(2, 300); self.maxlen.setValue(30.0); self.maxlen.setSuffix(" s max")
        for x in (self.thr, self.minlen, self.maxlen):
            r.addWidget(x)
        lay.addLayout(r)
        d = QPushButton("Detect scenes")
        d.clicked.connect(self.detect_scenes)
        lay.addWidget(d)
        self.vision_btn = QPushButton("AI describe scenes (Gemini Vision)")
        self.vision_btn.setToolTip("Sends one small representative frame per scene to Gemini; the full movie and audio are not uploaded.")
        self.vision_btn.clicked.connect(self.describe_scenes)
        lay.addWidget(self.vision_btn)
        return w

    def _beats_box(self):
        w, lay = self._box()
        lay.addWidget(QLabel("One beat per line. Optional time in the movie first, e.g.  00:01:20-00:01:45 The hero finds the letter"))
        self.beats_edit = QPlainTextEdit()
        self.beats_edit.setMinimumHeight(120)
        lay.addWidget(self.beats_edit)
        b = QPushButton("Use these beats")
        b.clicked.connect(self.load_beats)
        lay.addWidget(b)
        auto = QPushButton("Draft story beats from AI scene descriptions")
        auto.setToolTip("Requires AI-described scenes. Review and edit the generated beats; it cannot infer unseen dialogue or off-screen events.")
        auto.clicked.connect(self.auto_story_beats)
        lay.addWidget(auto)
        return w

    def _script_box(self):
        w, lay = self._box()
        r = QHBoxLayout()
        self.target = QSpinBox(); self.target.setRange(10, 7200); self.target.setValue(180); self.target.setSuffix(" s total")
        self.style = QComboBox(); self.style.addItems(list(V.STYLES))
        r.addWidget(self.target); r.addWidget(self.style)
        lay.addLayout(r)
        self.chars = QLineEdit(); self.chars.setPlaceholderText("Characters / names (optional)")
        lay.addWidget(self.chars)
        g = QPushButton("Generate Khmer script (AI)")
        g.clicked.connect(self.generate_script)
        lay.addWidget(g)
        self.instr = QLineEdit(); self.instr.setPlaceholderText("Instruction for the selected paragraph, e.g. shorter / sadder")
        lay.addWidget(self.instr)
        rg = QPushButton("Regenerate selected paragraph")
        rg.clicked.connect(self.regenerate_selected)
        lay.addWidget(rg)
        lay.addWidget(QLabel("Always read the Khmer yourself before generating voice."))
        return w

    def _voice_box(self):
        w, lay = self._box()
        self.voice = QComboBox()
        self.refresh_voices()
        self.voice.currentIndexChanged.connect(self._voice_note)
        lay.addWidget(self.voice)
        self.speed = QDoubleSpinBox(); self.speed.setRange(0.5, 2.0); self.speed.setSingleStep(0.05); self.speed.setValue(1.0)
        lay.addWidget(self.speed)
        self.voice_note = QLabel("")
        self.voice_note.setWordWrap(True)
        lay.addWidget(self.voice_note)
        profile_row = QHBoxLayout()
        add_profile = QPushButton("Add WAV voice profile…")
        add_profile.clicked.connect(self.add_voice_profile)
        remove_profile = QPushButton("Remove selected profile")
        remove_profile.clicked.connect(self.remove_voice_profile)
        profile_row.addWidget(add_profile); profile_row.addWidget(remove_profile)
        lay.addLayout(profile_row)
        n = QPushButton("Generate narration (only changed lines)")
        n.clicked.connect(self.narrate)
        lay.addWidget(n)
        self._voice_note()
        return w

    def _export_box(self):
        w, lay = self._box()
        self.mono = QCheckBox("Keep scenes in story order"); self.mono.setChecked(True)
        lay.addWidget(self.mono)
        p = QPushButton("Match scenes and plan")
        p.clicked.connect(self.plan)
        lay.addWidget(p)
        self.subs = QCheckBox("Burn Khmer subtitles"); self.subs.setChecked(True)
        lay.addWidget(self.subs)
        self.font = QLineEdit("Noto Sans Khmer")
        lay.addWidget(self.font)
        self.keep_original_audio = QCheckBox("Mix original movie audio quietly under narration")
        self.keep_original_audio.setChecked(True)
        lay.addWidget(self.keep_original_audio)
        music_row = QHBoxLayout()
        self.music_path = QLineEdit(); self.music_path.setPlaceholderText("Optional background music file")
        music_browse = QPushButton("Browse…")
        music_browse.clicked.connect(self._choose_music)
        music_row.addWidget(self.music_path, 1); music_row.addWidget(music_browse)
        lay.addLayout(music_row)
        lay.addWidget(QLabel("The subtitle font must be installed on this PC (not bundled)."))
        e = QPushButton("Export video…")
        e.clicked.connect(self.export)
        lay.addWidget(e)
        return w

    # ------------------------------------------------------------------ small getters (tests replace these)
    def _beats_text(self):
        return self.beats_edit.toPlainText()

    def _provider(self):
        from ..ai.providers import task_provider
        return task_provider("story")

    def _voice_choice(self):
        return self.voice.currentData()          # ("edge", voice_id) | ("clone", name)

    def refresh_voices(self):
        self.voice.clear()
        for label, vid in VOICES.items():
            self.voice.addItem(label, ("edge", vid))
        try:
            from ..translator import voice_clone as VC
            for v in VC.list_voices():
                self.voice.addItem(v["name"], ("clone", v["name"]))
        except Exception:                      # noqa - cloning is optional
            pass
        for profile in self.profile_store.list():
            self.voice.addItem("Profile: " + profile.get("name", "Voice"), ("profile", profile.get("id")))

    def add_voice_profile(self):
        name, ok = QInputDialog.getText(self, "New voice profile", "Profile name:")
        if not ok or not str(name).strip():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose your voice reference WAV", "", "WAV audio (*.wav)")
        if not path:
            return
        try:
            self.profile_store.add(name, path)
            self.refresh_voices()
            self.status.setText("Voice profile added. The WAV stays on this PC.")
        except Exception as e:
            self._warn(e)

    def remove_voice_profile(self):
        ch = self._voice_choice()
        if not ch or ch[0] != "profile":
            QMessageBox.information(self, "Voice profiles", "Select a saved Profile voice first.")
            return
        self.profile_store.remove(ch[1])
        self.refresh_voices()

    def _voice_note(self, *_):
        ch = self._voice_choice()
        self.voice_note.setText(("Saved WAV reference profile. Clone engine availability and voice similarity depend on your configured model." if ch and ch[0] == "profile" else V.limitation(ch[0], self.style.currentText()) if ch else ""))

    # ------------------------------------------------------------------ running work off the GUI thread
    def _run(self, fn, *a, done=None, **kw):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "Another step is still running.")
            return
        self.bar.setValue(0)
        w = Worker(fn, *a, **kw)
        w.progress.connect(lambda v: self.bar.setValue(v) if isinstance(v, int) else None)
        w.done.connect(lambda r: (done(r) if done else None))
        w.failed.connect(self._failed)
        self.worker = w
        w.start()

    def _failed(self, msg):
        self.status.setText("Stopped." if msg == "Cancelled" else "Failed: " + msg[:160])
        if msg != "Cancelled":
            QMessageBox.warning(self, "Movie Summary", msg)

    def stop_all(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.status.setText("Stopping…")

    def _warn(self, msg):
        QMessageBox.warning(self, "Movie Summary", str(msg))

    def _guard(self, fn):
        """Run a quick controller call on the GUI thread; show errors instead of crashing."""
        try:
            return fn()
        except (ValueError, RuntimeError) as e:
            QMessageBox.warning(self, "Movie Summary", str(e))
        return None

    # ------------------------------------------------------------------ actions
    def open_movie(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open movie", "", "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;All files (*)")
        if path:
            self._open(path)

    def _open(self, path, work=None):
        p = self._guard((lambda: self.ctl.open_video(path, work=work)) if work else (lambda: self.ctl.open_video(path)))
        if p:
            self._set_preview_source(path)
            self.info.setText(f"{Path(p.source).name}\n{p.width}x{p.height}  {p.fps:g} fps  {p.duration:.1f} s  |  "
                              f"{len(p.scenes)} scene(s), {len(p.segments)} segment(s)")
            self.beats_edit.setPlainText("\n".join(self._beat_line(b) for b in p.beats))
            self.status.setText("Ready.")
            self._refresh()

    @staticmethod
    def _beat_line(b):
        if b.t_hint:
            return f"{b.t_hint[0]:g}-{b.t_hint[1]:g} {b.text}"
        return b.text

    # ------------------------------------------------------------------ Import from Web Link
    def _web_busy(self, busy):
        for x in (self.check_btn, self.web_btn, self.url_edit, self.episode_box):
            x.setEnabled(not busy)                                    # បិទពេលកំពុងធ្វើការ
        self.web_cancel.setEnabled(busy)
        if not busy:
            self.web_btn.setEnabled(bool(self._web_report and self._web_report.usable))

    def check_link(self):
        url = self.url_edit.text()
        self._web_report = None
        self.web_btn.setEnabled(False)
        self.episode_box.setVisible(False)
        self.link_note.setText("Checking link…")
        ck = (self.ctx.settings.get("cookies_file") if self.ctx else "") or None   # cookies របស់អ្នកប្រើ (បើមាន)
        self._run(lambda progress=None, cancel=None: WI.check_link(url, ck), done=self._link_checked)

    def _link_checked(self, rep):
        self._web_report = rep
        self.link_note.setText(rep.message)
        self.episode_box.clear()
        for it in rep.items:
            self.episode_box.addItem(f"{it.episode or ''} {it.title}".strip(), it)     # ទុក Item ក្នុង combo
        self.episode_box.setVisible(rep.status == WI.EPISODES)
        self.web_btn.setEnabled(rep.usable and self.ctx is not None)
        if rep.usable and self.ctx is None:
            self.link_note.setText(rep.message + " (Download Manager unavailable.)")

    def import_web(self):
        item = self.episode_box.currentData() if self._web_report and self._web_report.usable else None
        if item is None or self.ctx is None:
            return
        job = WI.make_job(item, self.ctl.root)                        # Job ដែលមានស្រាប់ -> source/
        self._web_job = job
        self._web_busy(True)
        self.status.setText("Downloading…")
        self.ctx.downloads.submit(job)

    def cancel_web(self):
        if self._web_job is not None and self.ctx is not None:
            self.ctx.downloads.cancel(self._web_job)

    def _web_job_updated(self, job):
        if job is not self._web_job:                                  # មិនមែន job របស់ទំព័រនេះ
            return
        if job.status in ("Queued", "Downloading", "Paused"):
            self.bar.setValue(int(job.progress))
            self.status.setText(f"{job.status} {int(job.progress)} %")
            return
        self._web_job = None
        self._web_busy(False)
        try:
            path = WI.finished_path(job)                              # Done / Duplicate
            WI.validate_video(path)                                   # ត្រូវតែមាន video stream ពិត
        except (ValueError, RuntimeError) as e:
            self.status.setText("Import stopped.")
            self.link_note.setText(str(e))
            if job.status != "Cancelled":
                QMessageBox.warning(self, "Movie Summary", str(e))
            return
        self.bar.setValue(100)
        self._open(path, work=WI.work_dir_for(self.ctl.root, job.item))   # បញ្ជូនទៅ pipeline ដូចគ្នានឹង Import Video

    def detect_scenes(self):
        if not self.ctl.project:
            return self._need_movie()
        self.status.setText("Detecting scenes…")
        self._run(self.ctl.analyse, threshold=self.thr.value() / 100.0, min_len=self.minlen.value(), max_len=self.maxlen.value(),
                  done=lambda r: (self.status.setText(f"{len(r)} scene(s) found."), self._refresh()))

    def auto_story_beats(self):
        if not self.ctl.project:
            return self._need_movie()
        try:
            prov = self._provider()
        except Exception as e:
            self._warn(e)
            return
        self.status.setText("Drafting story beats from scene evidence…")
        self._run(self.ctl.extract_story_beats, prov, done=lambda beats: (self.beats_edit.setPlainText("\n".join(self._beat_line(b) for b in beats)), self.status.setText(f"Drafted {len(beats)} beat(s). Review every beat before script generation."), self._refresh()))

    def load_beats(self):
        if not self.ctl.project:
            return self._need_movie()
        b = self._guard(lambda: self.ctl.set_beats_text(self._beats_text()))
        if b is not None:
            self.status.setText(f"{len(b)} beat(s).")
            self._refresh()

    def generate_script(self):
        if not self.ctl.project:
            return self._need_movie()
        try:
            prov = self._provider()
        except Exception as e:  # noqa - missing key / unknown provider
            self._warn(e)
            return
        self.status.setText("Writing the Khmer script…")

        def job(progress=None, cancel=None):
            return self.ctl.make_script(prov, self.target.value(), self.style.currentText(), self.chars.text())
        self._run(job, done=lambda r: (self.status.setText(f"{len(r)} paragraph(s) written. Read and edit them."), self._refresh()))

    def regenerate_selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            QMessageBox.information(self, "Movie Summary", "Select exactly one paragraph.")
            return
        sid = int(self.table.item(next(iter(rows)), 0).text())
        try:
            prov = self._provider()
        except Exception as e:  # noqa
            self._warn(e)
            return

        def job(progress=None, cancel=None):
            return self.ctl.regenerate_paragraph(prov, sid, self.instr.text())
        self._run(job, done=lambda r: (self.status.setText("Paragraph rewritten."), self._refresh()))

    def narrate(self):
        ch = self._voice_choice()
        if not self.ctl.project or not ch:
            return self._need_movie()
        kind, name = ch
        try:
            if kind == "edge":
                synth, key = V.edge_synth(name), f"edge:{name}"
            elif kind == "profile":
                profile = self.profile_store.get(name)
                if not profile or not Path(profile["reference_wav"]).is_file():
                    raise ValueError("Saved voice reference WAV is missing. Add the profile again.")
                synth, key = V.clone_synth(profile["reference_wav"]), f"profile:{profile['id']}"
            else:
                from ..translator import voice_clone as VC
                synth, key = V.clone_synth(VC.reference_path(name)), f"clone:{name}"
        except Exception as e:  # noqa
            self._warn(e)
            return
        self.status.setText("Generating narration…")
        self._run(self.ctl.narrate, synth, key, self.speed.value(),
                  done=lambda r: (self.status.setText(f"{len(r)} line(s) generated."), self._refresh()))

    def describe_scenes(self):
        if not self.ctl.project:
            return self._need_movie()
        self.status.setText("Describing scene frames with Gemini…")
        self._run(self.ctl.describe_scenes, done=lambda r: (self.status.setText(f"AI described {len(r.get('updated', []))} scene(s); {len(r.get('errors', []))} issue(s)."), self._refresh(), self._show_vision_errors(r)))

    def _show_vision_errors(self, result):
        self.issues.clear()
        for msg in result.get("errors", []):
            self.issues.addItem("⚠  " + msg)
        if result.get("errors"):
            self.issues.addItem("Review AI descriptions before relying on scene matches.")

    def plan(self):
        issues = self._guard(lambda: self.ctl.plan(monotonic=self.mono.isChecked()))
        if issues is not None:
            self._show_issues(issues)
            n = sum(1 for i in issues if i["level"] != "info")
            self.status.setText(f"Plan ready, {n} item(s) to check." if self.ctl.project.clips else "No plan could be made.")
            self._refresh()

    def _choose_music(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose background music", "", "Audio (*.wav *.mp3 *.m4a *.aac *.flac *.ogg);;All files (*)")
        if path:
            self.music_path.setText(path)

    def export(self):
        if not self.ctl.project:
            return self._need_movie()
        out, _ = QFileDialog.getSaveFileName(self, "Export video", "movie_summary.mp4", "MP4 video (*.mp4)")
        if not out:
            return
        self.status.setText("Rendering…")
        self._run(self.ctl.export, out, self.subs.isChecked(), self.font.text().strip() or "Noto Sans Khmer",
                  mix_original_audio=self.keep_original_audio.isChecked(), music=self.music_path.text().strip() or None,
                  done=lambda r: self.status.setText("Exported: " + str(r)))

    def _need_movie(self):
        QMessageBox.information(self, "Movie Summary", "Open a movie first.")

    # ------------------------------------------------------------------ table <-> controller
    def _show_issues(self, issues):
        self.issues.clear()
        for i in issues:
            self.issues.addItem(f"{ICON.get(i['level'], '•')}  {i['detail']}")

    def _refresh(self):
        p = self.ctl.project
        if not p:
            return
        self._filling = True
        try:
            self.table.setRowCount(len(p.segments))
            for r, g in enumerate(p.segments):
                b = p.beat(g.beat_id)
                cells = [str(g.id), b.text if b else "", g.km, "", ("" if g.scene_id is None else str(g.scene_id)) + (" 🔒" if g.locked else ""),
                         f"{g.match_score:.2f}", f"{g.dur:.1f}s" if g.dur else "—", "ok" if g.audio else "needs voice"]
                for c, txt in enumerate(cells):
                    it = QTableWidgetItem(txt)
                    if c not in (2, 4):
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    self.table.setItem(r, c, it)
                cb = QComboBox()
                cb.addItems(list(V.STYLES))
                cb.setCurrentText(next((n for n in V.STYLES if n.lower() == (g.emotion or "").lower()), "Neutral"))
                cb.currentTextChanged.connect(lambda t, sid=g.id: self._style_changed(sid, t))
                self.table.setCellWidget(r, 3, cb)
            self.scenes.setRowCount(len(p.scenes))
            for r, s in enumerate(p.scenes):
                for c, txt in enumerate([str(s.id), f"{s.start:.1f}–{s.end:.1f}", s.caption or s.visual_summary]):
                    it = QTableWidgetItem(txt)
                    if c != 2:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    self.scenes.setItem(r, c, it)
            self.timeline.setRowCount(len(p.clips))
            out_t = 0.0
            for r, clip in enumerate(p.clips):
                seg = p.segment(clip.segment_id)
                scene_id = seg.scene_id if seg else "—"
                end_t = out_t + clip.out_len
                vals = [str(clip.segment_id), str(scene_id), f"{clip.src_start:.2f}–{clip.src_end:.2f}", f"{out_t:.2f}–{end_t:.2f}", f"x{clip.speed:.2f}", f"{clip.freeze:.2f}s"]
                for c, value in enumerate(vals):
                    it = QTableWidgetItem(value)
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    self.timeline.setItem(r, c, it)
                out_t = end_t
        finally:
            self._filling = False

    def _cell_changed(self, item):
        if self._filling:
            return
        row, col = item.row(), item.column()
        sid = int(self.table.item(row, 0).text())
        if col == 2:
            self._guard(lambda: self.ctl.edit_segment(sid, km=item.text()))
        elif col == 4:
            m = re.search(r"\d+", item.text())
            if m:
                self._guard(lambda: self.ctl.edit_segment(sid, scene_id=int(m[0])))
            QTimer.singleShot(0, self._refresh)          # never rebuild a table from inside its own itemChanged signal

    def _style_changed(self, sid, text):
        if not self._filling:
            self._guard(lambda: self.ctl.edit_segment(sid, emotion=text))
            self._voice_note()

    def _caption_changed(self, item):
        if self._filling or item.column() != 2:
            return
        sid = int(self.scenes.item(item.row(), 0).text())
        self._guard(lambda: self.ctl.set_scene_caption(sid, item.text()))

    # ------------------------------------------------------------------ window close
    def shutdown(self):
        try:
            self.stop_all()
            if self.preview_player:
                self.preview_player.stop()
            if self.ctl.project:
                self.ctl.save()
        except Exception:  # noqa
            pass
