"""STEP 2 - unified audio engine (no Qt, so it can be unit-tested).

One MixEngine = one read cursor over up to six TrackSource objects.  The Qt side (app/ui/playback.py) only pulls PCM with
`render(n)` and pushes it to ONE QAudioSink, so there is exactly one audio output and one clock.

  TrackSource   read-only view (np.memmap) of a stereo 44.1 kHz 16-bit wav that was RENDERED from the project (cache files; the
                user's original files are never changed).
  MixEngine     cursor + live gains.  The gain of every track is read from the shared studio.model.Mixer at the start of every
                chunk, so a slider / mute / solo / enable change is heard within one chunk (+ the sink buffer).
  build_signature / prepare_sources
                decide when the cache is stale, and (in a worker thread) render the six tracks to wav files.
"""
import hashlib
import json
import struct
from pathlib import Path

import numpy as np

SR = 44100
FRAME = 4                   # bytes per stereo int16 frame
CHANNELS = ("orig_vocals", "khmer_voice", "ai_voice", "orig_bg", "bgm", "sfx")     # == model.MIXER_TRACKS


# ------------------------------------------------------------------ wav access
def wav_span(path):
    """-> (data_offset, data_bytes, channels, rate, bits) read from the RIFF header (works for ffmpeg wavs with extra chunks)."""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a wav file")
        ch = rate = bits = 0
        while True:
            h = f.read(8)
            if len(h) < 8:
                raise ValueError("wav has no data chunk")
            cid, size = h[:4], struct.unpack("<I", h[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                _tag, ch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", fmt[:16])
            elif cid == b"data":
                off = f.tell()
                real = Path(path).stat().st_size - off
                return off, (min(size, real) if size != 0xFFFFFFFF else real), ch, rate, bits
            else:
                f.seek(size + (size & 1), 1)


class TrackSource:
    """Stereo / 44.1 kHz / int16 audio of ONE track, memory-mapped (a 2 h movie does not have to fit in RAM)."""

    def __init__(self, path):
        off, nbytes, ch, rate, bits = wav_span(path)
        if (ch, rate, bits) != (2, SR, 16):
            raise ValueError(f"{path}: need stereo 44100 Hz 16-bit, got {ch} ch {rate} Hz {bits} bit")
        self.path = str(path)
        self.frames = nbytes // FRAME
        self.data = (np.memmap(path, dtype="<i2", mode="r", offset=off, shape=(self.frames, 2))
                     if self.frames else np.zeros((0, 2), dtype=np.int16))

    @property
    def duration(self):
        return self.frames / SR

    def read(self, start, n):
        """float32 (n, 2) in -1..1 for frames [start, start+n); silence outside the file."""
        out = np.zeros((n, 2), dtype=np.float32)
        a, b = max(0, start), min(self.frames, start + n)
        if b > a:
            out[a - start:b - start] = self.data[a:b]
            out[a - start:b - start] *= (1.0 / 32768.0)
        return out

    def close(self):
        self.data = np.zeros((0, 2), dtype=np.int16)      # drops the memmap so the file can be replaced / deleted (Windows)


# ------------------------------------------------------------------ the mixer
class MixEngine:
    """cursor (frames) + sources + the shared Mixer.  `render(n)` mixes the next n frames and moves the cursor."""

    def __init__(self, mixer=None):
        self.mixer = mixer
        self.sources = {}
        self.cursor = 0
        self.length = 0                       # frames; the end of the project (video length / longest source)
        self.signature = ""
        self.listen_only = None               # channel key: "play only this track" (its own volume x master; mute / solo ignored)
        self._g = {}                          # gain used at the end of the previous chunk (ramped from -> no clicks)

    # ---- sources ----
    def set_sources(self, sources, length_s=0.0, signature=""):
        old = self.sources
        self.sources = dict(sources)
        self.signature = signature
        self.length = int(max([length_s] + [s.duration for s in self.sources.values()]) * SR)
        self._g = {k: self._g.get(k, 0.0) for k in self.sources}
        for k, s in old.items():
            if self.sources.get(k) is not s:
                s.close()

    def has_audio(self):
        return bool(self.sources)

    # ---- clock ----
    def seek(self, t):
        self.cursor = max(0, int(round(float(t) * SR)))
        self._g = {k: self._current_gains().get(k, 0.0) for k in self.sources}   # no fade-in after a seek: start at the target gain

    @property
    def time(self):
        return self.cursor / SR

    def at_end(self):
        return self.length > 0 and self.cursor >= self.length

    # ---- gains (live) ----
    def _current_gains(self):
        if self.mixer is None:
            return {k: 1.0 for k in self.sources}
        if self.listen_only in self.sources:
            c = self.mixer.channels[self.listen_only]
            return {k: ((c.volume / 100.0) * (self.mixer.master / 100.0) if k == self.listen_only else 0.0) for k in self.sources}
        return self.mixer.gains(available=set(self.sources))

    def render(self, n):
        """Mix frames [cursor, cursor+n) -> bytes (int16 stereo little-endian) and advance the cursor."""
        target = self._current_gains()
        acc = np.zeros((n, 2), dtype=np.float32)
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        for key, src in self.sources.items():
            g1 = float(target.get(key, 0.0))
            g0 = float(self._g.get(key, g1))
            self._g[key] = g1
            if g0 <= 0.0005 and g1 <= 0.0005:                    # silent track: do not even read the file
                continue
            chunk = src.read(self.cursor, n)
            if abs(g1 - g0) < 1e-4:
                acc += chunk * g1
            else:                                                  # volume / mute / solo changed: smooth ramp across this chunk
                acc += chunk * (g0 + (g1 - g0) * ramp)[:, None]
        self.cursor += n
        np.clip(acc, -1.0, 1.0, out=acc)
        return (acc * 32767.0).astype("<i2").tobytes()


# ------------------------------------------------------------------ cache: when is it stale, how is it built
def _mt(path):
    try:
        st = Path(path).stat()
        return [str(path), st.st_size, int(st.st_mtime_ns)]
    except OSError:
        return [str(path), 0, 0]


def build_signature(video, cues, clips, mixer, duration, voice_offset=0.0):
    """Fingerprint of everything that changes the RENDERED audio.  Volume / mute / solo are NOT in it (they are live);
    enabled IS (a disabled track is not rendered at all)."""
    from . import audio as AU
    stems = AU.stem_paths(video) if video else {}
    d = {"video": _mt(video) if video else None, "dur": round(float(duration or 0), 2), "voff": round(float(voice_offset), 3),
         "stems": [_mt(p) for p in stems.values()] if stems and AU.have_stems(video) else [],
         "cues": [[_mt(c.audio), round(c.start, 3), round(c.end, 3)] for c in cues if c.audio and Path(c.audio).exists()],
         "clips": [[_mt(k.path), k.to_dict()] for k in clips],
         "on": {k: bool(c.enabled) for k, c in mixer.channels.items()}}
    return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _to_44k_stereo(src, dst, cancel=None):
    from ..shared.ffmpeg_util import run_ffmpeg
    try:
        off, nb, ch, rate, bits = wav_span(src)
        if (ch, rate, bits) == (2, SR, 16):
            return str(src)
    except Exception:  # noqa - not a plain wav: let ffmpeg decode it
        pass
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-y", "-i", str(src), "-vn", "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le", str(dst)], cancel=cancel)
    return str(dst)


def prepare_sources(video, cues, clips, mixer, duration, out_dir, voice_offset=0.0, progress=None, cancel=None):
    """Worker-thread job: render every ENABLED track that has audio to a stereo 44.1 kHz wav in out_dir.  -> {channel: wav path}.
    Reuses the project's own renderers (build_voice_track, render_clip_track, Demucs stems) so playback == export."""
    from . import audio as AU
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    on = {k for k, c in mixer.channels.items() if c.enabled}
    voice = None
    spoken = [c for c in cues if c.audio and Path(c.audio).exists()]
    if "khmer_voice" in on and spoken:
        from ..translator.voice_synchronizer import build_voice_track
        voice = out_dir / "khmer_voice_24k.wav"
        build_voice_track(spoken, duration, voice, cancel=cancel, offset=voice_offset)
    by = {"sfx": [], "bgm": [], "ai_voice": []}
    for k in clips:
        if k.track in by and k.track in on:
            by[k.track].append(k)
    orig = None
    others = bool(voice or any(by.values()) or (video and AU.have_stems(video)))
    # nothing else to mix and Original Audio at 0 % -> engine stays idle and the video plays its own audio as before
    if "orig_vocals" in on and video and not AU.have_stems(video) and (others or mixer.channels["orig_vocals"].volume > 0):
        orig = AU.extract_original(video, out_dir.parent / "original_audio.wav", cancel=cancel)
    inputs = AU.gather_inputs(video, voice, by["sfx"], by["bgm"], duration, tmp_dir=out_dir, cancel=cancel, ai_clips=by["ai_voice"], orig_wav=orig)
    res = {}
    for i, (k, p) in enumerate(inputs.items()):
        if k in on and k in CHANNELS:
            res[k] = _to_44k_stereo(p, out_dir / f"{k}_44k.wav", cancel=cancel)
        if progress:
            progress(int(100 * (i + 1) / max(1, len(inputs))))
    return res


def load_sources(paths):
    """{channel: wav path} -> {channel: TrackSource}"""
    return {k: TrackSource(p) for k, p in paths.items()}


def cleanup_old(root, keep):
    """Delete render caches of older signatures (best effort - a file that is still mapped on Windows is simply left)."""
    import shutil
    root = Path(root)
    if not root.exists():
        return
    for d in root.iterdir():
        if d.is_dir() and d.name != keep:
            shutil.rmtree(d, ignore_errors=True)
