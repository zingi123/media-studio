"""AutoDubMixin: ONE button that runs the steps the Translator page already has, in order, and stops on the first problem.

    1 Smart Script   (skipped when the script already exists  -> resume)
    2 Translate      (only the lines that have no Khmer yet)
    3 Demucs         (only when the box is ticked; skipped when the stems exist)
    4 Generate Voice (only the lines that have no voice yet)
    5 Quality check  (report; errors ask you before exporting)
    6 Export         (Video + Khmer voiceover)

AI polish (on by default, setting `autodub_ai_polish`; needs an LLM engine, not NLLB): after Translate a REVIEW pass fixes meaning / unnatural
Khmer / pronouns (skipped when the text is unchanged since the last review); after Voice a TIMING-FIT loop rewrites lines whose voice does not
fit their slot shorter and regenerates only those voices (max 3 rounds).

PERFORMANCE MATCH (setting `autodub_prosody`, default on, no AI key needed): before the voice, each original line's pitch / loudness / pace is measured
against the SAME speaker's normal voice and planned into the Khmer voice (Cue.prosody); after the voice the Khmer clips are measured the same way,
parameters are corrected and only the lines that are off are regenerated (max 2 rounds). Every line gets Cue.match_score (0-100).
OPTIONAL auto clone (`autodub_auto_clone`, default off): one cloned voice per character from the cleanest original lines.

It adds NO new engine: every step calls the page's own method, so settings, voices, emotion, speakers and the mixer behave exactly as
when you press the buttons yourself.  STOP ALL stops the chain (the step in progress is cancelled and nothing else starts).
Run it again later and it continues from what is already done.
"""
from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QMessageBox

from ..shared.paths import work_dir
from ..studio import audio as AU
from ..translator import qa_report as QA
from ..translator import fit_loop as FIT
from ..translator import prosody_match as PM
from ..translator import auto_reference as AR
from ..translator import voice_clone as VC
from ..translator.audio_extractor import extract_audio
from ..translator import review as RV
from ..translator import story_info as ST
from ..translator.translator_engine import make_provider

_DUB_MODES = (3, 4)       # Video + Khmer voiceover (+ subtitles / NO subtitles)


