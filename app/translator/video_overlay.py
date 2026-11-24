"""
Video overlays used by BOTH the preview and the final export, so what you preview is what you get.

Order of effects:  1) blur original subtitle area  ->  2) Khmer subtitles (ASS, libass+HarfBuzz)  ->  3) logo.

Notes / limits
- Blur applies to a rectangle YOU place (default: bottom band). The app does not auto-detect where the hard-coded
  original subtitle is; use the preview to line the rectangle up.
- Khmer shaping needs an FFmpeg build with libass + HarfBuzz (the common Windows builds have it). Preview shows the truth.
- Khmer has no spaces between words, so very long lines may wrap badly: keep translations short.
"""
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..shared.ffmpeg_util import probe, run_ffmpeg
from ..shared.paths import app_root

FONT_DIR = app_root() / "fonts"
BUNDLED = {"Battambang": FONT_DIR / "Battambang-Regular.ttf", "Noto Sans Khmer": FONT_DIR / "NotoSansKhmer.ttf"}
LOGO_POS = ["Top Left", "Top Center", "Top Right"]
WM_POS = ["Top Left", "Top Center", "Top Right", "Center", "Bottom Left", "Bottom Center", "Bottom Right"]
SUB_POS = {"Bottom": 2, "Middle": 5, "Top": 8}      # ASS alignment numbers


@dataclass
class Blur:
    enabled: bool = False
    percent: int = 60       # intensity 0-100
    top: int = 70           # region, % of frame
    height: int = 11
    left: int = 5
    width: int = 90


@dataclass
class Sub:
    enabled: bool = False
    font_file: str = ""     # "" -> bundled Battambang
    font_name: str = ""     # family name inside the font file (UI fills it)
    size: int = 54          # relative to a 1080-px-high frame
    color: str = "#FFFFFF"
    outline: bool = True
    position: str = "Bottom"
    margin: int = 220       # distance from edge, 1080-px scale


@dataclass
class Logo:
    enabled: bool = False
    path: str = ""
    size: int = 15          # % of video width
    opacity: int = 100      # %
    position: str = "Top Right"


@dataclass
class Track:
    """Track tab: 1) mirror (horizontal flip) the video  2) watermark with adjustable opacity."""
    mirror: bool = False
    wm_enabled: bool = False
    wm_path: str = ""
    wm_size: int = 20       # % of video width
    wm_opacity: int = 50    # %
    wm_position: str = "Bottom Right"


@dataclass
class Overlay:
    blur: Blur = field(default_factory=Blur)
    sub: Sub = field(default_factory=Sub)
    logo: Logo = field(default_factory=Logo)
    track: Track = field(default_factory=Track)
    scene: object = None        # studio.model.Scene - when set, the on-screen objects / frame replace blur+sub+logo+watermark (see studio/render.py)

    def copy(self):
        return Overlay(replace(self.blur), replace(self.sub), replace(self.logo), replace(self.track),
                       self.scene.copy() if self.scene is not None else None)

    def active(self):
        if self.scene is not None and self.scene.has_effects():
            return True
        return (self.blur.enabled and self.blur.percent > 0) or self.sub.enabled or self.track.mirror or \
               (self.logo.enabled and bool(self.logo.path) and Path(self.logo.path).exists()) or \
               (self.track.wm_enabled and bool(self.track.wm_path) and Path(self.track.wm_path).exists())


def family_of(font_file):
    """Real family name of a font file (what libass must be given), not the file name."""
    try:
        from fontTools.ttLib import TTFont
        n = TTFont(str(font_file), lazy=True)["name"]
        return str(n.getBestFamilyName())
    except Exception:
        return Path(font_file).stem.split("-")[0]


def resolve_font(sub):
    """-> (font_path, family). Falls back to bundled Battambang."""
    f = sub.font_file if sub.font_file and Path(sub.font_file).exists() else str(BUNDLED["Battambang"])
    if not Path(f).exists():
        return "", sub.font_name or "Battambang"
    return f, (sub.font_name if sub.font_name and f == sub.font_file else family_of(f))


