"""STEP 2 tests - need NO Qt, but need numpy + ffmpeg.  Run from the project root:  python tests/test_step2_audio.py -v
They test the model, the MixEngine (what the speakers would receive) and the whole six-track render with real ffmpeg.
They can NOT test QAudioSink / QMediaPlayer / the widgets (see STEP2_NOTES.md)."""
import json, os, shutil, subprocess, sys, tempfile, unittest, wave
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.studio import model as M                      # noqa: E402
from app.studio import audio as AU                     # noqa: E402
from app.studio import engine as EN                    # noqa: E402
from app.shared.paths import work_dir                  # noqa: E402
from app.translator.subtitles import Cue               # noqa: E402

SR = 44100
FREQ = {"orig_vocals": 220, "khmer_voice": 330, "ai_voice": 440, "orig_bg": 550, "bgm": 660, "sfx": 880}   # one tone per track


def tone(freq, secs, amp=0.1, sr=SR, ch=2):
    t = np.arange(int(secs * sr)) / sr
    x = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype("<i2")
    return np.repeat(x[:, None], ch, axis=1) if ch > 1 else x


def write_wav(path, data, sr=SR):
    ch = 1 if data.ndim == 1 else data.shape[1]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr); w.writeframes(data.tobytes())
    return str(path)


def level(pcm_bytes, freq):
    """amplitude (0..1) of `freq` Hz in an int16 stereo byte block (block length = whole seconds -> exact FFT bins)"""
    a = np.frombuffer(pcm_bytes, dtype="<i2").reshape(-1, 2)[:, 0].astype(np.float64) / 32768.0
    spec = np.abs(np.fft.rfft(a)) / (len(a) / 2)
    return float(spec[int(round(freq * len(a) / SR))])


def render_seconds(eng, secs, chunk=2205):
    out, left = [], int(round(secs * SR))
    while left > 0:
        n = min(chunk, left)
        out.append(eng.render(n)); left -= n
    return b"".join(out)


def peak_levels(pcm):
    return {k: level(pcm, f) for k, f in FREQ.items()}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ms_step2_")
        cls.wav = {k: write_wav(os.path.join(cls.tmp, f"{k}.wav"), tone(f, 12)) for k, f in FREQ.items()}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def engine(self, keys=None, mixer=None):
        e = EN.MixEngine(mixer or M.Mixer())
        e.mixer.reset()
        for c in e.mixer.channels.values():
            c.volume = 100
        e.set_sources({k: EN.TrackSource(self.wav[k]) for k in (keys or FREQ)}, 12.0, "t")
        return e


# ---------------------------------------------------------------------------------------------------------------- model
class TestModel(unittest.TestCase):
    def test_six_tracks_separate(self):
        self.assertEqual(set(M.MIXER_TRACKS), set(FREQ))
        self.assertEqual(len(M.MIXER_TRACKS), 6)
        self.assertIn("khmer_voice", M.MIXER_TRACKS); self.assertIn("ai_voice", M.MIXER_TRACKS)

    def test_old_project_5_channels_loads(self):
        old = {"master": 80, "channels": {k: {"volume": 70, "mute": False, "solo": False}
                                           for k in ("orig_vocals", "orig_bg", "khmer_voice", "sfx", "bgm")}}
        m = M.Mixer.from_dict(json.loads(json.dumps(old)))
        self.assertEqual(m.master, 80)
        self.assertEqual(m.channels["khmer_voice"].volume, 70)
        self.assertEqual(m.channels["ai_voice"].volume, 100)                    # the new channel gets its default
        self.assertTrue(all(c.enabled for c in m.channels.values()))            # old files: everything enabled
        self.assertEqual(M.Mixer.from_dict(m.to_dict()).to_dict(), m.to_dict())  # new files round-trip

    def test_mute_solo_enable(self):
        m = M.Mixer(); [setattr(c, "volume", 100) for c in m.channels.values()]
        m.channels["sfx"].mute = True
        g = m.gains(); self.assertEqual(g["sfx"], 0.0); self.assertTrue(all(g[k] == 1.0 for k in g if k != "sfx"))
        m.channels["sfx"].mute = False; m.channels["ai_voice"].solo = True
        g = m.gains(); self.assertEqual([k for k, v in g.items() if v > 0], ["ai_voice"])
        m.channels["ai_voice"].enabled = False                                   # a disabled track's solo flag must not silence the others
        g = m.gains(); self.assertEqual(g["ai_voice"], 0.0); self.assertTrue(all(g[k] == 1.0 for k in g if k != "ai_voice"))
        m.channels["khmer_voice"].solo = True; m.channels["khmer_voice"].mute = True   # mute wins over solo
        self.assertEqual(m.gains()["khmer_voice"], 0.0)

    def test_master(self):
        m = M.Mixer(); m.master = 50
        for c in m.channels.values():
            c.volume = 100
        self.assertTrue(all(abs(v - 0.5) < 1e-9 for v in m.gains().values()))


