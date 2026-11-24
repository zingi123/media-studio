"""Everything the Movie Summary page does, without Qt - so it is testable. Every mutating call autosaves (atomic JSON).

Work folder per movie:  <MediaStudio home>/summary/<name>_<fingerprint>/   project.json, thumbs/, audio/, render/, narration.wav
"""
from pathlib import Path

from ..shared.paths import data_dir
from ..translator.project_state import fingerprint
from . import intelligence as VI, narration as NR, pipeline as PL, quality as Q, render as RD, script as SCR, story_intelligence as SI
from .model import load_project, save_project


class SummaryController:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_dir() / "summary"
        self.project = None
        self.work = None

    # ---------------------------------------------------------------- project
    def open_video(self, path, work=None):
        path = Path(path)
        if not path.exists():
            raise ValueError(f"File not found: {path}")
        self.work = Path(work) if work else self.root / f"{path.stem[:40]}_{fingerprint(path)[:8]}"   # work = folder របស់ web import
        self.work.mkdir(parents=True, exist_ok=True)
        pj = self.work / "project.json"
        if pj.exists():
            self.project = load_project(pj)            # ValueError if damaged: the file is left untouched
            self.project.source = str(path.resolve())
        else:
            self.project = PL.new_project(path)
        self.save()
        return self.project

    def save(self):
        save_project(self.project, self.work / "project.json")

    def _need(self):
        if self.project is None:
            raise ValueError("Open a movie first.")

    # ---------------------------------------------------------------- analysis
    def analyse(self, threshold=0.3, min_len=1.0, max_len=30.0, progress=None, cancel=None):
        self._need()
        PL.analyse(self.project, self.work, threshold, min_len, max_len, progress, cancel)
        for g in self.project.segments:                 # new scene ids: old links no longer mean anything
            g.scene_id, g.locked, g.alternatives, g.match_score = None, False, [], 0.0
        self.project.clips = []
        self.save()
        return self.project.scenes

    def describe_scenes(self, progress=None, cancel=None, model="gemini-3.8-flash", overwrite=False):
        self._need()
        result = VI.describe_scenes(self.project, progress=progress, cancel=cancel, model=model, overwrite=overwrite)
        self.save()
        return result

    def set_scene_caption(self, scene_id, caption):
        """What is visible in a scene (typed by the editor now; a vision describer can fill it later). Used as matching evidence."""
        self._need()
        sc = self.project.scene(scene_id)
        if sc is None:
            raise ValueError(f"No scene {scene_id}.")
        sc.caption = (caption or "").strip()
        self.project.clips = []
        self.save()
        return sc

    def extract_story_beats(self, provider, max_beats=18, progress=None, cancel=None):
        self._need()
        self.project.beats = SI.extract_beats(provider, self.project.scenes, max_beats=max_beats)
        keep = {b.id for b in self.project.beats}
        self.project.segments = [g for g in self.project.segments if g.beat_id in keep]
        self.project.clips = []
        self.save()
        return self.project.beats

    def set_beats_text(self, text):
        self._need()
        self.project.beats = PL.beats_from_text(text)
        keep = {b.id for b in self.project.beats}
        self.project.segments = [g for g in self.project.segments if g.beat_id in keep]
        self.project.clips = []
        self.save()
        return self.project.beats

    # ---------------------------------------------------------------- script
    def make_script(self, provider, target_seconds, style="Neutral", characters="", glossary=""):
        self._need()
        if not self.project.beats:
            raise ValueError("Add story beats first.")
        segs = SCR.generate_script(provider, self.project.beats, target_seconds, style, characters, glossary)
        for g in segs:
            g.emotion = style
        self.project.segments, self.project.clips = segs, []
        self.save()
        return segs

    def regenerate_paragraph(self, provider, segment_id, instruction=""):
        self._need()
        g = self.project.segment(segment_id)
        if g is None:
            raise ValueError(f"No segment {segment_id}.")
        new = SCR.regenerate_segment(provider, self.project.beat(g.beat_id), g, instruction, style=g.emotion,
                                     seconds=g.dur or None)
        g.km = new
        self.save()
        return new

    def edit_segment(self, segment_id, km=None, emotion=None, scene_id=None, locked=None):
        self._need()
        g = self.project.segment(segment_id)
        if g is None:
            raise ValueError(f"No segment {segment_id}.")
        if km is not None:
            g.km = SCR.normalize_khmer(km)
        if emotion is not None:
            g.emotion = emotion
        if scene_id is not None:
            if self.project.scene(scene_id) is None:
                raise ValueError(f"No scene {scene_id}.")
            g.scene_id, g.locked = scene_id, True       # a manual choice is never moved by the matcher
        if locked is not None:
            g.locked = bool(locked)
        self.save()
        return g

    # ---------------------------------------------------------------- voice, plan, export
    def narrate(self, synth, voice_key, speed=1.0, progress=None, cancel=None):
        self._need()
        done = NR.generate_narration(self.project, self.work / "audio", synth, voice_key, speed, progress, cancel)
        self.save()
        return done

    def plan(self, **kw):
        self._need()
        if not self.project.scenes:
            raise ValueError("Run scene analysis first.")
        issues = PL.plan_all(self.project, **kw)
        self.save()
        return issues

    def export(self, out, subtitles=True, font="Noto Sans Khmer", progress=None, cancel=None, mix_original_audio=True, music=None, original_volume=0.16, music_volume=0.12):
        self._need()
        errors = [i for i in Q.check(self.project) if i["level"] == "error"]
        if errors:
            raise ValueError("Cannot export: " + " ".join(i["detail"] for i in errors))
        track = NR.build_track(self.project, self.work / "narration.wav")
        return RD.render_edl(self.project, out, narration=track, subtitles=subtitles, font=font,
                             work=self.work / "render", progress=progress, cancel=cancel, mix_original_audio=mix_original_audio,
                             music=music, original_volume=original_volume, music_volume=music_volume)
