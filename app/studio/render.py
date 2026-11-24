"""Scene -> FFmpeg.  One code path for the preview frame AND the final export, so what the canvas shows is what is rendered.

Order of effects (same as the canvas draws them, bottom to top):
    source crop -> frame (fit / fill / zoom / offset, bars) -> mirror -> blur areas -> logos / watermarks -> Khmer text (ASS)

Text (Khmer subtitles and free text, incl. running text) is drawn by libass + HarfBuzz through an .ass file, because that is
what shapes Khmer correctly.  Images (logo / watermark) are overlaid with FFmpeg's overlay filter (rotation, opacity, time window).
"""
import math
import shutil
from pathlib import Path

from .model import Scene, Item

PREVIEW_LONG = 1280


# ------------------------------------------------------------------ small helpers
def _even(v, lo=2):
    return max(lo, int(round(v / 2.0)) * 2)


def _ass_color(hexcolor, alpha=0):
    h = (hexcolor or "#FFFFFF").lstrip("#")
    h = (h + "FFFFFF")[:6]
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{max(0, min(255, alpha)):02X}{b}{g}{r}".upper()


def _ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_text(s):
    return (s or "").replace("\\", "").replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N")


def image_items(scene):
    """Items that need an extra FFmpeg image input, in drawing order: [(item, path)]."""
    out = []
    for it in scene.items:
        if it.kind in ("logo", "watermark") and it.enabled and it.path and Path(it.path).exists():
            out.append((it, it.path))
    return out


def text_items(scene):
    return [i for i in scene.items if i.kind in ("subtitle", "text") and i.enabled]


def frame_size(scene, src_w, src_h, max_long=None):
    return scene.frame.out_size(src_w, src_h, max_long)


def _enable(it, offset, duration):
    """enable='between(t,a,b)' for items with a visibility window, '' for always-on items."""
    a, b = it.window(duration)
    if a <= 0 and (it.end <= 0 or (duration and b >= duration - 0.01)):
        return ""
    a, b = a - offset, b - offset
    return f":enable='between(t,{a:.3f},{b:.3f})'"


# ------------------------------------------------------------------ frame
def frame_steps(scene, src_w, src_h):
    """FFmpeg filter fragments (list of str) that turn the source into the output frame; returns (steps, FW, FH)."""
    f = scene.frame
    steps = []
    if f.has_crop():
        steps.append(f"crop=iw*{1 - f.crop_l - f.crop_r:.6f}:ih*{1 - f.crop_t - f.crop_b:.6f}:iw*{f.crop_l:.6f}:ih*{f.crop_t:.6f}")
    FW, FH = f.out_size(src_w, src_h)
    if f.is_identity(src_w, src_h):
        if src_w % 2 or src_h % 2:
            steps.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        steps.append("setsar=1")
        return steps, FW, FH
    if f.fit == "stretch":
        steps += [f"scale={FW}:{FH}", "setsar=1"]
        return steps, FW, FH
    l, t, w, h = f.picture_rect(src_w, src_h)
    pw, ph = _even(w * FW), _even(h * FH)
    px, py = int(round(l * FW)), int(round(t * FH))
    ix0, iy0 = max(0, px), max(0, py)
    ix1, iy1 = min(FW, px + pw), min(FH, py + ph)
    cw, ch = (ix1 - ix0) // 2 * 2, (iy1 - iy0) // 2 * 2
    if cw < 2 or ch < 2:                                  # picture completely outside the frame -> show it centred instead
        steps += [f"scale={FW}:{FH}:force_original_aspect_ratio=decrease", f"pad={FW}:{FH}:(ow-iw)/2:(oh-ih)/2:color={_pad_color(f.bg)}", "setsar=1"]
        return steps, FW, FH
    steps.append(f"scale={pw}:{ph}:flags=bicubic")
    if cw != pw or ch != ph:
        steps.append(f"crop={cw}:{ch}:{ix0 - px}:{iy0 - py}")
    if cw != FW or ch != FH:
        steps.append(f"pad={FW}:{FH}:{ix0 // 2 * 2}:{iy0 // 2 * 2}:color={_pad_color(f.bg)}")
    steps.append("setsar=1")
    return steps, FW, FH


def _pad_color(c):
    return "0x" + (c or "#000000").lstrip("#")[:6].upper()


# ------------------------------------------------------------------ ASS (Khmer subtitles + text objects)
def _measure(text, font_file, size):
    """Rough pixel width of one line (only used to place running text off-screen)."""
    try:
        from PIL import ImageFont
        if font_file and Path(font_file).exists():
            return float(ImageFont.truetype(str(font_file), int(size)).getlength(text))
    except Exception:
        pass
    return len(text) * size * 0.62


