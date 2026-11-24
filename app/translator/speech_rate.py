"""
Speech-rate matching (ADD-ON, does not change any existing behaviour unless switched on).

1. measure_rates(cues)   -> per-speaker + per-line speaking rate of the ORIGINAL speech
                            (units/second, where unit = word for spaced languages, character for CJK/Thai...).
                            Pause tags and emotion tags are ignored; Cue.start/end are already the real speech span.
2. fit_cues(cues, idxs)  -> after the Khmer voice is generated, time-stretch each line (ffmpeg atempo, pitch kept)
                            so its length matches the ORIGINAL line length. Lines of the same speaker are pulled
                            towards that speaker's own median so one character keeps one consistent pace.

Why durations and not words/second: Khmer and the source language have different syllable density, so a
"rate" cannot be compared directly. The ORIGINAL line duration is the target that is always valid.
"""
import re
import statistics
import wave
from pathlib import Path
from ..shared.ffmpeg_util import Cancelled, run_ffmpeg

TAG_RE = re.compile(r"\([^()]*\)|\[pause\s*([0-9.]+)s\]", re.I)   # same tags as script_builder.TAG_RE
WORD_RE = re.compile(r"[A-Za-z0-9\u00C0-\u024F']+")
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0e00-\u0e7f]")

MIN_F, MAX_F = 0.85, 1.40      # allowed tempo (1.0 = untouched). Below 0.85 sounds drawn out, above 1.4 sounds rushed.
BLEND = 0.5                    # 0 = every line exactly on its own length, 1 = whole speaker at one pace
MIN_LINE = 0.3                 # lines shorter than this (s) are left alone: too short to measure reliably
FAST, SLOW = 1.25, 0.80        # line rate / speaker median -> label


def _split_tags(text):
    """-> (clean text, total seconds of explicit [pause Ns] tags)."""
    pause = sum(float(m.group(1)) for m in TAG_RE.finditer(text or "") if m.group(1))
    return TAG_RE.sub(" ", text or ""), pause


def count_units(text):
    clean, _ = _split_tags(text)
    cjk = len(CJK_RE.findall(clean))
    return cjk + len(WORD_RE.findall(CJK_RE.sub(" ", clean)))


def speech_seconds(c):
    """Original time the character really talks in this line (line span minus explicit pauses)."""
    _, pause = _split_tags(c.text)
    return max(0.2, (c.end - c.start) - pause)


def measure_rates(cues):
    """-> {speaker: {'rate': units/s, 'lines': n, 'seconds': s}}, [per-line rate or 0.0]"""
    per_line, acc = [], {}
    for c in cues:
        n, d = count_units(c.text), speech_seconds(c)
        per_line.append(n / d if n else 0.0)
        a = acc.setdefault(c.speaker or "Speaker", {"units": 0, "sec": 0.0, "lines": 0})
        a["units"] += n; a["sec"] += d; a["lines"] += 1
    spk = {k: {"rate": v["units"] / v["sec"] if v["sec"] else 0.0, "lines": v["lines"], "seconds": round(v["sec"], 1)}
           for k, v in acc.items()}
    return spk, per_line


def describe(cues):
    """Human-readable summary lines for the log."""
    spk, per_line = measure_rates(cues)
    out = []
    for k, v in sorted(spk.items()):
        rel = [r / v["rate"] for c, r in zip(cues, per_line) if (c.speaker or "Speaker") == k and r and v["rate"]]
        fast = sum(1 for x in rel if x >= FAST); slow = sum(1 for x in rel if x <= SLOW)
        out.append(f"  {k}: {v['rate']:.1f} units/s over {v['lines']} line(s) ({fast} fast, {slow} slow)")
    return out


def _wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _trimmed_seconds(src, tmp_wav):
    """Length of the generated clip without leading/trailing silence (same trim as voice_synchronizer)."""
    sil = "silenceremove=start_periods=1:start_threshold=-45dB"
    run_ffmpeg(["-y", "-i", str(src), "-ac", "1", "-ar", "24000", "-filter:a", f"{sil},areverse,{sil},areverse",
                "-c:a", "pcm_s16le", str(tmp_wav)])
    return _wav_seconds(tmp_wav)


def plan_factors(cues, clip_secs, apply_to=None):
    """clip_secs: {idx: generated clip length} (may include lines that are NOT being changed, so the speaker's
    median stays meaningful when only a few lines are regenerated). -> {idx: tempo factor} for apply_to."""
    raw = {i: clip_secs[i] / speech_seconds(cues[i]) for i in clip_secs
           if clip_secs[i] > 0 and (cues[i].end - cues[i].start) >= MIN_LINE}
    by_spk = {}
    for i, f in raw.items():
        by_spk.setdefault(cues[i].speaker or "Speaker", []).append(f)
    med = {k: statistics.median(v) for k, v in by_spk.items()}
    out = {}
    for i, f in raw.items():
        if apply_to is not None and i not in apply_to:
            continue
        m = med[cues[i].speaker or "Speaker"]
        f2 = (m ** BLEND) * (f ** (1 - BLEND))              # geometric blend: speaker pace <-> this line's own length
        out[i] = max(MIN_F, min(MAX_F, f2))
    return out


def fit_cues(cues, idxs, work, progress=None, cancel=None):
    """Time-stretch the generated audio of cues[idxs] to the original speaking speed. Sets c.audio to the new file.
    Returns a list of log lines. Original TTS files are kept; only c.audio points to the fitted copy."""
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    tmp = work / "_rate_probe.wav"
    mine = set(idxs)
    probe_list = [i for i, c in enumerate(cues)
                  if c.audio and Path(c.audio).exists() and c.translation.strip()
                  and (i in mine or not Path(c.audio).stem.endswith("_fit"))]   # already-fitted others would distort the median
    clip = {}
    for n, i in enumerate(probe_list, 1):
        if cancel and cancel():
            raise Cancelled()
        clip[i] = _trimmed_seconds(cues[i].audio, tmp)
        if progress:
            progress(int(n * 50 / max(1, len(probe_list))))
    fac = plan_factors(cues, clip, apply_to=mine)
    todo = [i for i in sorted(mine) if i in fac]
    log, done = [], 0
    for n, i in enumerate(todo, 1):
        if cancel and cancel():
            raise Cancelled()
        f = fac[i]
        if abs(f - 1.0) >= 0.03:                              # ignore tiny changes (keeps audio untouched)
            src = Path(cues[i].audio)
            out = work / f"{src.stem}_fit.wav"
            run_ffmpeg(["-y", "-i", str(src), "-filter:a", f"atempo={f:.3f}", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
            cues[i].audio = str(out); done += 1
        if progress:
            progress(50 + int(n * 50 / max(1, len(todo))))
    tmp.unlink(missing_ok=True)
    spk = {}
    for i in todo:
        spk.setdefault(cues[i].speaker or "Speaker", []).append(fac[i])
    log.append(f"Speech-rate match: {done}/{len(todo)} line(s) adjusted.")
    for k, v in sorted(spk.items()):
        log.append(f"  {k}: tempo {min(v):.2f}-{max(v):.2f} (median {statistics.median(v):.2f})")
    return log
