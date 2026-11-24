import os, shutil, subprocess, tempfile, wave, json
from pathlib import Path
import numpy as np
from app.translator import project_state as PS
from app.translator.subtitles import Cue

FF = shutil.which("ffmpeg")


class SkipTest(Exception): pass


def need_ffmpeg():
    if not FF: raise SkipTest("ffmpeg not installed")


def tmp(): return Path(tempfile.mkdtemp(prefix="ms_t_"))


def sine(path, freq, dur, vol=4.0):   # ffmpeg's sine is 0.125 peak, so 4.0 -> 0.5 peak
    subprocess.run([FF, "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}", "-af", f"volume={vol}",
                    "-ac", "1", "-ar", "24000", str(path)], check=True)


def read(path):
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return (a.reshape(-1, ch).mean(axis=1) if ch > 1 else a), sr


def onset(a, sr, thr=0.02):
    i = int(np.argmax(np.abs(a) > thr)); return i / sr


def rms(a, sr, t0, t1):
    s = a[int(t0 * sr):int(t1 * sr)]; return float(np.sqrt(np.mean(s ** 2))) if len(s) else 0.0


# ------------------------------------------------------------------ state / resume / chunks
def test_state_resume_skips_done_steps_and_survives_restart():
    d = tmp(); video = d / "v.mp4"; video.write_bytes(os.urandom(5000))
    art = d / "audio.wav"; art.write_bytes(b"x")
    ran = []
    st = PS.ProjectState.open_or_create(d / "p.json", video)
    st.run_step("extract_audio", lambda: (ran.append("a"), st.steps["extract_audio"].artifacts.update(a=str(art))), {"sr": 16000})
    st.run_step("whisper", lambda: ran.append("w"), {"model": "small"})
    # --- "restart the application": fresh objects from disk
    st2 = PS.ProjectState.open_or_create(d / "p.json", video)
    _, skipped = st2.run_step("extract_audio", lambda: ran.append("a2"), {"sr": 16000})
    assert skipped and ran == ["a", "w"]
    _, skipped = st2.run_step("whisper", lambda: ran.append("w2"), {"model": "medium"})   # param changed -> redo
    assert not skipped and ran == ["a", "w", "w2"]
    art.unlink()                                                                           # cached file gone -> redo
    assert not PS.ProjectState.load(d / "p.json").is_done("extract_audio", {"sr": 16000})


def test_interrupted_running_step_is_redone_and_failure_is_recorded_without_secrets():
    d = tmp(); video = d / "v.bin"; video.write_bytes(b"1" * 100)
    st = PS.ProjectState.open_or_create(d / "p.json", video)
    st.start("translation", {"e": 1})                      # app killed while running
    assert PS.ProjectState.load(d / "p.json").steps["translation"].status == PS.PENDING
    try: st.run_step("voice", lambda: (_ for _ in ()).throw(RuntimeError("bad key sk-ant-ABCDEFGHIJKLMN")))
    except RuntimeError: pass
    r = PS.ProjectState.load(d / "p.json").steps["voice"]
    assert r.status == PS.FAILED and "ABCDEFGH" not in r.error


def test_other_video_does_not_reuse_state():
    d = tmp(); a = d / "a.bin"; b = d / "b.bin"; a.write_bytes(b"A" * 3000); b.write_bytes(b"B" * 3000)
    s = PS.ProjectState.open_or_create(d / "p.json", a); s.finish("ocr"); 
    s2 = PS.ProjectState.open_or_create(d / "p.json", b)
    assert s2.steps["ocr"].status == PS.PENDING and (d / "p.json.bak").exists()


def test_pause_resume_cancel():
    import threading, time
    c = PS.RunControl(); out = []
    def work():
        try:
            for i in range(50):
                c.check(); out.append(i); time.sleep(0.01)
        except PS.Cancelled: out.append("cancelled")
    c.pause(); t = threading.Thread(target=work); t.start(); time.sleep(0.15)
    assert out == []                      # paused: nothing ran
    c.resume(); time.sleep(0.1); assert out
    c.cancel(); t.join(2); assert out[-1] == "cancelled"


