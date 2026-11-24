"""STEP 2 - Qt side of the unified playback.  NOTE: written without a Qt runtime available; see STEP2_NOTES.md (what must be tested).

ONE clock: the video player (QMediaPlayer) position.  Every other thing follows it.
ONE audio output: AudioOutput = one QAudioSink fed by studio.engine.MixEngine (all six tracks mixed in Python/numpy).
The video player's own audio is muted while the engine plays, so nothing is heard twice.

    Play / Pause / Stop / end of video  ->  vplayer.playbackStateChanged  ->  audio starts / stops   (single source of truth)
    Seek / skip / jump                  ->  vplayer.setPosition + audio re-sync
    Drift (every 50 ms)                 ->  if audio and video differ by > DRIFT s the audio is restarted at the video position
"""
import copy
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QMessageBox

try:
    from PySide6.QtMultimedia import QAudioSink, QAudioFormat, QMediaDevices
except Exception:  # noqa - QtMultimedia missing: the page keeps using the video's own audio
    QAudioSink = QAudioFormat = QMediaDevices = None

from ..shared.paths import work_dir
from ..studio import audio as AU
from ..studio import engine as EN
from ..studio import model as M
from ..studio.live_playback import LiveMixPlayer, should_resync
from .workers import Worker

BUFFER_S = 0.15        # audio buffer: also the delay between moving a slider and hearing it
PUMP_MS = 10           # how often the buffer is topped up
SYNC_MS = 50           # how often audio is compared with video
DRIFT = 0.12           # seconds of audio/video difference that triggers a re-sync
RESYNC_GAP = 0.5       # never re-sync more often than this (s)


class AudioOutput(QObject):
    """The only audio output of the studio.  start(t) = (re)start playing the engine from time t."""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._sink = None
        self._io = None
        self._pending = b""
        self._timer = QTimer(self)
        self._timer.setInterval(PUMP_MS)
        self._timer.timeout.connect(self._pump)
        self.available = False
        try:
            if QAudioSink is not None:
                dev = QMediaDevices.defaultAudioOutput()
                self.available = (not dev.isNull()) and dev.isFormatSupported(self._fmt())
        except Exception:  # noqa
            self.available = False

    @staticmethod
    def _fmt():
        f = QAudioFormat()
        f.setSampleRate(EN.SR)
        f.setChannelCount(2)
        f.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        return f

    @property
    def running(self):
        return self._io is not None

    def start(self, t):
        self.stop()
        if not self.available:
            return
        self.engine.seek(t)
        self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), self._fmt(), self)
        self._sink.setBufferSize(int(EN.SR * BUFFER_S) * EN.FRAME)
        self._io = self._sink.start()
        self._pending = b""
        self._pump()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._sink, self._io = None, None
        self._pending = b""

    def _pump(self):
        if self._io is None:
            return
        for _ in range(3):
            # QIODevice.write() may accept fewer bytes than supplied. Flush
            # the unwritten tail before asking the engine for more PCM.
            if self._pending:
                written = self._io.write(self._pending)
                if written is None or written <= 0:
                    return
                self._pending = self._pending[written:]
                if self._pending:
                    return
            n = self._sink.bytesFree() // EN.FRAME
            if n < 256:
                return
            payload = self.engine.render(min(n, 4096))
            written = self._io.write(payload)
            if written is None or written < 0:
                self._pending = payload
                return
            if written < len(payload):
                self._pending = payload[written:]
                return

    def audible_time(self):
        """Timeline time at the speaker, accounting for queued and not-yet-written PCM."""
        if self._sink is None:
            return self.engine.time
        buffered = max(0, self._sink.bufferSize() - self._sink.bytesFree()) / EN.FRAME
        pending = len(self._pending) / EN.FRAME
        return self.engine.time - (buffered + pending) / EN.SR


