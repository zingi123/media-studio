"""Audio side of the studio: Demucs stems (manual), the 6-channel mixer, SFX / music / AI-voice clips, waveform peaks, mix preview.

Channels (see model.MIXER_TRACKS):
    orig_vocals  - "Original Audio": vocals.wav from Demucs when the stems exist, otherwise the video's own full audio
    orig_bg      - "Background Audio": no_vocals.wav from Demucs (music + effects of the original; only after the Demucs button)
    khmer_voice  - "Khmer Voice": the track built from the dialogue lines (cue audio)
    ai_voice     - "AI Voice": its OWN clips (AudioClip.track == "ai_voice"); never merged with khmer_voice
    sfx / bgm    - clips the user placed on the SFX / Music tracks

Export NEVER starts Demucs by itself any more: it only uses stems that already exist.
"""
import hashlib
import subprocess
import sys
import wave
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

import numpy as np

from ..shared.ffmpeg_util import require_ffmpeg, run_ffmpeg, probe, Cancelled
from ..shared.paths import work_dir
from .model import Mixer, MIXER_TRACKS, new_id

_NOWIN = 0x08000000 if sys.platform.startswith("win") else 0


# ------------------------------------------------------------------ Demucs stems
def stem_dir(video):
    return work_dir(Path(video).stem) / "demucs" / "htdemucs" / "orig"


def stem_paths(video):
    d = stem_dir(video)
    return {"vocals": d / "vocals.wav", "no_vocals": d / "no_vocals.wav"}


def have_stems(video):
    if not video:
        return False
    p = stem_paths(video)
    return p["vocals"].exists() and p["no_vocals"].exists()


def run_demucs(video, device="cpu", progress=None, cancel=None):
    """The Demucs BUTTON.  Separates once (cached); returns {"vocals": path, "no_vocals": path}."""
    from ..translator.separation import separate
    separate(video, device, progress=progress, cancel=cancel)
    p = stem_paths(video)
    if not (p["vocals"].exists() and p["no_vocals"].exists()):
        raise RuntimeError("Demucs finished but the stems were not found.")
    return {k: str(v) for k, v in p.items()}


# ------------------------------------------------------------------ SFX / music clips
@dataclass
class AudioClip:
    id: str = field(default_factory=lambda: new_id("a"))
    track: str = "sfx"            # sfx | bgm | ai_voice
    path: str = ""
    name: str = ""
    start: float = 0.0            # position on the timeline (s)
    src_in: float = 0.0           # trim: where in the file the clip begins (s)
    dur: float = 1.0              # length on the timeline (s)
    src_len: float = 0.0          # full length of the file (s), 0 = unknown
    volume: int = 100             # % of the channel
    mute: bool = False

    @property
    def end(self):
        return self.start + self.dur

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def clip_from_file(path, track, start=0.0):
    info = probe(path)
    length = float(info.get("duration") or 0.0) or 1.0
    return AudioClip(track=track, path=str(path), name=Path(path).name, start=max(0.0, start), dur=length, src_len=length)


def render_clip_track(clips, total_duration, out_wav, cancel=None):
    """Mix all (un-muted) clips of one track into one stereo 44.1 kHz wav. Returns the path, or None when there is nothing to mix."""
    live = [c for c in clips if not c.mute and c.dur > 0.01 and Path(c.path).exists()]
    if not live:
        return None
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    args, filt, labs = ["-y"], [], []
    for i, c in enumerate(live):
        args += ["-ss", f"{max(0.0, c.src_in):.3f}", "-t", f"{c.dur:.3f}", "-i", str(Path(c.path).resolve())]
        ms = int(round(max(0.0, c.start) * 1000))
        filt.append(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={max(0, c.volume) / 100:.3f},"
                    f"adelay={ms}|{ms}[c{i}]")
        labs.append(f"[c{i}]")
    if len(live) == 1:
        filt.append(f"{labs[0]}anull[a]")
    else:
        filt.append("".join(labs) + f"amix=inputs={len(live)}:duration=longest:normalize=0,alimiter=limit=0.95[a]")
    args += ["-filter_complex", ";".join(filt), "-map", "[a]", "-t", f"{max(total_duration, 0.1):.3f}", "-ar", "44100", "-ac", "2",
             "-c:a", "pcm_s16le", str(out_wav)]
    run_ffmpeg(args, cancel=cancel)
    return str(out_wav)