class AutoDubMixin:
    # ------------------------------------------------------------------ start
    def auto_dub_clicked(self):
        if not self._need_video():
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Auto Dub", "Another step is still running. Press STOP ALL first, then start Auto Dub.")
            return
        self._collect_focus()
        have_script = bool(self.cues)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question); box.setWindowTitle("Auto Dub")
        box.setText("Run the whole dubbing in one go?")
        box.setInformativeText(
            "Smart Script → Translate → Voice → Quality check → Export.\n"
            + ("Your existing script is kept; only what is missing is done.\n" if have_script else "")
            + "On a CPU a long film can take hours. STOP ALL stops it; run it again to continue.")
        cb = QCheckBox("Also run Demucs (keeps the original music / effects as Background Audio)")
        cb.setChecked(bool(self.ctx.settings.get("autodub_demucs")))
        box.setCheckBox(cb)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel); box.setDefaultButton(QMessageBox.Yes)
        if box.exec() != QMessageBox.Yes:
            return
        self.ctx.settings.set("autodub_demucs", bool(cb.isChecked())); self.ctx.settings.save()
        self._auto = {"demucs": bool(cb.isChecked())}
        self.tog["Process"].setChecked(True)
        self.log.appendPlainText("═══ Auto Dub started ═══")
        self._ad_step("scan")

    # ------------------------------------------------------------------ helpers
    def _ad_then(self, step):
        """done-callback: wait for the finished worker thread (a new _run would say 'Busy' otherwise), then go on."""
        def go(*_a):
            try:
                self.worker.wait()
            except Exception:  # noqa
                pass
            self._ad_step(step)
        return go

    def _ad_abort(self, msg="", say=True):
        if getattr(self, "_auto", None) is None:
            return
        self._auto = None
        if say:
            self.log.appendPlainText(f"■ Auto Dub stopped{': ' + msg if msg else '.'} Fix it and press Auto Dub again - finished steps are kept.")
        if getattr(self, "_batch", None) is not None:
            self._batch_video_failed(msg)

    def _ad_step(self, name):
        if getattr(self, "_auto", None) is None:
            return
        try:
            getattr(self, "_ad_" + name)()
        except Exception as e:  # noqa - report instead of silently freezing the chain
            self._ad_abort(f"{name}: {e}")

    # ------------------------------------------------------------------ the steps
    def _ad_scan(self):
        if self._auto["demucs"] and not self.cues and not AU.have_stems(self.video) and not self._auto.get("pre_demucs"):
            self._auto["pre_demucs"] = True        # music removed BEFORE Whisper listens -> far fewer invented words
            self.log.appendPlainText("①a Demucs first (Whisper then listens to the voice only) ...")
            self.mixer_panel.set_demucs("Demucs: running...", True)

            def done(res):
                self._demucs_done(res)
                self._ad_then("scan")()
            return self._run(AU.run_demucs, self.video, self.device.currentText(), done=done,
                             on_fail=lambda m: (self._st_refresh_mixer_state(), self._ad_abort(m)))
        if self.cues:
            self.log.appendPlainText(f"① Script exists ({len(self.cues)} lines) - scan skipped.")
            return self._ad_step("translate")
        self.log.appendPlainText("① Smart Script ...")
        self.smart_scan(then=self._ad_after_scan)

    def _ad_after_scan(self):
        if not self.cues:
            return self._ad_abort("the scan found no speech / text.")
        self._ad_step("translate")

    def _ad_translate(self):
        if any(c.text.strip() and not c.translation.strip() for c in self.cues):
            self.log.appendPlainText("② Translate ...")
            self.translate(then=lambda: self._ad_step("review"), on_fail=self._ad_abort)
        else:
            self.log.appendPlainText("② Khmer text exists - translate skipped.")
            self._ad_step("review")

    # ------------------------------------------------------------------ AI polish helpers
    def _ad_provider(self):
        """LLM provider for the polish passes, or None (polish off / offline NLLB engine / not configured)."""
        if self.ctx.settings.get("autodub_ai_polish") is False:
            return None
        try:
            p = make_provider(self.engine.currentIndex(), self.ctx.settings.get("gemini_key"), self.ctx.settings.get("gemini_model"))
        except Exception:  # noqa
            return None
        if p is None or not p.configured()[0]:
            return None
        return p

    def _ad_review(self):
        prov = self._ad_provider()
        if prov is None:
            return self._ad_step("demucs")
        sig_file = work_dir(Path(self.video).stem) / "review.sig"
        try:
            if sig_file.exists() and sig_file.read_text(encoding="utf-8").strip() == RV.signature(self.cues):
                self.log.appendPlainText("②b Khmer review done before - skipped.")
                return self._ad_step("demucs")
        except Exception:  # noqa
            pass
        self.log.appendPlainText("②b Reviewing the Khmer text (AI second pass) ...")
        gl = ST.glossary_text(self.story)

        def done(changes):
            self.refresh()
            try:
                sig_file.write_text(RV.signature(self.cues), encoding="utf-8")
            except Exception:  # noqa
                pass
            self.log.appendPlainText(f"   {len(changes)} line(s) improved.")
            for c in changes[:15]:
                self.log.appendPlainText(f"   #{c['line']}: {c['before']}  →  {c['after']}")
            self._ad_then("demucs")()
        self._run(RV.review_cues, self.cues, prov, gl, done=done, on_fail=lambda m: (self.log.appendPlainText(f"   (review skipped: {m})"), self._ad_then("demucs")()))

    def _ad_fit(self):
        prov = self._ad_provider()
        if prov is None:
            return self._ad_step("qa")
        self.log.appendPlainText("④b Timing fit (shorten lines whose voice is too long) ...")
        gl = ST.glossary_text(self.story)

        def job(cues, progress=None, cancel=None):
            return FIT.run(cues, prov, lambda idxs: self._gen(idxs, progress=progress, cancel=cancel, retry=True),
                           glossary=gl, progress=progress, cancel=cancel)

        def done(rep):
            self.refresh()
            self.log.appendPlainText(f"   {len(rep)} line(s) shortened, {sum(1 for r in rep if r['ok'])} now fit.")
            for r in rep[:15]:
                self.log.appendPlainText(f"   #{r['line']}: {r['before']}  →  {r['after']}")
            self._ad_then("qa")()
        self._run(job, self.cues, done=done, on_fail=lambda m: (self.log.appendPlainText(f"   (timing fit skipped: {m})"), self._ad_then("qa")()))

    def _ad_demucs(self):
        if not self._auto["demucs"]:
            return self._ad_step("clone")
        if AU.have_stems(self.video):
            self.log.appendPlainText("③ Demucs stems exist - skipped.")
            return self._ad_step("clone")
        self.log.appendPlainText("③ Demucs (separating original voice / music) ...")
        self.mixer_panel.set_demucs("Demucs: running...", True)

        def done(res):
            self._demucs_done(res)
            self._ad_then("clone")()
        self._run(AU.run_demucs, self.video, self.device.currentText(), done=done,
                  on_fail=lambda m: (self._st_refresh_mixer_state(), self._ad_abort(m)))

    def _ad_clone(self):
        """OPTIONAL (setting `autodub_auto_clone`, default off): clone each character's real voice from the cleanest lines of the original."""
        if not self.ctx.settings.get("autodub_auto_clone"):
            return self._ad_step("prosody")
        self.log.appendPlainText("③a Cloning each character's voice from the original (needs the clone engine: GPU or Hugging Face) ...")
        video = self.video

        def job(cues, progress=None, cancel=None):
            wav = AU.stem_paths(video)["vocals"] if AU.have_stems(video) else \
                extract_audio(video, work_dir(Path(video).stem) / "audio16k.wav", cancel=cancel)
            y = PM.load_audio(wav)
            return AR.build_voices(cues, y, work_dir(Path(video).stem) / "refs", Path(video).stem[:12], VC, progress=progress, cancel=cancel)

        def done(res):
            self.refresh()
            for sp, r in res.items():
                self.log.appendPlainText(f"   {sp}: " + (r["error"] if "error" in r else r["voice"] + (" (kept)" if r.get("kept") else "")))
            self._ad_then("prosody")()
        self._run(job, self.cues, done=done, on_fail=lambda m: (self.log.appendPlainText(f"   (voice cloning skipped: {m})"), self._ad_then("prosody")()))

    def _ad_prosody(self):
        """Measure how each original actor really performs every line (pitch / loudness / pace vs their own normal) and plan the Khmer voice."""
        if self.ctx.settings.get("autodub_prosody") is False:
            return self._ad_step("voice")
        if any(c.prosody for c in self.cues):
            self.log.appendPlainText("③b Performance plan exists - skipped.")
            return self._ad_step("voice")
        self.log.appendPlainText("③b Measuring the original actors' performance (pitch, loudness, pace) ...")
        video = self.video

        def job(cues, progress=None, cancel=None):
            wav = AU.stem_paths(video)["vocals"] if AU.have_stems(video) else \
                extract_audio(video, work_dir(Path(video).stem) / "audio16k.wav", cancel=cancel)
            y = PM.load_audio(wav)
            tg = PM.analyze_original(cues, y, cancel=cancel)
            return PM.plan(cues, tg), len(cues)

        def done(res):
            self.refresh()
            self.log.appendPlainText(f"   {res[0]} of {res[1]} line(s) planned from the real performance (others use the emotion label).")
            self._ad_then("voice")()
        self._run(job, self.cues, done=done, on_fail=lambda m: (self.log.appendPlainText(f"   (performance analysis skipped: {m})"), self._ad_then("voice")()))

    def _ad_match(self):
        """Self-check: measure the Khmer voice the same way, compare with the target, correct and regenerate only lines that are off."""
        if self.ctx.settings.get("autodub_prosody") is False or not any(c.prosody for c in self.cues):
            return self._ad_step("fit")
        self.log.appendPlainText("④a Checking that the Khmer voice follows the original performance ...")

        def job(cues, progress=None, cancel=None):
            return PM.verify_and_fix(cues, lambda idxs: self._gen(idxs, progress=progress, cancel=cancel, retry=True),
                                     progress=progress, cancel=cancel)

        def done(rep):
            self.refresh()
            self.log.appendPlainText(f"   Match score {rep['avg']:.0f}/100 on {rep['scored']} line(s); {rep['fixed']} correction(s) made.")
            if rep["low"]:
                self.log.appendPlainText("   Still weak (edge-tts cannot act; a cloned voice follows emotion better): lines " +
                                         ", ".join(map(str, rep["low"][:25])) + (" ..." if len(rep["low"]) > 25 else ""))
            self._ad_then("fit")()
        self._run(job, self.cues, done=done, on_fail=lambda m: (self.log.appendPlainText(f"   (performance check skipped: {m})"), self._ad_then("fit")()))

    def _ad_voice(self):
        idxs = [i for i, c in enumerate(self.cues)
                if c.translation.strip() and getattr(c, "use_voice", True) and not (c.audio and Path(c.audio).exists())]
        if not idxs:
            self.log.appendPlainText("④ All lines already have a voice - skipped.")
            return self._ad_step("match")
        self.log.appendPlainText(f"④ Generating {len(idxs)} voice clips (edge-tts needs internet) ...")

        def done(errs):
            self._gen_done(errs)
            self._ad_then("match")()
        self._run(self._gen, idxs, retry=True, done=done, on_fail=self._ad_abort)

    def _ad_qa(self):
        self.log.appendPlainText("⑤ Quality check ...")
        dur = float(self.vid_dur or 0.0) or None

        def done(issues):
            self._ad_report(issues)
        self._run(QA.run, self.cues, dur, done=done, on_fail=self._ad_abort)

    def _ad_report(self, issues):
        try:
            self.worker.wait()
        except Exception:  # noqa
            pass
        if getattr(self, "_auto", None) is None:
            return
        errs, warns = QA.counts(issues)
        stem = Path(self.video).stem
        try:
            rp = self._outdir() / f"{stem}_quality_report.txt"
            rp.write_text(QA.to_text(issues, f"Quality report - {stem}"), encoding="utf-8")
            self.log.appendPlainText(f"   Report saved: {rp}")
        except Exception as e:  # noqa
            self.log.appendPlainText(f"   (report not saved: {e})")
        self.log.appendPlainText(f"   {errs} error(s), {warns} warning(s).")
        for x in issues[:25]:
            self.log.appendPlainText("   " + str(x))
        if len(issues) > 25:
            self.log.appendPlainText(f"   ... and {len(issues) - 25} more (see the report file).")
        if errs and getattr(self, "_batch", None) is not None:
            self.log.appendPlainText(f"   {errs} problem line(s) - exported anyway in batch mode (see the report).")
        elif errs:
            first = "\n".join(str(x) for x in issues if x.level == "error")[:900]
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning); box.setWindowTitle("Auto Dub - quality check")
            box.setText(f"{errs} line(s) have a problem that will be heard in the export.")
            box.setInformativeText(first + "\n\nExport anyway?")
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel); box.setDefaultButton(QMessageBox.Cancel)
            if box.exec() != QMessageBox.Yes:
                self._ad_abort(say=False)
                self.log.appendPlainText("■ Auto Dub paused before export. Fix the lines (see the report), then press Auto Dub again.")
                return
        self._ad_step("export")

    def _ad_export(self):
        if self.export_mode.currentIndex() not in _DUB_MODES:
            self.export_mode.setCurrentIndex(_DUB_MODES[0])      # an Auto Dub always makes a dubbed video
        self.log.appendPlainText(f"⑥ Export: {self.export_mode.currentText()} ...")
        batch = getattr(self, "_batch", None) is not None
        self._auto = None                                        # the chain ends here; export runs (and reports) on its own
        if batch:
            self.export(then=self._batch_video_done, on_fail=self._batch_video_failed)
        else:
            self.export()
