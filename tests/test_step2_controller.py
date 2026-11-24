"""STEP 2 - controller LOGIC test with a stubbed PySide6 (no Qt, no sound card).  It runs the real PlaybackMixin code from
app/ui/playback.py against fake player / fake audio output and checks Play / Pause / Stop / Seek / Resume / drift / re-render rules.
It does NOT test real QMediaPlayer / QAudioSink behaviour.   Run from the project root:  python tests/test_step2_controller.py -v"""
import os, shutil, subprocess, sys, tempfile, types, unittest, wave
from types import SimpleNamespace
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------- minimal PySide6 stub
class _Sig:
    def __init__(self): self.s = []
    def connect(self, f): self.s.append(f)
    def emit(self, *a):
        for f in list(self.s): f(*a)

class Signal:
    def __init__(self, *a): pass
    def __set_name__(self, o, n): self.n = "_sig_" + n
    def __get__(self, obj, t=None):
        if obj is None: return self
        if not hasattr(obj, self.n): setattr(obj, self.n, _Sig())
        return getattr(obj, self.n)

class QObject:
    def __init__(self, *a, **k): pass

class QTimer(QObject):
    timeout = Signal()
    def __init__(self, *a): self.on = False
    def setInterval(self, ms): pass
    def start(self): self.on = True
    def stop(self): self.on = False

class QThread(QObject):
    def isRunning(self): return False
    def start(self): self.run()                       # synchronous: results arrive immediately

for name, attrs in {"PySide6": {}, "PySide6.QtCore": dict(QObject=QObject, QTimer=QTimer, QThread=QThread, Signal=Signal, QRunnable=object),
                    "PySide6.QtWidgets": dict(QMessageBox=SimpleNamespace(information=lambda *a, **k: None))}.items():
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m
sys.modules["PySide6"].QtCore = sys.modules["PySide6.QtCore"]; sys.modules["PySide6"].QtWidgets = sys.modules["PySide6.QtWidgets"]

from app.ui import playback as PB          # noqa: E402  (real file, stubbed Qt)
from app.studio import model as M, audio as AU, engine as EN   # noqa: E402
from app.shared.paths import work_dir      # noqa: E402

SR = 44100
def wavf(p, f, secs, amp=0.1):
    t = np.arange(int(secs * SR)) / SR
    x = np.repeat((np.sin(2 * np.pi * f * t) * amp * 32767).astype("<i2")[:, None], 2, axis=1)
    with wave.open(p, "wb") as w: w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(x.tobytes())
    return p


class FakePlayer:
    StoppedState, PlayingState, PausedState = 0, 1, 2
    def __init__(self): self.state, self.ms = 0, 0; self.playbackStateChanged = _Sig(); self.calls = []
    def playbackState(self): return self.state
    def position(self): return self.ms
    def setPosition(self, ms): self.ms = ms; self.calls.append(("seek", ms))
    def _set(self, s): self.state = s; self.playbackStateChanged.emit(s)
    def play(self): self.calls.append("play"); self._set(1)
    def pause(self): self.calls.append("pause"); self._set(2)


class FakeOut:
    available = True
    def __init__(self, engine): self.engine, self.running, self.starts, self.stops, self.audible = engine, False, [], 0, None
    def start(self, t): self.running = True; self.starts.append(t); self.engine.seek(t)
    def stop(self): self.running = False; self.stops += 1
    def audible_time(self): return self.engine.time if self.audible is None else self.audible


class Page(PB.PlaybackMixin):
    def __init__(self, video, clips):
        self.mixer = M.Mixer(); self.cues = []; self.clips = clips; self.video = video; self.vid_dur = 12.0
        self.voice_off = SimpleNamespace(value=lambda: 0.0); self.btn_play = SimpleNamespace(setEnabled=lambda v: None)
        self.log = SimpleNamespace(msgs=[], appendPlainText=lambda m: self.log.msgs.append(m))
        self.vplayer = FakePlayer(); self._QMP = FakePlayer
        self.vaudio = SimpleNamespace(muted=None, volume=0.8,
                                      setMuted=lambda v: setattr(self.vaudio, "muted", v),
                                      setVolume=lambda v: setattr(self.vaudio, "volume", v))
        self._pb_init(); self._pb_out = FakeOut(self.engine); self._pb_wire(); self.prepares = 0
        orig = self._pb_prepare
        def counted(then_play=False): self.prepares += 1; orig(then_play)
        self._pb_prepare = counted
    def _playing(self): return self.vplayer.state == FakePlayer.PlayingState