# ------------------------------------------------------------------ the mixer
def render_mix(inputs, mixer, out_wav, start=0.0, duration=None, cancel=None):
    """inputs = {channel: wav path}.  Mixes with the mixer's volume / mute / solo and writes a stereo 44.1 kHz wav.
    start / duration select a window (used by the preview).  Returns the path."""
    inputs = {k: str(v) for k, v in inputs.items() if v and Path(v).exists() and k in MIXER_TRACKS}
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    gains = mixer.gains(available=set(inputs))
    live = [(k, g) for k, g in gains.items() if g > 0.0005]
    win = []
    if start > 0:
        win += ["-ss", f"{start:.3f}"]
    if duration:
        win += ["-t", f"{duration:.3f}"]
    if not live:                                           # everything muted -> silence of the right length
        a = ["-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{(duration or 1.0):.3f}", "-c:a", "pcm_s16le", str(out_wav)]
        run_ffmpeg(a, cancel=cancel)
        return str(out_wav)
    args, filt, labs = ["-y"], [], []
    for i, (k, g) in enumerate(live):
        args += win + ["-i", inputs[k]]
        filt.append(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={g:.4f}[m{i}]")
        labs.append(f"[m{i}]")
    if len(live) == 1:
        filt.append(f"{labs[0]}alimiter=limit=0.95[a]")
    else:
        filt.append("".join(labs) + f"amix=inputs={len(live)}:duration=longest:normalize=0,alimiter=limit=0.95[a]")
    args += ["-filter_complex", ";".join(filt), "-map", "[a]", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(out_wav)]
    run_ffmpeg(args, cancel=cancel)
    return str(out_wav)


def solo_mixer(mixer, channel):
    """A copy of the mixer in which only `channel` plays (at its own volume) - 'listen to this sound alone'."""
    m = Mixer.from_dict(mixer.to_dict())
    for k, c in m.channels.items():
        c.solo = (k == channel)
        if k == channel:
            c.mute = False
    return m


def extract_original(video, out_wav, cancel=None):
    """The video's own audio as a stereo 44.1 kHz wav (cached by name+mtime). Returns the path, or None when the video has no audio."""
    out_wav = Path(out_wav)
    stamp = out_wav.with_suffix(".stamp")
    try:
        st = Path(video).stat()
        sig = f"{st.st_size}|{int(st.st_mtime_ns)}"
    except OSError:
        sig = ""
    if out_wav.exists() and stamp.exists() and stamp.read_text() == sig:
        return str(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_ffmpeg(["-y", "-i", str(Path(video).resolve()), "-vn", "-map", "0:a:0", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(out_wav)],
                   cancel=cancel)
    except Cancelled:
        raise
    except Exception:  # noqa - no audio stream
        return None
    stamp.write_text(sig)
    return str(out_wav)


def gather_inputs(video, voice_wav=None, sfx_clips=(), bgm_clips=(), total_duration=0.0, tmp_dir=None, cancel=None,
                  ai_clips=(), orig_wav=None):
    """Collect the wavs of every channel that has audio.  Demucs stems are used only if they already exist.
    orig_wav: the video's full audio, used for 'orig_vocals' ONLY when there are no stems (so Original Audio is never doubled)."""
    out = {}
    if video and have_stems(video):
        p = stem_paths(video)
        out["orig_vocals"], out["orig_bg"] = str(p["vocals"]), str(p["no_vocals"])
    elif orig_wav and Path(orig_wav).exists():
        out["orig_vocals"] = str(orig_wav)
    if voice_wav and Path(voice_wav).exists():
        out["khmer_voice"] = str(voice_wav)
    tmp_dir = Path(tmp_dir or work_dir(Path(video).stem if video else "studio"))
    for key, clips in (("sfx", sfx_clips), ("bgm", bgm_clips), ("ai_voice", ai_clips)):
        if clips:
            r = render_clip_track(list(clips), total_duration, tmp_dir / f"{key}_track.wav", cancel=cancel)
            if r:
                out[key] = r
    return out


# ------------------------------------------------------------------ waveform peaks
PEAK_RATE = 100        # peak columns per second kept in the cache (enough for ~ 20 px/s .. 400 px/s zoom)
_cache = {}


def _key(path):
    p = Path(path)
    try:
        st = p.stat()
        return f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return str(path)


def waveform_peaks(path, rate=PEAK_RATE, cancel=None):
    """-> float32 array in 0..1, `rate` values per second (max |sample| of each slice). Cached in memory."""
    k = _key(path)
    if k in _cache:
        return _cache[k]
    ff = require_ffmpeg()
    sr = 8000
    cmd = [ff, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
    raw = np.frombuffer(p.stdout[: len(p.stdout) // 2 * 2], dtype=np.int16)
    step = sr // rate
    n = len(raw) // step
    if n <= 0:
        peaks = np.zeros(1, dtype=np.float32)
    else:
        peaks = (np.abs(raw[: n * step].reshape(n, step).astype(np.int32)).max(axis=1) / 32768.0).astype(np.float32)
    _cache[k] = peaks
    return peaks


def peaks_for_window(peaks, t0, t1, columns, rate=PEAK_RATE):
    """Down-sample `peaks` for the time window [t0, t1] to `columns` bars (max of each bucket)."""
    if columns <= 0 or peaks is None or len(peaks) == 0 or t1 <= t0:
        return np.zeros(max(0, columns), dtype=np.float32)
    a = np.clip(np.linspace(t0 * rate, t1 * rate, columns + 1), 0, len(peaks)).astype(int)
    out = np.zeros(columns, dtype=np.float32)
    for i in range(columns):
        if a[i + 1] > a[i]:
            out[i] = peaks[a[i]:a[i + 1]].max()
        elif a[i] < len(peaks):
            out[i] = peaks[a[i]]
    return out


def wav_duration(path):
    """Seconds of a wav (fast, header only); falls back to ffmpeg for other formats."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        try:
            return float(probe(path).get("duration") or 0.0)
        except Exception:
            return 0.0
