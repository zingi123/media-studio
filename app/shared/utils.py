import os
import re
import subprocess
import sys
from pathlib import Path

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def kind_of(path) -> str:
    e = Path(path).suffix.lower()
    if e in VIDEO_EXT:
        return "video"
    if e in IMAGE_EXT:
        return "image"
    if e in AUDIO_EXT:
        return "audio"
    return "other"


def safe_name(s: str, maxlen=120) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip(" .")
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen].strip(" .")) or "untitled"


def unique_path(p: Path) -> Path:
    """Never overwrite: add _1, _2... if the file exists."""
    p = Path(p)
    if not p.exists():
        return p
    i = 1
    while True:
        q = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not q.exists():
            return q
        i += 1


def open_path(path):
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    total_ms = int(round(sec * 1000))
    s, ms = divmod(total_ms, 1000)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},{ms:03d}"


def parse_time(t: str) -> float:
    parts = [float(x) for x in t.strip().replace(",", ".").split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def fmt_duration(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def natural_key(name):
    """Sort key so that 'Episode 2' < 'Episode 10' (numbers compare as numbers, not text)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", Path(str(name)).name)]
