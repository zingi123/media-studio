import functools
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QTabWidget, QSpinBox, QColorDialog, QDialog, QScrollArea, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox, QDoubleSpinBox,
                               QTableWidget, QTableWidgetItem, QFileDialog, QMessageBox, QProgressBar, QPlainTextEdit,
                               QSlider, QInputDialog, QHeaderView, QGroupBox, QGridLayout, QListWidget, QListWidgetItem, QCheckBox, QStyledItemDelegate,
                               QSplitter, QFrame, QStackedWidget, QMenu)
from ..shared.ffmpeg_util import probe, Cancelled
from ..shared.paths import work_dir
from ..shared.utils import fmt_time, parse_time, open_path, unique_path
from ..translator import subtitles as S
from ..translator.exporter import export_audio, export_video
from ..translator.khmer_tts import synthesize, VOICES, GENDER
from ..translator.speech_recognition import MODELS
from ..translator.smart_script import scan_smart_script   # NEW: speaker-turn script with pauses + emotion tags
from ..translator import script_builder as SB
from ..translator import prosody_match as PM   # NEW: measured performance matching (pitch / loudness / pace per line)
from ..translator import story_info as ST   # NEW: title / summary / character names header
from ..translator.scan import scan_script, METHODS, LANGS   # voice / on-screen text -> script (Auto fallback)
from ..translator.translator_engine import translate_cues, ENGINES
from ..translator.voice_synchronizer import build_voice_track, timing_report
from ..translator import emotion_raw as ER   # NEW: structured Emotion Raw field (separate from the Khmer text)
from ..translator import tts_providers as TP   # NEW: provider adapter - what each TTS engine can really do with an emotion
from ..translator import speech_rate as SR   # NEW: match the original speaking speed of each character
from ..translator.voice_presets import BASE, load_saved, save_saved, display_name, process as preset_process
from ..translator import voice_clone as VC   # CLONE VOICE (VoxCPM2 zero-shot) - permanent cloned-voice storage
from .voice_clone_dialog import AddVoiceTypeCloneDialog, confirm_delete_clone
from .ai_providers_dialog import AIProvidersDialog
from .system_status_dialog import SystemStatusDialog
from .ocr_review_dialog import OcrReviewDialog
from .speaker_manager_dialog import SpeakerManagerDialog
from ..translator.speakers import SpeakerBook, SpeakerError
from ..translator import video_overlay as VO
from ..translator.bg_remove import remove_background
from .workers import Worker
from .translator_widgets import card, Collapsible, CueTimeline, SectionBox, TransportBar
from .accordion import AccordionGroup
from .studio_integration import StudioMixin
from .auto_dub import AutoDubMixin
from .batch_queue import BatchMixin
from .hook_finder import HookMixin
from ..studio import audio as AU
from ..studio import dialogue as DLG
from ..studio.model import Mixer as _Mixer
from .theme import T

HEAD = ["No.", "Start", "End", "Original Text", "Khmer Translation", "Voice Type", "Audio",
        "Speaker", "Emotion", "Pace", "Action", "Emotion Raw"]   # NEW 7-10: notes from Smart Script (Pace = original speaking speed, read-only)  # Audio=✓ បង្កើតរួច
RETRY_ROUNDS = 10  # ចំនួនជុំអតិបរមា ពេលបង្កើតសំឡេងឡើងវិញ
EXPORTS = ["Audio only (MP3)", "Audio only (WAV)", "Video + Khmer subtitles", "Video + Khmer voiceover + subtitles",
           "Video + Khmer voiceover (NO subtitles)"]


VOICES2 = ["", "Male", "Female"]            # only 2 voices: Male = P (Piseth), Female = S (Sreymom)
VT_VOICE = {"Male": BASE["P"], "Female": BASE["S"]}
GENDER_VT = {"male": "Male", "female": "Female"}


def _clone_guard(fn):   # wraps _gen: marks cloned voices "in use" (blocks DELETE meanwhile) and checks the GPU once up front
    @functools.wraps(fn)
    def wrapper(self, idxs, *a, **kw):
        idxs = list(idxs)
        clones = {self.cues[i].voice_type for i in idxs if self.cues[i].translation.strip() and VC.is_clone_name(self.cues[i].voice_type)}
        if clones:
            g = VC.check_gpu()
            if not g["ok"]:
                return [g["message"]]   # shown once by _gen_done; no pointless retry rounds
        with VC.in_use(clones):
            return fn(self, idxs, *a, **kw)
    return wrapper


class VoiceDelegate(QStyledItemDelegate):   # Voice Type cell = dropdown (Male / Female + Selected Voice Types)
    def __init__(self, parent=None, extra=None):
        super().__init__(parent)
        self.extra = extra   # callable -> names of the Selected Voice Types

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent); cb.addItems(VOICES2 + (list(self.extra()) if self.extra else [])); return cb

    def setEditorData(self, ed, index):
        ed.setCurrentText(index.data() or "")

    def setModelData(self, ed, model, index):
        model.setData(index, ed.currentText())


