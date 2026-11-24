"""Performance matching + self-check (no Qt, numpy only).

What it does, per dialogue line:
  1. MEASURE the real performance of the original actor (pitch, pitch movement, loudness, pace) RELATIVE TO THAT SAME SPEAKER'S OWN
     normal voice - so 'higher / louder / faster than this character usually is' is what gets copied, not absolute numbers.
  2. PLAN edge-tts prosody (rate %, pitch Hz, volume %) from those differences (stored on Cue.prosody, identity-safe limits).
  3. After the Khmer voice exists, MEASURE the Khmer clip the same way, compare with what was wanted, correct the parameters and
     regenerate only the lines that are off (max `rounds`).  Every line ends with a match score (Cue.match_score 0-100).
Honest limits: edge-tts Khmer cannot 'act' - it only follows rate/pitch/volume. A cloned voice (VoxCPM2) can follow a style text; here it
only gets the loudness correction. The pitch tracker is autocorrelation based (good for speech, wrong on singing / heavy music).
"""
import json
import math
import re
import wave
from pathlib import Path

import numpy as np

from ..shared.ffmpeg_util import Cancelled

SR = 16000
FMIN, FMAX = 70.0, 400.0
GAIN_P, GAIN_L, GAIN_R = 0.8, 0.9, 0.7          # how much of the original's deviation is copied (Khmer prosody is not the same language)
MAX_PITCH_HZ, MAX_VOL, RATE_MIN, RATE_MAX = 30, 60, -25, 35
NOMINAL_F0 = {"male": 120.0, "female": 210.0}    # rough base pitch of the two edge-tts Khmer voices; the verify step measures the real one
TOL_ST, TOL_DB, TOL_PACE = 1.2, 2.5, 0.15
OK_SCORE = 75


