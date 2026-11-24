"""Dialogue <-> AI voice synchronisation (no Qt).

Every dialogue line (a `Cue`) already carries: original text (Chinese ...), Khmer translation, timing, character (speaker), emotion,
voice type / name and speed.  This module adds what is needed to keep the generated voice in step with those fields:

 * a fingerprint of everything the voice depends on  -> only lines that really changed are regenerated;
 * a check after generation (real audio length vs the time slot of the line on the timeline);
 * saving / loading the whole dialogue next to the project so nothing is lost between sessions.
"""
import hashlib
import json
import os
from dataclasses import asdict, fields
from pathlib import Path

from ..translator.subtitles import Cue

MAX_SPEEDUP = 1.5      # same limit build_voice_track uses at export


def voice_sig(c):
    """Fingerprint of every field the generated voice depends on (timing is NOT part of it: moving a line keeps its voice)."""
    parts = [c.translation.strip(), c.voice_type, getattr(c, "voice_name", ""), f"{getattr(c, 'speed', 1.0):.3f}", c.emotion,
             getattr(c, "emotion_raw", ""), getattr(c, "prosody", ""), c.speaker, c.gender, "1" if c.use_voice else "0"]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def has_audio(c):
    return bool(c.audio and Path(c.audio).exists())


def needs_voice(c):
    """True when the line must (re)generate its voice: it has Khmer text, wants a voice, and has no audio / its audio is outdated.
    Legacy audio without a fingerprint counts as up to date (never regenerated behind the user's back)."""
    if not c.translation.strip() or not c.use_voice:
        return False
    if not has_audio(c):
        return True
    return bool(c.audio_sig) and c.audio_sig != voice_sig(c)


def dirty_rows(cues):
    return [i for i, c in enumerate(cues) if needs_voice(c)]


def mark_generated(c, duration=None):
    c.audio_sig = voice_sig(c)
    if duration is not None:
        c.voice_dur = float(duration)


def invalidate(c):
    c.audio, c.audio_sig, c.voice_dur = "", "", 0.0


def measure(cues, probe_fn=None, force=False):
    """Fill voice_dur (real clip length) for lines that have audio. Returns the indices that were measured."""
    if probe_fn is None:
        from .audio import wav_duration as probe_fn
    done = []
    for i, c in enumerate(cues):
        if has_audio(c) and (force or c.voice_dur <= 0):
            try:
                c.voice_dur = float(probe_fn(c.audio))
                done.append(i)
            except Exception:
                pass
        elif not has_audio(c):
            c.voice_dur = 0.0
    return done


def check_sync(cues, total_duration=None):
    """After generation: compare each voice clip with the room it has on the timeline.
    -> list of dicts {row (0-based), slot, voice, status: ok | long | too_long | missing, fix}.
    A clip may use the slot AND the gap up to the next line; 'long' = fits after a speed-up (<= MAX_SPEEDUP), 'too_long' = would be cut."""
    out = []
    order = sorted(range(len(cues)), key=lambda k: cues[k].start)
    nxt = {a: (cues[b].start if b is not None else None) for a, b in zip(order, order[1:] + [None])}
    for i in order:
        c = cues[i]
        if not c.translation.strip() or not c.use_voice:
            continue
        slot = max(0.2, c.end - c.start)
        if not has_audio(c):
            out.append({"row": i, "slot": slot, "voice": 0.0, "status": "missing", "fix": "generate"})
            continue
        d = c.voice_dur
        n = nxt.get(i)
        room = slot if n is None else max(slot, n - 0.05 - c.start)
        if total_duration and n is None:
            room = max(slot, total_duration - c.start)
        if d <= 0:
            continue
        if d <= room + 0.05:
            out.append({"row": i, "slot": slot, "voice": d, "status": "ok", "fix": ""})
        elif d <= room * MAX_SPEEDUP + 0.05:
            out.append({"row": i, "slot": slot, "voice": d, "status": "long", "fix": f"speed x{d / room:.2f} or extend End"})
        else:
            out.append({"row": i, "slot": slot, "voice": d, "status": "too_long", "fix": "shorten the Khmer text or extend End"})
    return out


def summarize(report):
    n = len(report)
    bad = [r for r in report if r["status"] in ("long", "too_long", "missing")]
    return n, bad


def extend_to_fit(cues, total_duration=None, pad=0.05):
    """Where a voice is longer than its slot and the next line leaves room, move the line's END to fit it.
    Never overlaps the next line and never touches Start. -> indices changed."""
    order = sorted(range(len(cues)), key=lambda k: cues[k].start)
    changed = []
    for a, b in zip(order, order[1:] + [None]):
        c = cues[a]
        if not has_audio(c) or c.voice_dur <= 0:
            continue
        want = c.start + c.voice_dur + pad
        if want <= c.end + 1e-6:
            continue
        limit = cues[b].start - 0.02 if b is not None else (total_duration or want)
        new_end = min(want, limit)
        if new_end > c.end + 1e-6:
            c.end = new_end
            changed.append(a)
    return changed


# ------------------------------------------------------------------ persistence
def _cue_from(d):
    known = {f.name for f in fields(Cue)}
    return Cue(**{k: v for k, v in d.items() if k in known})


def save_dialogue(path, cues, video_fp=""):
    """Atomic save of every line with ALL its data (original, Khmer, timing, speaker, emotion, voice settings, audio path)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "video_fp": video_fp, "cues": [asdict(c) for c in cues]}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)


def load_dialogue(path, video_fp=None):
    """-> list[Cue]; [] if the file is missing / damaged / belongs to another video."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if video_fp is not None and d.get("video_fp") not in ("", None, video_fp):
            return []
        return [_cue_from(x) for x in d.get("cues", [])]
    except Exception:
        return []
