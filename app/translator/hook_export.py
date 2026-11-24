"""Hook export (no Qt): offline hook scoring + cutting hook windows out of the dubbed film.
Every hook clip is rendered by the SAME export_video the page uses (same Khmer subtitle / blur / logo styling), on a short cut of the
source, so a 6 s hook takes seconds, not minutes.  Intro modes: hook clip + full dubbed film joined with ffmpeg concat (stream copy:
both parts come from the same encoder settings, resolution and fps)."""
import copy
import json
import tempfile
from pathlib import Path

from ..shared.ffmpeg_util import run_ffmpeg

MODES = ("MP3 - Khmer voice only",
         "Video + Khmer voice (no subtitle)",
         "Video + Khmer subtitle + Khmer voice",
         "Hook + voice + video  (best hook first, then the full dubbed film, no subtitle)",
         "Hook + voice + video + subtitle  (best hook first, then the full film, Khmer subtitle)")
_EMO = {"angry": 3.0, "anger": 3.0, "fear": 3.0, "fearful": 3.0, "surprise": 3.0, "surprised": 3.0, "shout": 3.0, "sad": 2.0, "cry": 2.5,
        "happy": 1.0, "disgust": 2.0}


# ------------------------------------------------------------------ offline scoring (no AI key needed)
def _line_score(c):
    s = _EMO.get((c.emotion or "").lower(), 0.0)
    try:
        p = json.loads(getattr(c, "prosody", "") or "{}")
        s += abs(float(p.get("want_st", 0))) * 0.5 + max(0.0, float(p.get("want_db", 0))) * 0.3
    except (ValueError, TypeError):
        pass
    t = (c.translation or "") + (c.text or "")
    if "?" in t or "？" in t:
        s += 1.0
    if "!" in t or "！" in t:
        s += 1.0
    return s


def heuristic_hooks(cues, n=8, min_s=4.0, max_s=8.0):
    """Windows that START on a strong line (emotion / pitch+loudness above the speaker's normal / ? !) and end on a whole line inside min..max."""
    cand = []
    for a, c in enumerate(cues):
        if not ((c.translation or "").strip() or (c.text or "").strip()):
            continue
        b = a
        while b + 1 < len(cues) and cues[b + 1].end - c.start <= max_s:
            b += 1
        if cues[b].end - c.start < min_s:
            continue
        sc = _line_score(c) + 0.3 * _line_score(cues[min(a + 1, b)])
        cand.append((sc, a, b))
    out, used = [], []
    for sc, a, b in sorted(cand, reverse=True):
        s, e = cues[a].start, cues[b].end
        if sc <= 0 or any(s < u1 and e > u0 for u0, u1 in used):
            continue
        used.append((s, e))
        out.append({"segments": [{"start": s, "end": e, "start_id": a, "end_id": b}], "start": s, "end": e, "dur": round(e - s, 1),
                    "hook_type": "offline", "score": round(min(10.0, sc), 1), "special": "", "caption_km": "",
                    "why": "strong emotion / voice peak (offline scoring, the story is not analysed)", "risk": ""})
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------------ cutting
def shift_cues(cues, s, e):
    """Cues that touch [s, e] re-timed to start at 0 (so the Khmer subtitle follows the cut)."""
    out = []
    for c in cues:
        if c.end <= s or c.start >= e:
            continue
        d = copy.copy(c)
        d.start, d.end = max(0.0, c.start - s), min(e - s, c.end - s)
        out.append(d)
    return out