# ------------------------------------------------------------------ ASS
def _t(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _bgr(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper().replace("&H", "&H")


def write_ass(path, cues, sub, family, w, h, offset=0.0):
    """Khmer Translation -> .ass, using the ORIGINAL cue start/end (shifted by `offset` for single-frame previews)."""
    ph = 1080
    pw = max(2, round(ph * w / h))
    style = (f"Style: K,{family},{int(sub.size)},{_bgr(sub.color)},&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,"
             f"{3 if sub.outline else 0},0,{SUB_POS.get(sub.position, 2)},60,60,{int(sub.margin)},1")
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {pw}", f"PlayResY: {ph}", "WrapStyle: 0",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,"
             "StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
             style, "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
    n = 0
    for c in cues:
        txt = (c.translation or "").strip()
        if not txt or c.end - offset <= 0:
            continue
        txt = txt.replace("\\", "").replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{_t(c.start - offset)},{_t(c.end - offset)},K,,0,0,0,,{txt}")
        n += 1
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return n


# ------------------------------------------------------------------ filter graph
def video_chain(fx, w, h, logo_idx=None, ass_name="subs.ass", scale_to=None, wm_idx=None):
    """filter_complex fragment: [0:v] ... -> [vout]. logo_idx = ffmpeg input index of the logo image (or None)."""
    parts, cur = [], "0:v"

    def nxt(s):
        nonlocal cur
        parts.append(s)
        cur = s.rsplit("[", 1)[1].rstrip("]")

    b = fx.blur
    if b.enabled and b.percent > 0:
        x = max(0, min(w - 2, int(w * b.left / 100)))
        y = max(0, min(h - 2, int(h * b.top / 100)))
        bw = max(2, min(w - x, int(w * b.width / 100)))
        bh = max(2, min(h - y, int(h * b.height / 100)))
        sigma = max(1.0, b.percent / 100 * 40 * h / 1080)
        nxt(f"[{cur}]split[bk][bz];[bz]crop={bw}:{bh}:{x}:{y},gblur=sigma={sigma:.1f}[bl];[bk][bl]overlay={x}:{y}[v1]")
    if fx.track.mirror:   # flip AFTER blur (blur area is placed on the original frame) and BEFORE subtitle/logo/watermark (so they are not mirrored)
        nxt(f"[{cur}]hflip[vm]")
    if fx.sub.enabled:
        nxt(f"[{cur}]ass={ass_name}:fontsdir=.[v2]")
    lg = fx.logo
    if logo_idx is not None:
        lw = max(8, int(w * lg.size / 100)) // 2 * 2
        m = max(4, int(w * 0.02))
        X = {"Top Left": str(m), "Top Center": "(main_w-overlay_w)/2", "Top Right": f"main_w-overlay_w-{m}"}.get(lg.position, str(m))
        parts.append(f"[{logo_idx}:v]format=rgba,colorchannelmixer=aa={max(0, min(100, lg.opacity)) / 100:.2f},scale={lw}:-1[lg]")
        nxt(f"[{cur}][lg]overlay={X}:{m}:shortest=1[v3]")
    tk = fx.track
    if wm_idx is not None:
        ww = max(8, int(w * tk.wm_size / 100)) // 2 * 2
        m = max(4, int(w * 0.02))
        xs = {"Left": str(m), "Center": "(main_w-overlay_w)/2", "Right": f"main_w-overlay_w-{m}"}
        ys = {"Top": str(m), "Center": "(main_h-overlay_h)/2", "Bottom": f"main_h-overlay_h-{m}"}
        pos = tk.wm_position
        if pos == "Center":
            xk = yk = "Center"
        else:
            yk, xk = pos.split(" ", 1)
        parts.append(f"[{wm_idx}:v]format=rgba,colorchannelmixer=aa={max(0, min(100, tk.wm_opacity)) / 100:.2f},scale={ww}:-1[wm]")
        nxt(f"[{cur}][wm]overlay={xs.get(xk, str(m))}:{ys.get(yk, str(m))}:shortest=1[v5]")
    if scale_to:
        nxt(f"[{cur}]scale={scale_to}:-2[v4]")
    parts.append(f"[{cur}]null[vout]")
    return ";".join(parts)


def stage_files(fx, cues, tmp, w, h, offset=0.0):
    """Write subs.ass + copy font into tmp. Returns nothing; chain refers to them relatively (cwd=tmp)."""
    if fx.sub.enabled:
        fpath, fam = resolve_font(fx.sub)
        if fpath:
            shutil.copy(fpath, Path(tmp) / Path(fpath).name)
        write_ass(Path(tmp) / "subs.ass", cues, fx.sub, fam, w, h, offset)


def logo_ok(fx):
    return fx.logo.enabled and fx.logo.path and Path(fx.logo.path).exists()


def wm_ok(fx):
    return fx.track.wm_enabled and fx.track.wm_path and Path(fx.track.wm_path).exists()


def render_preview(video, cues, fx, t, out_png, progress=None, cancel=None):
    """One frame at time t with ALL enabled effects, using exactly the export filter graph. Returns png path."""
    if fx.scene is not None:                       # on-screen objects: the studio renderer (same graph as the export)
        from ..studio.render import render_preview_frame
        return render_preview_frame(video, cues, fx.scene, t, out_png, cancel=cancel)
    info = probe(video)
    w, h = info["width"], info["height"]
    if not w:
        raise RuntimeError("Could not read video size.")
    t = max(0.0, min(t, max(0.0, info["duration"] - 0.1)))
    tmp = Path(tempfile.mkdtemp(prefix="ms_prev_"))
    try:
        stage_files(fx, cues, tmp, w, h, offset=t)
        args = ["-y", "-ss", f"{t:.3f}", "-i", str(Path(video).resolve())]
        li = None
        if logo_ok(fx):
            args += ["-loop", "1", "-i", str(Path(fx.logo.path).resolve())]
            li = 1
        wi = None
        if wm_ok(fx):
            args += ["-loop", "1", "-i", str(Path(fx.track.wm_path).resolve())]
            wi = 2 if li is not None else 1
        args += ["-filter_complex", video_chain(fx, w, h, li, scale_to=min(w, 1280), wm_idx=wi), "-map", "[vout]", "-frames:v", "1",
                 str(Path(out_png).resolve())]
        Path(out_png).parent.mkdir(parents=True, exist_ok=True)
        run_ffmpeg(args, cancel=cancel, cwd=str(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return str(out_png)


def export_srt_ass(path_base, cues, fx, w=1920, h=1080):
    """'Generate Khmer Subtitle' file output: .ass (styled) next to .srt (plain). Returns [paths]."""
    from .subtitles import save_subtitle_file
    fpath, fam = resolve_font(fx.sub)
    base = Path(path_base)
    base.parent.mkdir(parents=True, exist_ok=True)
    ass, srt = base.with_suffix(".ass"), base.with_suffix(".srt")
    write_ass(ass, cues, fx.sub, fam, w, h)
    save_subtitle_file(srt, cues, "translation")
    return [str(ass), str(srt)]