class TestAudioOutputBuffering(unittest.TestCase):
    def test_partial_device_write_is_retained_before_more_audio_is_rendered(self):
        class Engine:
            time = 0.0
            def __init__(self): self.calls = 0
            def render(self, frames):
                self.calls += 1
                self.time += frames / EN.SR
                return bytes(frames * EN.FRAME)
        class Sink:
            def bytesFree(self): return 4096 * EN.FRAME
            def bufferSize(self): return 4096 * EN.FRAME
        class Device:
            def __init__(self): self.total = 0
            def write(self, payload):
                n = min(100, len(payload)); self.total += n; return n
        obj = PB.AudioOutput.__new__(PB.AudioOutput)
        obj.engine, obj._sink, obj._io = Engine(), Sink(), Device()
        obj._pending = b""
        obj._pump()
        self.assertEqual(obj.engine.calls, 1)  # no second render while first payload remains queued
        self.assertGreater(len(obj._pending), 0)
        self.assertEqual(obj._pending, bytes(4096 * EN.FRAME - obj._io.total))
        self.assertLess(obj.audible_time(), obj.engine.time)  # includes the unwritten tail in its cursor correction


class TestController(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ms_ctl_")
        cls.video = os.path.join(cls.tmp, "step2_ctl_video.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=12:r=10", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=12", "-shortest", "-c:v", "libx264", "-c:a", "aac", cls.video], check=True)
        cls.sfx = wavf(os.path.join(cls.tmp, "sfx.wav"), 880, 5)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True); shutil.rmtree(work_dir("step2_ctl_video"), ignore_errors=True)

    def page(self, with_clip=True):
        clips = [AU.AudioClip(track="sfx", path=self.sfx, name="sfx", start=1.0, dur=4.0, src_len=5.0)] if with_clip else []
        return Page(self.video, clips)

    def test_play_prepares_then_plays_everything_together(self):
        p = self.page()
        p._pb_play_pressed()
        self.assertEqual(p.prepares, 1)
        self.assertEqual(p.vplayer.calls, ["play"])                    # video started once, AFTER the audio was ready
        self.assertTrue(p._pb_out.running); self.assertEqual(len(p._pb_out.starts), 1)   # audio started by the same Play (no 2nd player)
        self.assertTrue(p.vaudio.muted)                                # the video's own audio is muted: no double audio
        self.assertIn("sfx", p.engine.sources); self.assertIn("orig_vocals", p.engine.sources)

    def test_pause_resume_stop(self):
        p = self.page(); p._pb_play_pressed(); p.vplayer.ms = 3000
        p.vplayer.pause()
        self.assertFalse(p._pb_out.running)                            # pause stops audio
        p.vplayer.ms = 3000; p._pb_play_pressed()                      # resume: no re-render, audio restarts at the video position
        self.assertEqual(p.prepares, 1); self.assertEqual(p._pb_out.starts[-1], 3.0); self.assertTrue(p._pb_out.running)
        p._pb_stop()
        self.assertFalse(p._pb_out.running); self.assertEqual(p.vplayer.position(), 0)
        p.vplayer.state = FakePlayer.StoppedState; p.vplayer.playbackStateChanged.emit(0)   # end of media
        self.assertFalse(p._pb_out.running)

    def test_seek_moves_audio_with_video(self):
        p = self.page(); p._pb_play_pressed()
        p._pb_set_pos(7.5)
        self.assertEqual(p.vplayer.position(), 7500); self.assertEqual(p._pb_out.starts[-1], 7.5)
        self.assertAlmostEqual(p.engine.time, 7.5, places=3)

    def test_seek_while_paused_audio_follows_on_play(self):
        p = self.page(); p._pb_play_pressed(); p.vplayer.pause()
        n = len(p._pb_out.starts); p._pb_set_pos(9.0)
        self.assertEqual(len(p._pb_out.starts), n)                     # paused: nothing plays
        p._pb_play_pressed(); self.assertEqual(p._pb_out.starts[-1], 9.0)

    def test_drift_resync_rules(self):
        p = self.page(); t = [100.0]; PB.time = SimpleNamespace(monotonic=lambda: t[0])
        p._pb_play_pressed(); p.vplayer.ms = 4000; n = len(p._pb_out.starts)
        p._pb_out.audible = 4.05; t[0] += 5; p._pb_sync()
        self.assertEqual(len(p._pb_out.starts), n)                     # 50 ms off: tolerated
        p._pb_out.audible = 4.30; p._pb_sync()
        self.assertEqual(len(p._pb_out.starts), n + 1); self.assertEqual(p._pb_out.starts[-1], 4.0)   # 300 ms off: re-sync to the video
        p._pb_out.audible = 9.0; p._pb_sync()
        self.assertEqual(len(p._pb_out.starts), n + 1)                 # not again within RESYNC_GAP
        import time; PB.time = time

    def test_second_play_does_not_rerender_and_changes_do(self):
        p = self.page(); p._pb_play_pressed(); p.vplayer.pause(); p._pb_play_pressed()
        self.assertEqual(p.prepares, 1)
        p.mixer.channels["sfx"].volume = 10; p.mixer.channels["sfx"].mute = True; p._pb_mixer_changed()
        self.assertEqual(p.prepares, 1)                                # volume / mute: live, no re-render
        p.vplayer.pause(); p.clips[0].start = 2.0; p._pb_play_pressed()
        self.assertEqual(p.prepares, 2)                                # edited clip: re-rendered before playing

    def test_enable_toggle_rerenders_while_playing(self):
        p = self.page(); p.mixer.channels["sfx"].enabled = False; p._pb_play_pressed()
        self.assertNotIn("sfx", p.engine.sources)
        p.mixer.channels["sfx"].enabled = True; p.vplayer.ms = 2000; p._pb_mixer_changed()
        self.assertIn("sfx", p.engine.sources); self.assertEqual(p._pb_out.starts[-1], 2.0)   # new source mixed in, playback continued

    def test_nothing_to_mix_keeps_video_audio(self):
        p = self.page(with_clip=False); p._pb_play_pressed()
        self.assertEqual(p.vplayer.calls, ["play"]); self.assertFalse(p.engine.has_audio())
        self.assertFalse(p._pb_out.running); self.assertFalse(p.vaudio.muted)     # like before STEP 2: video plays its own sound

    def test_native_video_audio_honours_original_volume(self):
        p = self.page(with_clip=False)
        p.mixer.channels["orig_vocals"].volume = 50
        p._pb_apply_vaudio()
        self.assertFalse(p.vaudio.muted)
        self.assertAlmostEqual(p.vaudio.volume, 0.4)

    def test_native_video_audio_honours_mute_disable_and_solo(self):
        p = self.page(with_clip=False)
        p.mixer.channels["orig_vocals"].volume = 100
        p.mixer.channels["orig_vocals"].mute = True
        p._pb_apply_vaudio()
        self.assertTrue(p.vaudio.muted)
        p.mixer.channels["orig_vocals"].mute = False
        p.mixer.channels["orig_vocals"].enabled = False
        p._pb_apply_vaudio()
        self.assertTrue(p.vaudio.muted)
        p.mixer.channels["orig_vocals"].enabled = True
        p.mixer.channels["khmer_voice"].solo = True
        p._pb_apply_vaudio()
        self.assertTrue(p.vaudio.muted)

    def test_native_video_audio_preserves_default_passthrough(self):
        p = self.page(with_clip=False)
        p._pb_apply_vaudio()
        self.assertFalse(p.vaudio.muted)
        self.assertAlmostEqual(p.vaudio.volume, 0.8)

    def test_listen_only_uses_main_playback(self):
        p = self.page(); p._pb_listen("sfx")
        self.assertEqual(p.engine.listen_only, "sfx"); self.assertTrue(p._pb_out.running)
        self.assertEqual(p.vplayer.calls, ["play"])                    # the video plays too - nothing is paused for a preview
        p2 = self.page(); p2._pb_listen("ai_voice")                    # a track without audio
        self.assertIsNone(p2.engine.listen_only); self.assertTrue(any("no audio" in m for m in p2.log.msgs))

    def test_failed_render_falls_back_to_video_audio(self):
        p = self.page(); p.video = os.path.join(self.tmp, "missing.mp4")
        p.clips[0].path = os.path.join(self.tmp, "nope.wav")
        p._pb_play_pressed()
        self.assertIn("play", p.vplayer.calls)                         # never stuck: the video still plays


if __name__ == "__main__":
    unittest.main()
