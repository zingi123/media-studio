"""Merge many episode files into ONE video, in episode order (Episode / Part / Clip 001 -> any number).

Order comes from the NUMBER in each file name (Episode 001, Episode 2, EP10, E05, 第12集 ...), never from the
text order, so 'Episode 2' always comes before 'Episode 10'.

Method:
  * all files have the same codec/size/fps/audio -> joined WITHOUT re-encoding (fast, no quality loss)
  * otherwise (Auto) only the files that differ are re-encoded to a common format, then everything is joined
  * if the fast join fails in Auto mode, everything is re-encoded once (slow but safe)
"""
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from ..shared.ffmpeg_util import Cancelled, require_ffmpeg, run_ffmpeg, _NOWIN
from ..shared.paths import work_dir
from ..shared.utils import natural_key, unique_path

MODES = ["Auto", "Fast (no re-encode)", "Safe (re-encode all)"]

_WORDS = r"(episode|part|clip|chapter|vol|volume|ep)"
_PATTERNS = [re.compile(r"\b(?:episode|part|clip|chapter|volume|vol)\s*[._\-#]*\s*(\d+)", re.I),
             re.compile(r"\bep\.?\s*[._\-#]*\s*(\d+)", re.I),
             re.compile(r"第\s*(\d+)\s*[集话話]"),
             re.compile(r"(?<![A-Za-z])e\s*(\d{1,4})(?!\d)", re.I)]   # E05, S01E05


def episode_number(path):
    """'Episode 001.mp4' -> 1.  Falls back to the last number in the name; None if there is no number."""
    stem = Path(str(path)).stem
    for rx in _PATTERNS:
        m = rx.search(stem)
        if m:
            return int(m[1])
    nums = re.findall(r"\d+", stem)
    return int(nums[-1]) if nums else None


def prefix_of(paths, default="Part"):
    """The word the files use for their number (Episode / Part / Clip ...), taken from the first file that has one."""
    for p in paths:
        m = re.search(r"\b" + _WORDS + r"\b\.?\s*[._\-#]*\s*\d", Path(str(p)).stem, re.I)
        if m:
            w = m[1].capitalize()
            return "Episode" if w == "Ep" else w
    return default


def plan_order(paths):
    """-> dict(order, numbers, missing, dupes, unnumbered). Sorted by episode number; unnumbered files go last."""
    items = [(episode_number(p), str(p)) for p in paths]
    items.sort(key=lambda t: (t[0] is None, t[0] or 0, natural_key(t[1])))
    nums = [n for n, _ in items if n is not None]
    cnt = Counter(nums)
    have = set(nums)
    return {"order": [p for _, p in items], "numbers": [n for n, _ in items],
            "missing": [n for n in range(min(nums), max(nums) + 1) if n not in have] if nums else [],
            "dupes": sorted(n for n, c in cnt.items() if c > 1),
            "unnumbered": [Path(p).name for n, p in items if n is None]}


def label_for(numbers, prefix="Part"):
    """[1..45] -> 'Part 001-045'  (width = at least 3 digits; any amount of files)."""
    nums = [n for n in numbers if n is not None]
    if not nums:
        return "Merged"
    w = max(3, len(str(max(nums))))
    a, b = min(nums), max(nums)
    return f"{prefix} {a:0{w}d}" + (f"-{b:0{w}d}" if b != a else "")


