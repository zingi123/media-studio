import random
from pathlib import Path
from ..shared.ffmpeg_util import probe, run_ffmpeg, Cancelled


def plan_segments(total, mode="fixed", a=20.0, b=35.0, rng=None):
    """Return [(start, length)]. fixed: every clip is `a` seconds. random: each clip is uniform in [a, b].
    A leftover tail shorter than 1 second is dropped; a longer tail is kept as the last clip."""
    rng = rng or random.Random()
    if a <= 0 or (mode == "random" and b < a):
        raise ValueError("Invalid durations")
    segs, t = [], 0.0
    while total - t >= 1.0:
        want = a if mode == "fixed" else rng.uniform(a, b)
        length = min(want, total - t)
        segs.append((round(t, 3), round(length, 3)))
        t += length
    return segs


def split_video(src, out_root=None, mode="fixed", a=20, b=35, name_fmt="Part_{n}", progress=None, cancel=None):
    """Split into a 'short' folder next to the source (or in out_root). Re-encodes for frame-accurate cuts."""
    src = Path(src)
    info = probe(src)
    if not info["duration"]:
        raise RuntimeError("Could not read the video duration.")
    out_dir = Path(out_root or src.parent) / "short"
    out_dir.mkdir(parents=True, exist_ok=True)
    segs = plan_segments(info["duration"], mode, a, b)
    outs = []
    for i, (start, length) in enumerate(segs, 1):
        if cancel and cancel():
            raise Cancelled()
        out = out_dir / f"{src.stem}_{name_fmt.format(n=i)}.mp4"
        n = 1
        while out.exists():
            out = out_dir / f"{src.stem}_{name_fmt.format(n=i)}_{n}.mp4"
            n += 1
        run_ffmpeg(["-n", "-ss", start, "-i", src, "-t", length, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out],
                   cancel=cancel)
        outs.append(str(out))
        if progress:
            progress(int(i * 100 / len(segs)))
    return outs
