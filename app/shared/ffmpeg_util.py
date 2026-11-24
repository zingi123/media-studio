import re
import shutil
import subprocess
import sys
from pathlib import Path
from .paths import app_root
from .settings import settings

_NOWIN = 0x08000000 if sys.platform.startswith("win") else 0


class Cancelled(Exception):
    pass


def find_ffmpeg():
    cand = settings().get("ffmpeg_path")
    if cand and Path(cand).exists():
        return str(cand)
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    local = app_root() / "bin" / exe
    if local.exists():
        return str(local)
    w = shutil.which("ffmpeg")
    if w:
        return w
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def require_ffmpeg():
    f = find_ffmpeg()
    if not f:
        raise RuntimeError("FFmpeg not found. Put ffmpeg.exe in the 'bin' folder, install it, "
                           "or set its path in Settings.")
    return f


def probe(path) -> dict:
    """Read duration/resolution/fps by parsing `ffmpeg -i` output (works without ffprobe)."""
    ff = require_ffmpeg()
    r = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=_NOWIN)
    t = r.stderr
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0, "has_video": False,
            "has_audio": "Audio:" in t, "vcodec": ""}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", t)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Video:\s*([A-Za-z0-9_]+).*?,\s*(\d{2,5})x(\d{2,5})", t)
    if m:
        info.update(has_video=True, vcodec=m[1], width=int(m[2]), height=int(m[3]))
    m = re.search(r"([\d.]+)\s*fps", t)
    if m:
        info["fps"] = float(m[1])
    return info


def run_ffmpeg(args, duration=None, progress=None, cancel=None, cwd=None):
    """Run ffmpeg, report progress 0-100, raise on failure. `cancel` is a callable -> bool."""
    ff = require_ffmpeg()
    cmd = [ff, "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats"] + [str(a) for a in args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", creationflags=_NOWIN, cwd=cwd)
    tail = []
    try:
        for line in p.stdout:
            line = line.strip()
            if cancel and cancel():
                p.kill()
                raise Cancelled()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=")[1])
                    if duration and progress:
                        progress(min(99, int(us / 1_000_000 / duration * 100)))
                except ValueError:
                    pass
            elif "=" not in line:
                tail.append(line)
                tail = tail[-12:]
    finally:
        p.stdout.close()
        p.wait()
    if p.returncode != 0:
        raise RuntimeError("FFmpeg failed:\n" + "\n".join(tail[-6:]))
    if progress:
        progress(100)


_encoders = None


def available_encoders() -> set:
    global _encoders
    if _encoders is None:
        try:
            r = subprocess.run([require_ffmpeg(), "-hide_banner", "-encoders"], capture_output=True,
                               text=True, errors="replace", creationflags=_NOWIN)
            _encoders = set(re.findall(r"\s(h264_nvenc|h264_qsv|h264_amf|libx264|libvpx-vp9)\s", r.stdout))
        except Exception:
            _encoders = set()
    return _encoders