def write_ass(path, scene, cues, FW, FH, fonts, offset=0.0, duration=0.0):
    """Write every text object of the scene.  `fonts` = {item.id: (font_path, family)}.  Returns the number of events."""
    PH = 1080
    PW = max(2, round(PH * FW / FH))
    items = text_items(scene)
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {PW}", f"PlayResY: {PH}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,"
            "StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding"]
    ev = ["", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
    n = 0
    for k, it in enumerate(items):
        fpath, fam = fonts.get(it.id, ("", "Battambang"))
        style = f"S{k}"
        head.append(f"Style: {style},{fam},{int(it.size)},{_ass_color(it.color)},&H000000FF,{_ass_color(it.outline_color)},&H64000000,"
                    f"{-1 if it.bold else 0},0,0,0,100,100,0,0,1,{3 if it.outline else 0},0,5,0,0,0,1")
        a = int(round((1.0 - max(0.0, min(1.0, it.opacity))) * 255))
        al = f"\\1a&H{a:02X}&\\3a&H{a:02X}&\\4a&H{a:02X}&" if a else ""
        rot = f"\\frz{-it.rot:.2f}" if abs(it.rot) > 0.01 else ""
        x, y = it.cx * PW, it.cy * PH
        w0, w1 = it.window(duration)
        ml = int(max(0, (it.cx - it.w / 2) * PW))
        mr = int(max(0, PW - (it.cx + it.w / 2) * PW))
        if it.kind == "subtitle":
            for c in cues:
                txt = (c.translation or "").strip()
                if not txt:
                    continue
                s0, s1 = max(c.start, w0), min(c.end, w1)
                if s1 - offset <= 0 or s1 <= s0:
                    continue
                tags = f"{{\\an5\\pos({x:.1f},{y:.1f}){rot}{al}}}"
                ev.append(f"Dialogue: 0,{_ass_time(s0 - offset)},{_ass_time(s1 - offset)},{style},,{ml},{mr},0,,{tags}{_ass_text(txt)}")
                n += 1
        else:                                               # free text object (static or running text)
            txt = (it.text or "").strip()
            if not txt or w1 - offset <= 0:
                continue
            if not it.scroll:
                tags = f"{{\\an5\\pos({x:.1f},{y:.1f}){rot}{al}}}"
                ev.append(f"Dialogue: 0,{_ass_time(w0 - offset)},{_ass_time(w1 - offset)},{style},,{ml},{mr},0,,{tags}{_ass_text(txt)}")
                n += 1
            else:
                one = _ass_text(txt).replace("\\N", "   ")
                tw = _measure(txt.replace("\n", "   "), fpath, it.size) + 40
                speed = max(0.02, it.scroll_speed) * PW           # px / s
                period = (PW + tw) / speed
                t0, cnt = w0, 0
                while t0 < w1 and cnt < 600:
                    t1 = min(w1, t0 + period)
                    if t1 - offset > 0:
                        dur_ms = int(period * 1000)
                        tags = (f"{{\\an5\\q2{rot}{al}\\move({PW + tw / 2:.1f},{y:.1f},{-tw / 2:.1f},{y:.1f},0,{dur_ms})}}")
                        ev.append(f"Dialogue: 0,{_ass_time(max(0.0, t0 - offset))},{_ass_time(t1 - offset)},{style},,0,0,0,,{tags}{one}")
                        n += 1
                    t0 += period
                    cnt += 1
    Path(path).write_text("\n".join(head + ev) + "\n", encoding="utf-8-sig")
    return n


def resolve_fonts(scene, bundled=None, family_of=None):
    """-> {item.id: (font_path, family)} for the text objects (falls back to the bundled Battambang)."""
    from .. translator import video_overlay as VO
    bundled = bundled or VO.BUNDLED
    family_of = family_of or VO.family_of
    out = {}
    for it in text_items(scene):
        f = it.font_file if it.font_file and Path(it.font_file).exists() else str(bundled["Battambang"])
        if not Path(f).exists():
            out[it.id] = ("", it.font_name or "Battambang")
            continue
        fam = it.font_name if (it.font_name and f == it.font_file) else family_of(f)
        out[it.id] = (f, fam)
    return out


def stage_files(scene, cues, tmp, FW, FH, offset=0.0, duration=0.0, fonts=None):
    """Write subs.ass + copy the fonts into tmp (the filter graph refers to them relatively, cwd=tmp). Returns True if ASS is used."""
    if not text_items(scene):
        return False
    fonts = fonts or resolve_fonts(scene)
    for fpath, _ in fonts.values():
        if fpath and Path(fpath).exists():
            try:
                shutil.copy(fpath, Path(tmp) / Path(fpath).name)
            except OSError:
                pass
    return write_ass(Path(tmp) / "subs.ass", scene, cues, FW, FH, fonts, offset, duration) > 0


# ------------------------------------------------------------------ filter graph
def build_graph(scene, src_w, src_h, image_index=None, use_ass=False, offset=0.0, duration=0.0, scale_long=None):
    """-> (filter_complex string ending in [vout], FW, FH).
    image_index = {item.id: ffmpeg input index} for the images returned by image_items() (already added as inputs)."""
    image_index = image_index or {}
    steps, FW, FH = frame_steps(scene, src_w, src_h)
    parts = []
    cur = "0:v"
    n = [0]

    def nxt(expr):
        nonlocal cur
        n[0] += 1
        lab = f"v{n[0]}"
        parts.append(f"[{cur}]{expr}[{lab}]")
        cur = lab

    nxt(",".join(steps) if steps else "null")
    if scene.mirror:
        nxt("hflip")

    # blur areas (axis aligned, drawn on the finished frame)
    for it in scene.items:
        if it.kind != "blur" or not it.enabled or it.blur_percent <= 0:
            continue
        l, t, w, h = it.rect()
        x = max(0, min(FW - 2, int(l * FW)))
        y = max(0, min(FH - 2, int(t * FH)))
        bw = max(2, min(FW - x, int(w * FW)))
        bh = max(2, min(FH - y, int(h * FH)))
        bw, bh = bw // 2 * 2 or 2, bh // 2 * 2 or 2
        sigma = max(1.0, it.blur_percent / 100 * 40 * FH / 1080)
        n[0] += 1
        k = n[0]
        op = ""
        if it.opacity < 0.999:
            op = f",format=rgba,colorchannelmixer=aa={max(0.0, min(1.0, it.opacity)):.2f}"
        parts.append(f"[{cur}]split[bk{k}][bz{k}];[bz{k}]crop={bw}:{bh}:{x}:{y},gblur=sigma={sigma:.1f}{op}[bl{k}];"
                     f"[bk{k}][bl{k}]overlay={x}:{y}{_enable(it, offset, duration)}[v{k}]")
        cur = f"v{k}"

    # images: logo / watermark
    for it in scene.items:
        idx = image_index.get(it.id)
        if idx is None or it.kind not in ("logo", "watermark"):
            continue
        n[0] += 1
        k = n[0]
        iw = _even(it.w * FW, 8)
        chain = f"[{idx}:v]format=rgba,colorchannelmixer=aa={max(0.0, min(1.0, it.opacity)):.2f},scale={iw}:-1"
        if abs(it.rot) > 0.01:
            rad = math.radians(it.rot)
            chain += f",rotate=a={rad:.5f}:c=none:ow='rotw({rad:.5f})':oh='roth({rad:.5f})'"
        parts.append(chain + f"[im{k}]")
        parts.append(f"[{cur}][im{k}]overlay=x='{it.cx * FW:.1f}-overlay_w/2':y='{it.cy * FH:.1f}-overlay_h/2':shortest=1"
                     f"{_enable(it, offset, duration)}[v{k}]")
        cur = f"v{k}"

    if use_ass:
        nxt("ass=subs.ass:fontsdir=.")
    if scale_long and max(FW, FH) > scale_long:
        k = scale_long / max(FW, FH)
        nxt(f"scale={_even(FW * k)}:{_even(FH * k)}")
        FW, FH = _even(FW * k), _even(FH * k)
    parts.append(f"[{cur}]null[vout]")
    return ";".join(parts), FW, FH


# ------------------------------------------------------------------ used by the exporter and the preview
def prepare(scene, cues, info, tmp, first_index, offset=0.0, scale_long=None):
    """Stage the files and build the graph.  -> dict(args=[extra ffmpeg input args], graph=str, w, h, next_index).
    first_index = index the first extra image input will get in the ffmpeg command line."""
    src_w, src_h = info["width"], info["height"]
    duration = float(info.get("duration") or 0.0)
    FW, FH = frame_size(scene, src_w, src_h)
    args, index, idx = [], {}, first_index
    for it, path in image_items(scene):
        args += ["-loop", "1", "-i", str(Path(path).resolve())]
        index[it.id] = idx
        idx += 1
    use_ass = stage_files(scene, cues, tmp, FW, FH, offset=offset, duration=duration)
    graph, w, h = build_graph(scene, src_w, src_h, index, use_ass, offset, duration, scale_long)
    return {"args": args, "graph": graph, "w": w, "h": h, "next_index": idx}


def render_preview_frame(video, cues, scene, t, out_png, cancel=None):
    """One frame with ALL scene effects, through exactly the export graph. Returns the png path."""
    import tempfile
    from ..shared.ffmpeg_util import probe, run_ffmpeg
    info = probe(video)
    if not info["width"]:
        raise RuntimeError("Could not read video size.")
    t = max(0.0, min(float(t), max(0.0, info["duration"] - 0.1)))
    tmp = Path(tempfile.mkdtemp(prefix="ms_scene_"))
    try:
        pre = prepare(scene, cues, info, tmp, 1, offset=t, scale_long=PREVIEW_LONG)
        args = ["-y", "-ss", f"{t:.3f}", "-i", str(Path(video).resolve())] + pre["args"]
        args += ["-filter_complex", pre["graph"], "-map", "[vout]", "-frames:v", "1", str(Path(out_png).resolve())]
        Path(out_png).parent.mkdir(parents=True, exist_ok=True)
        run_ffmpeg(args, cancel=cancel, cwd=str(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return str(out_png)
