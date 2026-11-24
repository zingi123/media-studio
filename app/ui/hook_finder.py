"""HookMixin: the 🎣 Hook Finder button.

Click -> dialog: editable AI prompt (saved), number of hooks, 4-8 s length -> [Find hooks] (AI reads the dubbed transcript + emotion / pitch /
loudness scores; falls back to offline scoring when there is no AI engine) -> tick the hooks you like -> choose the export:
  MP3 | Video + voice | Video + subtitle + voice | Hook + voice + video | Hook + voice + video + subtitle
Files go to <output folder>/Hooks/.  Needs the Khmer voice (step 5) like a normal export.
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout)

from ..ai import hook_prompt as HP
from ..shared.ffmpeg_util import probe
from ..shared.paths import work_dir
from ..studio import audio as AU
from ..studio.model import Mixer as _Mixer
from ..translator import hook_export as HE
from ..translator import hook_fusion as HF
from ..translator.exporter import export_audio, export_video
from ..translator.translator_engine import make_provider
from ..translator.voice_synchronizer import build_voice_track


def _mmss(t):
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


class HookMixin:
    _hooks = []
    _hook_dlg = None

    # ------------------------------------------------------------------ dialog
    def hook_clicked(self):
        if not self._need_video():
            return
        if not self.cues or not any((c.translation or "").strip() for c in self.cues):
            QMessageBox.information(self, "Hook Finder", "Translate the script to Khmer first (the hooks use the Khmer text and the dub).")
            return
        if self._hook_dlg is not None and self._hook_dlg.isVisible():
            self._hook_dlg.raise_()
            return
        d = QDialog(self)
        d.setWindowTitle("🎣 Hook Finder"); d.resize(720, 700)
        lay = QVBoxLayout(d)
        lay.addWidget(QLabel("Step 1: the AI reads the WHOLE story (central question, twists, secrets, special lines)  ->  STORY signal.\n"
                             "Step 2: the VOICE signal (emotion, pitch, loudness vs that speaker's normal) is blended with it, line by line.\n"
                             "Step 3: the AI chooses the best blended moments. A moment special in BOTH ways wins. ({n} {min_s} {max_s} are filled in)"))
        self._hk_prompt = QPlainTextEdit()
        saved = self.ctx.settings.get("hook_prompt2")
        self._hk_prompt.setPlainText(saved or HP.default_template())
        self._hk_prompt.setMinimumHeight(220)
        lay.addWidget(self._hk_prompt, 3)
        row = QHBoxLayout()
        self._hk_n, self._hk_min, self._hk_max = QSpinBox(), QSpinBox(), QSpinBox()
        self._hk_n.setRange(1, 30); self._hk_n.setValue(8)
        self._hk_min.setRange(2, 20); self._hk_min.setValue(4)
        self._hk_max.setRange(3, 30); self._hk_max.setValue(8)
        for lbl, w in (("Hooks", self._hk_n), ("Min s", self._hk_min), ("Max s", self._hk_max)):
            row.addWidget(QLabel(lbl)); row.addWidget(w)
        self._hk_ai = QCheckBox("Use AI (off = offline scoring, no key needed)"); self._hk_ai.setChecked(True)
        self._hk_vw = QSpinBox(); self._hk_vw.setRange(0, 100); self._hk_vw.setValue(40); self._hk_vw.setSuffix(" %")
        self._hk_vw.setToolTip("How much the VOICE counts in the blend (the rest is the story). 0 = story only, 100 = voice only.")
        self._hk_story = QCheckBox("Analyse the story first"); self._hk_story.setChecked(True)
        self._hk_story.setToolTip("An extra AI call that reads the whole script before choosing (better hooks, uses more of your free quota)")
        row.addWidget(self._hk_ai); row.addStretch(1)
        reset = QPushButton("Reset prompt"); reset.clicked.connect(lambda: self._hk_prompt.setPlainText(HP.default_template()))
        row.addWidget(reset)
        lay.addLayout(row)
        row2 = QHBoxLayout(); row2.addWidget(self._hk_story); row2.addWidget(QLabel("Voice weight")); row2.addWidget(self._hk_vw); row2.addStretch(1); lay.addLayout(row2)
        self._hk_find = QPushButton("🔎  Find hooks"); self._hk_find.clicked.connect(self.hook_find)
        lay.addWidget(self._hk_find)
        self._hk_list = QListWidget(); lay.addWidget(self._hk_list, 4)
        self._hk_mode = QComboBox(); self._hk_mode.addItems(list(HE.MODES))
        lay.addWidget(QLabel("Export as:")); lay.addWidget(self._hk_mode)
        self._hk_export = QPushButton("💾  Export selected hooks"); self._hk_export.setEnabled(False)
        self._hk_export.clicked.connect(self.hook_export_clicked)
        lay.addWidget(self._hk_export)
        self._hook_dlg = d
        d.show()

    # ------------------------------------------------------------------ find
    def hook_find(self):
        n, mn, mx = self._hk_n.value(), float(self._hk_min.value()), float(self._hk_max.value())
        if mn > mx:
            mn, mx = mx, mn
        tpl = self._hk_prompt.toPlainText()
        self.ctx.settings.set("hook_prompt2", "" if tpl.strip() == HP.default_template().strip() else tpl); self.ctx.settings.save()
        use_ai, cues = self._hk_ai.isChecked(), self.cues
        use_story, wv, story_hint = self._hk_story.isChecked(), self._hk_vw.value() / 100.0, self.story
        try:
            prov = make_provider(self.engine.currentIndex(), self.ctx.settings.get("gemini_key"), self.ctx.settings.get("gemini_model"))
            if prov is not None and not prov.configured()[0]:
                prov = None
        except Exception:  # noqa
            prov = None
        self.tog["Process"].setChecked(True)
        self.log.appendPlainText("🎣 Finding hooks ...")
        self._hk_find.setEnabled(False)

        def job(progress=None, cancel=None):
            hooks, note, smap = [], "", {}
            if use_ai and prov is not None and use_story:
                try:
                    if progress:
                        progress("   Step 1/3: reading the whole story ...")
                    smap = HP.parse_story(prov.generate(HP.build_story_prompt(cues, story_hint), system=HP.STORY_SYSTEM, json_mode=True,
                                                        temperature=0.3))
                except Exception as e:  # noqa
                    note = f"Story analysis failed ({str(e)[:100]}). "
            elif use_ai and prov is None:
                note = "No AI engine is set up (NLLB / no key) - voice + text signals only. "
            story, voice = HF.story_scores(cues, smap), HF.voice_scores(cues)
            if story is None and use_ai and prov is not None and use_story:
                note += "No usable story map. "
            if voice is None:
                note += "No voice measurements yet (run Auto Dub's performance step) - story only. "
            if progress:
                progress("   Step 2/3: blending story + voice ...")
            fused = HF.fuse(story, voice, wv if (story is not None and voice is not None) else 1.0)
            cands = HF.candidates(cues, fused, story, voice, HF.avoid_ids(smap), max(3 * n, 12), mn, mx)
            if use_ai and prov is not None and cands:
                try:
                    if progress:
                        progress("   Step 3/3: the AI chooses the best moments ...")
                    txt = prov.generate(HP.build_hook_prompt(HF.candidates_json(cands), smap), system=HP.render(tpl, n, mn, mx),
                                        json_mode=True, temperature=0.4)
                    hooks = HP.parse_hooks(txt, cands, n)
                except Exception as e:  # noqa
                    note += f"AI ranking failed ({str(e)[:100]}) - best blended moments used. "
            if not hooks:
                hooks = HF.top_hooks(cands, n)
            if not hooks:
                hooks = HE.heuristic_hooks(cues, n, mn, mx)
            return hooks, note.strip(), smap

        def done(r):
            hooks, note, smap = r
            if smap.get("logline"):
                self.log.appendPlainText(f"📖 Story: {smap['logline']}")
            if smap.get("central_question"):
                self.log.appendPlainText(f"   Central question: {smap['central_question']}")
            self._hk_find.setEnabled(True)
            self._hooks = sorted(hooks, key=lambda h: h["start"])
            self._hk_list.clear()
            for h in self._hooks:
                segs = h.get("segments") or [{"start": h["start"], "end": h["end"]}]
                when = "  +  ".join(f"{_mmss(g['start'])}-{_mmss(g['end'])}" for g in segs)
                it = QListWidgetItem(f"{when}  ({h.get('dur', 0)}s)  ·  {h['hook_type']}  ·  {h['score']:.1f}   [{HF.describe(h)}]\n"
                                     f"      {h.get('special') or h.get('why', '')}" + (f"\n      “{h['caption_km']}”" if h.get("caption_km") else ""))
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable); it.setCheckState(Qt.Checked)
                self._hk_list.addItem(it)
            self._hk_export.setEnabled(bool(self._hooks))
            self.log.appendPlainText(f"✓ {len(self._hooks)} hook(s) found." + (f" {note}" if note else ""))
        self._run(job, done=done, on_fail=lambda m: self._hk_find.setEnabled(True))

    # ------------------------------------------------------------------ export
    def hook_export_clicked(self):
        picked = [h for i, h in enumerate(self._hooks) if i < self._hk_list.count() and self._hk_list.item(i).checkState() == Qt.Checked]
        if not picked:
            QMessageBox.information(self, "Hook Finder", "Tick at least one hook.")
            return
        if not any(c.audio and Path(c.audio).exists() for c in self.cues):
            QMessageBox.information(self, "Hook Finder", "Generate the Khmer voice first (step 5).")
            return
        self._collect_focus()
        mode = self._hk_mode.currentIndex()
        video, stem, cues = self.video, Path(self.video).stem, self.cues
        out = self._outdir() / "Hooks"
        dur = probe(video)["duration"]
        font = self.ctx.settings.get("khmer_font_path")
        keep_bg, voff = self.keep_bg.isChecked(), self.voice_off.value()
        fx = self._collect_fx().copy()
        mixer = _Mixer.from_dict(self.mixer.to_dict())
        sfx = [AU.AudioClip.from_dict(k.to_dict()) for k in self.clips if k.track == "sfx"]
        bgm = [AU.AudioClip.from_dict(k.to_dict()) for k in self.clips if k.track == "bgm"]
        aiv = [AU.AudioClip.from_dict(k.to_dict()) for k in self.clips if k.track == "ai_voice"]
        self.tog["Process"].setChecked(True)
        self.log.appendPlainText(f"💾 Exporting {len(picked)} hook(s): {HE.MODES[mode]}")
        if mode >= 3:
            self.log.appendPlainText("   (this also renders the full dubbed film - it takes as long as a normal export)")

        def job(progress=None, cancel=None):
            msgs = []
            wav = work_dir(stem) / "voice_track.wav"
            build_voice_track(cues, dur, wav, cancel=cancel, offset=voff)
            oc = mixer.channels["orig_vocals"]
            orig = (AU.extract_original(video, work_dir(stem) / "original_audio.wav", cancel=cancel)
                    if keep_bg and oc.enabled and oc.volume > 0 and not AU.have_stems(video) else None)
            inputs = AU.gather_inputs(video, wav, sfx, bgm, dur, tmp_dir=work_dir(stem), cancel=cancel, ai_clips=aiv, orig_wav=orig)
            if not keep_bg:
                inputs.pop("orig_vocals", None); inputs.pop("orig_bg", None)
            mix = AU.render_mix(inputs, mixer, work_dir(stem) / "hook_mix.wav", cancel=cancel)
            out.mkdir(parents=True, exist_ok=True)
            if mode <= 2:
                for k, h in enumerate(picked, 1):
                    base = out / f"{stem}_hook{k:02d}_{h['hook_type']}"
                    if mode == 0:
                        msgs.append("Saved " + HE.render_audio(mix, h, base.with_suffix(".mp3"), export_audio, cancel))
                    else:
                        msgs.append("Saved " + HE.render_clip(video, mix, cues, h, base.with_suffix(".mp4"), mode == 2,
                                                              font, fx, export_video, cancel))
                    if progress:
                        progress(int(k * 100 / len(picked)))
            else:
                sub = mode == 4
                top = max(picked, key=lambda h: h["score"])
                tag = "_sub" if sub else ""
                clip = HE.render_clip(video, mix, cues, top, out / f"{stem}_intro_hook{tag}.mp4", sub, font, fx,
                                      export_video, cancel)
                full = export_video(video, out / f"{stem}_full_dub{tag}.mp4", cues, mix, 0.0, "burn" if sub else "none", font, progress, cancel,
                                    bg_wav=None, fx=HE.fx_for(fx, sub), voice_volume=1.0, bg_volume=1.0)
                msgs.append("Saved " + HE.concat_intro(clip, full, out / f"{stem}_hook_intro{tag}.mp4", cancel))
                if len(picked) > 1:
                    msgs.append("   (only the best-scoring ticked hook is used as the intro)")
            return msgs

        def done(msgs):
            for m in msgs:
                self.log.appendPlainText(m)
            self.log.appendPlainText(f"✓ Hooks saved in {out}")
            QMessageBox.information(self, "Hook Finder", f"Done. Files are in:\n{out}")
        self._run(job, done=done, alert=True)