def test_chunk_plan_offsets_and_speaker_ids_not_assumed_same():
    ch = PS.plan_chunks(1190, 600, 0)
    assert [(c["start"], c["end"]) for c in ch] == [(0, 600), (600, 1190)]
    assert [(c["start"], c["end"]) for c in PS.plan_chunks(1210, 600)] == [(0, 600), (600, 1210)]   # 10 s tail absorbed, no tiny chunk
    assert len(PS.plan_chunks(1300, 600)) == 3 and len(PS.plan_chunks(620, 600)) == 1
    r0 = [Cue(10, 12, "a", speaker="Speaker 1")]; r1 = [Cue(5, 7, "b", speaker="Speaker 1")]
    m = PS.merge_chunk_cues([r0, r1], ch)
    assert [(c.start, c.end) for c in m] == [(10, 12), (605, 607)]
    assert [c.speaker for c in m] == ["c0:Speaker 1", "c1:Speaker 1"]          # NOT merged automatically
    sug = PS.link_speakers({"c0:Speaker 1": [1, 0], "c1:Speaker 1": [1, .05], "c1:Speaker 2": [0, 1]})
    assert [(a, b) for a, b, _ in sug] == [("c0:Speaker 1", "c1:Speaker 1")]   # only a suggestion


# ------------------------------------------------------------------ sync + export with real FFmpeg
def build_track(cues, total):
    from app.translator import voice_synchronizer as VS
    out = tmp() / "voice.wav"; rep = []
    over = VS.build_voice_track(cues, total, out, report=rep)
    return out, over, rep


def test_sync_places_each_clip_at_its_row_start_and_reports_overflow():
    need_ffmpeg()
    d = tmp()
    for n, (f, dur) in enumerate([(440, 1.0), (880, 1.0), (1320, 4.0), (660, 1.0)]): sine(d / f"c{n}.wav", f, dur)
    cues = [Cue(9.0, 10.5, "c", audio=str(d / "c1.wav")),          # deliberately NOT in time order
            Cue(1.0, 2.5, "a", audio=str(d / "c0.wav")),
            Cue(15.0, 16.0, "n", audio=str(d / "c3.wav")),        # next row -> limits the slot of the long clip
            Cue(13.0, 15.0, "x", audio=str(d / "c2.wav"))]         # 4 s clip, 2 s slot -> must be trimmed + reported
    out, over, rep = build_track(cues, 20)
    a, sr = read(out)
    assert abs(onset(a[int(0.5 * sr):], sr) + 0.5 - 1.0) < 0.06
    assert abs(onset(a[int(8.0 * sr):], sr) + 8.0 - 9.0) < 0.06
    assert abs(onset(a[int(12.0 * sr):], sr) + 12.0 - 13.0) < 0.06
    assert rms(a, sr, 4, 8) < 0.001 and rms(a, sr, 17, 20) < 0.001      # silence elsewhere
    assert over == [3] and rep[0]["line"] == 4
    seg = a[int(15.1 * sr):int(15.9 * sr)]; spec = np.abs(np.fft.rfft(seg)); fr = np.fft.rfftfreq(len(seg), 1 / sr)
    p = lambda f: spec[np.argmin(abs(fr - f))]   # noqa
    assert p(660) > 20 * p(1320)                                       # long clip was cut at its slot (15.0), it did not run over the next row
    pre = a[int(14.5 * sr):int(14.9 * sr)]; sp2 = np.abs(np.fft.rfft(pre)); f2 = np.fft.rfftfreq(len(pre), 1 / sr)
    assert sp2[np.argmin(abs(f2 - 1320))] > 20 * sp2[np.argmin(abs(f2 - 660))]   # before 15.0 only the long clip; 660 Hz row starts at 15.0


def test_sync_different_speakers_overlap_is_mixed_not_moved():
    need_ffmpeg()
    d = tmp(); sine(d / "a.wav", 440, 2.0); sine(d / "b.wav", 880, 2.0)
    cues = [Cue(1.0, 3.0, "a", audio=str(d / "a.wav"), speaker="A"), Cue(2.0, 4.0, "b", audio=str(d / "b.wav"), speaker="B")]
    out, _, _ = build_track(cues, 6); a, sr = read(out)
    assert abs(onset(a[int(1.5 * sr):], sr) + 1.5 - 1.0) < 1.0           # sanity
    seg = a[int(2.2 * sr):int(2.8 * sr)]
    spec = np.abs(np.fft.rfft(seg)); fr = np.fft.rfftfreq(len(seg), 1 / sr)
    p = lambda f: spec[np.argmin(abs(fr - f))]   # noqa
    assert p(440) > 50 and p(880) > 50                                     # both voices audible in the overlap


