"""Scene detection with FFmpeg's own `scene` score (no extra package; PySceneDetect can be added later behind the same API).

Cuts closer than min_len to a neighbour are dropped; scenes longer than max_len are split evenly so the matcher always has
usable granularity even for long single takes.
"""
import math
import re
import subprocess
from pathlib import Path

from ..shared.ffmpeg_util import Cancelled, probe, require_ffmpeg, run_ffmpeg, _NOWIN
from .model import Scene

_PTS = re.compile(r"pts_time:([0-9.]+)")


def _cut_times(video, threshold, duration, progress, cancel):
    cmd = [require_ffmpeg(), "-hide_banner", "-nostdin", "-i", str(video), "-an", "-sn",
           "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", creationflags=_NOWIN)
    cuts, tail = [], []
    try:
        for line in p.stderr:
            if cancel and cancel():
                p.kill()
                raise Cancelled()
            m = _PTS.search(line) if "showinfo" in line else None
            if m:
                t = float(m[1])
                cuts.append(t)
                if progress and duration:
                    progress(min(99, int(t / duration * 100)))
            else:
                tail = (tail + [line.strip()])[-6:]
    finally:
        p.stderr.close()
        p.wait()
    if p.returncode != 0:
        raise RuntimeError("FFmpeg scene detection failed:\n" + "\n".join(tail))
    return sorted(cuts)


def detect_scenes(video, threshold=0.3, min_len=1.0, max_len=30.0, progress=None, cancel=None):
    info = probe(video)
    dur = info["duration"]
    if not info["has_video"] or dur <= 0:
        raise ValueError("The file has no readable video stream.")
    cuts, last = [], 0.0
    for c in _cut_times(video, threshold, dur, progress, cancel):
        if c - last >= min_len and dur - c >= min_len:
            cuts.append(c)
            last = c
    bounds = [0.0] + cuts + [dur]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        n = max(1, math.ceil((b - a) / max_len - 1e-9)) if max_len and max_len > 0 else 1
        step = (b - a) / n
        for k in range(n):
            out.append((a + k * step, a + (k + 1) * step if k < n - 1 else b))
    scenes = [Scene(id=i, start=round(a, 3), end=round(b, 3)) for i, (a, b) in enumerate(out)]
    for x, y in zip(scenes, scenes[1:]):      # rounding must never leave a gap
        y.start = x.end
    if progress:
        progress(100)
    return scenes


def extract_thumbs(video, scenes, out_dir, progress=None, cancel=None):
    """One frame from the middle of every scene -> scene.thumb (small JPEG)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for k, s in enumerate(scenes):
        if cancel and cancel():
            raise Cancelled()
        f = out_dir / f"scene_{s.id:04d}.jpg"
        run_ffmpeg(["-y", "-ss", f"{(s.start + s.end) / 2:.3f}", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=320:-2", "-q:v", "4", str(f)], cancel=cancel)
        s.thumb = str(f)
        if progress:
            progress(int((k + 1) * 100 / len(scenes)))
    return scenes
