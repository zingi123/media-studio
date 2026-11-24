from pathlib import Path
from ..shared.ffmpeg_util import probe, run_ffmpeg, Cancelled


def extract_frames(src, out_root=None, count=10, interval=0, fmt="jpg", progress=None, cancel=None):
    """Extract `count` evenly spaced frames, or one frame every `interval` seconds if interval > 0."""
    src = Path(src)
    dur = probe(src)["duration"]
    if not dur:
        raise RuntimeError("Could not read the video duration.")
    if interval and interval > 0:
        times = [t * interval for t in range(int(dur // interval) + 1) if t * interval < dur]
    else:
        times = [(i + 0.5) * dur / count for i in range(count)]
    out_dir = Path(out_root or src.parent) / "Extracted_Frames" / src.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    outs = []
    for i, t in enumerate(times, 1):
        if cancel and cancel():
            raise Cancelled()
        out = out_dir / f"Frame_{i:03d}.{fmt}"
        if out.exists():
            n = 1
            while (out_dir / f"Frame_{i:03d}_{n}.{fmt}").exists():
                n += 1
            out = out_dir / f"Frame_{i:03d}_{n}.{fmt}"
        extra = ["-q:v", "2"] if fmt in ("jpg", "jpeg") else []
        run_ffmpeg(["-n", "-ss", round(t, 3), "-i", src, "-frames:v", 1] + extra + [out], cancel=cancel)
        outs.append(str(out))
        if progress:
            progress(int(i * 100 / len(times)))
    return outs