class PlaybackMixin:
    """Mixed into StudioMixin -> TranslatorPage.  Needs: self.vplayer, self.vaudio, self._QMP, self.mixer, self.cues, self.clips,
    self.video, self.vid_dur, self.voice_off, self.btn_play, self.log."""

    # ------------------------------------------------------------------ setup
    def _pb_init(self):
        self.mix_engine = EN.MixEngine(self.mixer)
        self._pb_out = AudioOutput(self.mix_engine, self)
        self.live_player = LiveMixPlayer(self.mix_engine, self._pb_out)
        self._pb_worker = None
        self._pb_debounce = QTimer(self)
        self._pb_debounce.setSingleShot(True)
        self._pb_debounce.setInterval(400)
        self._pb_debounce.timeout.connect(self._pb_rebuild_debounced)
        self._pb_last_resync = 0.0
        self._pb_rendered_on = None            # which tracks were enabled when the cache was last rendered
        self._pb_sync_timer = QTimer(self)
        self._pb_sync_timer.setInterval(SYNC_MS)
        self._pb_sync_timer.timeout.connect(self._pb_sync)

    def _pb_wire(self):
        if self.vplayer is not None:
            self.vplayer.playbackStateChanged.connect(self._pb_on_state)

    def _pb_set_mixer(self, mixer):
        self.mix_engine.mixer = mixer

    # ------------------------------------------------------------------ helpers
    def _pb_enabled_snapshot(self):
        return tuple(sorted((k, c.enabled) for k, c in self.mixer.channels.items()))

    def _pb_signature(self):
        return EN.build_signature(self.video, self.cues, self.clips, self.mixer, self.vid_dur, self.voice_off.value())

    def _pb_engine_mode(self):
        return self._pb_out.available and self.mix_engine.has_audio()

    def _pb_apply_vaudio(self):
        """Engine plays -> the video's own audio is muted (no double audio).  Engine idle -> video audio as before; there only
        Mute / Disable of 'Original Audio' is honoured (its volume default of 0 % is a dubbing default, not 'silence the video')."""
        if getattr(self, "vaudio", None) is None:
            return
        if self._pb_engine_mode():
            # The unified QAudioSink owns every track. Never let QMediaPlayer's
            # embedded audio leak into the mix.
            self.vaudio.setMuted(True)
            return

        # With no mixed sources, QMediaPlayer is the native pass-through for
        # the video's original audio. Still honour the channel's mixer controls.
        gains = self.mixer.gains()
        gain = float(gains.get("orig_vocals", 0.0))
        original = self.mixer.channels["orig_vocals"]
        any_solo = any(c.solo and c.enabled for c in self.mixer.channels.values())
        if (original.enabled and not original.mute and not any_solo
                and original.volume == 0):
            # Preserve the existing dubbing default: when no other mixed
            # tracks exist, 0% means native source pass-through, not silence.
            gain = 1.0
        self.vaudio.setMuted(gain <= 0.0005)
        if hasattr(self.vaudio, "setVolume"):
            self.vaudio.setVolume(max(0.0, min(1.0, 0.8 * gain)))

    # ------------------------------------------------------------------ the main controls
    def _pb_play_pressed(self):
        """Start the video immediately; prepare/update the live mix asynchronously.

        Audio preparation must never gate basic video playback. If the mixer is not
        ready (or Qt audio output is unavailable), QMediaPlayer continues with the
        video's embedded audio. Once the worker finishes, the live mix is hot-swapped
        at the current video position.
        """
        if not self.video or self.vplayer is None:
            return
        # 1) Video FIRST: nothing in the audio mixer may ever block or break basic playback.
        try:
            self.vaudio.setMuted(False)
        except Exception:  # noqa
            pass
        self.vplayer.play()
        QTimer.singleShot(1500, self._pb_check_started)
        # 2) Then (optionally) the live mix; any failure here is logged, the video keeps playing with its own audio.
        try:
            if self._pb_signature() != self.mix_engine.signature:
                if self._pb_worker is None or not self._pb_worker.isRunning():
                    self._pb_prepare(then_play=False)
            if not self._pb_engine_mode():
                self._pb_apply_vaudio()
        except Exception as e:  # noqa
            self.log.appendPlainText(f"Playback mixer error (video plays with its own audio): {e!r}")

    def _pb_check_started(self):
        """Diagnostic: tell the user WHY the video did not start (shown in the log)."""
        try:
            vp = self.vplayer
            if vp is None or vp.playbackState() == self._QMP.PlayingState or self._playing():
                return
            self.log.appendPlainText(f"Preview: play did not start. status={vp.mediaStatus().name} state={vp.playbackState().name} "
                                     f"error={vp.error().name}: {vp.errorString()} source={vp.source().toString()}")
            if vp.mediaStatus() in (self._QMP.InvalidMedia, self._QMP.NoMedia):
                self._pv_proxy_tried = False
                self._pv_try_proxy("play failed")
        except Exception as e:  # noqa
            self.log.appendPlainText(f"Preview diagnostic failed: {e!r}")

    def _pb_stop(self):
        """Stop = pause + back to the start."""
        self.mix_engine.listen_only = None
        self.vplayer.pause()
        self._pb_set_pos(0.0)

    def _pb_set_pos(self, t):
        """Seek video and audio together."""
        t = max(0.0, float(t))
        self.vplayer.setPosition(int(t * 1000))
        if self._pb_out.running:
            self._pb_out.start(t)
            self._pb_last_resync = time.monotonic()

    def _pb_on_state(self, state):
        if state == self._QMP.PlayingState:
            if self._pb_engine_mode():
                self._pb_out.start(self.vplayer.position() / 1000.0)
                self._pb_last_resync = time.monotonic()
                self._pb_sync_timer.start()
        else:
            self._pb_sync_timer.stop()
            self._pb_out.stop()
            if state == self._QMP.StoppedState:
                self.mix_engine.listen_only = None

    def _pb_sync(self):
        if not self._pb_out.running or self.vplayer is None:
            return
        now = time.monotonic()
        if now - self._pb_last_resync < RESYNC_GAP:
            return
        vt = self.vplayer.position() / 1000.0
        if should_resync(int(vt * 1000), int(self._pb_out.audible_time() * 1000)):
            self.live_player.play(vt)
            self._pb_last_resync = now

    # ------------------------------------------------------------------ preparing the audio (worker thread)
    def _pb_prepare(self, then_play=False):
        self._pb_debounce.stop()
        if not self.video:
            return
        sig = self._pb_signature()
        self._pb_rendered_on = self._pb_enabled_snapshot()
        job_args = (self.video, copy.deepcopy(self.cues), [AU.AudioClip.from_dict(k.to_dict()) for k in self.clips],
                    M.Mixer.from_dict(self.mixer.to_dict()), self.vid_dur, work_dir(Path(self.video).stem) / "playback" / sig,
                    self.voice_off.value())
        w = Worker(EN.prepare_sources, *job_args)
        w.done.connect(lambda res: self._pb_prepared(res, sig, then_play))
        w.failed.connect(lambda msg: self._pb_failed(msg, then_play))
        self._pb_worker = w
        if then_play:
            self.log.appendPlainText("Preparing audio tracks for playback (only when something changed)...")
        w.start()

    def _pb_prepared(self, res, sig, then_play):
        if self.video and sig != self._pb_signature():
            # The timeline changed during rendering. Discard the stale result and prepare the current state.
            QTimer.singleShot(0, lambda: self._pb_prepare(then_play=then_play or self._playing()))
            return
        try:
            srcs = EN.load_sources(res)
        except Exception as e:  # noqa
            self.log.appendPlainText(f"Playback mixer could not load the audio ({e}); the video's own audio is used.")
            srcs = {}
        was_playing = self._playing()
        self.mix_engine.set_sources(srcs, self.vid_dur, sig)
        EN.cleanup_old(work_dir(Path(self.video).stem) / "playback", sig)
        lo = self.mix_engine.listen_only
        if lo and lo not in srcs:
            self.log.appendPlainText("That track has no audio yet (or it is disabled).")
            self.mix_engine.listen_only = None
        self.btn_play.setEnabled(True)
        if hasattr(self, "mixer_panel"):
            self.mixer_panel.set_audio_status("Audio ready")
        self._pb_apply_vaudio()
        if srcs and not self._pb_out.available:
            self.log.appendPlainText("No usable audio output device for the mixer; the video's own audio is used.")
        if then_play and not was_playing:
            self.vplayer.play()
        elif was_playing and self._pb_engine_mode():
            self._pb_out.start(self.vplayer.position() / 1000.0)           # new sources while playing: continue seamlessly

    def _pb_failed(self, msg, then_play):
        self.btn_play.setEnabled(True)
        if hasattr(self, "mixer_panel"):
            self.mixer_panel.set_audio_status("Audio unavailable · using fallback")
        if msg != "Cancelled":
            self.log.appendPlainText(f"Playback mixer: preparing the audio failed ({msg}). The video plays with its own audio.")
        self.mix_engine.set_sources({}, self.vid_dur, self._pb_signature())
        self._pb_apply_vaudio()
        if then_play:
            self.vplayer.play()

    def _pb_shutdown(self):
        """Stop timers, cancel pending preparation and release mapped channel WAVs on app close."""
        try:
            self._pb_debounce.stop()
            self._pb_sync_timer.stop()
            if self._pb_worker is not None and self._pb_worker.isRunning():
                self._pb_worker.cancel()
            self._pb_out.stop()
            if self.vplayer is not None:
                self.vplayer.pause()
            self.mix_engine.set_sources({}, 0.0, "")
        except Exception:
            pass

    # ------------------------------------------------------------------ mixer panel hooks
    def _pb_content_changed(self):
        """Debounce timeline edits; stale worker results are discarded by signature before hot-swap."""
        if not self.video or not self.mix_engine.signature:
            return
        if self._pb_signature() != self.mix_engine.signature:
            if hasattr(self, "mixer_panel"):
                self.mixer_panel.set_audio_status("Updating audio…")
            self._pb_debounce.start()

    def _pb_rebuild_debounced(self):
        if not self.video or self._pb_signature() == self.mix_engine.signature:
            return
        if self._pb_worker is not None and self._pb_worker.isRunning():
            return  # _pb_prepared detects a stale signature and schedules the replacement
        self._pb_prepare(then_play=self._playing())

    def _pb_mixer_changed(self):
        """Volume / mute / solo need nothing: the engine reads the Mixer on every chunk.  Enable / disable changes WHAT has to be
        rendered, so the cache is rebuilt (playback continues when it is ready)."""
        if (self.mix_engine.signature and self.video and self._pb_enabled_snapshot() != self._pb_rendered_on
                and (self._pb_worker is None or not self._pb_worker.isRunning())):
            self._pb_prepare(then_play=False)
        self._pb_apply_vaudio()

    def _pb_listen(self, channel):
        """The per-track ▶ / 'Play mix' buttons: the SAME playback as the main Play button (video + mix), optionally with only
        one track audible.  No separate preview player."""
        if not self.video or self.vplayer is None:
            QMessageBox.information(self, "Video", "Import a video first.")
            return
        self.mix_engine.listen_only = channel or None
        if self._playing():
            return
        self._pb_play_pressed()