def make_video(path, dur=12):
    subprocess.run([FF, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={dur}:r=25",
                    "-f", "lavfi", "-i", f"sine=frequency=200:duration={dur}", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(path)], check=True)


def extract_audio(video, wav):
    subprocess.run([FF, "-y", "-v", "error", "-i", str(video), "-ac", "1", "-ar", "24000", str(wav)], check=True)
    return read(wav)


def test_export_dubbed_video_no_subs_with_original_volume_and_voice_timing():
    need_ffmpeg()
    from app.translator.exporter import export_video
    from app.shared.ffmpeg_util import probe
    d = tmp(); v = d / "v.mp4"; make_video(v)
    sine(d / "k.wav", 1000, 1.5, 6.0)
    cues = [Cue(4.0, 5.5, "你好", "សួស្តី", audio=str(d / "k.wav"))]
    voice, _, _ = build_track(cues, 12)
    out = export_video(v, d / "out.mp4", cues, voice_wav=voice, original_volume=0.1, subs="none", voice_volume=1.0)
    info = probe(out)
    assert info["has_audio"] and abs(info["duration"] - 12) < 0.3
    sub = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "s", "-show_entries", "stream=index", "-of", "csv", out], capture_output=True, text=True)
    assert sub.stdout.strip() == ""                                        # NO subtitle stream
    a, sr = extract_audio(out, d / "o.wav")
    assert abs(onset(a[int(2 * sr):], sr, 0.15) + 2 - 4.0) < 0.15            # Khmer voice starts at the row start
    oa, _ = extract_audio(v, d / "orig.wav")
    ratio = rms(a, sr, 7, 11) / rms(oa, sr, 7, 11)
    assert 0.07 < ratio < 0.13                                              # original kept quietly: ~0.1 x


def test_export_volumes_and_mute_original():
    need_ffmpeg()
    from app.translator.exporter import export_video
    d = tmp(); v = d / "v.mp4"; make_video(v); sine(d / "k.wav", 1000, 2.0, 6.0)
    cues = [Cue(2.0, 4.0, "a", "b", audio=str(d / "k.wav"))]
    voice, _, _ = build_track(cues, 12)
    loud = export_video(v, d / "loud.mp4", cues, voice_wav=voice, original_volume=0.0, subs="none", voice_volume=1.0)
    quiet = export_video(v, d / "quiet.mp4", cues, voice_wav=voice, original_volume=0.0, subs="none", voice_volume=0.25)
    la, sr = extract_audio(loud, d / "l.wav"); qa, _ = extract_audio(quiet, d / "q.wav")
    assert rms(la, sr, 2.5, 3.5) > 3 * rms(qa, sr, 2.5, 3.5) > 0                # Khmer volume control works
    assert rms(la, sr, 7, 11) < 0.005                                            # original muted (volume 0)
    both = export_video(v, d / "both.mp4", cues, voice_wav=voice, original_volume=0.3, subs="none", voice_volume=0.25)
    ba, _ = extract_audio(both, d / "b.wav")
    assert rms(ba, sr, 2.5, 3.5) > 0 and 0.02 < rms(ba, sr, 7, 11) < 0.2         # voice volume honoured together with original (fixed bug)


def test_export_soft_subtitles_and_original_video_untouched():
    need_ffmpeg()
    from app.translator.exporter import export_video
    d = tmp(); v = d / "v.mp4"; make_video(v); size = v.stat().st_size; data = v.read_bytes()
    cues = [Cue(1, 3, "你好", "សួស្តី")]
    out = export_video(v, d / "s.mp4", cues, voice_wav=None, subs="soft")
    r = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "s", "-show_entries", "stream=codec_name", "-of", "csv", out], capture_output=True, text=True)
    assert "mov_text" in r.stdout
    assert v.read_bytes() == data                                              # source never modified


def test_preview_mix_has_requested_length():
    need_ffmpeg()
    from app.translator.exporter import preview_mix
    from app.shared.ffmpeg_util import probe
    d = tmp(); v = d / "v.mp4"; make_video(v); sine(d / "k.wav", 1000, 2.0)
    cues = [Cue(2.0, 4.0, "a", "b", audio=str(d / "k.wav"))]
    voice, _, _ = build_track(cues, 12)
    p = preview_mix(v, voice, d / "prev.wav", original_volume=0.2, voice_volume=0.8, start=1.0, duration=4.0)
    assert abs(probe(p)["duration"] - 4.0) < 0.15