# ---------------------------------------------------------------------------------------------------------------- engine
class TestEngine(Base):
    def test_each_track_alone(self):
        for k in FREQ:
            e = self.engine([k])
            lv = peak_levels(render_seconds(e, 1))
            self.assertAlmostEqual(lv[k], 0.1, delta=0.005, msg=k)
            self.assertTrue(all(v < 0.003 for kk, v in lv.items() if kk != k), (k, lv))

    def test_all_six_together_no_duplicates(self):
        e = self.engine()
        lv = peak_levels(render_seconds(e, 1))
        for k in FREQ:
            self.assertAlmostEqual(lv[k], 0.1, delta=0.005, msg=k)                # each exactly once (a duplicate would show 0.2)
        self.assertEqual(e.cursor, SR)                                           # one cursor, exactly 1 s consumed

    def test_mute_solo_enable_in_engine(self):
        e = self.engine()
        e.mixer.channels["khmer_voice"].mute = True
        lv = peak_levels(render_seconds(e, 1)); self.assertLess(lv["khmer_voice"], 0.003); self.assertGreater(lv["ai_voice"], 0.09)
        e.mixer.channels["khmer_voice"].mute = False; e.mixer.channels["sfx"].solo = True
        render_seconds(e, 0.1); lv = peak_levels(render_seconds(e, 1))           # 0.1 s: lets the gain ramp finish
        self.assertGreater(lv["sfx"], 0.09); self.assertTrue(all(v < 0.003 for k, v in lv.items() if k != "sfx"))
        e.mixer.channels["sfx"].solo = False; e.mixer.channels["bgm"].enabled = False
        render_seconds(e, 0.1); lv = peak_levels(render_seconds(e, 1))
        self.assertLess(lv["bgm"], 0.003); self.assertGreater(lv["sfx"], 0.09)

    def test_volume_change_during_playback(self):
        e = self.engine(["ai_voice"])
        before = level(render_seconds(e, 1), 440)
        e.mixer.channels["ai_voice"].volume = 50                                 # slider moved while playing
        chunk = e.render(2205)                                                   # the very next chunk already contains the ramp
        first = np.abs(np.frombuffer(chunk, dtype="<i2")).astype(float)
        self.assertLess(first[-200:].max(), 0.6 * 0.1 * 32768 * 1.0)             # end of that chunk is already near 50 %
        after = level(render_seconds(e, 1), 440)
        self.assertAlmostEqual(after / before, 0.5, delta=0.02)
        e.mixer.master = 50
        render_seconds(e, 0.1)
        self.assertAlmostEqual(level(render_seconds(e, 1), 440) / before, 0.25, delta=0.02)

    def test_volume_200_clips_not_wraps(self):
        loud = write_wav(os.path.join(self.tmp, "loud.wav"), tone(440, 2, amp=0.5))                 # 0.5 x 2 x 2 = 2.0 -> must saturate
        e = EN.MixEngine(M.Mixer()); e.set_sources({"ai_voice": EN.TrackSource(loud)}, 2.0, "t")
        e.mixer.channels["ai_voice"].volume = 200; e.mixer.master = 200
        a = np.frombuffer(render_seconds(e, 1), dtype="<i2")
        self.assertLessEqual(int(a.max()), 32767); self.assertGreater(int(a.max()), 30000)   # saturates, never wraps to negative

    def test_pause_resume_continuous(self):
        e1, e2 = self.engine(["sfx", "bgm"]), self.engine(["sfx", "bgm"])
        straight = render_seconds(e1, 2)
        part1 = render_seconds(e2, 0.7)
        t = e2.time                                                              # --- pause: nothing is rendered, the cursor stays
        self.assertAlmostEqual(t, 0.7, places=3)
        part2 = render_seconds(e2, 1.3)                                          # --- resume
        self.assertEqual(part1 + part2, straight)                                # no gap, no repeated samples

    def test_seek_matches_source(self):
        e = self.engine(["sfx"])
        e.seek(5.0)
        got = np.frombuffer(e.render(SR // 2), dtype="<i2").reshape(-1, 2)
        src = EN.TrackSource(self.wav["sfx"]).data[5 * SR:5 * SR + SR // 2]
        self.assertLessEqual(int(np.abs(got.astype(int) - src.astype(int)).max()), 1)    # same samples as the file at 5.0 s
        e.seek(1.0); self.assertAlmostEqual(e.time, 1.0, places=4)               # seek backwards works too
        e.seek(100.0); self.assertEqual(e.render(100).__len__(), 400)            # past the end: silence, no crash
        self.assertTrue(e.at_end())

    def test_listen_only_one_track(self):
        e = self.engine(); e.mixer.channels["sfx"].mute = True; e.listen_only = "sfx"
        render_seconds(e, 0.1); lv = peak_levels(render_seconds(e, 1))
        self.assertGreater(lv["sfx"], 0.09); self.assertTrue(all(v < 0.003 for k, v in lv.items() if k != "sfx"))
        e.listen_only = None


# ---------------------------------------------------------------------------------------------------------------- whole pipeline
@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class TestSixTrackPipeline(Base):
    """Real ffmpeg: render the six tracks exactly like the Play button does, then mix them with the engine."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.video = os.path.join(cls.tmp, "step2_test_video.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=12:r=10", "-f", "lavfi", "-i",
                        f"sine=frequency={FREQ['orig_vocals']}:sample_rate=48000:duration=12", "-ac", "1", "-shortest",
                        "-c:v", "libx264", "-c:a", "aac", cls.video], check=True)
        d = AU.stem_dir(cls.video); d.mkdir(parents=True, exist_ok=True)         # fake Demucs stems: vocals=220 Hz, background=550 Hz
        write_wav(d / "vocals.wav", tone(FREQ["orig_vocals"], 12)); write_wav(d / "no_vocals.wav", tone(FREQ["orig_bg"], 12))
        cls.stems = d
        cls.cue_wav = write_wav(os.path.join(cls.tmp, "kh.wav"), tone(FREQ["khmer_voice"], 2, amp=0.2, sr=24000, ch=1), sr=24000)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(work_dir("step2_test_video"), ignore_errors=True)
        super().tearDownClass()

    def project(self):
        cues = [Cue(start=2.0, end=4.0, translation="x", audio=self.cue_wav), Cue(start=7.0, end=9.0, translation="y", audio=self.cue_wav)]
        mk = lambda tr, f, start: AU.AudioClip(track=tr, path=self.wav[f], name=f, start=start, dur=4.0, src_len=12.0)
        clips = [mk("ai_voice", "ai_voice", 3.0), mk("bgm", "bgm", 0.0), mk("sfx", "sfx", 5.0)]
        return cues, clips

    def prep(self, mixer, tag):
        cues, clips = self.project()
        out = EN.prepare_sources(self.video, cues, clips, mixer, 12.0, os.path.join(self.tmp, "pb_" + tag))
        return {k: EN.TrackSource(p) for k, p in out.items()}

    def test_six_tracks_end_to_end(self):
        m = M.Mixer(); m.channels["orig_vocals"].volume = 100
        srcs = self.prep(m, "all")
        self.assertEqual(set(srcs), set(FREQ))                                   # six separate tracks, Khmer Voice and AI Voice not merged
        self.assertNotEqual(srcs["khmer_voice"].path, srcs["ai_voice"].path)
        e = EN.MixEngine(m); e.set_sources(srcs, 12.0, "x")
        # at 3.0-4.0 s everything overlaps: orig(0-12) + khmer(2-4) + ai(3-7) + bg(0-12) + bgm(0-4) ; sfx starts at 5 s
        e.seek(3.0); lv = peak_levels(render_seconds(e, 1))
        for k in ("orig_vocals", "khmer_voice", "ai_voice", "orig_bg", "bgm"):
            self.assertGreater(lv[k], 0.05, (k, lv))
        self.assertLess(lv["sfx"], 0.003)                                        # sfx clip has not started: no accidental playback
        e.seek(5.0); lv = peak_levels(render_seconds(e, 1))
        self.assertGreater(lv["sfx"], 0.05); self.assertGreater(lv["ai_voice"], 0.05)
        self.assertLess(lv["khmer_voice"], 0.003); self.assertLess(lv["bgm"], 0.003)       # their clips ended
        e.seek(10.0); lv = peak_levels(render_seconds(e, 1))
        self.assertLess(lv["sfx"], 0.003); self.assertLess(lv["ai_voice"], 0.003)

    def test_clip_timing_sample_accurate(self):
        m = M.Mixer(); srcs = self.prep(m, "timing")
        a = np.abs(srcs["ai_voice"].data[:, 0].astype(int))                      # AI Voice clip was placed at 3.000 s
        onset = int(np.argmax(a > 500)) / SR
        self.assertAlmostEqual(onset, 3.0, delta=0.012)
        k = np.abs(srcs["khmer_voice"].data[:, 0].astype(int)); self.assertAlmostEqual(int(np.argmax(k > 500)) / SR, 2.0, delta=0.05)
        s = np.abs(srcs["sfx"].data[:, 0].astype(int)); self.assertAlmostEqual(int(np.argmax(s > 500)) / SR, 5.0, delta=0.012)

    def test_disabled_track_not_rendered_nor_played(self):
        m = M.Mixer(); m.channels["sfx"].enabled = False; m.channels["ai_voice"].enabled = False
        srcs = self.prep(m, "dis")
        self.assertNotIn("sfx", srcs); self.assertNotIn("ai_voice", srcs)
        e = EN.MixEngine(m); e.set_sources(srcs, 12.0, "x"); e.seek(5.0)
        lv = peak_levels(render_seconds(e, 1)); self.assertLess(lv["sfx"], 0.003); self.assertLess(lv["ai_voice"], 0.003)

    def test_original_audio_fallback_without_demucs(self):
        for f in self.stems.glob("*.wav"):
            f.rename(f.with_suffix(".bak"))
        try:
            m = M.Mixer(); m.channels["orig_vocals"].volume = 100
            srcs = self.prep(m, "nodemucs")
            self.assertIn("orig_vocals", srcs); self.assertNotIn("orig_bg", srcs)
            e = EN.MixEngine(m); e.set_sources(srcs, 12.0, "x"); e.seek(8.0)
            self.assertAlmostEqual(level(render_seconds(e, 1), 220), 0.1, delta=0.03)      # the video's own audio (aac) is there
            m0 = M.Mixer()                                                                  # nothing else + Original at 0 % -> engine idle
            out = EN.prepare_sources(self.video, [], [], m0, 12.0, os.path.join(self.tmp, "pb_idle"))
            self.assertEqual(out, {})
        finally:
            for f in self.stems.glob("*.bak"):
                f.rename(f.with_suffix(".wav"))

    def test_signature(self):
        cues, clips = self.project(); m = M.Mixer()
        s0 = EN.build_signature(self.video, cues, clips, m, 12.0)
        m.channels["sfx"].volume = 20; m.channels["sfx"].mute = True; m.master = 30; m.channels["bgm"].solo = True
        self.assertEqual(EN.build_signature(self.video, cues, clips, m, 12.0), s0)         # live settings do not re-render
        clips[0].start += 0.5
        self.assertNotEqual(EN.build_signature(self.video, cues, clips, m, 12.0), s0)      # editing a clip does
        clips[0].start -= 0.5; m.channels["bgm"].enabled = False
        self.assertNotEqual(EN.build_signature(self.video, cues, clips, m, 12.0), s0)      # enable/disable does

    def test_export_path_accepts_ai_voice(self):
        cues, clips = self.project()
        ins = AU.gather_inputs(self.video, None, [], [], 12.0, tmp_dir=os.path.join(self.tmp, "ex"), ai_clips=[clips[0]])
        self.assertIn("ai_voice", ins)
        m = M.Mixer(); out = AU.render_mix(ins, m, os.path.join(self.tmp, "ex", "mix.wav"))
        with wave.open(out) as w:
            self.assertEqual((w.getframerate(), w.getnchannels()), (44100, 2))


if __name__ == "__main__":
    unittest.main()
