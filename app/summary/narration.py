"""Narration generation and the narration track. Voice engines are injected (`synth`), so edge-tts / VoxCPM2 clone / any future
engine plug in without touching this module; only segments whose voice inputs changed are regenerated (voice_sig).
Track building reuses the Translator's voice_synchronizer (decode, silence trim, mixing) instead of a second implementation.
"""
import wave
from pathlib import Path
from types import SimpleNamespace

from ..shared.ffmpeg_util import Cancelled
from ..studio.audio import wav_duration
from ..translator import voice_synchronizer as VS
from . import sync as SY
from .model import voice_sig
from .script import normalize_khmer


def generate_narration(project, workdir, synth, voice_key, speed=1.0, progress=None, cancel=None):
    """synth(text, out_path, emotion=, speed=) -> path of an audio file. Returns ids of segments that were (re)generated."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    done = []
    todo = [g for g in project.segments]
    for k, g in enumerate(todo):
        if cancel and cancel():
            raise Cancelled()
        text = normalize_khmer(g.km)
        if not text:
            g.audio, g.dur, g.audio_sig = "", 0.0, ""
            continue
        sig = voice_sig(g, voice_key, speed)
        if g.audio and Path(g.audio).exists() and g.audio_sig == sig:
            continue
        path = synth(text, str(workdir / f"seg_{g.id}_{sig[:8]}.wav"), emotion=g.emotion, speed=speed)
        g.audio, g.audio_sig = str(path), sig
        g.dur = round(float(wav_duration(path)), 3)
        done.append(g.id)
        if progress:
            progress(int((k + 1) * 100 / len(todo)))
    return done


def build_track(project, out_wav, progress=None, cancel=None):
    """One mono wav, exactly as long as the plan: every narration clip starts where its picture clip starts."""
    st = SY.starts(project)
    cues, total = [], 0.0
    for g in project.segments:
        if g.id in st and g.audio and Path(g.audio).exists():
            cues.append(SimpleNamespace(start=st[g.id], end=st[g.id] + g.dur, audio=g.audio))
            total = max(total, st[g.id] + g.dur)
    if not cues:
        raise ValueError("There is no narration audio to build a track from.")
    out_wav = Path(out_wav)
    VS.build_voice_track(cues, total, out_wav, max_speedup=1.0, progress=progress, cancel=cancel)
    with wave.open(str(out_wav), "rb") as r:                       # build_voice_track pads 1 s; cut to the plan length
        params, n = r.getparams(), int(total * r.getframerate())
        frames = r.readframes(n)
    with wave.open(str(out_wav), "wb") as w:
        w.setparams(params)
        w.writeframes(frames)
    return str(out_wav)
