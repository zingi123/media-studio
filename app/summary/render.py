"""Plan (clips) -> final video with one FFmpeg run: trim + slow motion + freeze + concat + narration + Khmer subtitles.

Khmer subtitles go through libass/HarfBuzz (.ass) exactly like the Translator's export, because that is what shapes Khmer
correctly; the font must exist on the machine (or in the work folder, fontsdir=.).
"""
import subprocess
from pathlib import Path

from ..shared.ffmpeg_util import available_encoders, require_ffmpeg, run_ffmpeg, _NOWIN
from . import sync as SY

INLINE_LIMIT = 24000        # longer filter graphs go to a file (Windows command lines are limited to ~32k characters)


def _even(v):
    return max(2, int(round(v / 2.0)) * 2)


def _ass_time(t):
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(path, project, W, H, font="Noto Sans Khmer", size=None):
    size = size or max(18, int(H * 0.05))
    head = ("[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
            f"PlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
            "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,"
            f"{int(W * 0.05)},{int(W * 0.05)},{int(H * 0.06)},1\n\n[Events]\n"
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    lines = []
    for g, s, e in SY.timeline(project):
        txt = (g.km or "").replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", "\\N").strip()
        if txt:
            lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{txt}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def _ffmpeg_major():
    try:
        r = subprocess.run([require_ffmpeg(), "-version"], capture_output=True, text=True, creationflags=_NOWIN)
        import re
        m = re.search(r"version\s+n?(\d+)", r.stdout)
        return int(m[1]) if m else 0
    except Exception:  # noqa
        return 0


def _graph_args(graph, work, inline_limit=INLINE_LIMIT):
    """Inline -filter_complex, or a graph file for very long edits (the option name changed in FFmpeg 7)."""
    if len(graph) <= inline_limit:
        return ["-filter_complex", graph]
    f = Path(work) / "graph.txt"
    f.write_text(graph, encoding="utf-8")
    return ["-/filter_complex", "graph.txt"] if _ffmpeg_major() >= 7 else ["-filter_complex_script", "graph.txt"]


def build_graph(project, W, H, fps, with_subs, has_source_audio=False, has_narration=False, has_music=False, original_volume=0.16, music_volume=0.12):
    parts, labels = [], []
    for i, c in enumerate(project.clips):
        f = (f"[0:v]trim=start={c.src_start:.6f}:end={c.src_end:.6f},setpts=(PTS-STARTPTS)/{c.speed:.6f},fps={fps:g},"
             f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
        if c.freeze > 1e-6:
            f += f",tpad=stop_mode=clone:stop_duration={c.freeze:.6f}"
        parts.append(f + f"[v{i}]")
        labels.append(f"[v{i}]")
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vc]")
    parts.append("[vc]ass=subs.ass:fontsdir=.[vout]" if with_subs else "[vc]null[vout]")
    audio_labels = []
    if has_source_audio:
        for i, c in enumerate(project.clips):
            parts.append(f"[0:a]atrim=start={c.src_start:.6f}:end={c.src_end:.6f},asetpts=PTS-STARTPTS,atempo={c.speed:.6f},apad=pad_dur={c.freeze:.6f},atrim=duration={c.out_len:.6f}[oa{i}]")
            audio_labels.append(f"[oa{i}]")
        parts.append("".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1[orig]")
        parts.append(f"[orig]volume={max(0.0, min(1.0, float(original_volume))):.3f}[origmix]")
        audio_labels = ["[origmix]"]
    if has_narration:
        parts.append("[1:a]aresample=48000,atrim=duration=" + f"{sum(c.out_len for c in project.clips):.6f}" + ",asetpts=PTS-STARTPTS[narr]")
        audio_labels.append("[narr]")
    if has_music:
        music_idx = 2 if has_narration else 1
        parts.append(f"[{music_idx}:a]aresample=48000,atrim=duration={sum(c.out_len for c in project.clips):.6f},asetpts=PTS-STARTPTS,volume={max(0.0, min(1.0, float(music_volume))):.3f}[music]")
        audio_labels.append("[music]")
    if audio_labels:
        if len(audio_labels) == 1:
            parts.append(audio_labels[0] + "anull[aout]")
        else:
            parts.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=2,alimiter=limit=0.95[aout]")
    return ";".join(parts)


def render_edl(project, out, narration=None, subtitles=False, work=None, font="Noto Sans Khmer", width=None, height=None,
               progress=None, cancel=None, inline_limit=INLINE_LIMIT, mix_original_audio=True, music=None, original_volume=0.16, music_volume=0.12):
    if not project.clips:
        raise ValueError("There is no plan to render - run the sync plan first.")
    if not Path(project.source).exists():
        raise ValueError(f"The source movie is missing: {project.source}")
    work = Path(work or Path(out).parent)
    work.mkdir(parents=True, exist_ok=True)
    W, H = _even(width or project.width or 1280), _even(height or project.height or 720)
    fps = project.fps or 25.0
    total = sum(c.out_len for c in project.clips)
    if subtitles:
        write_ass(work / "subs.ass", project, W, H, font)
    from ..shared.ffmpeg_util import probe
    source_has_audio = bool(mix_original_audio and probe(project.source).get("has_audio"))
    has_narration = bool(narration and Path(narration).is_file())
    has_music = bool(music and Path(music).is_file())
    graph = build_graph(project, W, H, fps, subtitles, source_has_audio, has_narration, has_music, original_volume, music_volume)
    enc = "libx264" if "libx264" in available_encoders() else "mpeg4"
    args = ["-y", "-i", str(Path(project.source).resolve())]
    if has_narration:
        args += ["-i", str(Path(narration).resolve())]
    if has_music:
        args += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
    args += _graph_args(graph, work, inline_limit) + ["-map", "[vout]"]
    if source_has_audio or has_narration or has_music:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-an"]
    args += ["-c:v", enc] + (["-preset", "veryfast", "-crf", "20"] if enc == "libx264" else ["-q:v", "3"])
    args += ["-pix_fmt", "yuv420p", "-t", f"{total:.3f}", str(Path(out).resolve())]
    run_ffmpeg(args, duration=total, progress=progress, cancel=cancel, cwd=str(work))
    return str(out)
