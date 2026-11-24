import wave
from pathlib import Path
import numpy as np
from ..shared.ffmpeg_util import Cancelled, probe, run_ffmpeg

SR = 24000
FADE_S = 0.08          # fade-out when a clip still has to be trimmed (was 50 ms; a longer fade hides the cut better)
LAST_REPORT = []       # filled by build_voice_track: [{"line": 1-based, "clip": s, "slot": s, "over": s, "speedup": x}] (read-only info)


def _decode(src, tempo, tmp_wav):
    sil = "silenceremove=start_periods=1:start_threshold=-45dB"  # កាត់ស្ងាត់ដើម
    f = f"{sil},areverse,{sil},areverse"  # កាត់ស្ងាត់ចុង
    if tempo > 1.01:
        f += f",atempo={min(tempo, 2.0):.3f}"  # បង្កើនល្បឿន
    run_ffmpeg(["-y", "-i", src, "-ac", "1", "-ar", str(SR), "-filter:a", f, "-c:a", "pcm_s16le", tmp_wav])  # decode
    with wave.open(str(tmp_wav), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def build_voice_track(cues, total_duration, out_wav, max_speedup=1.5, progress=None, cancel=None, offset=0.0, report=None):
    """Place each cue's audio at its start time on one timeline.
    If a clip is longer than its slot it is sped up (up to max_speedup); anything still too long may spill into
    the gap before the next cue, otherwise it is trimmed. Returns indices (0-based) that overran.
    offset: seconds added to every clip's start (default 0 = voice starts exactly with the cue; +0.5 = 0.5 s later; negative = earlier).
    Rows are placed by START TIME whatever their order in the list; every clip keeps its own row's start (no drift),
    and clips of different speakers that overlap in the original are mixed (natural cross-talk), never moved.
    report: optional list that receives the overrun entries (use it instead of the module-global LAST_REPORT)."""
    out_wav = Path(out_wav)
    tmp = out_wav.parent / "_clip.wav"
    n = int((total_duration + max(0.0, offset)) * SR) + SR
    track = np.zeros(n, dtype=np.int32)
    overran = []
    LAST_REPORT.clear()
    spoken = sorted([(i, c) for i, c in enumerate(cues) if c.audio and Path(c.audio).exists()], key=lambda t: t[1].start)
    for k, (i, c) in enumerate(spoken):
        if cancel and cancel():
            raise Cancelled()
        raw = _decode(c.audio, 1.0, tmp)
        clip_dur = len(raw) / SR
        nxt = spoken[k + 1][1].start if k + 1 < len(spoken) else total_duration  # ចាប់ផ្តើមឃ្លាបន្ទាប់
        limit = max(c.end, nxt - 0.05) - c.start  # ពេលដែលមាន (អាចលើសចូល gap)
        limit = max(0.2, limit)  # យ៉ាងតិច 0.2s
        speedup = 1.0
        if clip_dur > limit:
            speedup = min(max_speedup, clip_dur / limit)
            raw = _decode(c.audio, speedup, tmp)  # បង្កើនល្បឿនតាមតម្រូវការ
        if len(raw) / SR > limit + 0.05:
            entry = {"line": i + 1, "clip": round(clip_dur, 2), "slot": round(limit, 2),
                     "over": round(len(raw) / SR - limit, 2), "speedup": round(speedup, 2)}
            LAST_REPORT.append(entry)
            if report is not None:
                report.append(entry)
            overran.append(i)
            raw = raw[:int(limit * SR)].astype(np.float32)  # កាត់ចុង
            n_f = min(len(raw), int(FADE_S * SR))  # fade
            raw[-n_f:] *= np.linspace(1, 0, n_f)  # fade-out
            raw = raw.astype(np.int16)
        s = max(0, int((c.start + offset) * SR))
        track[s:s + len(raw)] += raw[:max(0, n - s)]
        if progress:
            progress(int((k + 1) * 100 / len(spoken)))
    tmp.unlink(missing_ok=True)
    track = np.clip(track, -32768, 32767).astype(np.int16)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(track.tobytes())
    return overran


def timing_report(cues, total_duration=None, max_speedup=1.5, durations=None):
    """Check the timing BEFORE export. -> list of warning strings (empty = fine).
    Looks at: overlapping lines, lines past the end of the video, and generated clips that cannot fit their slot
    even at max_speedup (they would be trimmed). `durations` = {index: clip seconds} (default: read with ffprobe)."""
    out = []
    order = sorted(range(len(cues)), key=lambda k: cues[k].start)
    for a, b in zip(order, order[1:]):
        if cues[a].end > cues[b].start + 0.05:
            out.append(f"Lines {a + 1} and {b + 1} overlap by {cues[a].end - cues[b].start:.2f}s.")
    if total_duration:
        out += [f"Line {k + 1} ends after the video ({cues[k].end:.1f}s > {total_duration:.1f}s)." for k in order if cues[k].end > total_duration + 0.05]
    spoken = [k for k in order if cues[k].audio and Path(cues[k].audio).exists()]
    for j, k in enumerate(spoken):
        try:
            d = durations[k] if durations and k in durations else probe(cues[k].audio)["duration"]
        except Exception:  # noqa - an unreadable clip is reported elsewhere (it will be silent)
            continue
        nxt = cues[spoken[j + 1]].start if j + 1 < len(spoken) else (total_duration or cues[k].end + 5)
        slot = max(0.2, max(cues[k].end, nxt - 0.05) - cues[k].start)
        if d > slot * max_speedup + 0.05:
            out.append(f"Line {k + 1}: voice {d:.1f}s does not fit {slot:.1f}s even at x{max_speedup:g} - the end will be cut. "
                       "Shorten the Khmer text, or move the Start/End.")
        elif d > slot + 0.05:
            out.append(f"Line {k + 1}: voice {d:.1f}s is longer than {slot:.1f}s - will be sped up x{d / slot:.2f}.")
    return out


def video_duration(video):
    return probe(video)["duration"]