class TranslatorPage(StudioMixin, AutoDubMixin, BatchMixin, HookMixin, QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._studio_init()          # scene / mixer / clips / history (studio layer)
        self.video = ""
        self.out_dir = ctx.settings.get("output_dir") or ""   # user-chosen output folder ("" = default next to video)
        self.cues = []
        self.story = None          # Smart Script story info {title, summary, characters}
        self.worker = None
        self._loading = False
        self.player = None         # voice-clip player (Play Selected Voice)
        self.vplayer = None        # embedded source-video preview (created in _build_preview, may stay None)
        self.vid_dur = 0.0
        self._queue = []           # rows being played in order (Play Selected Voice)
        self.fx = VO.Overlay()      # blur / Khmer subtitle / logo
        self.voices = self._load_voices()   # Selected Voice Types {name: preset} (saved in settings; cloned voices live in cloned_voices/)
        self._cur_voice = None

        self._create_controls()     # every real control (same widgets / names / settings keys as before)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)
        # Media import/output controls live once in the workflow panel.
        # This avoids the duplicate video-import box above the workspace.

        # ---- upper row: workflow panel | preview | inspector ----
        top = QSplitter(Qt.Horizontal); self.top_split = top
        top.setChildrenCollapsible(False)
        top.setCollapsible(0, True); top.setCollapsible(2, True)
        top.addWidget(self._build_left())
        top.addWidget(self._build_preview())
        top.addWidget(self.inspector)
        self.inspector.setMinimumWidth(250)
        top.setStretchFactor(0, 0); top.setStretchFactor(1, 1); top.setStretchFactor(2, 0)
        top.setSizes([340, 800, 280])

        # ---- lower row: cue editor (timeline + table) | export ----
        bottom = self._build_editor(); self.bottom_w = bottom

        main = QSplitter(Qt.Vertical); self.main_split = main
        main.setChildrenCollapsible(False)
        main.addWidget(top)
        main.addWidget(bottom)
        main.setCollapsible(1, True); main.setHandleWidth(8)     # drag the handle down: timeline collapses, video grows
        main.setStretchFactor(0, 4); main.setStretchFactor(1, 1)
        main.setSizes([560, 300])
        root.addWidget(main, 1)
        # Refit the canvas after either splitter changes its available viewport.
        top.splitterMoved.connect(lambda *_: QTimer.singleShot(0, self.canvas.fit_view))
        main.splitterMoved.connect(lambda *_: QTimer.singleShot(0, self.canvas.fit_view))

        # ---- progress strip + (hidden) log ----
        r = QHBoxLayout()
        r.addWidget(self.bar, 1)
        self.pause_btn = QPushButton("PAUSE")
        self.pause_btn.setToolTip("Pause at the next safe processing checkpoint")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.pause_processing)
        r.addWidget(self.pause_btn)
        self.resume_btn = QPushButton("RESUME")
        self.resume_btn.setEnabled(False)
        self.resume_btn.clicked.connect(self.resume_processing)
        r.addWidget(self.resume_btn)
        b = QPushButton("STOP ALL"); b.setObjectName("danger"); b.clicked.connect(self.stop_all)
        r.addWidget(b)
        root.addLayout(r)
        lw = QWidget(); lw.setObjectName("plain"); ll = QVBoxLayout(lw); ll.setContentsMargins(0, 0, 0, 0); ll.addWidget(self.log)
        lw.setVisible(False)
        root.addWidget(lw)
        self.sec = {"Process": lw}      # kept: other methods call self.tog["Process"].setChecked(True) to reveal progress/errors

        self._build_side()              # left rail (self.side) used by MainWindow
        self._studio_wire()             # Style tabs <-> on-screen objects, mixer <-> quick sliders
        self._show_output()
        self._restore_workspace_layout()
        self.refresh()

    # ---------- controls (the real widgets; unchanged names and settings keys) ----------

    # ---------- video ----------
    def pick_video(self):
        f, _ = QFileDialog.getOpenFileName(self, "Video", "", "Video (*.mp4 *.mkv *.mov *.avi *.webm *.flv *.m4v)")
        if f:
            self.load_video(f)

    def load_video(self, path):
        self.video = str(path)
        self.vpath.setText(self.video)
        if hasattr(self, "media_name"):
            self.media_name.setText(Path(self.video).name)
            self.media_name.setToolTip(self.video)
        self.vid_dur = 0.0
        self._pv_vcodec = ""
        self._show_output()
        i = {}
        try:
            i = probe(path)
            self._pv_vcodec = i.get("vcodec", "")
            self.vid_dur = float(i.get("duration") or 0.0)
            self.vmeta.setText(f"{i['width']}×{i['height']} · {self._tc(self.vid_dur)}")
            self.log.appendPlainText(f"Loaded {Path(path).name}: {i['width']}x{i['height']}, {i['duration']:.0f}s")
        except Exception as e:  # noqa
            self.vmeta.setText("")
            self.log.appendPlainText(f"Loaded, but could not read info: {e}")
        self.tl.set_duration(self.vid_dur)
        self._open_preview(path)
        try:
            self._studio_on_video(path, {"width": int(i.get("width") or 0), "height": int(i.get("height") or 0), "duration": self.vid_dur})
        except Exception as e:  # noqa - the studio layer must never block importing a video
            self.log.appendPlainText(f"Studio: could not load the project of this video ({e})")
        self._sync_views()

    def _show_output(self):
        d = self.out_dir or (str(Path(self.video).parent / "Dubbing_Output") if self.video else "")
        self.opath.setText(d)
        self.opath.setPlaceholderText("Default: 'Dubbing_Output' folder next to the video")
        self._update_summary()

    def pick_output(self):
        start = self.out_dir or (str(Path(self.video).parent) if self.video else "")
        d = QFileDialog.getExistingDirectory(self, "Output folder", start)
        if d:
            self.out_dir = d; self.ctx.settings.set("output_dir", d); self.ctx.settings.save(); self._show_output()

    def reset_output(self):
        self.out_dir = ""; self.ctx.settings.set("output_dir", ""); self.ctx.settings.save(); self._show_output()

    def open_output(self):
        if not (self.out_dir or self.video):
            self.log.appendPlainText("Import a video or choose an output folder first."); return
        open_path(str(self._outdir()))

    def _outdir(self):
        d = Path(self.out_dir) if self.out_dir else Path(self.video).parent / "Dubbing_Output"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def pause_processing(self):
        """Pause the active worker at its next cooperative checkpoint."""
        if self.worker and self.worker.isRunning():
            self.worker.pause()
            self.pause_btn.setEnabled(False)
            self.resume_btn.setEnabled(True)
            self.log.appendPlainText("Pause requested; processing will pause at the next safe checkpoint.")

    def resume_processing(self):
        """Resume a worker paused at a cooperative checkpoint."""
        if self.worker and self.worker.isRunning():
            self.worker.resume()
            self.pause_btn.setEnabled(True)
            self.resume_btn.setEnabled(False)
            self.log.appendPlainText("Processing resumed.")

    def _worker_idle(self):
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)

    def stop_all(self):  # បញ្ឈប់ដំណើរការទាំងអស់
        self._queue = []
        if getattr(self, "_batch", None) is not None:
            self.log.appendPlainText("■ Batch stopped.")
        self._batch = None
        self._st_stop_listen()
        if self.player:
            self.player.stop()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()  # ffmpeg/Demucs/TTS/Whisper ពិនិត្យ flag នេះ
            self.log.appendPlainText("Stopping...")
        else:
            self.log.appendPlainText("Nothing is running.")

    def _need_video(self):
        if not self.video:
            QMessageBox.information(self, "Video", "Import a video first (or send one from the Media Library).")
            return False
        return True

    # ---------- table <-> cues ----------
    def refresh(self):
        self._loading = True
        self.table.setRowCount(len(self.cues))
        try:                                   # original speaking speed of every line, relative to its own speaker
            spk_rate, line_rate = SR.measure_rates(self.cues)
        except Exception:  # noqa
            spk_rate, line_rate = {}, [0.0] * len(self.cues)
        for r, c in enumerate(self.cues):
            avg = spk_rate.get(c.speaker or "Speaker", {}).get("rate", 0.0)
            lr = line_rate[r] if r < len(line_rate) else 0.0
            pace = "" if not (lr and avg) else f"{lr:.1f}/s " + ("fast" if lr / avg >= SR.FAST else "slow" if lr / avg <= SR.SLOW else "normal")
            if getattr(c, "speed", 1.0) not in (0, 1.0):
                pace = (pace + " · " if pace else "") + f"voice x{c.speed:g}"          # per-row speed of the generated voice
            vals = [str(r + 1), fmt_time(c.start), fmt_time(c.end), c.text, c.translation, c.voice_type,
                    "✓" if c.audio and Path(c.audio).exists() else "",
                    c.speaker, c.emotion, pace, c.action, self._raw_summary(c)]
            for k, t in enumerate(vals):
                it = QTableWidgetItem(t)
                if k in (0, 6, 9, 11):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)  # not editable by hand
                if k == 3 and getattr(c, "text_conflict", False):          # speech and burned-in subtitle disagree
                    it.setBackground(QColor("#6b2d5c")); it.setForeground(QColor("#ffffff"))
                    it.setToolTip(f"Subtitle (OCR) says: {c.ocr_text}  [{c.ocr_sim:.0%} similar]. Use 'Review OCR vs speech differences...' to decide.")
                if k == 7 and getattr(c, "speaker_uncertain", False):      # speaker not sure -> amber cell + tooltip, review it
                    it.setBackground(QColor("#7a5c00")); it.setForeground(QColor("#ffffff"))
                    ov = f" Overlapping with: {c.overlap_with}." if c.overlap_with else ""
                    it.setToolTip(f"Uncertain speaker (confidence {c.speaker_conf:.0%}).{ov} Edit the cell, or right-click > Change speaker.")
                self.table.setItem(r, k, it)
        self._loading = False
        self._sync_views()
        self._update_inspector(self.table.currentRow())

    def _edited(self, it):
        if self._loading:
            return
        r, k = it.row(), it.column()
        c = self.cues[r]
        try:
            if k == 1:
                c.start = parse_time(it.text())
            elif k == 2:
                c.end = parse_time(it.text())
            elif k == 3:
                c.text = it.text()
                c.text_conflict, c.ocr_text, c.ocr_sim = False, "", 0.0     # a manual edit settles the OCR/speech difference
                it.setBackground(QColor(0, 0, 0, 0)); it.setToolTip("")
            elif k == 5:
                vt = it.text() if (it.text() in VOICES2 or it.text() in self.voices) else ""
                c.voice_type, c.gender, c.audio = vt, {"Male": "male", "Female": "female"}.get(vt, ""), ""
                self._loading = True
                self.table.item(r, 6).setText("")
                self._loading = False
            elif k == 7:
                c.speaker = it.text().strip()
                c.speaker_uncertain, c.speaker_conf, c.overlap_with = False, 1.0, ""      # a manual correction is final
                it.setBackground(QColor(0, 0, 0, 0)); it.setToolTip("")
            elif k == 8:                       # emotion note is used when the voice is generated -> regenerate
                c.emotion = it.text().strip(); c.audio = ""
                c.emotion_raw = ER.from_legacy(c.emotion).to_json()      # Emotion Raw follows the edited text (manual = never auto-overwritten)
                ER.set_on_cue(c, ER.of_cue(c), manual=True)
                self._loading = True
                self.table.item(r, 11).setText(self._raw_summary(c))
                self._loading = False
                self._loading = True
                self.table.item(r, 6).setText("")
                self._loading = False
            elif k == 10:
                c.action = it.text().strip()
            elif k == 4:
                c.translation = it.text()
                c.audio = ""           # text changed -> voice must be regenerated
                self._loading = True
                self.table.item(r, 6).setText("")
                self._loading = False
        except ValueError:
            QMessageBox.warning(self, "Time", "Use the format HH:MM:SS,mmm (e.g. 00:01:05,500).")
            self.refresh()
        self._sync_views()

    def add_line(self):
        r = self.table.currentRow()
        base = self.cues[r].end if 0 <= r < len(self.cues) else (self.cues[-1].end if self.cues else 0)
        self.cues.insert(r + 1 if r >= 0 else len(self.cues), S.Cue(base, base + 2, "", ""))
        self.refresh()

    def del_lines(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.cues[r]
        self.refresh()

    # ---------- subtitles / projects ----------
    def import_sub(self):
        f, _ = QFileDialog.getOpenFileName(self, "Subtitles", "", "Subtitles (*.srt *.vtt)")
        if f:
            try:
                self.cues = S.load_subtitle_file(f)
                self.refresh()
                self.log.appendPlainText(f"Imported {len(self.cues)} lines.")
            except Exception as e:  # noqa
                QMessageBox.warning(self, "Import", str(e))

    def export_sub(self):
        start = str(self._outdir() / "subtitles.srt") if self.video else "subtitles.srt"  # ចាប់ផ្តើមក្នុង Output folder
        f, sel = QFileDialog.getSaveFileName(self, "Export subtitles", start, "SRT (*.srt);;VTT (*.vtt);;Script TXT (*.txt)")
        if not f:
            return
        if not f.lower().endswith((".srt", ".vtt", ".txt")):
            f += ".vtt" if "VTT" in sel else ".txt" if "TXT" in sel else ".srt"
        which = "translation"
        if not any(c.translation for c in self.cues):
            which = "text"
        dg = "male"  # lines without a detected gender are Male (the default voice)
        S.save_subtitle_file(f, self.cues, which, dg)
        self.log.appendPlainText(f"Saved {f} ({'Khmer' if which == 'translation' else 'original text - no translation yet'})")

    # ---------- background steps ----------
    def _on_progress(self, p):
        if isinstance(p, str):
            self.log.appendPlainText(p)  # បង្ហាញ error ពេលកំពុងរត់
        else:
            self.bar.setValue(p if isinstance(p, int) else 0)

    def _failed(self, m, alert=False):
        self.tog["Process"].setChecked(True)          # never hide an error
        self.log.appendPlainText("✗ " + m)
        if alert and m != "Cancelled":
            QMessageBox.warning(self, "Scan", m)

    def _run(self, fn, *a, done=None, alert=False, on_fail=None, **kw):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "Another step is still running.")
            return
        self.bar.setValue(0)
        w = Worker(fn, *a, **kw)
        w.progress.connect(self._on_progress)  # លេខ → bar ; អក្សរ → log
        w.done.connect(lambda r: (self._worker_idle(), done(r) if done else None))
        w.failed.connect(lambda m: (self._worker_idle(), self._failed(m, alert), on_fail(m) if on_fail and m != "Cancelled" else None))
        self.worker = w
        self.pause_btn.setEnabled(True)
        self.resume_btn.setEnabled(False)
        w.start()

    def _lang_code(self):
        return self.lang.currentData() or "auto"

    def extract(self, save=False):  # save=True → រក្សាទុកជាឯកសារបន្ទាប់ពី extract
        self.tog["Process"].setChecked(True)  # show progress + errors while scanning
        if any(c.translation.strip() for c in self.cues):  # មានការបកប្រែរួច → រំលង scan
            self.log.appendPlainText("Translation already exists → not scanning again (delete the lines to re-scan).")
            if save:
                self.export_sub()
            return
        if not self._need_video():
            return
        self.log.appendPlainText(f"Scanning: {self.method.currentText()} | language: {self.lang.currentText()} | "
                                 f"accuracy: {self.model.currentText()} ...")

        def done(r):   # scan finished -> (save: open the save dialog). No voice-type detection here.
            cues, note = r
            self._set_cues(cues, note); self.worker.wait()
            if save:
                self.export_sub()
        self._run(scan_script, self.video, method=self.method.currentData(), lang=self._lang_code(),
                  model=self.model.currentText(), device=self.device.currentText(), done=done, alert=True)

    def smart_scan(self, then=None):  # then = callable run after the scan (Auto Dub): no Save dialog, no 'Replace?' question
        # A2. voice → script: NEW line only when the speaker/voice changes; pauses from real silence; emotion tags
        self.tog["Process"].setChecked(True)
        if not self._need_video():
            return
        if self.cues and not callable(then) and QMessageBox.question(self, "Smart Script", "Replace the current lines with the Smart Script?\n"
                                              "(Cancel keeps your current lines untouched.)") != QMessageBox.Yes:
            return
        st = self.ctx.settings
        self.log.appendPlainText(f"Smart Script: language {self.lang.currentText()} | accuracy {self.model.currentText()} ...")

        def done(r):
            cues, note = r
            for c in cues:   # Male/Female default voice from the detected voice type (same as the "|male/female" script note)
                if c.gender:
                    c.voice_type = GENDER_VT[c.gender]
            self.story = getattr(cues, "story", None)
            self._set_cues(cues, note); self.worker.wait()
            self._after_scan(cues)
            if self.story:
                self.log.appendPlainText(f"  Story: {self.story.get('title') or '-'} | {len(self.story['characters'])} character(s)")
            for k, c in enumerate(cues[:5]):   # quick look at what was found (full details are in the saved file)
                self.log.appendPlainText(f"  #{k + 1} {c.speaker} | {c.emotion or 'normal'}" + (f" | {c.action}" if c.action else ""))
            if callable(then):
                then()
            else:
                self.export_smart_script()
        self._run(scan_smart_script, self.video, lang=self._lang_code(), model=self.model.currentText(),
                  device=self.device.currentText(), hf_token=st.get("hf_token"), min_conf=st.get("voice_min_conf"),
                  saved_presets=load_saved(self.ctx.settings), emo_min_pct=self.emo_min.value(),
                  event_min_pct=self.evt_min.value(), use_motion=self.motion_on.isChecked(), use_story=self.story_on.isChecked(), ocr_fix=self.ocr_fix_on.isChecked(), break_on_silence=self.split_sil.value(), max_line_secs=0.0,   # no length cap: a line ends only when the speaker / voice changes or after the silence setting
                  diar_mode=st.get("diar_mode"), diar_model=st.get("diar_model"),
                  gemini_key=st.get("gemini_key"), gemini_model=st.get("gemini_model"), done=done, alert=True,
                  asr_hint=self._asr_hint_text(),
                  on_fail=(self._ad_abort if callable(then) else None))

    def _asr_hint_text(self):
        """User's names + the character names of an earlier story analysis (spelling help for Whisper)."""
        names = ""
        try:
            names = ", ".join(n for n in ((c.get("name") if isinstance(c, dict) else str(c)) for c in ((self.story or {}).get("characters") or []))
                              if n and not str(n).lower().startswith("speaker"))
        except Exception:  # noqa
            names = ""
        return ", ".join(x for x in (self.asr_hint.text().strip(), names) if x)

    def export_sub_rich(self):  # SRT / VTT with the character names (VTT also gets the story header as a NOTE)
        if not self.cues:
            QMessageBox.information(self, "Export", "No lines yet."); return
        self._collect_focus()
        start = str(self._outdir() / "subtitles_names.srt") if self.video else "subtitles_names.srt"
        f, sel = QFileDialog.getSaveFileName(self, "Export subtitles with names", start, "SRT with names (*.srt);;VTT with names + story note (*.vtt)")
        if not f:
            return
        if not f.lower().endswith((".srt", ".vtt")):
            f += ".vtt" if "VTT" in sel else ".srt"
        which = "translation" if any(c.translation.strip() for c in self.cues) else "text"
        data = S.to_vtt_rich(self.cues, which, self.story) if f.lower().endswith(".vtt") else S.to_srt_rich(self.cues, which)
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(data)
        self.log.appendPlainText(f"Saved {f} (with character names).")

    def export_smart_script(self):  # start-end || text || emotion || speaker, voice   (.txt)
        if not self.cues:
            QMessageBox.information(self, "Smart Script", "No lines yet."); return
        self._collect_focus()
        start = str(self._outdir() / "smart_script.txt") if self.video else "smart_script.txt"
        f, _ = QFileDialog.getSaveFileName(self, "Save Smart Script", start, "Smart Script (*.txt)")
        if not f:
            return
        if not f.lower().endswith(".txt"):
            f += ".txt"
        which = "translation" if any(c.translation.strip() for c in self.cues) else "text"
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(SB.to_script_txt(self.cues, which, self.story, self._lang_code()))
        self.log.appendPlainText(f"Saved {f} ({'Khmer translation' if which == 'translation' else 'original text with pauses + emotion tags'}).")

    def _set_cues(self, cues, note=""):
        self.cues = cues
        self.refresh()
        self.bar.setValue(100)
        self.log.appendPlainText(f"✓ {len(cues)} lines extracted." + (" " + note if note else ""))

    # ---------- Speaker Manager / providers / row actions ----------
    def _book_path(self):
        return self._outdir() / "speakers.json" if self.video else None

    def _book(self):
        """The SpeakerBook of the current video (loaded from <output>/speakers.json, kept in memory)."""
        key = self.video or ""
        if getattr(self, "_book_for", None) != key:
            p = self._book_path()
            try:
                self._spk = SpeakerBook.load(p) if p else SpeakerBook()
            except SpeakerError as e:
                self.log.appendPlainText(f"Speaker settings could not be restored: {e}"); self._spk = SpeakerBook()
            self._book_for = key
        self._spk.sync_from_cues(self.cues)
        return self._spk

    def _save_book(self):
        p = self._book_path()
        if p:
            self._spk.save(p); self.log.appendPlainText(f"Speaker settings saved: {p.name}")

    def _after_scan(self, cues):
        """New scan: add the detected speakers to the book (locked / configured speakers keep their settings)."""
        d = getattr(cues, "diarization", None) or {}
        b = self._book()
        b.apply_detection([c.speaker for c in cues if c.speaker], d.get("embeddings"), d.get("voice_types"))
        self._merge_hint = d.get("merge_suggestions", [])
        n = sum(1 for c in cues if c.speaker_uncertain)
        if n:
            self.log.appendPlainText(f"⚠ {n} line(s) have an uncertain speaker (amber). Open Speaker Manager or right-click a row to fix them.")
        for a, b2, cs in self._merge_hint:
            self.log.appendPlainText(f"ℹ {a} and {b2} sound similar ({cs:.0%}) - NOT merged. Merge them in Speaker Manager only if it is the same person.")
        self._save_book()

    def open_speakers(self):
        if not self.cues:
            QMessageBox.information(self, "Speakers", "Extract or import lines first."); return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        dlg = SpeakerManagerDialog(self._book(), self.cues, dict(VOICES), rows, getattr(self, "_merge_hint", []),
                                   preview_cb=self._preview_speaker, save_cb=self._save_book, parent=self)
        dlg.changed.connect(lambda: (self.refresh(), self._save_book()))
        dlg.exec()
        self.refresh()

    def open_status(self):
        SystemStatusDialog(self.ctx.settings, self).exec()

    def open_ocr_review(self):
        n = sum(1 for c in self.cues if c.text_conflict)
        if not n:
            QMessageBox.information(self, "OCR review", "No differences to review. (Switch on 'OCR fix' before scanning to compare speech with burned-in subtitles.)"); return
        dlg = OcrReviewDialog(self.cues, self)
        dlg.changed.connect(self.refresh)
        dlg.exec(); self.refresh()

    def open_providers(self):
        AIProvidersDialog(self.ctx.settings, self).exec()

    def _preview_speaker(self, sid):
        sp = self._book().get(sid)
        out = work_dir(Path(self.video).stem if self.video else "preview") / "tts" / f"preview_{abs(hash(sid)) % 99999}_{sp.voice}.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        sample = next((c.translation for c in self.cues if c.speaker == sid and c.translation.strip()), "សួស្តី នេះគឺជាសំឡេងសាកល្បង")
        spd = float(sp.voice_profile.get("speed", 1.0)) if sp.voice_profile else 1.0
        def job(progress=None, cancel=None):
            return synthesize(sample, out, sp.voice, spd)
        self._run(job, done=lambda p: self._play_file(p), alert=True)

    def _selected_rows(self):
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _row_menu(self, pos):
        rows = self._selected_rows()
        if not rows:
            return
        m = QMenu(self)
        acts = [("Generate Voice", lambda: self._rows_generate(rows, False)), ("Regenerate Audio", lambda: self._rows_generate(rows, True)),
                ("Preview Audio", self.play_selected), ("Delete Audio", lambda: self._rows_delete_audio(rows)), None,
                ("Change Speaker...", lambda: self._rows_change_speaker(rows)), ("Change Voice...", lambda: self._rows_change_voice(rows)),
                ("Set Voice Speed...", lambda: self._rows_set_speed(rows))]
        for a in acts:
            if a is None:
                m.addSeparator()
            else:
                m.addAction(a[0], a[1])
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _rows_generate(self, rows, force):
        if not self._need_video():
            return
        todo = [r for r in rows if force or not (self.cues[r].audio and Path(self.cues[r].audio).exists())]
        for r in todo:
            self.cues[r].audio = ""
        if todo:
            self._run(self._gen, todo, done=self._gen_done)

    def _rows_delete_audio(self, rows):
        for r in rows:
            self.cues[r].audio = ""        # the file stays in the work folder; the row simply has no voice (and is skipped on export)
        self.refresh()

    def _rows_change_speaker(self, rows):
        b = self._book()
        ids = list(b.speakers)
        txt, ok = QInputDialog.getItem(self, "Change speaker", f"Speaker for {len(rows)} row(s) (type a new name to create one):", ids, 0, True)
        if not ok or not txt.strip():
            return
        try:
            b.reassign(rows, txt.strip(), self.cues)
            b.apply_to_cues(self.cues)
            for r in rows:
                self.cues[r].audio = ""    # another speaker = another voice -> regenerate
        except SpeakerError as e:
            QMessageBox.warning(self, "Speaker", str(e)); return
        self._save_book(); self.refresh()

    def _rows_change_voice(self, rows):
        labels = list(VOICES)
        txt, ok = QInputDialog.getItem(self, "Change voice", "Voice for the selected rows.\nNote: a character should keep ONE voice - "
                                       "use Speaker Manager to change it for the whole character.", labels, 0, False)
        if ok:
            for r in rows:
                self.cues[r].voice_name, self.cues[r].audio = VOICES[txt], ""
            self.refresh()

    def _rows_set_speed(self, rows):
        cur = float(getattr(self.cues[rows[0]], "speed", 1.0) or 1.0)
        v, ok = QInputDialog.getDouble(self, "Voice speed", "Speed of the generated Khmer voice (0.7 - 1.4 sounds natural; video speed is never changed):",
                                       cur, 0.5, 2.0, 2)
        if ok:
            for r in rows:
                self.cues[r].speed, self.cues[r].audio = v, ""
            self.refresh()

    def translate(self, then=None, on_fail=None):  # then = function រត់បន្ទាប់ពីបកប្រែរួច
        if not self.cues:
            QMessageBox.information(self, "Translate", "Extract or import text first.")
            return
        self._collect_focus()
        self.log.appendPlainText("Translating in batches of 50...")
        self._run(translate_cues, self.cues, engine=self.engine.currentIndex(),
                  api_key=self.ctx.settings.get("gemini_key"), model=self.ctx.settings.get("gemini_model"),
                  src_lang=self._lang_code(), glossary=ST.glossary_text(self.story), done=lambda c: self._tr_done(then), on_fail=on_fail)

    def _tr_done(self, then):  # បកប្រែរួច
        self.refresh(); self.log.appendPlainText("✓ Translated." + (" Generating voice..." if then else " Review and edit before generating voice."))
        if then:
            self.worker.wait()  # រង់ចាំ thread បិទ មុន _run ថ្មី
            then()  # ចុច Generate Voice (all) ស្វ័យប្រវត្តិ

    def _collect_focus(self):
        self.table.setCurrentCell(-1, -1)  # commit any in-progress edit

    def _tts_dir(self):
        return work_dir(Path(self.video).stem) / "tts"

    @_clone_guard
    def _gen(self, idxs, progress=None, cancel=None, retry=False):  # retry=True → ព្យាយាមរហូតគ្រប់ជួរមានសំឡេង
        voice, speed = self.voice.currentData(), self.speed.value()
        d = self._tts_dir()
        self._lim_seen = set()
        todo, errs = list(idxs), []  # ជួរត្រូវបង្កើត / error
        for rnd in range(RETRY_ROUNDS if retry else 1):  # ជុំៗ
            errs, fails = self._gen_pass(todo, voice, speed, d, progress, cancel, retry)
            if cancel and cancel():
                break  # STOP ALL
            if not fails or not retry or rnd == RETRY_ROUNDS - 1:
                break  # ចប់ មិន retry ឬជុំចុងក្រោយ
            if progress:
                progress(f"✗ Round {rnd + 1}: {len(fails)} line(s) failed ({errs[0]}) → going back {'bottom→top' if rnd % 2 == 0 else 'top→down'} in 3s...")  # បង្ហាញក្នុង log
            for _ in range(30):
                if cancel and cancel():
                    return errs
                time.sleep(0.1)  # រង់ចាំ 3 វិនាទី
            todo = fails[::-1]  # ជុំក្រោយ: តែជួរបរាជ័យ ហើយបញ្ច្រាសទិស (top→down ↔ bottom→top)
        if self.match_rate.isChecked() and not (cancel and cancel()):   # NEW: fit to the original speaking speed
            try:
                for ln in SR.fit_cues(self.cues, list(idxs), d / "fit", progress=None, cancel=cancel):
                    if progress:
                        progress(ln)
            except Cancelled:
                pass
            except Exception as e:  # noqa  (never break voice generation because of this add-on)
                errs.append(f"speech-rate match skipped: {e}")
        return errs

    def _gen_pass(self, todo, voice, speed, d, progress, cancel, retry):  # មួយជុំ → (errs, fails)
        errs, fails, dead = [], [], set()   # dead = Voice Types that cannot work now (no GPU / deleted) → not retried
        idxs = todo
        for n, i in enumerate(idxs, 1):
            if cancel and cancel():
                break
            c = self.cues[i]
            if not c.translation.strip():
                continue
            vt = c.voice_type or GENDER_VT.get(c.gender, "Male")   # Male = P (default for every line), Female = S
            if vt in dead:
                c.audio = ""
                continue
            try:
                row_speed = max(0.5, min(2.0, speed * (float(getattr(c, "speed", 1.0)) or 1.0)))   # per-row speed (generated voice only)
                c.audio = self._synth_any(i, c, vt, d, row_speed, voice, progress, cancel)
            except Cancelled:
                c.audio = ""
                break
            except VC.CloneFatal as e:
                c.audio = ""; dead.add(vt)
                errs.append(f"line {i + 1} ({vt}): {e}")
            except Exception as e:  # noqa
                c.audio = ""  # មិនឱ្យ ✓ ក្លែងក្លាយ
                errs.append(f"line {i + 1}: {e}"); fails.append(i)
                if not retry and len(errs) >= 3:
                    break   # probably offline - stop early rather than failing hundreds of times
            if progress:
                progress(int(n * 100 / len(idxs)))
        return errs, fails

    def _synth_any(self, i, c, vt, d, speed, voice, progress=None, cancel=None, raw=None, tag=""):
        """One line -> one audio file with ANY voice type. raw/tag are only used by the previews (own file name, c.audio untouched)."""
        if vt in self.voices or VC.is_clone_name(vt):   # a Selected Voice Type (cloned voice or catalog preset) → one WAV for this row
            return self._synth_selected(i, c, vt, d, speed, progress, cancel, raw=raw, tag=tag)
        use_voice = getattr(c, "voice_name", "") or VT_VOICE.get(vt, voice)       # Speaker Manager voice wins -> same voice for a character
        sig = f"{c.translation}|{use_voice}|{speed:.2f}|{c.emotion_raw}|{c.emotion}|{c.prosody}|{tag}"   # file name changes with voice/speed/emotion (no stale cache)
        f = d / f"line_{i + 1:04d}_{vt[:1] or 'D'}_{hashlib.md5(sig.encode('utf-8')).hexdigest()[:8]}{tag}.mp3"
        return self._edge_line(c, f, use_voice, speed, raw=raw, progress=progress)

    def _emotion_tts_on(self):
        w = getattr(self, "emo_tts_on", None)
        return True if w is None else w.isChecked()

    def _edge_line(self, c, out, voice, speed, raw=None, progress=None):
        """edge-tts for one line. The Emotion Raw only changes bounded speed/pitch/volume - never the voice - and the
        limitation (edge-tts Khmer has no emotion styles) is logged once per emotion instead of pretending."""
        raw = raw or ER.of_cue(c)
        pj = PM.load(c)
        if pj and self._emotion_tts_on():          # measured performance of the original actor beats the coarse emotion label
            from ..translator.khmer_tts import synthesize
            path = synthesize(c.translation, out, voice, max(0.5, min(2.0, speed * (1 + pj["rate"] / 100.0))),
                              pitch_hz=int(pj["pitch"]), volume_pct=int(pj["vol"]))
            lim = ""
        else:
            path, lim = TP.synth_edge(c.translation, out, voice, speed, raw, self._emotion_tts_on())
        seen = self.__dict__.setdefault("_lim_seen", set())
        if lim and raw.label not in seen:
            seen.add(raw.label)
            if progress:
                progress("ℹ " + lim)            # the worker's progress signal is the thread-safe way to reach the log
        return path

    @staticmethod
    def _raw_summary(c):
        r = ER.of_cue(c)
        if r.label == "Normal" and r.speed == r.pitch == r.loudness == "Normal":
            return "Normal"
        return f"{r.label} · {r.intensity} · {r.speed}/{r.pitch}/{r.loudness}" + (f" · {int(r.confidence * 100)}%" if r.confidence else "") + (" ✎" if r.source == "manual" else "")

    def _synth_selected(self, i, c, vt, d, speed, progress=None, cancel=None, raw=None, tag=""):
        """One row → one WAV with a Selected Voice Type. Cloned voice → VoxCPM2 (GPU); catalog voice → edge-tts + preset effects."""
        key = hashlib.md5((c.translation + c.prosody + tag).encode("utf-8")).hexdigest()[:8]
        if VC.is_clone_name(vt):
            style = TP.clone_style(raw or ER.of_cue(c))              # from Emotion Raw ("" = plain Normal speech)
            outp = VC.synthesize_clone(c.translation, d / f"line_{i + 1:04d}_clone_{key}.wav", VC.reference_path(vt), speed,
                                       progress, cancel, style=style)
            pj = PM.load(c)
            if pj and self._emotion_tts_on():      # a cloned voice has no pitch/rate parameter: only the loudness is corrected
                import math
                PM.apply_gain(outp, 20 * math.log10(max(0.05, 1 + pj["vol"] / 100.0)))
            return outp
        p = self.voices[vt]
        rawf = d / f"line_{i + 1:04d}_{key}.mp3"
        self._edge_line(c, rawf, BASE[p["base"]], speed * (1 + p["speed"] / 100), raw=raw, progress=progress)
        return preset_process(str(rawf), d / f"line_{i + 1:04d}_{key}.wav", p, cancel=cancel)

    # ---------- Clone voice engine: CPU / GPU / Hugging Face (Online) ----------
    CLONE_ENGINES = [("CPU", "cpu"), ("GPU", "cuda"), ("Hugging Face (Online)", "hf")]

    def _clone_engine_row(self):
        v = QVBoxLayout(); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)
        r = QHBoxLayout()
        r.addWidget(QLabel("Clone voice engine"))
        self.clone_dev = QComboBox()
        for label, code in self.CLONE_ENGINES:
            self.clone_dev.addItem(label, code)
        self.clone_dev.setToolTip("Where the cloned voice is generated.\nCPU = slow but works anywhere. GPU = fast, needs an NVIDIA GPU.\n"
                                  "Hugging Face (Online) = runs on a Hugging Face Space; your reference voice is uploaded; the token is "
                                  "set in Settings.")
        cur = str(self.ctx.settings.get("voxcpm_device") or "auto").lower()
        if cur not in ("cpu", "cuda", "hf"):                       # "auto" = GPU when an NVIDIA GPU is present, else CPU
            import shutil
            cur = "cuda" if shutil.which("nvidia-smi") else "cpu"
        self.clone_dev.setCurrentIndex(max(0, self.clone_dev.findData(cur)))
        self._clone_dev_prev = self.clone_dev.currentData()
        self.clone_hint = QLabel(); self.clone_hint.setObjectName("muted"); self.clone_hint.setWordWrap(True)
        self.clone_dev.currentIndexChanged.connect(self._clone_engine_changed)
        r.addWidget(self.clone_dev, 1)
        v.addLayout(r); v.addWidget(self.clone_hint)
        self._clone_engine_hint()
        return v

    def _clone_engine_hint(self):
        if self.clone_dev.currentData() == "hf":
            tok = str(self.ctx.settings.get("hf_token") or "").strip()
            self.clone_hint.setText("Online: token saved in Settings." if tok else
                                    "Online: no Hugging Face token in Settings (anonymous = very small quota).")
        else:
            self.clone_hint.setText("")

    def showEvent(self, e):                      # the token may have been changed in Settings meanwhile
        super().showEvent(e)
        if hasattr(self, "clone_hint"):
            self._clone_engine_hint()

    def _clone_engine_changed(self, _=None):
        dev = self.clone_dev.currentData()
        if dev == "hf" and not self.ctx.settings.get("hf_online_ack"):
            ok = QMessageBox.question(self, "Hugging Face (Online)",
                                      "Online cloning uploads your reference voice recording to a Hugging Face Space "
                                      "(a server run by someone else) every time a cloned voice is generated.\n\n"
                                      "The free quota of a Space can run out; then generating stops with an error "
                                      "(it does NOT switch to CPU/GPU by itself).\n\nUse Hugging Face (Online)?") == QMessageBox.Yes
            if not ok:
                self.clone_dev.blockSignals(True)
                self.clone_dev.setCurrentIndex(max(0, self.clone_dev.findData(self._clone_dev_prev)))
                self.clone_dev.blockSignals(False)
                return
            self.ctx.settings.set("hf_online_ack", True)
        self._clone_dev_prev = dev
        VC.set_device(dev)                       # saves 'voxcpm_device' and drops a loaded local model if the engine changed
        self._clone_engine_hint()
        self.log.appendPlainText(f"Clone voice engine: {self.clone_dev.currentText()}")

    # ---------- Voice Types: ADD VOICE TYPE / CLONE VOICE / Selected Voice Types ----------
    def _load_voices(self):
        try:
            v = load_saved(self.ctx.settings)
        except Exception:  # noqa
            v = {}
        return {n: p for n, p in v.items() if not VC.is_clone_name(n) or VC.get_voice(n)}   # a deleted clone never comes back

    def _voice_panel(self):
        self._cur_voice = None     # the voice type last clicked (the Remove / DELETE buttons act on it)
        box = QWidget(); box.setObjectName("plain")
        v = QVBoxLayout(box); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(6)
        cap = QLabel("Selected Voice Types"); cap.setObjectName("muted"); v.addWidget(cap)
        self._vp = QWidget(); self._vp.setObjectName("plain")
        self._vp_lay = QGridLayout(self._vp); self._vp_lay.setContentsMargins(0, 0, 0, 0); self._vp_lay.setSpacing(6)
        v.addWidget(self._vp)
        r = QHBoxLayout()
        rm = QPushButton("\u2715 Remove"); rm.setToolTip("Take the highlighted voice type out of Selected Voice Types (a cloned voice file is kept)")
        rm.clicked.connect(self._remove_current_voice)
        dl = QPushButton("DELETE voice type"); dl.setObjectName("danger")
        dl.setToolTip("Delete the highlighted CLONED voice permanently (asks for confirmation)"); dl.clicked.connect(self._delete_current_voice)
        r.addWidget(rm); r.addWidget(dl)
        v.addLayout(r)
        self._refresh_voices()
        return box

    def _refresh_voices(self):
        lay = self._vp_lay
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.setParent(None); w.deleteLater()
        if self._cur_voice not in self.voices:
            self._cur_voice = None
        if not self.voices:
            n = QLabel("(none yet - press ADD VOICE TYPE)"); n.setObjectName("muted")
            lay.addWidget(n, 0, 0, 1, 2)
        for k, (n, p) in enumerate(self.voices.items()):          # one card per voice type: click = use it on the selected rows
            b = QPushButton(display_name(n, p) if not VC.is_clone_name(n) else n)
            b.setObjectName("voiceCard"); b.setCheckable(True); b.setChecked(n == self._cur_voice)
            b.setToolTip("Click: use this voice type on the selected rows (all rows if none selected)")
            b.clicked.connect(lambda _=False, n=n: self._pick_voice(n))
            lay.addWidget(b, k // 2, k % 2)
        lay.setColumnStretch(0, 1); lay.setColumnStretch(1, 1)

    # ---------- Video Effects tabs (same widgets / settings keys; laid out for the narrow inspector) ----------

    def _pick_voice(self, name):
        self._cur_voice = name
        self._use_voice(name)
        self._refresh_voices()

    def _remove_current_voice(self):
        if not self._cur_voice:
            QMessageBox.information(self, "Voice Type", "Click a voice type button first, then press Remove."); return
        self._remove_voice(self._cur_voice)

    def _delete_current_voice(self):
        n = self._cur_voice
        if not n:
            QMessageBox.information(self, "Voice Type", "Click a voice type button first, then press DELETE."); return
        if not VC.is_clone_name(n):
            QMessageBox.information(self, "Voice Type", f"'{n}' is not a cloned voice, so there is no file to delete.\nUse Remove to take it out of the list."); return
        self._delete_clone(n)

    def add_voice_type(self):
        dlg = AddVoiceTypeCloneDialog(self.voices, self, self.ctx.settings.get("voice_nicks") or {}, in_use=VC.is_in_use)
        ok = dlg.exec() == QDialog.Accepted
        for n in dlg.deleted:   # cloned voices deleted inside the dialog
            self._forget_voice_rows(n)
        if ok and dlg.result_item:
            name, p = dlg.result_item
            self.voices[name] = p   # keyed by name → the same Voice Type can never appear twice
            self.log.appendPlainText(f"✓ Voice Type added: {name}")
        if dlg.deleted or (ok and dlg.result_item):
            save_saved(self.ctx.settings, self.voices)
        self._refresh_voices()

    def _forget_voice_rows(self, name):   # rows that used this Voice Type go back to the default (their audio files are kept)
        n = 0
        for c in self.cues:
            if c.voice_type == name:
                c.voice_type, c.gender = "", ""; n += 1
        if n:
            self.refresh()
            self.log.appendPlainText(f"{n} row(s) used '{name}': Voice Type cleared (default voice will be used when you generate again).")

    def _remove_voice(self, name):
        if self.voices.pop(name, None) is not None:
            save_saved(self.ctx.settings, self.voices)
            self._forget_voice_rows(name)
        self._refresh_voices()

    def _delete_clone(self, name):
        if confirm_delete_clone(self, name, VC.is_in_use):
            self.voices.pop(name, None)
            save_saved(self.ctx.settings, self.voices)
            self._forget_voice_rows(name)
            self.log.appendPlainText(f"✓ Cloned voice deleted: {name}")
            self._refresh_voices()

    def _use_voice(self, name):   # put this Voice Type on the selected rows (all rows if none selected)
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            if not self.cues or QMessageBox.question(self, "Voice Type", f"No rows selected. Use '{name}' for ALL {len(self.cues)} rows?") != QMessageBox.Yes:
                return
            rows = range(len(self.cues))
        g = {"Male": "male", "Female": "female"}.get(name, "")   # default Male/Female keep their gender (same rule as the table dropdown)
        for r in rows:
            c = self.cues[r]
            if c.voice_type != name:
                c.voice_type, c.gender, c.audio = name, g, ""   # voice changed → must be regenerated
        self.refresh()

    # ---------- voice cards ----------

    def _gen_done(self, errs):
        self.refresh()
        try:
            self._studio_after_generate()      # remember what each voice was made from, measure it, check it against the timeline
        except Exception as e:  # noqa
            self.log.appendPlainText(f"(voice timing check skipped: {e})")
        for e in errs[:10]:
            self.log.appendPlainText("✗ " + e)
        if len(errs) > 10:
            self.log.appendPlainText(f"... and {len(errs) - 10} more")  # កុំឱ្យ log វែងពេក
        self.log.appendPlainText("✓ Voice generation finished." if not errs else "Voice generation stopped with errors.")

    def gen_all(self):  # 5. Generate Voice (all): បកប្រែជួរដែលខ្វះ រួចបង្កើតសំឡេងរហូតគ្រប់ជួរ ✓
        if not self._need_video():
            return
        self._collect_focus()
        self.tog["Process"].setChecked(True)  # បង្ហាញ log ឱ្យឃើញ error
        if not self.cues:
            QMessageBox.information(self, "Generate Voice", "There is no script yet, so there is nothing to speak.\n"
                                    "Scan the video first (A. Video → Script file) or import a script (B. Script → Voice).")
            return
        if any(c.text.strip() and not c.translation.strip() for c in self.cues):  # មានជួរមិនទាន់បកប្រែ
            self.translate(then=self._gen_all_run)  # បកប្រែមុន រួចបន្តដោយស្វ័យប្រវត្តិ
            return
        self._gen_all_run()

    def _gen_all_run(self):  # បង្កើតសំឡេងគ្រប់ជួរដែលមិនទាន់មាន ✓
        idxs = [i for i, c in enumerate(self.cues) if c.translation.strip() and not (c.audio and Path(c.audio).exists())]
        if not idxs:
            self.log.appendPlainText("Nothing to generate (translate first, or all lines already have voice).")
            return
        self.log.appendPlainText(f"Generating {len(idxs)} voice clips (edge-tts needs internet)...")
        self._run(self._gen, idxs, retry=True, done=self._gen_done)  # retry រហូតគ្រប់ជួរ ✓

    def gen_selected(self):
        if not self._need_video():
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if rows:
            for r in rows:
                self.cues[r].audio = ""
            self._run(self._gen, rows, done=self._gen_done)

    def script_to_voice(self):  # B. Script -> Voice: import ONLY the Khmer words into "Khmer Translation" (no voice is generated)
        f, _ = QFileDialog.getOpenFileName(self, "Script", "", "Script (*.txt *.srt *.vtt)")
        if not f:
            return
        try:
            items = S.parse_khmer_script(open(f, "r", encoding="utf-8-sig").read())
        except Exception as e:  # noqa
            QMessageBox.warning(self, "Script", str(e)); return
        if not items:
            QMessageBox.information(self, "Script", "No Khmer text found in this file."); return
        old, tagged = len(self.cues), 0
        for k, (kh, st, en, g) in enumerate(items):
            if k < old:
                c = self.cues[k]                       # keep the row's time + original text
            else:                                      # more script lines than rows -> new row
                if st is None:
                    st = self.cues[-1].end if self.cues else 0.0
                    en = st + max(1.5, 0.08 * len(kh))   # rough length; edit Start/End if needed
                c = S.Cue(st, en if en is not None else st + 2.0, "", "")
                self.cues.append(c)
            c.translation, c.audio = kh, ""
            if g:                                      # "|male" / "|female" note -> Voice Type
                c.gender, c.voice_type = g, GENDER_VT[g]; tagged += 1
        try:   # Smart Script header (# TITLE / SUMMARY / CHARACTERS) → story info (used as translation context)
            st_ = ST.parse_header(open(f, "r", encoding="utf-8-sig").read())
            if st_:
                self.story = st_
                self.log.appendPlainText(f"Script header read: {st_.get('title') or '-'} | {len(st_['characters'])} character(s).")
        except Exception:  # noqa
            pass
        try:   # Smart Script file → keep each line's emotion / speaker (used as the voice style of cloned voices)
            raw = open(f, "r", encoding="utf-8-sig").read()
            full = [x for x in SB.parse_script_txt(raw) if S._KCORE.search(x.text)] if SB.is_script_format(raw) else []
            if len(full) == len(items):
                for k, x in enumerate(full):
                    if self.cues[k].emotion != x.emotion:
                        self.cues[k].emotion_raw = ""          # derived again from the imported emotion text
                    self.cues[k].emotion, self.cues[k].speaker, self.cues[k].voice_desc = x.emotion, x.speaker, x.voice_desc
        except Exception:  # noqa - optional extra, never blocks the import
            pass
        self.refresh()
        left = max(0, old - len(items))
        self.log.appendPlainText(f"Script: {len(items)} Khmer line(s) put in Khmer Translation ({tagged} with Male/Female). "
                                 "No voice generated - press 5. Generate Voice (all) when ready."
                                 + (f" Warning: {left} row(s) got no Khmer line (Generate Voice would machine-translate them)." if left else ""))

    def play_selected(self):  # លេងគ្រប់ជួរដែលបានជ្រើស តាមលំដាប់ (ជ្រើស 10 → លេង 10)
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        ok = [r for r in rows if self.cues[r].audio and Path(self.cues[r].audio).exists()]
        if not ok:
            self.log.appendPlainText("Select line(s) that have a ✓ voice first.")
            return
        skipped = len(rows) - len(ok)
        self.log.appendPlainText(f"Playing {len(ok)} selected voice(s)" + (f" ({skipped} without voice skipped)." if skipped else "."))
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            if not self.player:
                self.player = QMediaPlayer(self)
                self.audio_out = QAudioOutput(self)
                self.player.setAudioOutput(self.audio_out)
                self.player.mediaStatusChanged.connect(self._play_status)
            self._queue = list(ok)
            self._play_next()
        except Exception:
            open_path(self.cues[ok[0]].audio)

    def _play_next(self):
        if not self._queue:
            return
        r = self._queue.pop(0)
        self.table.setCurrentCell(r, 0)  # បង្ហាញជួរកំពុងលេង (ការជ្រើសរើសអាចប្តូរ)
        self.player.setSource(QUrl.fromLocalFile(self.cues[r].audio))
        self.player.play()

    def _play_status(self, st):
        from PySide6.QtMultimedia import QMediaPlayer
        if st == QMediaPlayer.EndOfMedia:
            self._play_next()


    # ---------- Video Effects (blur / Khmer subtitle / logo) ----------
    def _slider(self, lo, hi, val, suffix="%"):
        sl = QSlider(Qt.Horizontal); sl.setRange(lo, hi); sl.setValue(val)
        lb = QLabel(f"{val}{suffix}"); lb.setMinimumWidth(44)
        sl.valueChanged.connect(lambda x: lb.setText(f"{x}{suffix}"))
        return sl, lb

    def _spin(self, lo, hi, val, suffix="%"):
        sp = QSpinBox(); sp.setRange(lo, hi); sp.setValue(val); sp.setSuffix(suffix)
        return sp

    def _persist(self, wd, key):  # ចងចាំតម្លៃ widget ក្នុង settings.json
        wd.setValue(self.ctx.settings.get(key))  # ដាក់តម្លៃដែលបានរក្សា
        wd.valueChanged.connect(lambda x: (self.ctx.settings.set(key, x), self.ctx.settings.save()))  # រក្សាពេលប្តូរ

    def _save_key(self, key, val):
        self.ctx.settings.set(key, val); self.ctx.settings.save()

    def _persist_check(self, btn, key):  # ON/OFF button ឬ checkbox
        btn.setChecked(bool(self.ctx.settings.get(key)))
        btn.toggled.connect(lambda x: self._save_key(key, bool(x)))

    def _persist_combo(self, cb, key):
        i = cb.findText(str(self.ctx.settings.get(key)))
        if i >= 0:
            cb.setCurrentIndex(i)
        cb.currentTextChanged.connect(lambda t: self._save_key(key, t))

    def _prev_btn(self):
        b = QPushButton("Preview frame"); b.clicked.connect(self.preview_fx); return b

    def _vol_slider(self, r, val=100):  # slider 0-200% + ស្លាក → ដាក់ក្នុងជួរ r
        s = QSlider(Qt.Horizontal); s.setRange(0, 200); s.setValue(val); s.setFixedWidth(90)  # ទំហំ slider
        l = QLabel(f"{val}%"); l.setMinimumWidth(38); s.valueChanged.connect(lambda x: l.setText(f"{x}%"))  # បង្ហាញ %
        r.addWidget(s); r.addWidget(l)
        return s

    def _blur_tab(self):
        w = QWidget(); g = QGridLayout(w)
        self.blur_btn = QPushButton("Blur: OFF"); self.blur_btn.setCheckable(True)
        self.blur_btn.toggled.connect(lambda on: self.blur_btn.setText("Blur: ON" if on else "Blur: OFF"))
        self.blur_pct, self.blur_lbl = self._slider(0, 100, 60)
        self.b_top, self.b_h = self._spin(0, 100, 70), self._spin(1, 100, 11)
        self.b_left, self.b_w = self._spin(0, 100, 5), self._spin(1, 100, 90)
        g.addWidget(self.blur_btn, 0, 0, 1, 3)
        g.addWidget(QLabel("Blur %"), 1, 0); g.addWidget(self.blur_pct, 1, 1); g.addWidget(self.blur_lbl, 1, 2)
        for k, (t, sp) in enumerate((("Subtitle area: top", self.b_top), ("height", self.b_h), ("left", self.b_left), ("width", self.b_w))):
            g.addWidget(QLabel(t), 2 + k, 0); g.addWidget(sp, 2 + k, 1, 1, 2)
        for wd, k in ((self.blur_pct, "fx_blur_pct"), (self.b_top, "fx_top"), (self.b_h, "fx_h"),
                      (self.b_left, "fx_left"), (self.b_w, "fx_w")):
            self._persist(wd, k)  # remember Blur % + area
        self._persist_check(self.blur_btn, "fx_blur_on")
        g.addWidget(self._prev_btn(), 6, 0, 1, 3)
        g.setColumnStretch(1, 1); g.setRowStretch(7, 1)
        return w

    def _sub_tab(self):
        w = QWidget(); g = QGridLayout(w)
        b = QPushButton("Generate Khmer Subtitle"); b.setObjectName("primary"); b.clicked.connect(self.generate_khmer_sub)
        self.font_box = QComboBox(); self.font_box.addItems(["Battambang", "Noto Sans Khmer", "Custom font file..."])
        self.font_box.activated.connect(self._font_picked)
        self.font_file = str(VO.BUNDLED["Battambang"])
        self.s_size = self._spin(10, 200, 54, " px")
        self.s_color = QPushButton("Color"); self.s_color.clicked.connect(self._pick_color); self._color = "#FFFFFF"; self._paint_color()
        self.s_outline = QCheckBox("Outline"); self.s_outline.setChecked(True)
        self.s_pos = QComboBox(); self.s_pos.addItems(list(VO.SUB_POS))
        self.s_margin = self._spin(0, 500, 40, " px")
        self._persist(self.s_size, "fx_sub_size"); self._persist(self.s_margin, "fx_sub_margin")  # remember size + edge distance
        self._persist_check(self.s_outline, "fx_sub_outline"); self._persist_combo(self.s_pos, "fx_sub_pos")
        c = str(self.ctx.settings.get("fx_sub_color") or "#FFFFFF"); self._color = c; self._paint_color()
        ff = str(self.ctx.settings.get("fx_sub_font_file") or "")
        if ff and Path(ff).exists():  # restore the chosen font
            self.font_file = ff
            if Path(ff) == VO.BUNDLED["Noto Sans Khmer"]:
                self.font_box.setCurrentIndex(1)
            elif Path(ff) != VO.BUNDLED["Battambang"]:
                self.font_box.setItemText(2, Path(ff).name); self.font_box.setCurrentIndex(2)
        g.addWidget(b, 0, 0, 1, 3)
        g.addWidget(QLabel("Font"), 1, 0); g.addWidget(self.font_box, 1, 1, 1, 2)
        g.addWidget(QLabel("Size"), 2, 0); g.addWidget(self.s_size, 2, 1, 1, 2)
        g.addWidget(self.s_color, 3, 0); g.addWidget(self.s_outline, 3, 1, 1, 2)
        g.addWidget(QLabel("Position"), 4, 0); g.addWidget(self.s_pos, 4, 1, 1, 2)
        g.addWidget(QLabel("Edge distance"), 5, 0); g.addWidget(self.s_margin, 5, 1, 1, 2)
        g.addWidget(self._prev_btn(), 6, 0, 1, 3)
        g.setColumnStretch(1, 1); g.setRowStretch(7, 1)
        return w

    def _logo_tab(self):
        w = QWidget(); g = QGridLayout(w)
        b = QPushButton("Input Logo"); b.clicked.connect(self.pick_logo)
        self.logo_name = QLabel("(no logo)")
        self.logo_btn = QPushButton("Logo: OFF"); self.logo_btn.setCheckable(True)
        self.logo_btn.toggled.connect(lambda on: self.logo_btn.setText("Logo: ON" if on else "Logo: OFF"))
        self.l_size, self.l_size_lb = self._slider(3, 60, 15)
        self.l_op, self.l_op_lb = self._slider(5, 100, 100)
        self.l_pos = QComboBox(); self.l_pos.addItems(VO.LOGO_POS); self.l_pos.setCurrentText("Top Right")
        self.l_autobg = QCheckBox("Auto remove background"); self._persist_check(self.l_autobg, "logo_autobg")
        g.addWidget(b, 0, 0); g.addWidget(self.logo_name, 0, 1, 1, 2)
        g.addWidget(self.logo_btn, 1, 0, 1, 3)
        g.addWidget(self.l_autobg, 2, 0, 1, 3)
        g.addWidget(QLabel("Position"), 3, 0); g.addWidget(self.l_pos, 3, 1, 1, 2)
        g.addWidget(QLabel("Size (% width)"), 4, 0); g.addWidget(self.l_size, 4, 1); g.addWidget(self.l_size_lb, 4, 2)
        g.addWidget(QLabel("Opacity"), 5, 0); g.addWidget(self.l_op, 5, 1); g.addWidget(self.l_op_lb, 5, 2)
        g.addWidget(self._prev_btn(), 6, 0, 1, 3)
        g.setColumnStretch(1, 1); g.setRowStretch(7, 1)
        return w

    def _track_tab(self):
        w = QWidget(); g = QGridLayout(w)
        self.t_mirror = QCheckBox("Mirror video (flip left-right)"); self._persist_check(self.t_mirror, "trk_mirror")
        b = QPushButton("Input Watermark"); b.clicked.connect(self.pick_watermark)
        wp = str(self.ctx.settings.get("trk_wm_path") or "")
        self.fx.track.wm_path = wp if wp and Path(wp).exists() else ""
        self.wm_name = QLabel(Path(self.fx.track.wm_path).name if self.fx.track.wm_path else "(no watermark)")
        self.wm_btn = QPushButton("Watermark: OFF"); self.wm_btn.setCheckable(True)
        self.wm_btn.toggled.connect(lambda on: self.wm_btn.setText("Watermark: ON" if on else "Watermark: OFF"))
        self._persist_check(self.wm_btn, "trk_wm_on")
        self.wm_size, self.wm_size_lb = self._slider(3, 80, int(self.ctx.settings.get("trk_wm_size")))
        self.wm_op, self.wm_op_lb = self._slider(1, 100, int(self.ctx.settings.get("trk_wm_opacity")))
        self.wm_pos = QComboBox(); self.wm_pos.addItems(VO.WM_POS)
        self._persist_combo(self.wm_pos, "trk_wm_pos")
        self.wm_size.valueChanged.connect(lambda x: self._save_key("trk_wm_size", x))
        self.wm_op.valueChanged.connect(lambda x: self._save_key("trk_wm_opacity", x))
        g.addWidget(self.t_mirror, 0, 0, 1, 3)
        g.addWidget(b, 1, 0); g.addWidget(self.wm_name, 1, 1, 1, 2)
        g.addWidget(self.wm_btn, 2, 0, 1, 3)
        g.addWidget(QLabel("Position"), 3, 0); g.addWidget(self.wm_pos, 3, 1, 1, 2)
        g.addWidget(QLabel("Size (% width)"), 4, 0); g.addWidget(self.wm_size, 4, 1); g.addWidget(self.wm_size_lb, 4, 2)
        g.addWidget(QLabel("Opacity"), 5, 0); g.addWidget(self.wm_op, 5, 1); g.addWidget(self.wm_op_lb, 5, 2)
        g.addWidget(self._prev_btn(), 6, 0, 1, 3)
        g.setColumnStretch(1, 1); g.setRowStretch(7, 1)
        return w

    def pick_watermark(self):
        f, _ = QFileDialog.getOpenFileName(self, "Watermark", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if f:
            self.fx.track.wm_path = f; self.wm_name.setText(Path(f).name); self.wm_btn.setChecked(True)
            self._save_key("trk_wm_path", f)
            self._legacy_to_scene(("wm",), ("wm",))

    def _paint_color(self):
        self.s_color.setStyleSheet(f"background:{self._color};color:{'#000' if self._color.lower() in ('#ffffff', '#ffff00') else '#fff'}")

    def _pick_color(self):
        c = QColorDialog.getColor(Qt.white, self, "Subtitle color")
        if c.isValid():
            self._color = c.name(); self._paint_color(); self._save_key("fx_sub_color", self._color)

    def _font_family(self, path):  # ឈ្មោះ family ពិតក្នុងឯកសារ font
        try:
            from PySide6.QtGui import QFontDatabase
            fid = QFontDatabase.addApplicationFont(str(path))
            fam = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fam:
                return fam[0]
        except Exception:
            pass
        return VO.family_of(path)

    def _font_picked(self, i):
        if i == 2:
            f, _ = QFileDialog.getOpenFileName(self, "Khmer font (Unicode)", "", "Fonts (*.ttf *.otf)")
            if not f:
                self.font_box.setCurrentIndex(0 if Path(self.font_file) == VO.BUNDLED["Battambang"] else 1); return
            self.font_file = f; self.font_box.setItemText(2, Path(f).name)
        else:
            self.font_file = str(VO.BUNDLED["Battambang" if i == 0 else "Noto Sans Khmer"])
        self._save_key("fx_sub_font_file", self.font_file)

    def pick_logo(self):
        f, _ = QFileDialog.getOpenFileName(self, "Logo", "", "Images (*.png *.jpg *.jpeg)")
        if not f:
            return
        self.logo_src = f
        if not self.l_autobg.isChecked():
            self._logo_set(f, ""); return
        self.log.appendPlainText("Removing logo background...")
        out = str(work_dir("logos") / f"{Path(f).stem}_nobg.png")
        self._run(remove_background, f, out, done=lambda r, f=f: self._logo_set(r[0], r[1], f))

    def _logo_set(self, path, msg="", orig=None):
        self.fx.logo.path = path
        self.logo_name.setText(Path(orig or path).name + (" (no bg)" if path != (orig or path) else ""))
        self.logo_btn.setChecked(True)
        self._legacy_to_scene(("logo",), ("logo",))
        if msg:
            self.log.appendPlainText(msg)

    def _collect_fx(self):  # អានតម្លៃពី UI → Overlay
        b, s, lg = self.fx.blur, self.fx.sub, self.fx.logo
        b.enabled, b.percent = self.blur_btn.isChecked(), self.blur_pct.value()
        b.top, b.height, b.left, b.width = self.b_top.value(), self.b_h.value(), self.b_left.value(), self.b_w.value()
        s.enabled, s.size, s.color, s.outline = self.sub_mode.currentIndex() == 1, self.s_size.value(), self._color, self.s_outline.isChecked()
        s.position, s.margin, s.font_file = self.s_pos.currentText(), self.s_margin.value(), self.font_file
        s.font_name = self._font_family(self.font_file) if Path(self.font_file).exists() else ""
        lg.enabled, lg.size, lg.opacity, lg.position = self.logo_btn.isChecked(), self.l_size.value(), self.l_op.value(), self.l_pos.currentText()
        t = self.fx.track
        t.mirror, t.wm_enabled, t.wm_size, t.wm_opacity, t.wm_position = (self.t_mirror.isChecked(), self.wm_btn.isChecked(),
                                                                          self.wm_size.value(), self.wm_op.value(), self.wm_pos.currentText())
        # studio scene = what is on screen. It draws blur / subtitle / logo / watermark / mirror itself, so the old renderers are switched off
        # (their style values above stay, they are still used by 'Generate Khmer Subtitle').
        self._legacy_to_scene()
        self.fx.scene = self.scene.copy()
        b.enabled = s.enabled = lg.enabled = False
        t.wm_enabled = t.mirror = False
        return self.fx

    def _fx_time(self):  # ពេលសម្រាប់ preview: ជួរដែលបានជ្រើស ឬ cue ដំបូង
        r = self.table.currentRow()
        if 0 <= r < len(self.cues):
            return self.cues[r].start + 0.2
        first = next((c for c in self.cues if c.translation.strip()), None)
        return (first.start + 0.2) if first else 1.0

    def preview_fx(self):
        if not self._need_video():
            return
        self._collect_fx()
        if self.fx.sub.enabled and not any(c.translation.strip() for c in self.cues):
            self.log.appendPlainText("Khmer subtitle is ON but there is no Khmer Translation yet (translate first).")
        if not self.fx.active():
            self.log.appendPlainText("Nothing to preview: turn on Blur, Khmer subtitle, Logo, Mirror or Watermark first.")
            return
        png = str(work_dir(Path(self.video).stem) / "preview.png")
        self._run(VO.render_preview, self.video, self.cues, self.fx.copy(),
                  self._fx_time(), png, done=self._show_preview)

    def _show_preview(self, png):
        from PySide6.QtGui import QPixmap
        d = QDialog(self); d.setWindowTitle("Preview (same filters as export)")
        l = QVBoxLayout(d); im = QLabel(); im.setPixmap(QPixmap(png).scaled(960, 600, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        l.addWidget(im); b = QPushButton("Close"); b.clicked.connect(d.accept); l.addWidget(b)
        d.exec()

    def generate_khmer_sub(self):  # Khmer Translation → .ass/.srt (timestamp ដើម) + បើក burn-in + preview
        if not self._need_video():
            return
        if not any(c.translation.strip() for c in self.cues):
            QMessageBox.information(self, "Khmer Subtitle", "No Khmer Translation yet. Translate first (step 3)."); return
        self.sub_mode.setCurrentIndex(1); self._collect_fx()  # ជ្រើស Burn-in ស្វ័យប្រវត្តិ
        try:
            i = probe(self.video)
            files = VO.export_srt_ass(self._outdir() / f"{Path(self.video).stem}_khmer_subtitle", self.cues, self.fx, i["width"] or 1920, i["height"] or 1080)
            self.log.appendPlainText("✓ Khmer subtitle saved: " + ", ".join(files) + "  (will be burned in on video export)")
        except Exception as e:  # noqa
            self.log.appendPlainText("✗ " + str(e)); return
        self.preview_fx()

    # ---------- export ----------
    def export(self, then=None, on_fail=None):   # then / on_fail: used by Batch Auto Dub (no popups, continue with the next video)
        batch = getattr(self, "_batch", None) is not None
        if not self._need_video():
            return
        if not self.cues:
            QMessageBox.information(self, "Export", "The table is empty - there is no script, so nobody would speak.\n"
                                    "Scan the video first (A. Video → Script file) or import a script (B. Script → Voice).")
            return
        self._collect_focus()
        mode = self.export_mode.currentIndex()   # 4 = video + Khmer voice, NO subtitles
        stem = Path(self.video).stem
        out = self._outdir()
        dur = probe(self.video)["duration"]
        sub = "soft" if self.sub_mode.currentIndex() == 0 else "burn"
        font = self.ctx.settings.get("khmer_font_path")
        vv, bv = self.voice_vol.value() / 100, self.bg_vol.value() / 100  # volume ខ្មែរ / music
        keep_bg = self.keep_bg.isChecked()  # ប្រើ Demucs ឬអត់
        voff = self.voice_off.value()  # seconds added to voice start
        fx = self._collect_fx()
        fx = fx.copy()  # ចម្លង → thread-safe
        if mode == 4:
            fx.sub.enabled = False   # no Khmer subtitle burned in
            for it_ in fx.scene.items:
                if it_.kind == "subtitle":
                    it_.enabled = False
            sub = "none"            # no soft subtitle track either
        mixer = _Mixer.from_dict(self.mixer.to_dict())                      # thread-safe copies for the worker
        sfx_clips = [AU.AudioClip.from_dict(k.to_dict()) for k in self.clips if k.track == "sfx"]
        bgm_clips = [AU.AudioClip.from_dict(k.to_dict()) for k in self.clips if k.track == "bgm"]
        ai_clips = [AU.AudioClip.from_dict(k.to_dict()) for k in self.clips if k.track == "ai_voice"]      # STEP 2: AI Voice track

        def job(progress=None, cancel=None):
            msgs = []
            wav = None
            if mode in (0, 1, 3, 4):
                try:
                    msgs += ["⚠ " + w_ for w_ in timing_report(self.cues, dur)]      # overlaps / clips that cannot fit, before the export
                except Exception:  # noqa - a report must never block an export
                    pass
                wav = work_dir(stem) / "voice_track.wav"
                over = build_voice_track(self.cues, dur, wav, cancel=cancel, offset=voff)
                if over:
                    msgs.append(f"Lines too long for their time slot (trimmed/sped up): {[o + 1 for o in over]}. "
                                "Shorten the Khmer text or raise the speech speed.")
            # Final audio = the Audio mixer (volume / mute / solo per channel). Demucs is NEVER started here: only stems that the
            # Demucs button already made are used.
            if wav is not None:
                oc = mixer.channels["orig_vocals"]
                orig_wav = (AU.extract_original(self.video, work_dir(stem) / "original_audio.wav", cancel=cancel)
                            if keep_bg and oc.enabled and oc.volume > 0 and not AU.have_stems(self.video) else None)   # STEP 2
                inputs = AU.gather_inputs(self.video, wav, sfx_clips, bgm_clips, dur, tmp_dir=work_dir(stem), cancel=cancel,
                                          ai_clips=ai_clips, orig_wav=orig_wav)
                if not keep_bg:
                    inputs.pop("orig_vocals", None); inputs.pop("orig_bg", None)
                elif mode in (3, 4) and not AU.have_stems(self.video):
                    msgs.append("Note: original voice / music not included - press 'Run Demucs' first if you want them in the mix.")
                wav = AU.render_mix(inputs, mixer, work_dir(stem) / "final_mix.wav", cancel=cancel)
            bg, vv, bv = None, 1.0, 1.0
            if mode == 0:
                msgs.append("Saved " + export_audio(wav, out / f"{stem}_khmer.mp3", cancel=cancel, volume=vv))
            elif mode == 1:
                msgs.append("Saved " + export_audio(wav, out / f"{stem}_khmer.wav", cancel=cancel, volume=vv))
            elif mode == 2:
                msgs.append("Saved " + export_video(self.video, out / f"{stem}_khmer_subs.mp4", self.cues, None, 1.0,
                                                    sub, font, progress, cancel, fx=fx))
            else:
                msgs.append("Saved " + export_video(self.video, out / (f"{stem}_khmer_voice.mp4" if mode == 4 else f"{stem}_khmer_dub.mp4"), self.cues, wav, 0.0,
                                                    sub, font, progress, cancel, bg_wav=bg, fx=fx, voice_volume=vv, bg_volume=bv))  # original mute  # លាយជាមួយ music/SFX
            return msgs

        if mode in (0, 1, 3, 4) and not any(c.audio and Path(c.audio).exists() for c in self.cues):
            if batch:
                self.log.appendPlainText("✗ no Khmer voice was generated.")
                return on_fail("no Khmer voice was generated") if on_fail else None
            QMessageBox.information(self, "Export", "Generate the Khmer voice first (step 5).")
            return
        if mode in (0, 1, 3, 4):   # lines without a voice file would be silent in the export
            silent = [i + 1 for i, c in enumerate(self.cues) if not (c.audio and Path(c.audio).exists())]
            if silent and batch:
                self.log.appendPlainText(f"   {len(silent)} silent line(s) - exported anyway in batch mode.")
            elif silent:
                eg = ", ".join(map(str, silent[:8])) + (" ..." if len(silent) > 8 else "")
                if QMessageBox.question(self, "Silent lines", f"{len(silent)} of {len(self.cues)} line(s) have no voice and will be "
                                        f"SILENT (lines {eg}).\nExport anyway?") != QMessageBox.Yes:
                    return
        if mode in (2, 3) and sub == "burn" and not font:
            self.log.appendPlainText("Note: burn-in Khmer needs a Khmer font file set in Settings; otherwise text may show as boxes.")
        self._run(job, done=lambda m: ([self.log.appendPlainText(x) for x in m], then() if then else None), on_fail=on_fail)

    def _create_controls(self):
        ctx = self.ctx
        self.vpath = QLineEdit(); self.vpath.setReadOnly(True); self.vpath.setPlaceholderText("No video imported yet")
        self.opath = QLineEdit(); self.opath.setReadOnly(True)
        self.vmeta = QLabel(""); self.vmeta.setObjectName("muted")

        self.method = QComboBox()
        for label, code in METHODS:
            self.method.addItem(label, code)
        i = self.method.findData(ctx.settings.get("scan_method")); self.method.setCurrentIndex(max(0, i))
        self.asr_hint = QLineEdit(); self.asr_hint.setText(str(ctx.settings.get("asr_hint") or ""))
        self.asr_hint.setPlaceholderText("e.g. 李明, 王芳, 北京  (optional)")
        self.asr_hint.setToolTip("Character names / places / terms as they are SPOKEN. Whisper uses them to spell names correctly.\n"
                                 "Names found by the story analysis are added automatically on the next scan.")
        self.asr_hint.editingFinished.connect(lambda: self._save_key("asr_hint", self.asr_hint.text().strip()))
        self.lang = QComboBox()
        for label, code in LANGS:
            self.lang.addItem(label, code)
        i = self.lang.findData(ctx.settings.get("scan_lang")); self.lang.setCurrentIndex(max(0, i))
        self.model = QComboBox(); self.model.addItems(MODELS); self.model.setCurrentText(ctx.settings.get("whisper_model"))
        self.model.setToolTip("Whisper accuracy: tiny/base = fast but weak for Chinese/Japanese; small/medium = much better (slower).")
        self.device = QComboBox(); self.device.addItems(["cpu", "cuda"])
        self.engine = QComboBox(); self.engine.addItems(ENGINES)
        _EP = {"gemini": 0, "nllb": 1, "claude": 2, "ollama": 3}                  # provider per task: the translation engine is remembered
        self.engine.setCurrentIndex(_EP.get(ctx.settings.get("translate_provider"), 0))
        self.engine.currentIndexChanged.connect(lambda i: self._save_key("translate_provider", {v: k for k, v in _EP.items()}.get(i, "gemini")))
        self.voice = QComboBox()
        for k, val in VOICES.items():
            self.voice.addItem(k, val)
        i = self.voice.findData(ctx.settings.get("tts_voice"))
        self.voice.setCurrentIndex(max(0, i))
        self.speed = QDoubleSpinBox(); self.speed.setRange(0.5, 2.0); self.speed.setSingleStep(0.1); self.speed.setValue(1.0)
        self.match_rate = QCheckBox("Match original speed")
        self.match_rate.setChecked(bool(ctx.settings.get("match_rate")))
        self.match_rate.setToolTip("After the voice is generated, each line is time-stretched to the length of the original speech\n"
                                   "(pitch kept, limited to 0.85x-1.4x). Lines of one speaker keep a similar pace. OFF = old behaviour.")
        self.match_rate.toggled.connect(lambda v: self._save_key("match_rate", bool(v)))
        self.method.currentIndexChanged.connect(lambda _: self._save_key("scan_method", self.method.currentData()))
        self.lang.currentIndexChanged.connect(lambda _: self._save_key("scan_lang", self.lang.currentData()))
        self.model.currentTextChanged.connect(lambda t: self._save_key("whisper_model", t))

        self.emo_min = QSpinBox(); self.emo_min.setRange(30, 95); self.emo_min.setSuffix(" %")
        self.emo_min.setToolTip("Smart Script writes an emotion (angry, sad...) only when the AI is at least this sure.\n"
                                "Below it the line stays 'normal' with a light note on how it sounds (soft voice, speaking fast...).")
        self._persist(self.emo_min, "emotion_min_pct")
        self.evt_min = QSpinBox(); self.evt_min.setRange(20, 95); self.evt_min.setSuffix(" %")
        self.evt_min.setToolTip("Same for screaming / crying / shouting (these scores are naturally lower, so 50% is already strong).")
        self._persist(self.evt_min, "event_min_pct")
        self.split_sil = QDoubleSpinBox(); self.split_sil.setRange(0.0, 10.0); self.split_sil.setSingleStep(0.1); self.split_sil.setDecimals(1)
        self.split_sil.setSuffix(" s"); self.split_sil.setSpecialValueText("off")
        self.split_sil.setToolTip("The SAME character stays silent at least this long -> a new line (new time) starts when they speak again.\n"
                                  "Keeps the generated voice in step with the picture. 'off' = only a speaker/voice change starts a line.")
        self._persist(self.split_sil, "split_silence_s")
        self.motion_on = QCheckBox("Character movement"); self.motion_on.setChecked(bool(ctx.settings.get("smart_motion")))
        self.motion_on.setToolTip("Smart Script also looks at the video picture: face on screen, mouth moving, big/small movement...")
        self.motion_on.toggled.connect(lambda on: self._save_key("smart_motion", bool(on)))
        self.story_on = QCheckBox("Story info"); self.story_on.setChecked(bool(ctx.settings.get("smart_story")))
        self.story_on.setToolTip("Adds a header to the script: story title, short summary and character names.\n"
                                 "With a Gemini key (Settings) the AI writes it (a short part of the script is sent to Gemini);\n"
                                 "without a key only the facts are written (speakers, voices) and nothing is invented.")
        self.story_on.toggled.connect(lambda on: self._save_key("smart_story", bool(on)))
        self.ocr_fix_on = QCheckBox("Fix text with OCR"); self.ocr_fix_on.setChecked(bool(ctx.settings.get("smart_ocr_fix")))
        self.ocr_fix_on.setToolTip("If the video has burned-in subtitles (Chinese/English/Japanese/Korean/Thai/Vietnamese), their text corrects Whisper's words\n"
                                   "ONLY when it is similar to what was heard. Timing, speaker and emotion always stay from the audio. Slower scan.")
        self.ocr_fix_on.toggled.connect(lambda on: self._save_key("smart_ocr_fix", bool(on)))

        # ---- cue table (source of truth for editing) ----
        self.table = QTableWidget(0, len(HEAD))
        self.table.setHorizontalHeaderLabels(HEAD)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)   # 11 columns: fixed widths + horizontal scroll,
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Interactive)   # so the two text columns are never squeezed
        for k, w in ((0, 50), (1, 115), (2, 115), (3, 260), (4, 260), (5, 160), (6, 60), (7, 85), (8, 190), (9, 100), (10, 220), (11, 230)):
            self.table.setColumnWidth(k, w)
        self.table.setStyleSheet("font-size:13px"); self.table.verticalHeader().setDefaultSectionSize(30); self.table.setMinimumHeight(120)
        self.table.horizontalHeader().setFixedHeight(32)      # Figma: header 32, row 30, alt rows bg/raised
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._edited)
        self.table.itemSelectionChanged.connect(self._on_sel)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.table.setItemDelegateForColumn(5, VoiceDelegate(self.table, extra=lambda: list(self.voices)))

        # ---- export controls ----
        self.export_mode = QComboBox(); self.export_mode.addItems(EXPORTS)
        self.export_mode.currentIndexChanged.connect(lambda _: self._update_summary())
        self.voice_vol = None; self.bg_vol = None
        self.keep_bg = QCheckBox("Keep music/SFX (Demucs)"); self.keep_bg.setChecked(True)
        self.voice_off = QDoubleSpinBox(); self.voice_off.setRange(-5.0, 5.0); self.voice_off.setSingleStep(0.1); self.voice_off.setDecimals(2)
        self.voice_off.setSuffix(" s"); self.voice_off.setToolTip("Seconds added to the start of every Khmer voice clip in the video (+0.5 = 0.5 s later, negative = earlier)")
        self._persist(self.voice_off, "voice_offset")
        self.sub_mode = QComboBox(); self.sub_mode.addItems(["Soft subtitles (track)", "Burn-in subtitles"])
        self.sub_mode.currentIndexChanged.connect(lambda _: self._update_summary())

        self.bar = QProgressBar(); self.bar.setFixedHeight(14)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMinimumHeight(60); self.log.setMaximumHeight(130)

    # ---------- panels ----------

    def _header(self):
        f = QFrame(); f.setObjectName("topbar")
        g = QGridLayout(f); g.setContentsMargins(12, 8, 12, 8); g.setHorizontalSpacing(8); g.setVerticalSpacing(6)
        g.addWidget(QLabel("Video:"), 0, 0); g.addWidget(self.vpath, 0, 1); g.addWidget(self.vmeta, 0, 2)
        b = QPushButton("Import Video"); b.setObjectName("primary"); b.clicked.connect(self.pick_video); g.addWidget(b, 0, 3)
        b = QPushButton("Play Video"); b.setToolTip("Open the source video in your default player")
        b.clicked.connect(lambda: self.video and open_path(self.video)); g.addWidget(b, 0, 4)
        g.addWidget(QLabel("Output:"), 1, 0); g.addWidget(self.opath, 1, 1, 1, 2)
        for k, (t, fn) in enumerate((("Choose Output", self.pick_output), ("Use Video Folder", self.reset_output), ("Open Output", self.open_output))):
            b = QPushButton(t); b.clicked.connect(fn); g.addWidget(b, 1, 3 + k)
        g.setColumnStretch(1, 1)
        return f

    def _scroll(self, body):
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.setWidget(body)
        return sc

    def _build_left(self):
        body = QWidget(); body.setObjectName("plain")
        bl = QVBoxLayout(body); bl.setContentsMargins(0, 0, 6, 0); bl.setSpacing(8)

        # 0 · Media: the single place to import a source video and manage output.
        self.card_media, ml = card("")
        import_row = QHBoxLayout()
        self.media_name = QLabel("No video selected")
        self.media_name.setObjectName("cardTitle")
        self.media_name.setWordWrap(True)
        import_row.addWidget(self.media_name, 1)
        import_btn = QPushButton("＋ Import Video")
        import_btn.setObjectName("primary")
        import_btn.clicked.connect(self.pick_video)
        import_row.addWidget(import_btn)
        ml.addLayout(import_row)
        ml.addWidget(self.vmeta)
        auto_btn = QPushButton("⚡  Auto Dub  (one click)")
        auto_btn.setObjectName("primary"); auto_btn.setMinimumHeight(40)
        auto_btn.setToolTip("Smart Script → Translate → Voice → Quality check → Export, using the same steps and settings as the buttons below.\n"
                            "Steps already done are skipped, so you can stop and run it again.")
        auto_btn.clicked.connect(lambda: self.auto_dub_clicked())
        ml.addWidget(auto_btn)
        batch_btn = QPushButton("⚡⚡  Batch Auto Dub  (many videos)")
        batch_btn.setToolTip("Choose several videos; each one goes through Auto Dub in turn with no questions. A failure only skips that video.")
        batch_btn.clicked.connect(lambda: self.batch_clicked())
        ml.addWidget(batch_btn)
        hook_btn = QPushButton("🎣  Hook Finder  (best 4-8 s moments)")
        hook_btn.setToolTip("AI finds the strongest hook moments of the film; export them as MP3, video + voice, with Khmer subtitle, or as an intro in front of the film.")
        hook_btn.clicked.connect(lambda: self.hook_clicked())
        ml.addWidget(hook_btn)
        output_box = QWidget(); output_box.setObjectName("plain")
        og = QGridLayout(output_box); og.setContentsMargins(0, 0, 0, 0); og.setHorizontalSpacing(6); og.setVerticalSpacing(5)
        og.addWidget(QLabel("Output folder"), 0, 0, 1, 2)
        self.opath.setReadOnly(True)
        og.addWidget(self.opath, 1, 0, 1, 2)
        choose_out = QPushButton("Choose")
        choose_out.clicked.connect(self.pick_output)
        reset_out = QPushButton("Default")
        reset_out.clicked.connect(self.reset_output)
        open_out = QPushButton("Open")
        open_out.clicked.connect(self.open_output)
        og.addWidget(choose_out, 2, 0)
        og.addWidget(reset_out, 2, 1)
        og.addWidget(open_out, 3, 0, 1, 2)
        ml.addWidget(Collapsible("Output location", output_box))
        self.boxes = {}
        self.boxes["media"] = SectionBox("Media", self.card_media, "Import video · output folder", "▣", True); bl.addWidget(self.boxes["media"])

        # 1 · Extract script
        self.card_extract, l = card("")
        g = QGridLayout(); g.setHorizontalSpacing(8); g.setVerticalSpacing(6)
        for n, (lbl, wd) in enumerate((("Text source", self.method), ("Spoken language", self.lang), ("Accuracy", self.model), ("Device", self.device), ("Names (hint)", self.asr_hint))):
            g.addWidget(QLabel(lbl), n, 0); g.addWidget(wd, n, 1)
        g.setColumnStretch(1, 1)
        l.addLayout(g)
        for t, fn, prim in (("A. Video → Script file", lambda: self.extract(True), False),
                            ("A2. Video → Smart Script", self.smart_scan, True),
                            ("B. Script → Voice", self.script_to_voice, False)):
            b = QPushButton(t); b.clicked.connect(fn)
            if prim:
                b.setObjectName("primary")
            l.addWidget(b)
        sg = QGridLayout(); sg.setHorizontalSpacing(6); sg.setVerticalSpacing(6)   # edit / import / export lines (was above the table)
        for n, (t, fn) in enumerate((("Add Line", self.add_line), ("Delete Line(s)", self.del_lines), ("Import SRT/VTT", self.import_sub),
                                     ("Export SRT/VTT", self.export_sub), ("Export Smart Script", self.export_smart_script),
                                     ("Export SRT/VTT + names", self.export_sub_rich))):
            b = QPushButton(t); b.clicked.connect(fn); sg.addWidget(b, n // 2, n % 2)
        l.addLayout(sg)
        adv = QWidget(); adv.setObjectName("plain"); al = QVBoxLayout(adv); al.setContentsMargins(0, 0, 0, 0); al.setSpacing(6)
        r = QHBoxLayout(); r.addWidget(QLabel("New line after silence ≥")); r.addWidget(self.split_sil); al.addLayout(r)
        for cb in (self.motion_on, self.story_on, self.ocr_fix_on):
            al.addWidget(cb)
        l.addWidget(Collapsible("Advanced scan settings", adv))
        self.boxes["script"] = SectionBox("Script", self.card_extract, "Extract, edit, import/export lines", "▤"); bl.addWidget(self.boxes["script"])

        # 2 · Translate
        self.card_translate, l = card("")
        self.from_lbl = QLabel(); self.from_lbl.setObjectName("cardTitle")
        self.lang.currentIndexChanged.connect(lambda _: self.from_lbl.setText(self.lang.currentText()))
        self.from_lbl.setText(self.lang.currentText())
        to = QLabel("ខ្មែរ (Khmer)"); to.setObjectName("cardTitle")
        g = QGridLayout(); g.setHorizontalSpacing(8); g.setVerticalSpacing(6)
        g.addWidget(QLabel("From"), 0, 0); g.addWidget(self.from_lbl, 0, 1)
        g.addWidget(QLabel("To"), 1, 0); g.addWidget(to, 1, 1)
        g.addWidget(QLabel("Engine"), 2, 0); g.addWidget(self.engine, 2, 1)
        g.setColumnStretch(1, 1)
        l.addLayout(g)
        b = QPushButton("✦  Start Translate"); b.setObjectName("primary"); b.setMinimumHeight(40)
        b.setToolTip("Translate the script rows into Khmer (timing, speaker and voice assignments are kept)")
        b.clicked.connect(lambda: self.translate()); l.addWidget(b)
        hint = QLabel("“B. Script → Voice” imports a ready Khmer script and does not translate or generate audio.")
        hint.setObjectName("muted"); hint.setWordWrap(True); l.addWidget(hint)
        self.boxes["translate"] = SectionBox("Translate", self.card_translate, "Original → Khmer", "✦"); bl.addWidget(self.boxes["translate"])
        self.card_speakers, l = card("")
        b = QPushButton("Speaker Manager..."); b.setToolTip("Rename, merge, split, lock speakers; choose one voice per character")
        b.clicked.connect(self.open_speakers); l.addWidget(b)
        b = QPushButton("AI Providers...  (Gemini · Claude · Hugging Face · Ollama)"); b.setToolTip("Keys, models, connection tests, speaker-detection method")
        b.clicked.connect(self.open_providers); l.addWidget(b)
        b = QPushButton("Review OCR vs speech differences..."); b.setToolTip("Rows where the burned-in subtitle differs from what Whisper heard: you decide")
        b.clicked.connect(self.open_ocr_review); l.addWidget(b)
        b = QPushButton("System Status (AI environment)..."); b.setToolTip("What is installed, connected, downloaded and tested - separately for every component")
        b.clicked.connect(self.open_status); l.addWidget(b)
        hint = QLabel("Right-click a row: generate / preview / delete audio, change speaker, voice or speed.")
        hint.setObjectName("muted"); hint.setWordWrap(True); l.addWidget(hint)
        self.boxes["speakers"] = SectionBox("Speakers & AI", self.card_speakers, "Speaker Manager · providers", "☺"); bl.addWidget(self.boxes["speakers"])

        # 3 · Generate voice
        self.card_voice, l = card("")
        cw = QWidget(); cw.setObjectName("plain"); cw.setLayout(self._clone_engine_row()); l.addWidget(cw)
        b = QPushButton("Generate Voice (all)"); b.setObjectName("primary"); b.clicked.connect(self.gen_all); l.addWidget(b)
        for t, fn in (("Regenerate Selected", self.gen_selected), ("Play Selected Voice", self.play_selected)):
            b = QPushButton(t); b.clicked.connect(fn); l.addWidget(b)
        self._studio_voice_buttons(l)
        hint = QLabel("Choose voices, speed and emotion in the sections below.")
        hint.setObjectName("muted"); hint.setWordWrap(True); l.addWidget(hint)
        for t, w in (("Voice type", self._voice_tab()), ("Speed", self._speed_tab()), ("Emotion", self._emotion_tab())):
            l.addWidget(Collapsible(t, w))     # former right-panel tabs, now inside the Voice box
        self.boxes["voice"] = SectionBox("Voice", self.card_voice, "Generate, clone and preview", "♫"); bl.addWidget(self.boxes["voice"])
        self.boxes["style"] = SectionBox("Style", self._style_tab(), "Blur · Subtitle · Logo · Track", "◈"); bl.addWidget(self.boxes["style"])
        self._studio_make_canvas()      # creates canvas, Tools panel and inspector (once)
        self.boxes["tools"] = SectionBox("Tools", self.tools_panel, "Select · Blur · Logo · Text · Zoom", "✎"); bl.addWidget(self.boxes["tools"])
        self.boxes["export"] = SectionBox("Export", self._build_export(), "Mix and render final output", "⇧"); bl.addWidget(self.boxes["export"])
        # Accordion: opening a section (header click or rail button) closes the one that was open. Same SectionBoxes,
        # no copies - the group only listens to the existing header buttons.
        self.accordion = AccordionGroup(list(self.boxes.values()))
        bl.addStretch()
        self.left_scroll = self._scroll(body)
        self.left_scroll.setMinimumWidth(340)
        return self.left_scroll

    def _build_preview(self):
        f = QFrame(); f.setObjectName("preview")
        lay = QVBoxLayout(f); lay.setContentsMargins(6, 6, 6, 6); lay.setSpacing(6)
        self.pv_stack = QStackedWidget(); self.pv_stack.setObjectName("plain")
        empty = QWidget(); empty.setObjectName("plain")
        el = QVBoxLayout(empty); el.addStretch()
        self.pv_title = QLabel("Import a video to start"); self.pv_title.setObjectName("emptyTitle"); self.pv_title.setAlignment(Qt.AlignCenter)
        self.pv_note = QLabel("MP4 · MKV · MOV · AVI · WEBM · FLV · M4V"); self.pv_note.setObjectName("muted"); self.pv_note.setAlignment(Qt.AlignCenter)
        self.pv_note.setWordWrap(True)
        el.addWidget(self.pv_title); el.addWidget(self.pv_note); el.addStretch()
        self.pv_stack.addWidget(empty)
        # on-screen editor: the video AND every object (logo, watermark, Khmer subtitle, text, blur) live in one canvas
        self._studio_make_canvas()
        stage = QWidget(); stage.setObjectName("plain")
        sl_ = QVBoxLayout(stage); sl_.setContentsMargins(0, 0, 0, 0); sl_.setSpacing(0)
        sl_.addWidget(self.canvas, 1)                   # the video fills the whole stage
        self.pv_stack.addWidget(stage)
        try:    # embedded preview of the SOURCE video; if QtMultimedia is unavailable the page still works (Play Video opens it)
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            self._QMP = QMediaPlayer
            self.vplayer = QMediaPlayer(self)
            self.vaudio = QAudioOutput(self); self.vaudio.setVolume(0.8)
            self.vplayer.setAudioOutput(self.vaudio)
            if self.canvas.video_item is None:
                raise RuntimeError("no video item")
            self.vplayer.setVideoOutput(self.canvas.video_item)
            self.vplayer.positionChanged.connect(self._on_vpos)
            self.vplayer.durationChanged.connect(self._on_vdur)
            self.vplayer.playbackStateChanged.connect(lambda _s: self._sync_play_btn())
            self.vplayer.errorOccurred.connect(lambda _e, msg: self.log.appendPlainText("Preview: " + msg))
        except Exception:  # noqa
            self.vplayer = None
        lay.addWidget(self.pv_stack, 1)

        self.sub_orig = QLabel(""); self.sub_kh = QLabel("")
        for lb in (self.sub_orig, self.sub_kh):     # the two text strips under the video were removed from the layout (the video takes their space);
            lb.setObjectName("subLine"); lb.setAlignment(Qt.AlignCenter); lb.setWordWrap(True)
            lb.setParent(f); lb.hide()               # the labels still exist so the playback code can keep setting their text

        # Playback controls: ONE row when there is room, two rows when the preview is narrow (never overlap / squeezed).
        self.tc = QLabel("00:00.00 / 00:00.00"); self.tc.setObjectName("timecode")
        self.tc.ensurePolished(); self.tc.setMinimumWidth(self.tc.sizeHint().width() + 4)      # fixed room for the time text: the bar does not jitter while playing
        self.btn_prev = QPushButton("↺ 10s"); self.btn_play = QPushButton("▶"); self.btn_next = QPushButton("10s ↻")
        self.btn_stop = QPushButton("■"); self.btn_stop.setToolTip("Stop: video and all audio tracks stop, back to the start")
        self.btn_stop.setFixedWidth(40); self.btn_stop.clicked.connect(lambda: self._pb_stop() if self.vplayer else None)
        self.btn_prev.setToolTip("Back 10 seconds"); self.btn_next.setToolTip("Forward 10 seconds")
        for b, fn, w_ in ((self.btn_prev, lambda: self._skip(-10), 62), (self.btn_play, self._toggle_play, 44), (self.btn_next, lambda: self._skip(10), 62)):
            b.setFixedWidth(w_); b.clicked.connect(fn)
        self.seek = QSlider(Qt.Horizontal); self.seek.setRange(0, 1000)
        self.seek.sliderMoved.connect(lambda v: self._seek(v / 1000.0 * self.vid_dur))
        pb = self._prev_btn(); pb.setText("Preview Effects"); pb.setToolTip("Render one frame with the same blur / subtitle / logo filters the export will use")
        self.transport = TransportBar(self.tc, self.btn_prev, self.btn_play, self.btn_next, self.seek, pb, stop=self.btn_stop)
        lay.addWidget(self.transport)
        for b in (self.btn_prev, self.btn_play, self.btn_stop, self.btn_next, self.seek):
            b.setEnabled(self.vplayer is not None)
        return f

    def _voice_tab(self):
        w = QWidget(); w.setObjectName("plain")
        l = QVBoxLayout(w); l.setContentsMargins(4, 8, 8, 8); l.setSpacing(8)
        t = QLabel("Voice Type"); t.setObjectName("cardTitle"); l.addWidget(t)
        self.sel_info = QLabel("Select a row in the table to see its voice."); self.sel_info.setObjectName("muted"); self.sel_info.setWordWrap(True)
        l.addWidget(self.sel_info)
        r = QHBoxLayout()
        for vt in ("Male", "Female"):
            b = QPushButton(vt); b.setObjectName("chip"); b.setToolTip(f"Use the default {vt} voice on the selected rows (all rows if none selected)")
            b.clicked.connect(lambda _=False, vt=vt: self._use_voice(vt)); r.addWidget(b)
        r.addStretch(); l.addLayout(r)
        l.addWidget(self._voice_panel())
        b = QPushButton("＋  ADD VOICE TYPE"); b.clicked.connect(self.add_voice_type); l.addWidget(b)
        b = QPushButton("▶  Preview Voice"); b.setObjectName("primary"); b.clicked.connect(self.play_selected)
        b.setToolTip("Plays the generated voice of the selected row(s), in order"); l.addWidget(b)
        n = QLabel("Clicking a voice applies it to the selected rows; their voice is regenerated with Generate Voice.")
        n.setObjectName("muted"); n.setWordWrap(True); l.addWidget(n)
        l.addStretch()
        return w

    def _speed_tab(self):
        w = QWidget(); w.setObjectName("plain")
        l = QVBoxLayout(w); l.setContentsMargins(4, 8, 8, 8); l.setSpacing(8)
        t = QLabel("Speech speed"); t.setObjectName("cardTitle"); l.addWidget(t)
        r = QHBoxLayout()
        sl = QSlider(Qt.Horizontal); sl.setRange(5, 20); sl.setValue(int(round(self.speed.value() * 10)))
        sl.valueChanged.connect(lambda v: self.speed.setValue(v / 10.0))
        self.speed.valueChanged.connect(lambda v: (sl.blockSignals(True), sl.setValue(int(round(v * 10))), sl.blockSignals(False)))
        r.addWidget(sl, 1); r.addWidget(self.speed); l.addLayout(r)
        n = QLabel("Applies to every line the next time voice is generated (0.5× – 2.0×).")
        n.setObjectName("muted"); n.setWordWrap(True); l.addWidget(n)
        l.addWidget(self.match_rate)
        t = QLabel("Default Khmer voice"); t.setObjectName("cardTitle"); l.addWidget(t)
        l.addWidget(self.voice)
        n = QLabel("Used for lines whose Voice Type is empty.")
        n.setObjectName("muted"); n.setWordWrap(True); l.addWidget(n)
        l.addStretch()
        return w

    def _emotion_tab(self):
        w = QWidget(); w.setObjectName("plain")
        l = QVBoxLayout(w); l.setContentsMargins(4, 8, 8, 8); l.setSpacing(8)
        t = QLabel("Emotion of the selected row"); t.setObjectName("cardTitle"); l.addWidget(t)
        self.emo_edit = QLineEdit(); self.emo_edit.setPlaceholderText("e.g. angry, a bit screaming → sad")
        self.emo_edit.editingFinished.connect(self._emo_edit_done); l.addWidget(self.emo_edit)
        n = QLabel("Short text kept in script files. Editing it updates Emotion Raw below and clears that row's voice so it can be regenerated.")
        n.setObjectName("muted"); n.setWordWrap(True); l.addWidget(n)
        l.addLayout(self._emotion_raw_panel())
        t = QLabel("Smart Script detection"); t.setObjectName("cardTitle"); l.addWidget(t)
        g = QGridLayout(); g.setHorizontalSpacing(8)
        g.addWidget(QLabel("Emotion ≥"), 0, 0); g.addWidget(self.emo_min, 0, 1)
        g.addWidget(QLabel("Scream / cry ≥"), 1, 0); g.addWidget(self.evt_min, 1, 1)
        g.setColumnStretch(1, 1); l.addLayout(g)
        l.addStretch()
        return w

    # ---------- Emotion Raw (structured, per line, never inside the Khmer text) ----------
    def _emotion_raw_panel(self):
        v = QVBoxLayout(); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(6)
        t = QLabel("Emotion Raw (selected row)"); t.setObjectName("cardTitle"); v.addWidget(t)
        g = QGridLayout(); g.setHorizontalSpacing(8); g.setVerticalSpacing(4)
        self.raw_w = {}
        for r, (key, label, opts) in enumerate((("label", "Emotion", ER.LABELS), ("intensity", "Intensity", ER.INTENSITIES),
                                                 ("speed", "Speaking speed", ER.SPEEDS), ("pitch", "Pitch", ER.PITCHES),
                                                 ("loudness", "Loudness", ER.LOUDNESS))):
            cb = QComboBox(); cb.addItems(opts); self.raw_w[key] = cb
            g.addWidget(QLabel(label), r, 0); g.addWidget(cb, r, 1)
        for r, (key, label) in enumerate((("pause_behavior", "Pause behavior"), ("emphasis", "Word emphasis"),
                                          ("delivery_style", "Delivery style")), 5):
            e = QLineEdit(); self.raw_w[key] = e
            g.addWidget(QLabel(label), r, 0); g.addWidget(e, r, 1)
        g.setColumnStretch(1, 1); v.addLayout(g)
        self.raw_w["label"].currentTextChanged.connect(self._raw_label_changed)
        self.raw_conf = QLabel(""); self.raw_conf.setObjectName("muted"); v.addWidget(self.raw_conf)
        self.raw_instr = QLabel(""); self.raw_instr.setObjectName("muted"); self.raw_instr.setWordWrap(True); v.addWidget(self.raw_instr)
        self.raw_limit = QLabel(""); self.raw_limit.setWordWrap(True); v.addWidget(self.raw_limit)
        row = QHBoxLayout()
        for text, fn, tip in (("Apply", self._raw_apply, "Save these values for this row (marks it as edited by hand: automatic analysis will not overwrite it)."),
                              ("Re-analyse", self._raw_reanalyse, "Run the emotion AI again on THIS row's original audio and replace Emotion Raw (also overwrites hand edits)."),
                              ("▶ Emotional", lambda: self._raw_preview(True), "Hear this row with its Emotion Raw. Nothing is saved."),
                              ("▶ Normal", lambda: self._raw_preview(False), "Hear the same row as plain Normal speech, to compare. Nothing is saved."),
                              ("Regenerate row", self._raw_regen, "Generate the voice again for this row only.")):
            b = QPushButton(text); b.setToolTip(tip); b.clicked.connect(fn); row.addWidget(b)
        v.addLayout(row)
        self.emo_tts_on = QCheckBox("Apply emotion to default / preset voices (speed, pitch, volume only)")
        self.emo_tts_on.setChecked(self.ctx.settings.get("emotion_tts_prosody") is not False)
        self.emo_tts_on.toggled.connect(lambda on: self._save_key("emotion_tts_prosody", bool(on)))
        self.emo_tts_on.setToolTip("edge-tts Khmer voices have NO emotion styles. This only nudges speed / pitch / volume a little (the voice stays the same).\n"
                                   "Real emotional delivery needs a cloned voice. Off = default/preset voices ignore the emotion, as before.")
        v.addWidget(self.emo_tts_on)
        return v

    def _raw_load(self, c):
        r = ER.of_cue(c) if c is not None else ER.make()
        self._raw_loading = True
        for k in ("label", "intensity", "speed", "pitch", "loudness"):
            self.raw_w[k].setCurrentText(getattr(r, k))
        for k in ("pause_behavior", "emphasis", "delivery_style"):
            if not self.raw_w[k].hasFocus():
                self.raw_w[k].setText(getattr(r, k))
        self._raw_loading = False
        self.raw_conf.setText(f"Confidence {int(r.confidence * 100)}% · " + ("edited by hand" if r.source == "manual" else "AI / automatic")
                              if c is not None else "")
        self.raw_instr.setText(r.tts_instruction() if c is not None else "")
        vt = (c.voice_type if c is not None else "") or ""
        lim = TP.limitation("clone" if VC.is_clone_name(vt) else "edge", r) if c is not None else ""
        self.raw_limit.setText(("⚠ " + lim) if lim else "")

    def _raw_label_changed(self, text):
        """A new emotion starts from ITS default performance (speed, pitch, loudness, pauses...), so the old emotion's values
        are never carried over as if they were hand edits. Intensity is kept; every field stays editable afterwards."""
        if getattr(self, "_raw_loading", False):
            return
        d = ER.make(text, self.raw_w["intensity"].currentText(), 1.0)
        self._raw_loading = True
        for k in ("speed", "pitch", "loudness"):
            self.raw_w[k].setCurrentText(getattr(d, k))
        for k in ("pause_behavior", "emphasis", "delivery_style"):
            self.raw_w[k].setText(getattr(d, k))
        self._raw_loading = False

    def _raw_from_widgets(self, c):
        old = ER.of_cue(c)
        w = self.raw_w
        r = ER.make(w["label"].currentText(), w["intensity"].currentText(), 1.0 if w["label"].currentText() != "Normal" else 0.0,
                    speed=w["speed"].currentText(), pitch=w["pitch"].currentText(), loudness=w["loudness"].currentText(),
                    pause_behavior=w["pause_behavior"].text().strip(), emphasis=w["emphasis"].text().strip(),
                    delivery_style=w["delivery_style"].text().strip())
        r.confidence = old.confidence if r.label == old.label else 1.0 if r.label != "Normal" else 0.0
        r.vocal_performance = r.instruction() or "normal speech"
        return r

    def _raw_apply(self):
        r = self.table.currentRow()
        if not (0 <= r < len(self.cues)):
            return
        c = self.cues[r]
        if ER.set_on_cue(c, self._raw_from_widgets(c), manual=True):
            c.audio = ""                                    # emotion changed -> the voice must be generated again
            self.log.appendPlainText(f"Row {r + 1}: Emotion Raw saved ({ER.of_cue(c).label}). Press 'Regenerate row' to hear it.")
        self.refresh()

    def _raw_preview(self, emotional):
        if not self._need_video():
            return
        r = self.table.currentRow()
        if not (0 <= r < len(self.cues)) or not self.cues[r].translation.strip():
            self.log.appendPlainText("Select a row that has a Khmer translation first.")
            return
        c = self.cues[r]
        raw = self._raw_from_widgets(c) if emotional else ER.make()
        vt = c.voice_type or GENDER_VT.get(c.gender, "Male")
        voice, speed, d, tag = self.voice.currentData(), self.speed.value(), self._tts_dir(), "_emo" if emotional else "_norm"

        def job(progress=None, cancel=None):
            return self._synth_any(r, c, vt, d, speed, voice, progress, cancel, raw=raw, tag=tag)

        def done(path):
            self.log.appendPlainText(f"Preview ({'emotional' if emotional else 'normal'}) row {r + 1}: {Path(path).name}")
            self._play_file(path)
        self._run(job, done=done)

    def _play_file(self, path):
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            if not self.player:
                self.player = QMediaPlayer(self)
                self.audio_out = QAudioOutput(self)
                self.player.setAudioOutput(self.audio_out)
                self.player.mediaStatusChanged.connect(self._play_status)
            self._queue = []
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            self.player.play()
        except Exception:
            open_path(str(path))

    def _raw_regen(self):
        r = self.table.currentRow()
        if not (0 <= r < len(self.cues)) or not self._need_video():
            return
        self._raw_apply()
        self.cues[r].audio = ""
        self._run(self._gen, [r], done=self._gen_done)

    def _raw_reanalyse(self):
        """Emotion AI again on THIS row's original audio (needs the video's 16 kHz audio from a Smart Script scan)."""
        if not self._need_video():
            return
        r = self.table.currentRow()
        if not (0 <= r < len(self.cues)):
            return
        stem, dev = Path(self.video).stem, self.device.currentText()
        cues = [replace(x) for x in self.cues]
        emo_min, evt_min = self.emo_min.value() / 100.0, self.evt_min.value() / 100.0

        def job(progress=None, cancel=None):
            import librosa
            from ..translator.emotion_detect import analyze_windows
            wav = work_dir(stem) / "audio16k.wav"
            if not wav.exists():
                from ..translator.audio_extractor import extract_audio
                extract_audio(self.video, wav, cancel=cancel)
            y, _ = librosa.load(str(wav), sr=16000, mono=True)
            c = cues[r]
            spans, notes = analyze_windows(y, [(c.start, c.end, c.speaker or "Speaker")], dev, cancel=cancel)
            raw = ER.from_span((spans.get(c.speaker or "Speaker") or [None])[0], emo_min, evt_min)
            c.emotion_raw = raw.to_json(); c.emotion = ER.legacy_string(raw)
            ER.refine_with_audio(cues, y, 16000)               # speed / loudness vs this character's own normal
            return ER.of_cue(c), notes

        def done(res):
            raw, notes = res
            c = self.cues[r]
            raw.source = "auto"
            ER.set_on_cue(c, raw); c.audio = ""
            self.log.appendPlainText(f"Row {r + 1}: re-analysed → {raw.label} ({int(raw.confidence * 100)}%)" + ("  " + " | ".join(notes) if notes else ""))
            self.refresh()
        self._run(job, done=done)

    def _style_tab(self):
        w = QWidget(); w.setObjectName("plain")
        l = QVBoxLayout(w); l.setContentsMargins(4, 8, 8, 8); l.setSpacing(8)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._blur_tab(), "Blur")
        self.tabs.addTab(self._sub_tab(), "Subtitle")
        self.tabs.addTab(self._logo_tab(), "Logo")
        self.tabs.addTab(self._track_tab(), "Track")
        l.addWidget(self.tabs)
        n = QLabel("Blur hides the original burned-in subtitles; Subtitle styles the Khmer subtitle. "
                   "Preview Effects renders the same filters the export uses.")
        n.setObjectName("muted"); n.setWordWrap(True); l.addWidget(n)
        l.addStretch()
        return w

    def _build_editor(self):
        w = QWidget(); w.setObjectName("plain")
        l = QVBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(6)
        tl, tabs = self._studio_make_timeline()       # CapCut-style timeline + (dialogue table | audio mixer)
        sp = QSplitter(Qt.Vertical); sp.setChildrenCollapsible(False); self.editor_split = sp
        sp.addWidget(tl); sp.addWidget(tabs)
        sp.setStretchFactor(0, 3); sp.setStretchFactor(1, 2)
        l.addWidget(sp, 1)
        return w

    def _build_export(self):
        f, l = card("")
        self.exp_status = QLabel(""); self.exp_status.setObjectName("cardTitle"); self.exp_status.setWordWrap(True); l.addWidget(self.exp_status)
        self.exp_sum = QLabel(""); self.exp_sum.setObjectName("muted"); self.exp_sum.setWordWrap(True); l.addWidget(self.exp_sum)
        l.addWidget(self.export_mode)
        l.addWidget(self.sub_mode)
        mix = QWidget(); mix.setObjectName("plain"); ml = QGridLayout(mix); ml.setContentsMargins(0, 0, 0, 0); ml.setVerticalSpacing(6)
        ml.addWidget(QLabel("Khmer voice volume"), 0, 0, 1, 2)
        r = QHBoxLayout(); self.voice_vol = self._vol_slider(r); ml.addLayout(r, 1, 0, 1, 2)
        ml.addWidget(self.keep_bg, 2, 0, 1, 2)
        r = QHBoxLayout(); self.bg_vol = self._vol_slider(r); ml.addLayout(r, 3, 0, 1, 2)
        self.keep_bg.toggled.connect(self.bg_vol.setEnabled)
        ml.addWidget(QLabel("Voice offset"), 4, 0); ml.addWidget(self.voice_off, 4, 1)
        l.addWidget(Collapsible("Audio mix", mix))
        self._studio_export_buttons(l)
        l.addStretch()
        b = QPushButton("⬆  Export"); b.setObjectName("primary"); b.setMinimumHeight(42); b.clicked.connect(self.export); l.addWidget(b)
        f.setMinimumWidth(250)
        return f

    def _build_side(self):   # left rail used by MainWindow (self.side)
        self.side = QWidget(); self.side.setObjectName("plain")
        sl = QVBoxLayout(self.side); sl.setContentsMargins(9, 10, 9, 10); sl.setSpacing(6)
        cap = QLabel("WORKSPACE"); cap.setObjectName("muted"); cap.setAlignment(Qt.AlignCenter); sl.addWidget(cap)
        self.rail_btns = {}
        for t, key in (("▣\nMedia", "media"), ("▤\nScript", "script"), ("✦\nTranslate", "translate"),
                       ("♫\nVoice", "voice"), ("◈\nStyle", "style"), ("✎\nTools", "tools"), ("⇧\nExport", "export")):
            b = QPushButton(t); b.setObjectName("railBtn"); b.setFixedSize(66, 46)
            b.setProperty("active", False)
            b.clicked.connect(lambda _=False, k=key: self._goto(k)); sl.addWidget(b)
            self.rail_btns[key] = b
            self.boxes[key].btn.toggled.connect(lambda on, k=key: self._workflow_section_changed(k, on))
            self._rail_active(key, self.boxes[key].btn.isChecked())
        sl.addSpacing(4)
        bb = QPushButton("⤢\nBig view"); bb.setObjectName("railBtn"); bb.setFixedSize(66, 42); bb.setCheckable(True)
        bb.setToolTip("Video fills the whole workspace (hides the left panel, inspector and timeline). Click again to restore")
        bb.toggled.connect(self._toggle_big_view); bb.toggled.connect(lambda on, b=bb: self._set_active(b, on)); sl.addWidget(bb)
        self.big_btn = bb
        ib = QPushButton("◧\nInspector"); ib.setObjectName("railBtn"); ib.setFixedSize(66, 42); ib.setCheckable(True); ib.setChecked(True)
        ib.setToolTip("Show / hide the Selected object panel (X, Y, width, height, rotation, opacity...). Hidden: the video uses its space")
        ib.toggled.connect(self._toggle_inspector); ib.toggled.connect(lambda on, b=ib: self._set_active(b, on)); sl.addWidget(ib)
        self.insp_btn = ib; self._set_active(ib, True)
        mb = QPushButton("▤\nTimeline"); mb.setObjectName("railBtn"); mb.setFixedSize(66, 42); mb.setCheckable(True); mb.setChecked(True)
        mb.setToolTip("Show / hide the timeline. The Dialogue table / Audio mixer move up to take its place")
        mb.toggled.connect(self._toggle_timeline); mb.toggled.connect(lambda on, b=mb: self._set_active(b, on)); sl.addWidget(mb)
        self.tl_btn = mb; self._set_active(mb, True)
        sl.addStretch()
        self.tog = {}
        b = QPushButton("Progress / Log"); b.setObjectName("railLog"); b.setCheckable(True); b.setMinimumHeight(50); self.tog["Process"] = b
        b.toggled.connect(lambda on: self.sec["Process"].setVisible(on))
        sl.addWidget(b)

    @staticmethod
    def _set_active(b, on):
        b.setProperty("active", bool(on)); b.style().unpolish(b); b.style().polish(b)

    def _workspace_setting(self, key, default=None):
        try:
            return self.ctx.settings.get(key)
        except Exception:
            return default

    def _save_workspace_state(self):
        """Persist only new workspace keys; existing settings and project formats stay untouched."""
        try:
            active = next((k for k, bx in self.boxes.items() if bx.btn.isChecked()), "")
            self.ctx.settings.set("translator_active_section", active)
            self.ctx.settings.set("translator_timeline_visible", bool(self.tl_btn.isChecked()))
            self.ctx.settings.set("translator_inspector_visible", bool(self.insp_btn.isChecked()))
            sizes = self.top_split.sizes()
            if len(sizes) == 3 and sizes[0] > 0:
                self.ctx.settings.set("translator_left_width", int(sizes[0]))
            if len(sizes) == 3 and sizes[2] > 0:
                self.ctx.settings.set("translator_inspector_width", int(sizes[2]))
            ms = self.main_split.sizes()
            if len(ms) == 2 and ms[1] > 0:
                self.ctx.settings.set("translator_bottom_height", int(ms[1]))
            self.ctx.settings.save()
        except Exception:
            pass

    def _restore_workspace_layout(self):
        """Restore panel visibility and splitter proportions after widgets have been constructed."""
        active = self._workspace_setting("translator_active_section", "media") or "media"
        if active not in self.boxes:
            active = "media"
        for key, bx in self.boxes.items():
            bx.btn.setChecked(key == active)
        self.tl_btn.setChecked(bool(self._workspace_setting("translator_timeline_visible", True)))
        self.insp_btn.setChecked(bool(self._workspace_setting("translator_inspector_visible", True)))
        left = int(self._workspace_setting("translator_left_width", 340) or 340)
        inspector = int(self._workspace_setting("translator_inspector_width", 280) or 280)
        bottom = int(self._workspace_setting("translator_bottom_height", 300) or 300)
        QTimer.singleShot(0, lambda: (self.top_split.setSizes([left, max(400, self.width()-left-inspector), inspector]),
                                      self.main_split.setSizes([max(350, self.height()-bottom), bottom]),
                                      self.canvas.fit_view()))

    def _workflow_section_changed(self, key, on):
        self._rail_active(key, on)
        active = any(bx.btn.isChecked() for bx in self.boxes.values())
        if not self.big_btn.isChecked():
            if active:
                self.left_scroll.show()
                if self.top_split.sizes()[0] <= 0:
                    width = int(self._workspace_setting("translator_left_width", 340) or 340)
                    sizes = self.top_split.sizes()
                    self.top_split.setSizes([width, max(400, sum(sizes)-width-sizes[2]), max(0, sizes[2])])
            else:
                sizes = self.top_split.sizes()
                if sizes and sizes[0] > 0:
                    self._left_width_before_hide = sizes[0]
                self.left_scroll.hide()
                self.top_split.setSizes([0, max(1, sum(sizes)), 0 if not self.insp_btn.isChecked() else sizes[2]])
        self._save_workspace_state()
        QTimer.singleShot(0, self.canvas.fit_view)

    def _toggle_timeline(self, on):
        # Hiding the Timeline hides ONLY the timeline strip (toolbar + tracks); the Dialogue table / Audio mixer
        # move up and take its place. The bottom workspace itself stays visible.
        self.tl.setVisible(bool(on))
        if not self.big_btn.isChecked():
            self.bottom_w.show()
            ms = self.main_split.sizes()
            if len(ms) == 2 and ms[1] <= 0:
                h = int(self._workspace_setting("translator_bottom_height", 300) or 300)
                self.main_split.setSizes([max(350, sum(ms) - h), h])
        self._save_workspace_state()
        QTimer.singleShot(0, self.canvas.fit_view)

    def _toggle_inspector(self, on):
        if not self.big_btn.isChecked():
            if on:
                self.inspector.show()
                width = int(self._workspace_setting("translator_inspector_width", 280) or 280)
                sizes = self.top_split.sizes()
                self.top_split.setSizes([sizes[0], max(400, sum(sizes)-sizes[0]-width), width])
            else:
                sizes = self.top_split.sizes()
                if len(sizes) == 3 and sizes[2] > 0:
                    self._inspector_width_before_hide = sizes[2]
                self.inspector.hide()
                self.top_split.setSizes([sizes[0], max(1, sizes[1]+sizes[2]), 0])
        self._save_workspace_state()
        QTimer.singleShot(0, self.canvas.fit_view)

    def _toggle_big_view(self, on):
        if on:
            self._big_saved = (self.top_split.sizes(), self.main_split.sizes(),
                               self.left_scroll.isVisible(), self.inspector.isVisible(), self.bottom_w.isVisible())
            self.left_scroll.hide(); self.inspector.hide(); self.bottom_w.hide()
            self.top_split.setSizes([0, max(1, sum(self.top_split.sizes())), 0])
            self.main_split.setSizes([max(1, sum(self.main_split.sizes())), 0])
        else:
            ts, ms, _left_visible, _inspector_visible, _bottom_visible = getattr(
                self, "_big_saved", ([340, 800, 280], [560, 300], True, True, True))
            left_visible = any(b.btn.isChecked() for b in self.boxes.values())
            inspector_visible = self.insp_btn.isChecked()
            bottom_visible = True               # the table stays; the Timeline button only hides the timeline strip
            self.left_scroll.setVisible(left_visible)
            self.inspector.setVisible(inspector_visible)
            self.bottom_w.setVisible(bottom_visible)
            ts = list(ts); ms = list(ms)
            if left_visible and ts[0] <= 0:
                ts[0] = int(self._workspace_setting("translator_left_width", 340) or 340)
            if inspector_visible and ts[2] <= 0:
                ts[2] = int(self._workspace_setting("translator_inspector_width", 280) or 280)
            if bottom_visible and ms[1] <= 0:
                ms[1] = int(self._workspace_setting("translator_bottom_height", 300) or 300)
            if not bottom_visible:
                ms[1] = 0
            self.top_split.setSizes(ts); self.main_split.setSizes(ms)
        QTimer.singleShot(0, self.canvas.fit_view)

    def _rail_active(self, key, on):
        b = self.rail_btns.get(key)
        if b is not None:
            self._set_active(b, on)

    def _box_status(self, key, state):   # display only: colour of the group's status dot
        bx = getattr(self, "boxes", {}).get(key)
        if bx is not None and hasattr(bx, "set_status"):
            bx.set_status(state)

    def _goto(self, key):   # rail button: open that box (the previous one closes) and bring it into view; click the open one again to collapse it
        bx = self.boxes[key]
        if self.accordion.toggle(bx):
            QTimer.singleShot(0, lambda: self.left_scroll.ensureWidgetVisible(bx, 0, 10))

    # ---------- preview / timeline sync ----------

    @staticmethod
    def _tc(t):
        t = max(0.0, float(t or 0.0))
        return f"{int(t // 60):02d}:{t % 60:05.2f}"

    def _cue_at(self, t):
        for c in self.cues:
            if c.start <= t <= c.end:
                return c
        return None

    def _on_vpos(self, ms):
        t = ms / 1000.0
        self.tl.set_playhead(t)
        self.tc.setText(f"{self._tc(t)} / {self._tc(self.vid_dur)}")
        if self.vid_dur > 0 and not self.seek.isSliderDown():
            self.seek.blockSignals(True); self.seek.setValue(int(min(1.0, t / self.vid_dur) * 1000)); self.seek.blockSignals(False)
        c = self._cue_at(t)
        self.sub_orig.setText(c.text if c else ""); self.sub_kh.setText(c.translation if c else "")
        self.canvas.set_time(t)

    def _on_vdur(self, ms):
        if ms > 0:
            self.vid_dur = ms / 1000.0
            self.tl.set_duration(self.vid_dur)

    def _seek(self, t):   # t in seconds (timeline click or seek slider)
        if not (self.vplayer and self.video):
            self.tl.set_playhead(t); self.canvas.set_time(t); return
        self._pb_set_pos(t)                 # video + audio together

    def _playing(self):
        return bool(self.vplayer and self.vplayer.playbackState() == self._QMP.PlayingState)

    def _toggle_play(self):
        if not (self.vplayer and self.video):
            return
        if self._playing():
            self.vplayer.pause()
        else:
            try:
                self._pb_play_pressed()
            except Exception as e:  # noqa
                self.log.appendPlainText(f"Play error: {e!r}")
                self.vplayer.play()

    def _sync_play_btn(self):
        self.btn_play.setText("⏸" if self._playing() else "▶")

    def _skip(self, secs):   # back / forward by `secs` seconds (clamped to the video)
        if not (self.vplayer and self.video):
            return
        t = self.vplayer.position() / 1000.0 + secs
        t = max(0.0, min(t, self.vid_dur if self.vid_dur > 0 else t))
        self._pb_set_pos(t)

    def _jump_cue(self, d):
        if not (self.vplayer and self.cues):
            return
        now = self.vplayer.position() / 1000.0
        starts = sorted(c.start for c in self.cues)
        if d > 0:
            t = next((s for s in starts if s > now + 0.05), None)
        else:
            t = next((s for s in reversed(starts) if s < now - 0.5), 0.0)
        if t is not None:
            self._pb_set_pos(t)

    def _open_preview(self, path):
        self.pv_title.setText(Path(path).name)
        if not self.vplayer:
            self.pv_note.setText("Embedded preview is not available on this system - use Play Video to open the file.")
            return
        try:
            self._pv_orig = str(path)
            self._pv_proxy_tried = False
            if not getattr(self, "_pv_wired", False):      # fallback: if Qt cannot decode the file, play an H.264 proxy instead
                self._pv_wired = True
                self.vplayer.mediaStatusChanged.connect(self._pv_status)
                self.vplayer.errorOccurred.connect(lambda _e, _m: self._pv_try_proxy("player error: " + _m))
            src = str(path)
            proxy = self._pv_proxy_path(path)
            if proxy.exists() and proxy.stat().st_size > 1000:
                src = str(proxy)
            self.vplayer.setSource(QUrl.fromLocalFile(src))
            self.pv_stack.setCurrentIndex(1)
            self.vplayer.pause()      # show the first frame, do not autoplay
            vc = (self._pv_vcodec or "").lower()
            if src == str(path) and vc in ("hevc", "h265", "av1", "vp9", "prores", "mpeg4", "wmv3", "vc1"):
                self._pv_try_proxy(f"codec {vc} may not play")
        except Exception as e:  # noqa
            self.log.appendPlainText(f"Preview could not open the video: {e}")

    # ---- preview proxy: H.264 + AAC copy used ONLY for the on-screen player (export still uses the original file) ----
    def _pv_proxy_path(self, path):
        return work_dir(Path(path).stem) / "preview_proxy.mp4"

    def _pv_status(self, st):
        if st == self._QMP.InvalidMedia:
            self._pv_try_proxy("invalid media")

    def _pv_try_proxy(self, why):
        if getattr(self, "_pv_proxy_tried", True) or not getattr(self, "_pv_orig", None):
            return
        self._pv_proxy_tried = True
        src = self._pv_orig
        dst = self._pv_proxy_path(src)
        self.log.appendPlainText(f"Preview: cannot play the file directly ({why}). Making a compatible preview copy...")
        self.pv_title.setText("Preparing preview…")

        def job(progress=None, cancel=None):
            import subprocess
            from ..shared.ffmpeg_util import require_ffmpeg, _NOWIN
            tmp = dst.with_suffix(".tmp.mp4")
            cmd = [require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                   "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale='min(1280,iw)':-2,format=yuv420p",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", str(tmp)]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=_NOWIN)
            if r.returncode != 0 or not tmp.exists():
                raise RuntimeError((r.stderr or "ffmpeg failed")[-300:])
            tmp.replace(dst)
            return str(dst)

        w = Worker(job)
        w.done.connect(self._pv_proxy_ready)
        w.failed.connect(lambda m: self.log.appendPlainText(f"Preview copy failed: {m}"))
        self._pv_worker = w
        w.start()

    def _pv_proxy_ready(self, dst):
        if not self.video or str(self._pv_orig) != str(self.video):
            return
        pos = self.vplayer.position()
        self.vplayer.setSource(QUrl.fromLocalFile(dst))
        self.vplayer.setPosition(pos)
        self.vplayer.pause()
        self.pv_title.setText(Path(self.video).name)
        self.log.appendPlainText("Preview: using the compatible preview copy (export still uses your original video).")

    def _on_sel(self):
        if self._loading:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        self.tl.set_selected(rows)
        r = self.table.currentRow()
        self._update_inspector(r)
        if 0 <= r < len(self.cues) and self.vplayer and self.video and not self._playing():
            self.vplayer.setPosition(int(self.cues[r].start * 1000))

    def _update_inspector(self, r):
        if 0 <= r < len(self.cues):
            c = self.cues[r]
            self.sel_info.setText(f"Row {r + 1} · {c.speaker or 'no speaker'} · voice: {c.voice_type or 'default'}"
                                  + (" · ✓ generated" if c.audio and Path(c.audio).exists() else " · not generated"))
            if not self.emo_edit.hasFocus():
                self.emo_edit.setText(c.emotion)
            self._raw_load(c)
        else:
            self.sel_info.setText("Select a row in the table to see its voice.")
            if not self.emo_edit.hasFocus():
                self.emo_edit.setText("")
            self._raw_load(None)

    def _emo_edit_done(self):
        r = self.table.currentRow()
        if not (0 <= r < len(self.cues)):
            return
        txt = self.emo_edit.text().strip()
        it = self.table.item(r, 8)
        if it is not None and it.text() != txt:
            it.setText(txt)         # goes through _edited (sets the cue + clears its audio)

    def _sync_views(self):   # timeline colours + export summary follow the cue data
        ok = [bool(c.audio and Path(c.audio).exists()) for c in self.cues]
        if hasattr(self, "tl"):
            self.tl.set_state(ok)
            self.tl.refresh()
            self._st_refresh_mixer_state()
            self._st_schedule_save()
        self._update_summary()

    def _update_summary(self):
        if not hasattr(self, "exp_status"):
            return
        mode = self.export_mode.currentIndex()
        n = len(self.cues)
        tr = [c for c in self.cues if c.translation.strip()]
        voiced = sum(1 for c in tr if c.audio and Path(c.audio).exists())
        need_voice = mode in (0, 1, 3, 4)
        if not self.video:
            msg, good = "Import a video to export", False
        elif not n:
            msg, good = "No script lines yet", False
        elif not tr:
            msg, good = "Translate the script first", False
        elif need_voice and voiced < len(tr):
            msg, good = f"{len(tr) - voiced} line(s) have no voice yet", False
        else:
            msg, good = "Ready to Export!", True
        self.exp_status.setText(("✓  " if good else "⚠  ") + msg)
        self.exp_status.setStyleSheet("color:%s" % (T["success"] if good else T["warning"]))
        sub = ("none" if mode in (0, 1, 4) else "burn-in" if self.sub_mode.currentIndex() == 1 else "soft track")
        self.exp_sum.setText(f"Mode: {EXPORTS[mode]}\nSubtitles: {sub}\nLines: {len(tr)}/{n} translated · {voiced}/{len(tr)} voiced\n"
                             f"Output: {self.opath.text() or '-'}")
        # status dots on the group headers (display only)
        self._box_status("media", "ready" if self.video else "idle")
        self._box_status("script", "ready" if n else "idle")
        self._box_status("translate", "idle" if not tr else ("ready" if len(tr) == n else "warning"))
        self._box_status("voice", "idle" if not voiced else ("ready" if voiced >= len(tr) else "warning"))
        self._box_status("export", "ready" if good else "idle")

    # ---------- table <-> cues ----------