def cut_audio(wav, s, e, out, cancel=None):
    run_ffmpeg(["-y", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(wav), "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out)], cancel=cancel)
    return str(out)


def cut_video(video, s, e, out, cancel=None):
    run_ffmpeg(["-y", "-ss", f"{s:.3f}", "-i", str(video), "-t", f"{e - s:.3f}", "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "18", "-pix_fmt", "yuv420p", str(out)], cancel=cancel)
    return str(out)


def fx_for(fx, with_sub):
    """Copy of the page's overlay settings with the Khmer subtitle switched on or off (blur / logo / watermark are kept)."""
    if fx is None:
        return None
    f = fx.copy()
    if not with_sub:
        f.sub.enabled = False
        if f.scene is not None:
            for it in f.scene.items:
                if it.kind == "subtitle":
                    it.enabled = False
    return f


def _segs(h_or_segments):
    """A hook dict, a list of segment dicts or (start, end) -> list of {start, end}."""
    if isinstance(h_or_segments, dict):
        return h_or_segments.get("segments") or [{"start": h_or_segments["start"], "end": h_or_segments["end"]}]
    return list(h_or_segments)


def compose(video, mix_wav, cues, segments, tmp, cancel=None):
    """Cut every piece (video + voice) and join them in the listed order -> (video_path, wav_path, cues re-timed to the joined timeline).
    One piece = the same as a plain cut. Pieces from different scenes become ONE combined hook."""
    segs = _segs(segments)
    if len(segs) == 1:
        s, e = segs[0]["start"], segs[0]["end"]
        return (cut_video(video, s, e, tmp / "cut.mp4", cancel), cut_audio(mix_wav, s, e, tmp / "voice.wav", cancel), shift_cues(cues, s, e))
    vids, wavs, out_cues, off = [], [], [], 0.0
    for k, g in enumerate(segs):
        s, e = g["start"], g["end"]
        vids.append(cut_video(video, s, e, tmp / f"cut{k}.mp4", cancel))
        wavs.append(cut_audio(mix_wav, s, e, tmp / f"voice{k}.wav", cancel))
        for c in shift_cues(cues, s, e):
            c.start, c.end = c.start + off, c.end + off
            out_cues.append(c)
        off += e - s
    lv, la = tmp / "v.txt", tmp / "a.txt"
    lv.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in vids), encoding="utf-8")
    la.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in wavs), encoding="utf-8")
    vo, ao = tmp / "cut.mp4", tmp / "voice.wav"
    run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(lv), "-c", "copy", str(vo)], cancel=cancel)
    run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(la), "-c", "copy", str(ao)], cancel=cancel)
    return str(vo), str(ao), out_cues


def render_clip(video, mix_wav, cues, segments, out_path, with_sub, font, fx, export_video, cancel=None, progress=None):
    """One hook (single or combined: video + Khmer voice [+ Khmer subtitle]) -> path. export_video is the page's own function."""
    tmp = Path(tempfile.mkdtemp(prefix="hook_"))
    try:
        vid, wav, hc = compose(video, mix_wav, cues, segments, tmp, cancel)
        fxl = fx_for(fx, with_sub)
        res = export_video(vid, out_path, hc, wav, 0.0, "burn" if with_sub else "none", font, progress, cancel, bg_wav=None, fx=fxl,
                           voice_volume=1.0, bg_volume=1.0)
        return str(res)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def render_audio(mix_wav, segments, out_path, export_audio, cancel=None):
    tmp = Path(tempfile.mkdtemp(prefix="hook_"))
    try:
        segs = _segs(segments)
        parts = [cut_audio(mix_wav, g["start"], g["end"], tmp / f"a{k}.wav", cancel) for k, g in enumerate(segs)]
        if len(parts) == 1:
            wav = parts[0]
        else:
            lst = tmp / "a.txt"
            lst.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
            wav = str(tmp / "joined.wav")
            run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", wav], cancel=cancel)
        return str(export_audio(wav, out_path, cancel=cancel))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def concat_intro(first, second, out_path, cancel=None):
    """first + second -> one mp4 (stream copy; falls back to a re-encode of the join when the parts do not match)."""
    out = Path(out_path)
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in (first, second)), encoding="utf-8")
    try:
        try:
            run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)], cancel=cancel)
        except Exception as e:  # noqa
            if type(e).__name__ == "Cancelled":
                raise
            run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(out)], cancel=cancel)
    finally:
        lst.unlink(missing_ok=True)
    return str(out)
