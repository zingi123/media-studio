import shutil
import tempfile
from pathlib import Path
from ..shared.ffmpeg_util import probe, run_ffmpeg
from ..shared.utils import unique_path
from .subtitles import save_subtitle_file
from . import video_overlay as VO
from dataclasses import replace


def export_audio(voice_wav, out_path, progress=None, cancel=None, volume=1.0):  # volume = សំឡេងខ្មែរ
    out = unique_path(Path(out_path))
    if out.suffix.lower() == ".wav" and volume == 1.0:
        shutil.copy(voice_wav, out)
    else:
        codec = ["-c:a", "libmp3lame", "-q:a", "2"] if out.suffix.lower() == ".mp3" else []  # wav → ទុកដដែល
        run_ffmpeg(["-n", "-i", voice_wav, "-af", f"volume={volume},alimiter=limit=0.95", *codec, out], cancel=cancel)
    return str(out)


def export_video(video, out_path, cues, voice_wav=None, original_volume=0.0, subs="soft", font_path="",
                 progress=None, cancel=None, bg_wav=None, fx=None, voice_volume=1.0, bg_volume=1.0):  # bg_wav = music+SFX ពី Demucs ; fx = Overlay (blur/Khmer sub/logo)
    """subs: 'soft' (selectable track, safest for Khmer) | 'burn' (hard-coded; needs a Khmer font) | 'none'.
    voice_wav: Khmer voiceover. original_volume: 0 = mute original, 0.15 = keep quietly in background.
    Note: 'background' here only lowers the original track; it does NOT separate music from speech."""
    video = str(Path(video).resolve())
    voice_wav = str(Path(voice_wav).resolve()) if voice_wav else None
    info = probe(video)
    fxa = bool(fx and fx.active())
    scene = None
    if fxa:
        fx = fx.copy()
        scene = fx.scene
        if scene is not None:
            # On-screen objects (studio scene): the Khmer subtitle object IS the burned-in subtitle.
            if subs == "burn" and scene.primary("subtitle") is None and not any(i.kind == "subtitle" for i in scene.items):
                from ..studio.model import Item
                scene.add(Item(kind="subtitle", key="sub", cx=0.5, cy=0.88, w=0.9, h=0.1, size=54))
            if any(i.kind == "subtitle" and i.enabled for i in scene.items):
                subs = "none"            # no duplicate soft track
        else:
            if subs == "burn" and not fx.sub.enabled:
                fx.sub.enabled = True        # burn-in goes through the same ASS path as the preview
            if fx.sub.enabled:
                subs = "none"                # no duplicate soft track
    out = unique_path(Path(out_path)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ms_export_"))
    try:
        args = ["-n", "-i", video]
        idx = 1
        voice_idx = sub_idx = None
        if voice_wav:
            args += ["-i", voice_wav]
            voice_idx, idx = idx, idx + 1
        bg_idx = None  # index ឯកសារ music+SFX
        if bg_wav:  # បើមាន Demucs
            args += ["-i", str(Path(bg_wav).resolve())]  # បន្ថែម input
            bg_idx, idx = idx, idx + 1  # កត់ index
        logo_idx = None
        wm_idx = None
        if fxa and scene is None and VO.logo_ok(fx):
            args += ["-loop", "1", "-i", str(Path(fx.logo.path).resolve())]
            logo_idx, idx = idx, idx + 1
        if fxa and scene is None and VO.wm_ok(fx):
            args += ["-loop", "1", "-i", str(Path(fx.track.wm_path).resolve())]
            wm_idx, idx = idx, idx + 1
        pre = None
        if fxa and scene is not None:
            from ..studio import render as SR_
            pre = SR_.prepare(scene, cues, info, tmp, idx)          # image inputs + ASS + graph (same code as the preview)
            args += pre["args"]
            idx = pre["next_index"]
        if subs == "soft":
            srt = tmp / "subs.srt"
            save_subtitle_file(srt, cues, "translation")
            args += ["-i", srt]
            sub_idx, idx = idx, idx + 1
        filt = []
        if fxa and pre is not None:
            filt.append(pre["graph"])
            args += ["-map", "[vout]"]
        elif fxa:
            VO.stage_files(fx, cues, tmp, info["width"], info["height"])
            filt.append(VO.video_chain(fx, info["width"], info["height"], logo_idx, wm_idx=wm_idx))
            args += ["-map", "[vout]"]
        else:
            args += ["-map", "0:v:0"]
        if voice_wav:
            if bg_idx is not None:  # music+SFX ដើម + សំឡេងខ្មែរ
                filt.append(f"[{bg_idx}:a]volume={bg_volume}[b];[{voice_idx}:a]volume={voice_volume}[v];"
                            f"[b][v]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]")  # volume ដាច់ដោយឡែក  # លាយ + ការពារ clip
                amap = "[a]"
            elif original_volume > 0 and info["has_audio"]:
                filt.append(f"[0:a]volume={original_volume}[o];[{voice_idx}:a]volume={voice_volume}[v];"   # voice volume was ignored here (bug)
                            f"[o][v]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]")
                amap = "[a]"
            elif voice_volume != 1.0:  # គ្មាន music → កែ volume សំឡេងខ្មែរ
                filt.append(f"[{voice_idx}:a]volume={voice_volume},alimiter=limit=0.95[a]")
                amap = "[a]"
            else:
                amap = f"{voice_idx}:a"
            args += ["-map", amap]
        else:
            args += ["-map", "0:a?"]
        vf = None
        if subs == "burn" and not fxa:
            save_subtitle_file(tmp / "subs.srt", cues, "translation")
            style = "FontSize=22,Outline=2,Alignment=2"
            fd = ""
            if font_path and Path(font_path).exists():
                shutil.copy(font_path, tmp / Path(font_path).name)
                style = f"FontName={Path(font_path).stem},{style}"
                fd = ":fontsdir=."
            vf = f"subtitles=subs.srt{fd}:force_style='{style}'"
        if sub_idx is not None:
            args += ["-map", str(sub_idx)]
        if filt:
            args += ["-filter_complex", ";".join(filt)]
        if vf or fxa:
            if vf:
                args += ["-vf", vf]
            args += [ "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
        else:
            args += ["-c:v", "copy"]
        args += ["-c:a", "aac", "-b:a", "192k"]
        if sub_idx is not None:
            args += ["-c:s", "mov_text", "-metadata:s:s:0", "language=khm"]
        args += ["-t", f"{info['duration']:.3f}", "-movflags", "+faststart", out]
        run_ffmpeg(args, duration=info["duration"], progress=progress, cancel=cancel, cwd=str(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return str(out)


def preview_mix(video, voice_wav, out_wav, original_volume=0.0, voice_volume=1.0, bg_wav=None, bg_volume=1.0,
                start=0.0, duration=30.0, cancel=None):
    """Audio-only PREVIEW of the final mix (original/background + Khmer voice) for [start, start+duration] seconds,
    with the same volumes the export will use. Nothing is written next to the video."""
    video, voice_wav = str(Path(video).resolve()), str(Path(voice_wav).resolve())
    out = Path(out_wav)
    out.parent.mkdir(parents=True, exist_ok=True)
    info = probe(video)
    args = ["-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", voice_wav]
    if bg_wav:
        args += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(Path(bg_wav).resolve())]
        filt = f"[1:a]volume={bg_volume}[o];[0:a]volume={voice_volume}[v];[o][v]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]"
    elif original_volume > 0 and info["has_audio"]:
        args += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", video]
        filt = f"[1:a]volume={original_volume}[o];[0:a]volume={voice_volume}[v];[o][v]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]"
    else:
        filt = f"[0:a]volume={voice_volume},alimiter=limit=0.95[a]"
    args += ["-filter_complex", filt, "-map", "[a]", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out)]
    run_ffmpeg(args, cancel=cancel)
    return str(out)