# ------------------------------------------------------------------ stream info
def _info(path):
    """One `ffmpeg -i` call -> duration + a signature of the first real video and audio stream."""
    r = subprocess.run([require_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=_NOWIN)
    t = r.stderr
    d = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", t)
    info = {"duration": int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0, "has_video": False,
            "has_audio": False, "w": 0, "h": 0, "fps": 0.0, "sr": 0}
    lines = [l for l in t.splitlines() if "Stream #" in l]
    v = next((l for l in lines if "Video:" in l and "attached pic" not in l), "")
    a = next((l for l in lines if "Audio:" in l), "")
    vc = ac = pix = ch = ""
    if v:
        info["has_video"] = True
        vc = (re.search(r"Video:\s*([\w\-]+)", v) or [0, ""])[1]
        pix = (re.search(r"\b(yuvj?\d{3}p\w*|nv12|nv21|gray\w*|rgb\w*|bgr\w*|gbrp\w*)", v) or [0, ""])[1]
        m = re.search(r",\s*(\d{2,5})x(\d{2,5})[\s,\[]", v)
        if m:
            info["w"], info["h"] = int(m[1]), int(m[2])
        m = re.search(r"([\d.]+)\s*fps", v)
        if m:
            info["fps"] = round(float(m[1]), 2)
    if a:
        info["has_audio"] = True
        ac = (re.search(r"Audio:\s*([\w\-]+)", a) or [0, ""])[1]
        m = re.search(r"(\d+)\s*Hz", a)
        info["sr"] = int(m[1]) if m else 0
        m = re.search(r"Hz,\s*([^,]+)", a)
        ch = m[1].strip() if m else ""
    info["sig"] = (vc, pix, info["w"], info["h"], info["fps"], ac, info["sr"], ch)
    return info


def _even(n):
    return max(2, int(n) // 2 * 2)


def _target(infos):
    """Common format to re-encode the odd files to: the most common size/fps/sample-rate among the files."""
    wh = Counter((_even(i["w"]), _even(i["h"]), i["fps"] or 25.0) for i in infos if i["has_video"]).most_common(1)[0][0]
    srs = [i["sr"] for i in infos if i["has_audio"] and i["sr"]]
    sr = Counter(srs).most_common(1)[0][0] if srs else 44100
    chs = Counter(i["sig"][7] for i in infos if i["has_audio"] and i["sig"][7] in ("mono", "stereo")).most_common(1)
    ch = chs[0][0] if chs else "stereo"      # keep the files' own mono/stereo so matching files are not re-encoded
    sig = ("h264", "yuv420p", wh[0], wh[1], wh[2], "aac", sr, ch)
    return {"w": wh[0], "h": wh[1], "fps": wh[2], "sr": sr, "ch": ch, "sig": sig}


def _normalize(src, dst, T, has_audio, duration, progress, cancel):
    vf = (f"scale={T['w']}:{T['h']}:force_original_aspect_ratio=decrease,"
          f"pad={T['w']}:{T['h']}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={T['fps']}")
    args = ["-y", "-i", src]
    if has_audio:
        args += ["-map", "0:v:0", "-map", "0:a:0"]
    else:   # no audio track: add silence, otherwise the joined file would lose sound from this episode on
        args += ["-f", "lavfi", "-i", f"anullsrc=r={T['sr']}:cl={T['ch']}", "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    args += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-ar", str(T["sr"]), "-ac", "1" if T["ch"] == "mono" else "2", "-movflags", "+faststart", dst]
    run_ffmpeg(args, duration=duration, progress=progress, cancel=cancel)


def _list_line(p):
    return "file '" + Path(p).resolve().as_posix().replace("'", "'\\''") + "'"


def _chapters(names, durations, title):
    out = [";FFMETADATA1", f"title={title}"]
    t = 0.0
    for n, d in zip(names, durations):
        a, b = int(round(t * 1000)), int(round((t + d) * 1000))
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={a}", f"END={max(b, a + 1)}", f"title={n}"]
        t += d
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ main
def merge_videos(paths, out_dir, name="", mode="Auto", chapters=True, progress=None, cancel=None):
    """Merge `paths` (any order) into out_dir/<name>.mp4 ordered by episode number.
    name: '' = automatic ('Part 001-045').  -> dict(out, files, reencoded, method, missing, dupes)."""
    def say(msg):
        if progress:
            progress(msg)

    def pct(p):
        if progress:
            progress(max(0, min(99, int(p))))

    plan = plan_order(paths)
    order = plan["order"]
    if len(order) < 2:
        raise ValueError("Add at least 2 videos to merge.")
    infos = []
    for k, p in enumerate(order):
        if cancel and cancel():
            raise Cancelled()
        infos.append(_info(p))
        pct(5 * (k + 1) / len(order))
    bad = [Path(p).name for p, i in zip(order, infos) if not i["has_video"] or i["duration"] <= 0]
    if bad:
        raise RuntimeError("These files have no readable video: " + ", ".join(bad[:10]))

    pre = prefix_of(order)
    names = [(f"{pre} {n:03d}" if n is not None else Path(p).stem) for n, p in zip(plan["numbers"], order)]
    title = name.strip() or label_for(plan["numbers"], pre)
    first_ext = Path(order[0]).suffix.lower()
    same = all(i["sig"] == infos[0]["sig"] for i in infos)
    tmp = work_dir("merge_" + time.strftime("%Y%m%d_%H%M%S"))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def build(reencode_all):
        T = _target(infos)
        need = list(range(len(order))) if reencode_all else ([] if same else
               [k for k, i in enumerate(infos) if i["sig"] != T["sig"]])
        if need and mode.startswith("Fast"):
            raise RuntimeError("The files have different formats (size / fps / codec / audio), so Fast mode cannot join "
                               "them without re-encoding. Choose Auto.")
        copy_ext = first_ext if (not need and first_ext in (".mp4", ".mkv", ".mov", ".webm", ".m4v")) else ".mp4"
        out = unique_path(out_dir / (title + copy_ext))
        files = list(order)
        if need:
            say(f"Re-encoding {len(need)} of {len(order)} file(s) to one common format "
                f"({T['w']}x{T['h']}, {T['fps']} fps)...")
            total = sum(infos[k]["duration"] for k in need) or 1.0
            done = 0.0
            for k in need:
                if cancel and cancel():
                    raise Cancelled()
                dst = tmp / f"{k:04d}.mp4"
                base = done
                _normalize(order[k], dst, T, infos[k]["has_audio"], infos[k]["duration"],
                           lambda p, base=base, d=infos[k]["duration"]: pct(5 + 80 * (base + d * p / 100) / total), cancel)
                done += infos[k]["duration"]
                files[k] = str(dst)
        say("Joining " + ("without re-encoding..." if not need else "the files..."))
        lst = tmp / "list.txt"
        lst.write_text("\n".join(_list_line(f) for f in files) + "\n", encoding="utf-8")
        args = ["-y", "-f", "concat", "-safe", "0", "-i", lst]
        if chapters:
            meta = tmp / "chapters.txt"
            meta.write_text(_chapters(names, [i["duration"] for i in infos], title), encoding="utf-8")
            args += ["-i", meta, "-map_metadata", "1", "-map_chapters", "1"]
        args += ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
        if out.suffix in (".mp4", ".mov", ".m4v"):
            args += ["-movflags", "+faststart"]
        total_dur = sum(i["duration"] for i in infos)
        start = 85 if need else 5
        try:
            run_ffmpeg(args + [out], duration=total_dur, cancel=cancel,
                       progress=lambda p: pct(start + (99 - start) * p / 100))
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        return out, len(need)

    try:
        try:
            out, n = build(mode.startswith("Safe"))
        except Cancelled:
            raise
        except RuntimeError as e:
            if not mode.startswith("Auto") or "no readable" in str(e):
                raise
            say("Fast join failed (" + str(e).splitlines()[-1][:80] + ") → re-encoding everything, this is slower...")
            out, n = build(True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if progress:
        progress(100)
    return {"out": str(out), "files": len(order), "reencoded": n, "missing": plan["missing"],
            "dupes": plan["dupes"], "unnumbered": plan["unnumbered"], "title": title}
