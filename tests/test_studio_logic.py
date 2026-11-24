"""Studio layer without Qt: model, geometry, timeline ops, dialogue sync, mixer, and real-FFmpeg rendering."""
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from app.studio import audio as AU, dialogue as D, geometry as G, tracks as TR
from app.studio.model import Scene, Item, FrameSettings, Mixer, History, save_project, load_project
from app.translator.subtitles import Cue

TMP = Path(os.environ["MEDIASTUDIO_HOME"]) / "studio_logic"
TMP.mkdir(parents=True, exist_ok=True)


class SkipTest(Exception):
    pass


def _tone(name, f, dur, sr=24000, amp=0.4):
    p = TMP / name
    t = np.arange(int(dur * sr)) / sr
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes((np.sin(2 * np.pi * f * t) * amp * 32767).astype(np.int16).tobytes())
    return str(p)


def _rms(p):
    with wave.open(str(p)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
    return float(np.sqrt((x ** 2).mean()) / 32768)


def _video(name="v.mp4", size="640x360", dur=4):
    if not shutil.which("ffmpeg"):
        raise SkipTest("no ffmpeg")
    p = TMP / name
    if not p.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={dur}", "-f", "lavfi", "-i",
                        f"sine=frequency=330:duration={dur}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)], check=True)
    return str(p)


# ---------------------------------------------------------------- frame / auto fit
def test_frame_presets_keep_aspect_and_fit_modes():
    f = FrameSettings(preset="9:16")
    assert f.out_size(1920, 1080) == (1080, 1920)
    l, t, w, h = f.picture_rect(1920, 1080)                       # whole picture visible, centred, width = frame width
    assert (l, w) == (0.0, 1.0) and abs((w * 1080) / (h * 1920) - 1920 / 1080) < 1e-6 and abs(t + h / 2 - 0.5) < 1e-9
    f.fit = "fill"
    l, t, w, h = f.picture_rect(1920, 1080)
    assert h == 1.0 and w > 1.0
    assert FrameSettings(preset="1:1").out_size(1920, 1080) == (1920, 1920)
    assert FrameSettings(preset="Custom", custom_w=1000, custom_h=500).out_size(1920, 1080) == (1000, 500)
    f = FrameSettings(preset="9:16", zoom=2.0, off_x=0.1)
    f.auto_choose(1280, 720)                                       # Auto Fit: back to 'whole picture, centred', frame kept
    assert (f.zoom, f.off_x, f.fit, f.preset) == (1.0, 0.0, "fit", "9:16")


# ---------------------------------------------------------------- geometry
def test_geometry_resize_rotate_hit():
    FW, FH = 1000, 500
    it = Item(cx=.5, cy=.5, w=.2, h=.2)
    G.resize(it, "br", 700, 350, FW, FH)
    assert (round(it.cx * FW), round(it.cy * FH), round(it.w * FW), round(it.h * FH)) == (550, 275, 300, 150)
    it = Item(cx=.5, cy=.5, w=.2, h=.2, rot=90)
    G.resize(it, "r", 500, 450, FW, FH)                              # rotated item: the handle moves along ITS axis
    assert (round(it.cx * FW), round(it.cy * FH), round(it.w * FW), round(it.h * FH)) == (500, 300, 300, 100)
    it = Item(cx=.5, cy=.5, w=.2, h=.2)
    G.rotate_to(it, 700, 250, FW, FH); assert round(it.rot) == 90
    G.rotate_to(it, 500, 100, FW, FH); assert round(it.rot) == 0
    assert G.hit_handle(Item(cx=.5, cy=.5, w=.2, h=.2), 600, 300, FW, FH) == "br"
    assert G.contains(Item(cx=.5, cy=.5, w=.2, h=.1, rot=90), 500, 340, FW, FH)
    it = Item(cx=.5, cy=.5, w=.2, h=.2); G.resize(it, "br", 700, 350, FW, FH, keep_aspect=True)
    assert abs(it.w * FW / (it.h * FH) - 2.0) < 1e-6


def test_scene_items_copy_delete_order_and_history():
    sc = Scene()
    a = sc.add(Item(kind="logo", key="logo", path="x.png")); b = sc.add(Item(kind="text", text="hi"))
    c = sc.duplicate(a.id)
    assert c.key == "" and c.id != a.id and len(sc.items) == 3
    sc.move_order(a.id, 2); assert sc.items[-1].id == a.id
    h = History(); h.push(sc); sc.remove(b.id); h.push(sc)
    assert len(h.undo().items) == 3 and len(h.redo().items) == 2
    assert not Item(start=2, end=4).visible_at(1) and Item(start=2, end=4).visible_at(3) and Item(start=2, end=0).visible_at(99, 100)


def test_project_roundtrip_and_damaged_file():
    sc = Scene(); sc.add(Item(kind="blur", key="blur", blur_percent=77)); sc.mirror = True
    mx = Mixer(); mx.channels["sfx"].mute = True; mx.master = 90
    p = TMP / "proj.json"; save_project(p, sc, mx, {"a": 1})
    s2, m2, ex = load_project(p)
    assert s2.items[0].blur_percent == 77 and s2.mirror and m2.channels["sfx"].mute and m2.master == 90 and ex == {"a": 1}
    p.write_text("{broken"); s3, m3, _ = load_project(p)
    assert s3.items == [] and m3.channels["khmer_voice"].volume == 100


# ---------------------------------------------------------------- timeline operations
def test_cue_split_merge_duplicate_delete_keep_data():
    cues = [Cue(0, 4, "原文 a b", "ខ្មែរ one two", audio="x.wav", speaker="S1", emotion="angry", voice_type="Male", speed=1.2)]
    TR.cue_split(cues, 0, 2.0)
    assert (cues[0].end, cues[1].start) == (2.0, 2.0) and cues[0].audio == cues[1].audio == ""
    assert all(c.speaker == "S1" and c.emotion == "angry" and c.voice_type == "Male" and c.speed == 1.2 for c in cues)
    assert (cues[0].translation + cues[1].translation).replace(" ", "") == "ខ្មែរonetwo"
    i = TR.cue_merge(cues, [0, 1]); assert i == 0 and len(cues) == 1 and (cues[0].start, cues[0].end) == (0, 4)
    new = TR.cue_duplicate(cues, [0]); assert len(cues) == 2 and cues[new[0]].start == 4
    TR.cue_delete(cues, [1]); assert len(cues) == 1
    k = "សួស្តីអ្នកទាំងអស់គ្នា"; a, b = TR.split_text(k, 0.5); assert a + b == k and not b[0] in "\u17d2\u17b6\u17c1"   # never inside a cluster


def test_trim_move_snap_and_audio_clips():
    cues = [Cue(1, 3, "a", "b")]
    TR.cue_trim_start(cues, 0, 5); assert cues[0].start <= cues[0].end - TR.MIN_LEN
    TR.cue_move(cues, 0, -3); assert cues[0].start == 0
    assert TR.snap(2.04, [0, 2.0, 5], 0.1) == 2.0 and TR.snap(2.5, [0, 2.0], 0.1) == 2.5
    k = AU.AudioClip(path="a.wav", start=2, dur=4, src_len=10)
    left = [k]; TR.clip_split(left, 0, 4)
    assert (left[0].dur, left[1].start, left[1].src_in, left[1].dur) == (2, 4, 2, 2)
    assert TR.clip_merge(left, [0, 1]) == 0 and left[0].dur == 4 and len(left) == 1
    TR.clip_trim_start(k, 3); assert (k.start, k.src_in, k.dur) == (3, 1, 3)
    TR.clip_trim_end(k, 100); assert k.src_in + k.dur <= 10 + 1e-9


def test_one_history_for_cues_clips_scene():
    cues, clips, sc, h = [Cue(0, 1, "a", "b")], [], Scene(), TR.StudioHistory()
    h.capture(cues, clips, sc); cues[0].translation = "changed"; sc.add(Item(kind="text"))
    ok, s2 = h.undo(cues, clips, sc)
    assert ok and cues[0].translation == "b" and s2.items == []
    ok, s3 = h.redo(cues, clips, sc); assert cues[0].translation == "changed" and len(s3.items) == 1


# ---------------------------------------------------------------- dialogue <-> voice
def test_only_changed_lines_need_a_new_voice_and_timing_check():
    a = _tone("l1.wav", 300, 1.0); b = _tone("l2.wav", 400, 3.0)
    cues = [Cue(0, 2, "o", "ក", audio=a, voice_name="v"), Cue(2.0, 3.0, "o", "ខ", audio=b), Cue(4, 5, "o", "គ")]
    D.measure(cues); assert abs(cues[0].voice_dur - 1.0) < 0.01
    for c in cues[:2]:
        D.mark_generated(c)
    assert D.dirty_rows(cues) == [2]                                    # line 3 has no voice yet
    cues[0].translation = "កក"; assert 0 in D.dirty_rows(cues)          # text changed
    cues[0].translation = "ក"; cues[0].speed = 1.3; assert 0 in D.dirty_rows(cues)   # speed changed
    cues[0].speed = 1.0; assert 0 not in D.dirty_rows(cues)             # same inputs -> same voice, no regeneration
    cues[0].start, cues[0].end = 0.5, 2.5; assert 0 not in D.dirty_rows(cues)        # timing alone keeps the voice
    rep = {r["row"]: r for r in D.check_sync(cues, 10)}
    assert rep[0]["status"] == "ok" and rep[1]["status"] in ("long", "too_long") and rep[2]["status"] == "missing"
    changed = D.extend_to_fit(cues, 10)                                    # end moves up to the next line, never past it, never touches Start
    assert 1 in changed and cues[1].start == 2.0 and cues[1].end <= cues[2].start - 0.02 + 1e-9 and cues[1].end > 3.0


def test_dialogue_save_load_keeps_everything():
    c = Cue(1.5, 3.25, "你好", "សួស្តី", speaker="S2", emotion="sad", voice_type="Female", speed=0.9, voice_name="x", audio_sig="abc", voice_dur=1.2)
    p = TMP / "dlg.json"; D.save_dialogue(p, [c], "fp1")
    back = D.load_dialogue(p, "fp1")[0]
    assert back == c
    assert D.load_dialogue(p, "other-video") == [] and D.load_dialogue(TMP / "nope.json") == []


# ---------------------------------------------------------------- mixer
def test_mixer_gains_mute_solo_reset():
    m = Mixer(); g = m.gains()
    assert g["orig_vocals"] == 0 and g["khmer_voice"] == 1.0                       # dubbing: the original voice is off by default
    m.channels["khmer_voice"].volume = 50; assert m.gains()["khmer_voice"] == 0.5
    m.channels["sfx"].solo = True; g = m.gains(); assert g["sfx"] == 1.0 and g["khmer_voice"] == 0.0
    m.channels["sfx"].mute = True; assert m.gains()["sfx"] == 0.0                  # mute wins over solo
    m.reset(); assert m.gains()["khmer_voice"] == 1.0 and not m.channels["sfx"].solo
    assert Mixer.from_dict(json.loads(json.dumps(m.to_dict()))).to_dict() == m.to_dict()


def test_render_mix_volume_mute_solo_and_sfx_track():
    voice, bg = _tone("voice.wav", 440, 3), _tone("bg.wav", 220, 3, 44100)
    m = Mixer(); full = AU.render_mix({"khmer_voice": voice}, m, TMP / "m1.wav")
    m.channels["khmer_voice"].volume = 50; half = AU.render_mix({"khmer_voice": voice}, m, TMP / "m2.wav")
    assert abs(_rms(half) / _rms(full) - 0.5) < 0.05
    m.channels["khmer_voice"].mute = True
    assert _rms(AU.render_mix({"khmer_voice": voice}, m, TMP / "m3.wav", duration=2)) < 1e-4
    both = {"khmer_voice": voice, "orig_bg": bg}
    solo = AU.render_mix(both, AU.solo_mixer(Mixer(), "orig_bg"), TMP / "m4.wav")
    assert _rms(solo) < _rms(AU.render_mix(both, Mixer(), TMP / "m5.wav"))
    s = _tone("s.wav", 880, 0.5)
    c1, c2 = AU.clip_from_file(s, "sfx", 1.0), AU.clip_from_file(s, "sfx", 2.0)
    out = AU.render_clip_track([c1, c2], 4, TMP / "sfx_track.wav"); pk = AU.waveform_peaks(out)
    assert pk[105] > 0.2 and pk[160] < 0.01 and pk[205] > 0.2 and pk[20] < 0.01     # clips sit where they were placed


def test_stems_are_never_made_by_export_helpers():
    from app.shared.paths import work_dir
    assert not AU.have_stems(str(TMP / "never_separated.mp4"))
    inputs = AU.gather_inputs(str(TMP / "never_separated.mp4"), None, [], [], 3, tmp_dir=TMP)
    assert inputs == {}                                                          # no stems -> nothing is separated behind your back


# ---------------------------------------------------------------- rendering with real FFmpeg
def _frame(video, t, name):
    out = TMP / name
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", video, "-frames:v", "1", str(out)], check=True)
    from PIL import Image
    return Image.open(out).convert("RGB")


def test_export_frame_objects_time_window_rotation_and_mirror():
    from PIL import Image
    from app.translator import video_overlay as VO
    from app.translator.exporter import export_video
    src = _video()
    logo = TMP / "red.png"; Image.new("RGBA", (100, 50), (255, 0, 0, 255)).save(logo)
    sc = Scene(); sc.frame = FrameSettings(preset="9:16")
    sc.add(Item(kind="logo", key="logo", path=str(logo), cx=0.5, cy=0.15, w=0.4, start=1.0, end=2.5))
    sc.add(Item(kind="blur", key="blur", cx=0.5, cy=0.5, w=0.5, h=0.1, blur_percent=90))
    fx = VO.Overlay(); fx.scene = sc
    out = export_video(src, TMP / "o1.mp4", [], None, 1.0, "none", "", fx=fx)
    from app.shared.ffmpeg_util import probe
    i = probe(out); assert (i["width"], i["height"]) == (360, 640) and i["has_audio"]
    at2, at3 = _frame(out, 2.0, "f2.png"), _frame(out, 3.2, "f3.png")
    assert at2.getpixel((180, 96))[0] > 200 and at2.getpixel((180, 96))[1] < 60        # red logo visible inside its window
    assert at3.getpixel((180, 96)) == (0, 0, 0) or at3.getpixel((180, 96))[0] < 40       # gone after it (letterbox bar is black)
    sc2 = Scene(); sc2.mirror = True
    fx2 = VO.Overlay(); fx2.scene = sc2
    o2 = export_video(src, TMP / "o2.mp4", [], None, 1.0, "none", "", fx=fx2)
    a, b = _frame(src, 1.0, "s.png"), _frame(o2, 1.0, "m.png")
    assert a.getpixel((20, 180)) != b.getpixel((20, 180)) and abs(a.getpixel((20, 180))[0] - b.getpixel((620, 180))[0]) < 40


def test_export_subtitle_object_burns_khmer_cues_inside_its_box():
    from app.translator import video_overlay as VO
    from app.translator.exporter import export_video
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not Path(font).exists():
        raise SkipTest("no test font")
    src = _video()
    sc = Scene(); sc.add(Item(kind="subtitle", key="sub", cx=0.5, cy=0.8, w=0.8, h=0.1, size=60, font_file=font, font_name="DejaVu Sans"))
    fx = VO.Overlay(); fx.scene = sc
    out = export_video(src, TMP / "o3.mp4", [Cue(1, 3, "o", "HELLO WORLD")], None, 1.0, "burn", "", fx=fx)
    during, after = _frame(out, 2.0, "d.png"), _frame(out, 3.6, "a.png")
    box = (64, 270, 576, 330)
    bright = lambda im: sum(1 for px in im.crop(box).getdata() if min(px) > 235)
    assert bright(during) > 150 and bright(after) < 20                      # text only while the cue is active, and at the object's place
    assert sum(1 for px in during.crop((0, 0, 640, 200)).getdata() if min(px) > 235) < 30


def test_scene_off_means_old_pipeline_untouched():
    from app.translator import video_overlay as VO
    fx = VO.Overlay(); assert not fx.active()
    fx.scene = Scene(); assert not fx.active()                              # empty scene: no re-encode
    fx.scene.mirror = True; assert fx.active() and fx.copy().scene.mirror
