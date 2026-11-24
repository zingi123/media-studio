from pathlib import Path
from ..shared.ffmpeg_util import available_encoders, probe, run_ffmpeg
from ..shared.utils import unique_path

QUALITY_CRF = {"High": 18, "Medium": 23, "Small (compress)": 28}


def _video_args(fmt, quality, hw):
    crf = QUALITY_CRF.get(quality, 23)
    if fmt == "webm":
        return ["-c:v", "libvpx-vp9", "-crf", str(crf + 7), "-b:v", "0", "-c:a", "libopus"]
    enc = available_encoders()
    if hw:
        if "h264_nvenc" in enc:
            return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(crf), "-c:a", "aac"]
        if "h264_qsv" in enc:
            return ["-c:v", "h264_qsv", "-global_quality", str(crf), "-c:a", "aac"]
        if "h264_amf" in enc:
            return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-c:a", "aac"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac"]


def convert_video(src, out_dir, fmt="mp4", height=0, fps=0, quality="Medium", audio_kbps=128,
                  hw=False, progress=None, cancel=None):
    """Convert one video. Resolution/FPS only change if > 0. Falls back to CPU if hardware encode fails."""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = unique_path(out_dir / f"{src.stem}.{fmt}")
    info = probe(src)
    vf = []
    if height and height > 0:
        vf.append(f"scale=-2:{int(height)}")
    base = ["-i", src, "-map", "0:v:0?", "-map", "0:a:0?"]
    if vf:
        base += ["-vf", ",".join(vf)]
    if fps and fps > 0:
        base += ["-r", fps]

    def attempt(use_hw):
        args = base + _video_args(fmt, quality, use_hw) + ["-b:a", f"{audio_kbps}k"]
        if fmt in ("mp4", "mov"):
            args += ["-movflags", "+faststart"]
        run_ffmpeg(["-n"] + args + [out], duration=info["duration"], progress=progress, cancel=cancel)

    try:
        attempt(hw)
    except RuntimeError:
        if not hw:
            raise
        out.unlink(missing_ok=True)
        attempt(False)  # CPU fallback
    return str(out)
