"""Automatic voice-clone reference per character (no Qt): picks the cleanest, calmest, non-overlapping lines of each speaker from the
original (ideally Demucs vocals) audio, joins them into one 8-14 s reference and registers a cloned voice ("Clone - <nick>") for the speaker.
The clone model itself (VoxCPM2 local / Hugging Face Space) is the existing translator.voice_clone engine - nothing new is downloaded here.
"""
import wave
from pathlib import Path

import numpy as np

from . import prosody_match as PM

SR = PM.SR
MIN_LINE, MAX_LINE = 2.0, 12.0
TARGET_MIN, TARGET_MAX, ABS_MIN = 8.0, 14.0, 3.0
GAP_S = 0.2


def candidates(cues, y, speaker, sr=SR):
    """-> [(score, row, start_s, end_s)] best first. Score: lots of voiced speech, calm emotion, steady loudness, a loudness typical for the speaker."""
    rows = [i for i, c in enumerate(cues)
            if (c.speaker or "Speaker") == speaker and MIN_LINE <= c.end - c.start <= MAX_LINE
            and not getattr(c, "speaker_uncertain", False) and not getattr(c, "overlap_with", "")
            and not getattr(c, "text_conflict", False)]
    feats = {}
    for i in rows:
        f = PM.features(y[int(cues[i].start * sr):int(cues[i].end * sr)], sr)
        if f:
            feats[i] = f
    if not feats:
        return []
    med_db = float(np.median([f["loud_db"] for f in feats.values()]))
    out = []
    for i, f in feats.items():
        c = cues[i]
        voiced = f["voiced"] / max((c.end - c.start) * 100.0, 1.0)
        s = 2.0 * min(1.0, voiced) - 0.5 * max(0.0, f["dyn_db"] - 20) / 10 - abs(f["loud_db"] - med_db) / 12
        if (c.emotion or "normal").lower() not in ("", "normal", "calm"):
            s -= 0.6                                       # strong emotion changes the timbre - the plain voice is the better identity
        out.append((s, i, c.start, c.end))
    return sorted(out, reverse=True)


def pick_reference(cues, y, speaker, sr=SR):
    """-> (float32 audio, [rows]) or (None, []) when the speaker has too little clean speech."""
    got, total = [], 0.0
    for s, i, a, b in candidates(cues, y, speaker, sr):
        if total >= TARGET_MIN or total + (b - a) > TARGET_MAX:
            continue
        got.append((a, b, i))
        total += b - a
    if total < ABS_MIN:
        return None, []
    got.sort()
    gap = np.zeros(int(GAP_S * sr), np.float32)
    parts = []
    for a, b, _ in got:
        parts += [y[int(a * sr):int(b * sr)].astype(np.float32), gap]
    return np.concatenate(parts[:-1]), [i for _, _, i in got]


def write_wav(path, y, sr=SR):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path)


def build_voices(cues, y, out_dir, nick_prefix, VC, sr=SR, progress=None, cancel=None, only_unassigned=True):
    """Create (or reuse) one cloned voice per speaker and assign it to that speaker's lines (Cue.voice_type).
    VC = translator.voice_clone module (injected). -> {speaker: {"voice": name, "rows": [...], "seconds": s} | {"error": why}}"""
    say = progress or (lambda *_: None)
    res = {}
    speakers = sorted({(c.speaker or "Speaker") for c in cues if (c.translation or "").strip()})
    for sp in speakers:
        if cancel and cancel():
            break
        lines = [c for c in cues if (c.speaker or "Speaker") == sp]
        if only_unassigned and any(VC.is_clone_name(c.voice_type) for c in lines):
            res[sp] = {"voice": next(c.voice_type for c in lines if VC.is_clone_name(c.voice_type)), "rows": [], "seconds": 0.0, "kept": True}
            continue
        ref, rows = pick_reference(cues, y, sp, sr)
        if ref is None:
            res[sp] = {"error": "not enough clean, non-overlapping speech for a reference"}
            continue
        nick = f"{nick_prefix} {sp}"[:VC.NICK_MAX]
        name = VC.name_for(nick)
        try:
            if not VC.get_voice(name):
                wav = write_wav(Path(out_dir) / f"ref_{abs(hash(sp)) % 10**6}.wav", ref, sr)
                VC.save_voice(nick, VC.prepare_reference(wav))
            for c in lines:
                c.voice_type = name
            res[sp] = {"voice": name, "rows": rows, "seconds": round(len(ref) / sr, 1)}
            say(f"   {sp}: cloned voice '{name}' from {len(rows)} line(s), {len(ref) / sr:.1f}s")
        except Exception as e:  # noqa - one failing speaker must not stop the others (they keep the normal Khmer voice)
            res[sp] = {"error": str(e)[:150]}
    return res
