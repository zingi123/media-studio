"""Khmer Movie Summary engine - real FFmpeg + numpy, no Qt, no network, no AI models (providers are fakes)."""
import json
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

import conftest  # noqa
from app.shared.ffmpeg_util import probe, require_ffmpeg
from app.summary.model import (SummaryProject, Scene, Beat, Segment, Clip, save_project, load_project, voice_sig)
from app.summary import scenes as SC, script as SCR, matcher as M, sync as SY, render as RD, narration as NR, quality as Q

TMP = Path(tempfile.mkdtemp(prefix="ms_summary_"))


# ------------------------------------------------------------------ helpers
def make_video(name="movie.mp4", colors=("black", "white", "gray"), each=2.0):
    out = TMP / name
    if out.exists():
        return str(out)
    cmd = [require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    for c in colors:
        cmd += ["-f", "lavfi", "-i", f"color=c={c}:s=160x120:r=25:d={each}"]
    n = len(colors)
    cmd += ["-filter_complex", "".join(f"[{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0",
            "-pix_fmt", "yuv420p", "-c:v", "mpeg4", str(out)]
    subprocess.run(cmd, check=True)
    return str(out)


def make_tone(path, seconds, freq=440.0):
    sr = 24000
    t = np.arange(int(sr * seconds)) / sr
    a = (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(a.tobytes())
    return str(path)


def project_with(scene_lens, durs, hints=None):
    """Scenes laid end to end; one beat + one segment per entry of durs (segment i -> scene i)."""
    p = SummaryProject(source="x.mp4", duration=sum(scene_lens), width=160, height=120, fps=25.0)
    t = 0.0
    for i, L in enumerate(scene_lens):
        p.scenes.append(Scene(id=i, start=t, end=t + L)); t += L
    for i, d in enumerate(durs):
        p.beats.append(Beat(id=i, text=f"beat {i}", t_hint=(hints[i] if hints else None)))
        p.segments.append(Segment(id=i, beat_id=i, km=f"ឃ្លាទី {i}", dur=d, scene_id=i, match_score=1.0))
    return p


# ------------------------------------------------------------------ model
def test_project_roundtrip_and_damaged_file():
    p = project_with([4, 4], [2, 2])
    p.segments[0].locked = True
    f = TMP / "p.json"
    save_project(p, f)
    q = load_project(f)
    assert q.segments[0].locked and len(q.scenes) == 2 and q.beats[1].text == "beat 1"
    f.write_text("{not json", encoding="utf-8")
    try:
        load_project(f)
        raise AssertionError("damaged file must raise")
    except ValueError:
        pass


def test_voice_sig_changes_only_with_voice_inputs():
    s = Segment(id=1, beat_id=1, km="សួស្តី", emotion="calm")
    a = voice_sig(s, "edge:Sreymom", 1.0)
    s.scene_id, s.match_score, s.dur = 5, 0.2, 9.0           # timing / matching never change the voice
    assert voice_sig(s, "edge:Sreymom", 1.0) == a
    s.km = "សួស្តីបង"
    assert voice_sig(s, "edge:Sreymom", 1.0) != a
    assert voice_sig(s, "edge:Piseth", 1.0) != voice_sig(s, "edge:Sreymom", 1.0)


# ------------------------------------------------------------------ scenes
def test_scene_detection_finds_the_cuts_and_covers_the_whole_video():
    v = make_video()
    sc = SC.detect_scenes(v, threshold=0.3, min_len=0.5, max_len=60)
    assert len(sc) == 3, [(s.start, s.end) for s in sc]
    assert abs(sc[0].end - 2.0) < 0.2 and abs(sc[1].end - 4.0) < 0.2
    assert sc[0].start == 0.0 and abs(sc[-1].end - 6.0) < 0.2
    assert all(a.end == b.start for a, b in zip(sc, sc[1:]))            # no gaps, no overlap


def test_scene_min_len_merges_and_max_len_splits():
    v = make_video()
    merged = SC.detect_scenes(v, threshold=0.3, min_len=3.0, max_len=60)
    assert len(merged) < 3
    split = SC.detect_scenes(v, threshold=0.99, min_len=0.5, max_len=2.0)   # no cut found -> only max_len splits
    assert len(split) >= 3 and all(s.end - s.start <= 2.01 for s in split)


def test_thumbnails_are_real_images():
    v = make_video()
    sc = SC.detect_scenes(v, min_len=0.5, max_len=60)
    SC.extract_thumbs(v, sc, TMP / "thumbs")
    assert all(s.thumb and Path(s.thumb).stat().st_size > 100 for s in sc)


# ------------------------------------------------------------------ script
def test_prompt_has_every_beat_budget_and_parse_requires_all_beats():
    beats = [Beat(id=1, text="hero finds a letter"), Beat(id=2, text="villain arrives")]
    pr = SCR.build_prompt(beats, target_seconds=20, style="suspenseful")
    assert "hero finds a letter" in pr and "villain arrives" in pr and "suspenseful" in pr
    ok = SCR.parse_segments(json.dumps([{"id": 1, "km": "ក"}, {"id": 2, "km": "ខ"}]), beats)
    assert [s.beat_id for s in ok] == [1, 2] and ok[1].km == "ខ"
    try:
        SCR.parse_segments(json.dumps([{"id": 1, "km": "ក"}]), beats)
        raise AssertionError("a missing beat must raise")
    except ValueError as e:
        assert "2" in str(e)


class FakeProvider:
    def __init__(self, replies):
        self.replies, self.calls, self.last = list(replies), 0, ""

    def generate(self, prompt, system="", json_mode=False, **kw):
        self.calls += 1
        self.last = prompt
        return self.replies.pop(0)


def test_generate_script_retries_once_on_bad_output_then_gives_up():
    beats = [Beat(id=1, text="a")]
    good = json.dumps([{"id": 1, "km": "ក"}])
    prov = FakeProvider(["sorry, no JSON", good])
    assert SCR.generate_script(prov, beats, 10)[0].km == "ក" and prov.calls == 2
    prov = FakeProvider(["bad", "still bad"])
    try:
        SCR.generate_script(prov, beats, 10)
        raise AssertionError("must raise after the retry")
    except ValueError:
        assert prov.calls == 2


def test_khmer_normalisation_and_digit_warning():
    s = SCR.normalize_khmer("  សួស្តី\u00a0\u00a0បង \ufeff ")
    assert s == "សួស្តី បង"
    assert SCR.tts_warnings("ឆ្នាំ 2026 ហើយ") and not SCR.tts_warnings("ឆ្នាំ ហើយ")


# ------------------------------------------------------------------ matcher
def test_time_hint_decides_and_order_is_kept():
    p = project_with([10, 10, 10], [3, 3, 3], hints=[(1, 4), (12, 15), (22, 28)])
    for s in p.segments:
        s.scene_id, s.match_score = None, 0.0
    M.assign(p)
    assert [s.scene_id for s in p.segments] == [0, 1, 2]
    assert all(s.match_score > 0.9 for s in p.segments)


def test_monotonic_dp_beats_greedy_when_one_beat_is_wrong():
    # beat 1 points back at scene 0, but the story order forbids going back after beat 0 used scene 2
    p = project_with([10, 10, 10], [3, 3, 3], hints=[(21, 25), (2, 5), (23, 27)])
    for s in p.segments:
        s.scene_id = None
    M.assign(p, monotonic=True)
    ids = [s.scene_id for s in p.segments]
    assert ids == sorted(ids)
    for s in p.segments:
        s.scene_id = None
    M.assign(p, monotonic=False)
    assert [s.scene_id for s in p.segments] == [2, 0, 2]


def test_locked_segment_is_never_moved_and_low_confidence_is_reported():
    p = project_with([10, 10], [3, 3], hints=[(1, 4), None])
    p.segments[0].scene_id, p.segments[0].locked = 1, True            # user forced scene 1
    p.beats[1].text = "completely unrelated words"
    issues = M.assign(p, min_score=0.3)
    assert p.segments[0].scene_id == 1
    assert any(i["kind"] == "low_confidence" and i["segment"] == 1 for i in issues)


def test_text_overlap_helps_when_there_is_no_time_hint():
    p = project_with([10, 10], [3], hints=[None])
    p.beats[0].text = "the old man opens the red door"
    p.scenes[0].caption, p.scenes[1].caption = "a garden at night", "an old man opens a red door"
    p.segments[0].scene_id = None
    M.assign(p)
    assert p.segments[0].scene_id == 1


# ------------------------------------------------------------------ sync
def out_len(c):
    return (c.src_end - c.src_start) / c.speed + c.freeze


def test_long_scene_gives_plain_consecutive_clips_without_reusing_footage():
    p = project_with([10], [3, 3])
    p.segments[1].scene_id = 0
    clips, issues = SY.plan(p)
    assert (clips[0].src_start, clips[0].src_end) == (0.0, 3.0) and clips[0].speed == 1.0
    assert clips[1].src_start == 3.0 and not issues


def test_short_scene_is_slowed_then_frozen_and_every_clip_matches_its_narration():
    p = project_with([2], [3])
    clips, issues = SY.plan(p, max_slow=0.8, max_freeze=2.0)
    assert abs(out_len(clips[0]) - 3.0) < 1e-6 and clips[0].speed == 0.8 and clips[0].freeze > 0
    assert {i["kind"] for i in issues} >= {"slowed", "freeze"}


def test_very_short_scene_spills_into_the_next_footage_and_warns():
    p = project_with([1, 9], [6])
    clips, issues = SY.plan(p)
    assert abs(out_len(clips[0]) - 6.0) < 1e-6 and clips[0].src_end > 1.0
    assert any(i["kind"] == "spill" and i["level"] == "warning" for i in issues)


def test_consumed_scene_is_reused_and_flagged():
    p = project_with([4], [3, 3])
    p.segments[1].scene_id = 0
    clips, issues = SY.plan(p)
    assert abs(out_len(clips[1]) - 3.0) < 1e-6
    assert any(i["kind"] == "reused" and i["segment"] == 1 for i in issues)


# ------------------------------------------------------------------ narration + render (real FFmpeg)
def fake_synth(text, out_path, emotion="normal", speed=1.0):
    return make_tone(out_path, 0.5 + 0.1 * len(text.split()))


def test_only_changed_segments_are_regenerated_and_durations_are_measured():
    p = project_with([10], [0, 0])
    p.segments[0].km, p.segments[1].km = "មួយ ពីរ", "បី បួន ប្រាំ"
    calls = []

    def synth(text, out_path, emotion="normal", speed=1.0):
        calls.append(text)
        return fake_synth(text, out_path, emotion, speed)
    wd = TMP / "nar"
    NR.generate_narration(p, wd, synth, "edge:Sreymom")
    assert len(calls) == 2 and all(s.dur > 0.5 for s in p.segments)
    NR.generate_narration(p, wd, synth, "edge:Sreymom")
    assert len(calls) == 2                                              # nothing changed -> nothing regenerated
    p.segments[1].km = "បី"
    NR.generate_narration(p, wd, synth, "edge:Sreymom")
    assert calls[-1] == "បី" and len(calls) == 3


def test_track_is_as_long_as_the_plan_and_export_matches_narration():
    v = make_video()
    p = project_with([2, 2, 2], [1.5, 2.5, 1.0])
    p.source = v
    wd = TMP / "exp"
    wd.mkdir(exist_ok=True)
    for s in p.segments:
        s.audio = make_tone(wd / f"s{s.id}.wav", s.dur, 300 + 100 * s.id)
    clips, _ = SY.plan(p)
    p.clips = clips
    track = NR.build_track(p, wd / "track.wav")
    total = sum(s.dur for s in p.segments)
    assert abs(probe(track)["duration"] - total) < 0.15
    out = RD.render_edl(p, wd / "out.mp4", narration=track, subtitles=True, work=wd)
    info = probe(out)
    assert info["has_video"] and info["has_audio"]
    assert abs(info["duration"] - total) < 0.3, info


def test_export_refuses_a_plan_that_does_not_exist():
    p = project_with([2], [1.0])
    try:
        RD.render_edl(p, TMP / "nope.mp4", narration=None, work=TMP)
        raise AssertionError("no clips -> must raise")
    except ValueError:
        pass


# ------------------------------------------------------------------ quality
def test_quality_check_reports_the_real_problems():
    p = project_with([4, 4], [3, 3])
    p.segments[0].km = ""
    p.segments[1].match_score = 0.1
    p.segments[1].km = "ក" * 400                                       # far too long for 3 s
    kinds = {i["kind"] for i in Q.check(p)}
    assert {"empty_text", "no_audio", "low_confidence", "text_too_long", "no_plan"} <= kinds


# ------------------------------------------------------------------ added after the first run
def test_regenerate_one_paragraph_sends_the_old_text_and_the_instruction():
    beat = Beat(id=1, text="hero finds a letter", weight=2.0)
    seg = Segment(id=1, beat_id=1, km="អត្ថបទចាស់")
    prov = FakeProvider([json.dumps([{"id": 1, "km": " ថ្មី "}])])
    new = SCR.regenerate_segment(prov, beat, seg, instruction="make it sadder", style="sad", seconds=6)
    assert new == "ថ្មី"
    assert "អត្ថបទចាស់" in prov.last and "make it sadder" in prov.last and "hero finds a letter" in prov.last
    prov = FakeProvider(["nonsense", "nonsense"])
    try:
        SCR.regenerate_segment(prov, beat, seg, seconds=6)
        raise AssertionError("must raise on unusable output")
    except ValueError:
        assert seg.km == "អត្ថបទចាស់"                                  # the old text is never lost on failure


def test_very_long_edit_uses_a_graph_file_and_still_renders():
    v = make_video()
    p = project_with([2, 2, 2], [1.0, 1.0, 1.0])
    p.source = v
    clips, _ = SY.plan(p)
    p.clips = clips
    wd = TMP / "longgraph"
    out = RD.render_edl(p, wd / "o.mp4", narration=None, work=wd, inline_limit=10)      # forces graph.txt
    assert (wd / "graph.txt").exists()
    assert abs(probe(out)["duration"] - 3.0) < 0.3


# ------------------------------------------------------------------ voice adapters (existing engines are patched - no network, no GPU)
def test_the_six_narration_styles_map_to_real_engine_inputs():
    from app.summary import voices as V
    assert set(V.STYLES) == {"Neutral", "Serious", "Excited", "Suspenseful", "Sad", "Calm"}
    assert V.emotion_for("neutral").label == "Normal" and V.emotion_for("nonsense").label == "Normal"
    assert V.emotion_for("Excited").prosody()[0] > 0 and V.emotion_for("Sad").prosody()[0] < 0
    assert V.clone_style("Serious") and V.clone_style("Neutral") == ""
    assert "cannot act" in V.limitation("edge", "Sad").lower()           # honest: edge-tts only approximates
    assert V.limitation("clone", "Sad") == ""


def test_edge_adapter_passes_prosody_and_returns_the_real_file():
    import app.translator.khmer_tts as KT
    from app.summary import voices as V
    seen = {}

    def fake(text, out_path, voice="x", speed=1.0, pitch_hz=0, volume_pct=0):
        seen.update(text=text, voice=voice, speed=speed, pitch=pitch_hz, out=str(out_path))
        Path(out_path).write_bytes(b"x" * 10)
        return str(out_path)
    old, KT.synthesize = KT.synthesize, fake
    try:
        path = V.edge_synth("km-KH-PisethNeural")("សួស្តី", str(TMP / "e.wav"), emotion="Excited", speed=1.0)
    finally:
        KT.synthesize = old
    assert path.endswith(".mp3") and Path(path).exists()                  # edge-tts writes MP3, so the file is named honestly
    assert seen["voice"] == "km-KH-PisethNeural" and seen["speed"] > 1.0   # Excited -> faster


def test_clone_adapter_sends_the_documented_style_prefix_source():
    import app.translator.voice_clone as VC
    from app.summary import voices as V
    seen = {}

    def fake(text, out_wav, reference, speed=1.0, progress=None, cancel=None, style=""):
        seen.update(text=text, ref=reference, style=style)
        return make_tone(out_wav, 0.5)
    old, VC.synthesize_clone = VC.synthesize_clone, fake
    try:
        V.clone_synth("/ref.wav")("សួស្តី", str(TMP / "c.wav"), emotion="Calm", speed=1.0)
    finally:
        VC.synthesize_clone = old
    assert seen["ref"] == "/ref.wav" and "calm" in seen["style"].lower()


# ------------------------------------------------------------------ pipeline helpers
def test_beats_from_text_reads_optional_time_hints():
    from app.summary import pipeline as PL
    b = PL.beats_from_text("""
        # comment lines and blanks are ignored
        00:01:20-00:01:45 The hero finds the letter
        [90-120] The villain arrives
        No timestamp here
    """)
    assert [x.id for x in b] == [1, 2, 3]
    assert b[0].t_hint == (80.0, 105.0) and b[0].text == "The hero finds the letter"
    assert b[1].t_hint == (90.0, 120.0) and b[2].t_hint is None
    assert PL.beats_from_text("") == []


def test_new_project_and_plan_all_run_the_whole_engine_on_a_real_file():
    from app.summary import pipeline as PL
    v = make_video()
    p = PL.new_project(v)
    assert p.width == 160 and p.height == 120 and abs(p.duration - 6.0) < 0.2
    PL.analyse(p, TMP / "an", threshold=0.3, min_len=0.5, max_len=60)
    assert len(p.scenes) == 3 and all(s.thumb for s in p.scenes)
    p.beats = PL.beats_from_text("0-2 first\n2-4 second\n4-6 third")
    p.segments = [Segment(id=b.id, beat_id=b.id, km="ក", dur=1.5) for b in p.beats]
    issues = PL.plan_all(p)
    assert [s.scene_id for s in p.segments] == [0, 1, 2] and len(p.clips) == 3
    assert not [i for i in issues if i["level"] == "error" and i["kind"] != "no_audio"]


# ------------------------------------------------------------------ controller (everything the UI does, without Qt)
def test_controller_full_flow_autosave_edit_and_export():
    from app.summary.controller import SummaryController
    c = SummaryController(root=TMP / "ctl")
    c.open_video(make_video())
    c.analyse(threshold=0.3, min_len=0.5, max_len=60)
    c.set_beats_text("0-2 the hero wakes up\n2-4 a stranger knocks\n4-6 the door opens")
    prov = FakeProvider([json.dumps([{"id": 1, "km": "មួយ ពីរ"}, {"id": 2, "km": "បី បួន"}, {"id": 3, "km": "ប្រាំ"}])])
    c.make_script(prov, target_seconds=6, style="Serious")
    assert [s.km for s in c.project.segments] == ["មួយ ពីរ", "បី បួន", "ប្រាំ"]
    assert c.narrate(fake_synth, "edge:test") == [1, 2, 3]
    issues = c.plan()
    assert len(c.project.clips) == 3 and not [i for i in issues if i["level"] == "error"]
    # autosave: a fresh controller on the same video gets everything back
    c2 = SummaryController(root=TMP / "ctl")
    c2.open_video(make_video())
    assert [s.km for s in c2.project.segments] == ["មួយ ពីរ", "បី បួន", "ប្រាំ"] and len(c2.project.clips) == 3
    out = c.export(TMP / "ctl" / "final.mp4", subtitles=False)
    total = sum(s.dur for s in c.project.segments)
    assert abs(probe(out)["duration"] - total) < 0.3 and probe(out)["has_audio"]


def test_controller_edit_regenerates_only_that_line_and_stale_plan_blocks_export():
    from app.summary.controller import SummaryController
    c = SummaryController(root=TMP / "ctl2")
    c.open_video(make_video())
    c.analyse(threshold=0.3, min_len=0.5, max_len=60)
    c.set_beats_text("0-2 a\n2-4 b\n4-6 c")
    c.make_script(FakeProvider([json.dumps([{"id": i, "km": "ក ខ"} for i in (1, 2, 3)])]), 6)
    c.narrate(fake_synth, "v")
    c.plan()
    c.edit_segment(2, km="ក ខ គ ឃ ង ច ឆ ជ")                          # longer text -> longer audio
    assert c.narrate(fake_synth, "v") == [2]                          # only the edited line
    try:
        c.export(TMP / "ctl2" / "x.mp4")
        raise AssertionError("a stale plan must block the export")
    except ValueError as e:
        assert "plan" in str(e).lower()
    c.plan()
    assert Path(c.export(TMP / "ctl2" / "x.mp4")).exists()


def test_controller_manual_scene_choice_locks_and_survives_replanning():
    from app.summary.controller import SummaryController
    c = SummaryController(root=TMP / "ctl3")
    c.open_video(make_video())
    c.analyse(threshold=0.3, min_len=0.5, max_len=60)
    c.set_beats_text("0-2 a\n2-4 b")
    c.make_script(FakeProvider([json.dumps([{"id": 1, "km": "ក"}, {"id": 2, "km": "ខ"}])]), 4)
    c.narrate(fake_synth, "v")
    c.edit_segment(1, scene_id=2)
    c.plan()
    s = c.project.segment(1)
    assert s.scene_id == 2 and s.locked
    c.edit_segment(1, locked=False)
    c.plan()
    assert c.project.segment(1).scene_id == 0                        # matcher is free again and picks the timed scene


def test_controller_regenerate_paragraph_keeps_text_on_failure():
    from app.summary.controller import SummaryController
    c = SummaryController(root=TMP / "ctl4")
    c.open_video(make_video())
    c.set_beats_text("0-2 a")
    c.make_script(FakeProvider([json.dumps([{"id": 1, "km": "ចាស់"}])]), 2)
    assert c.regenerate_paragraph(FakeProvider([json.dumps([{"id": 1, "km": "ថ្មី"}])]), 1, "shorter") == "ថ្មី"
    assert c.project.segment(1).km == "ថ្មី"
    try:
        c.regenerate_paragraph(FakeProvider(["x", "y"]), 1)
        raise AssertionError("must raise")
    except ValueError:
        assert c.project.segment(1).km == "ថ្មី"


def test_controller_scene_caption_feeds_the_matcher():
    from app.summary.controller import SummaryController
    c = SummaryController(root=TMP / "ctl5")
    c.open_video(make_video())
    c.analyse(threshold=0.3, min_len=0.5, max_len=60)
    c.set_beats_text("the old man opens the red door")                  # no time hint: only captions can decide
    c.make_script(FakeProvider([json.dumps([{"id": 1, "km": "ក"}])]), 3)
    c.narrate(fake_synth, "v")
    c.set_scene_caption(2, "an old man opens a red door")
    c.plan()
    assert c.project.segment(1).scene_id == 2
    try:
        c.set_scene_caption(99, "x")
        raise AssertionError("unknown scene must raise")
    except ValueError:
        pass