# ------------------------------------------------------------------ analysis
def f0_track(y, sr=SR):
    """-> (f0 Hz per 10 ms frame, nan = unvoiced), (rms dB per frame)."""
    y = np.asarray(y, dtype=np.float32)
    win, hop = int(0.04 * sr), int(0.01 * sr)
    if len(y) < win + hop:
        return np.array([]), np.array([])
    n = 1 + (len(y) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    fr = y[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    db = 20 * np.log10(rms + 1e-8)
    lo, hi = int(sr / FMAX), int(sr / FMIN)
    f0 = np.full(n, np.nan)
    for a in range(0, n, 4000):                                  # batches keep memory small on long audio
        b = min(n, a + 4000)
        X = np.fft.rfft(fr[a:b] * np.hanning(win), n=2 * win, axis=1)
        r = np.fft.irfft(np.abs(X) ** 2, axis=1)[:, :win]
        r = r / (r[:, :1] + 1e-9)
        seg = r[:, lo:hi + 1]
        k = seg.argmax(axis=1)
        pk = seg[np.arange(len(k)), k]
        ok = (pk > 0.45) & (db[a:b] > db.max() - 40)
        lag = (k + lo).astype(np.float64)
        for j in np.nonzero(ok)[0]:                              # parabolic interpolation of the peak
            L = int(lag[j])
            if 0 < L < win - 1:
                y0, y1, y2 = r[j, L - 1], r[j, L], r[j, L + 1]
                den = y0 - 2 * y1 + y2
                lag[j] = L + (0.5 * (y0 - y2) / den if abs(den) > 1e-9 else 0.0)
        f0[a:b] = np.where(ok, sr / np.maximum(lag, 1.0), np.nan)
    return f0, db


def features(y, sr=SR):
    """-> dict(f0, range_st, loud_db, dyn_db, voiced) or None when there is too little voiced speech to measure."""
    f0, db = f0_track(y, sr)
    if len(f0) == 0:
        return None
    v = f0[~np.isnan(f0)]
    if len(v) < 12:                                              # < 120 ms voiced
        return None
    act = db[db > db.max() - 30]
    p10, p90 = np.percentile(v, 10), np.percentile(v, 90)
    return {"f0": float(np.median(v)), "range_st": float(12 * math.log2(max(p90, 1) / max(p10, 1))),
            "loud_db": float(np.median(act)), "dyn_db": float(np.percentile(act, 90) - np.percentile(act, 10)),
            "voiced": int(len(v))}


def _med(xs):
    xs = [x for x in xs if x is not None]
    return float(np.median(xs)) if xs else None


def _units(text):
    return len(re.sub(r"\s+", "", re.sub(r"\([^()]*\)|\[pause\s*[0-9.]+s\]", " ", text or "")))


def analyze_original(cues, y, sr=SR, cancel=None):
    """Original-audio performance of every line, relative to its own speaker -> {row: {d_st, d_db, d_range, d_pace}} (reliable lines only)."""
    feats = {}
    for i, c in enumerate(cues):
        if cancel and cancel():
            raise Cancelled()
        seg = y[int(c.start * sr):int(c.end * sr)]
        f = features(seg, sr) if len(seg) >= 0.4 * sr else None
        if f:
            f["pace"] = _units(c.text) / max(c.end - c.start, 0.2)
            feats[i] = f
    by = {}
    for i in feats:
        by.setdefault(cues[i].speaker or "Speaker", []).append(i)
    out = {}
    for sp, idx in by.items():
        if len(idx) < 3:
            continue                                             # no personal baseline -> nothing to copy
        b = {k: _med([feats[i][k] for i in idx]) for k in ("f0", "range_st", "loud_db", "pace")}
        for i in idx:
            f = feats[i]
            out[i] = {"d_st": 12 * math.log2(f["f0"] / b["f0"]), "d_db": f["loud_db"] - b["loud_db"],
                      "d_range": (f["range_st"] + 0.5) / (b["range_st"] + 0.5),
                      "d_pace": (f["pace"] / b["pace"]) if b["pace"] else 1.0}
    return out


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def plan(cues, targets):
    """Write Cue.prosody (JSON) for every analysed line. Lines without a reliable measurement keep '' (label-based emotion prosody)."""
    n = 0
    for i, t in targets.items():
        c = cues[i]
        base = NOMINAL_F0["female" if (c.gender or "").lower().startswith("f") else "male"]
        st = _clamp(t["d_st"], -5, 5) * GAIN_P
        db = _clamp(t["d_db"], -10, 10) * GAIN_L
        pace = _clamp(t["d_pace"], 0.7, 1.4) ** GAIN_R
        c.prosody = json.dumps({"rate": int(round(_clamp((pace - 1) * 100, RATE_MIN, RATE_MAX))),
                                "pitch": int(round(_clamp(base * (2 ** (st / 12) - 1), -MAX_PITCH_HZ, MAX_PITCH_HZ))),
                                "vol": int(round(_clamp((10 ** (db / 20) - 1) * 100, -50, MAX_VOL))),
                                "want_st": round(st, 2), "want_db": round(db, 2), "want_pace": round(pace, 3)})
        n += 1
    return n


def load(c):
    """Cue.prosody -> dict or None."""
    try:
        d = json.loads(getattr(c, "prosody", "") or "")
        return d if isinstance(d, dict) and {"rate", "pitch", "vol"} <= set(d) else None
    except ValueError:
        return None


# ------------------------------------------------------------------ audio io
def load_audio(path, sr=SR, tmp_dir=None):
    """Any audio file -> float32 mono at `sr` (ffmpeg decode)."""
    from ..shared.ffmpeg_util import run_ffmpeg
    from ..shared.paths import work_dir
    d = Path(tmp_dir) if tmp_dir else work_dir("_prosody")
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"_dec_{abs(hash(str(path))) % 10**8}.wav"
    run_ffmpeg(["-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(tmp)])
    with wave.open(str(tmp), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    tmp.unlink(missing_ok=True)
    return a


def apply_gain(path, db):
    """Loudness correction for voices that cannot take a volume parameter (cloned WAV only). In place; |db| < 0.5 is left alone."""
    if abs(db) < 0.5:
        return path
    from ..shared.ffmpeg_util import run_ffmpeg
    p = Path(path)
    out = p.with_name(p.stem + "_g.wav")
    run_ffmpeg(["-y", "-i", str(p), "-filter:a", f"volume={_clamp(db, -12, 12):.1f}dB,alimiter=limit=0.95", "-c:a", "pcm_s16le", str(out)])
    out.replace(p)
    return str(p)


# ------------------------------------------------------------------ verify + correct
def measure_lines(cues, idxs, loader=load_audio):
    """Khmer clip features {row: features + pace}."""
    out = {}
    for i in idxs:
        c = cues[i]
        if not (c.audio and Path(c.audio).exists()) or not (c.translation or "").strip():
            continue
        try:
            y = loader(c.audio)
        except Exception:  # noqa - unreadable clip is reported by QA
            continue
        f = features(y)
        if f:
            f["pace"] = _units(c.translation) / max(len(y) / SR, 0.2)
            out[i] = f
    return out


def _errors(cues, targets, meas):
    """-> {row: (err_st, err_db, err_pace)}: wanted minus achieved, measured relative to the Khmer speaker's own median."""
    by = {}
    for i in meas:
        by.setdefault(cues[i].speaker or "Speaker", []).append(i)
    res, base = {}, {}
    for sp, idx in by.items():
        base[sp] = {k: _med([meas[i][k] for i in idx]) for k in ("f0", "loud_db", "pace")}
    for i, f in meas.items():
        c, p = cues[i], load(cues[i])
        if not p:
            continue
        b = base[c.speaker or "Speaker"]
        got_st = 12 * math.log2(f["f0"] / b["f0"]) if len(by[c.speaker or "Speaker"]) >= 3 else 0.0
        got_db = f["loud_db"] - b["loud_db"] if len(by[c.speaker or "Speaker"]) >= 3 else 0.0
        got_pace = (f["pace"] / b["pace"]) if b["pace"] else 1.0
        res[i] = (p["want_st"] - got_st, p["want_db"] - got_db, p["want_pace"] - got_pace, b["f0"])
    return res


def score(err):
    es, ed, ep = abs(err[0]), abs(err[1]), abs(err[2])
    return int(round(max(0.0, 100 - min(50, es * 10) - min(30, ed * 5) - min(20, ep * 50))))


def verify_and_fix(cues, regen, rounds=2, idxs=None, loader=load_audio, progress=None, cancel=None):
    """Measure -> correct -> regenerate (max `rounds`). regen(idxs) must regenerate the voice of those rows with their (new) Cue.prosody.
    Sets Cue.match_score / Cue.match_note. -> {'avg': float, 'low': [1-based rows], 'fixed': n}"""
    say = progress or (lambda *_: None)
    idxs = list(range(len(cues))) if idxs is None else list(idxs)
    fixed = 0
    for rnd in range(rounds + 1):
        if cancel and cancel():
            raise Cancelled()
        meas = measure_lines(cues, idxs, loader)
        errs = _errors(cues, None, meas)
        for i, e in errs.items():
            cues[i].match_score = float(score(e[:3]))
            cues[i].match_note = f"pitch {e[0]:+.1f} st, loudness {e[1]:+.1f} dB, pace {e[2]:+.2f} from the target"
        if rnd == rounds:
            break
        redo = []
        for i, e in errs.items():
            if cues[i].match_score >= OK_SCORE and abs(e[0]) <= TOL_ST and abs(e[1]) <= TOL_DB:
                continue
            p = load(cues[i])
            base = e[3] or 150.0
            if abs(e[0]) > TOL_ST:
                p["pitch"] = int(round(_clamp(p["pitch"] + base * (2 ** (_clamp(e[0], -4, 4) / 12) - 1), -MAX_PITCH_HZ, MAX_PITCH_HZ)))
            if abs(e[1]) > TOL_DB:
                p["vol"] = int(round(_clamp(((1 + p["vol"] / 100) * 10 ** (_clamp(e[1], -8, 8) / 20) - 1) * 100, -50, MAX_VOL)))
            if abs(e[2]) > TOL_PACE:
                p["rate"] = int(round(_clamp(((1 + p["rate"] / 100) * (1 + _clamp(e[2], -0.4, 0.4)) - 1) * 100, RATE_MIN, RATE_MAX)))
            cues[i].prosody = json.dumps(p)
            cues[i].audio, cues[i].audio_sig, cues[i].voice_dur = "", "", 0.0
            redo.append(i)
        if not redo:
            break
        say(f"   Performance match round {rnd + 1}/{rounds}: correcting {len(redo)} line(s) ...")
        regen(redo)
        fixed += len(redo)
        idxs = redo + [i for i in idxs if i not in redo]
    sc = [c.match_score for c in cues if getattr(c, "match_score", -1) >= 0]
    low = [i + 1 for i, c in enumerate(cues) if 0 <= getattr(c, "match_score", -1) < OK_SCORE]
    return {"avg": (sum(sc) / len(sc)) if sc else 0.0, "low": low, "fixed": fixed, "scored": len(sc)}
