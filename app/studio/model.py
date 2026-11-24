"""Data model for on-screen editing.

Geometry is stored NORMALISED to the video frame (0..1), so the same numbers drive the Qt canvas preview and the
FFmpeg export - what you see on screen is what is rendered:

    cx, cy   centre of the object          (fraction of frame width / height)
    w        width                         (fraction of frame width)
    h        height                        (fraction of frame height; images keep their own aspect, h is derived)
    rot      rotation in degrees (clockwise)
"""
import copy
import json
import uuid
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

KINDS = ("logo", "watermark", "text", "subtitle", "blur")
PRIMARY = {"blur": "blur", "subtitle": "sub", "logo": "logo", "watermark": "wm"}     # legacy Style-tab objects (one each)

FRAME_PRESETS = {"Original": None, "9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1), "4:3": (4, 3), "Custom": None}
FIT_MODES = ("fit", "fill", "stretch")      # fit = whole picture visible (bars), fill = cover the frame (crop), stretch = not recommended


def new_id(prefix="o"):
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


@dataclass
class Item:
    """One object placed on the video."""
    id: str = field(default_factory=new_id)
    kind: str = "logo"
    key: str = ""                  # "blur" / "sub" / "logo" / "wm" for the Style-tab objects, "" for extra copies
    name: str = ""
    enabled: bool = True
    # geometry (normalised)
    cx: float = 0.5
    cy: float = 0.5
    w: float = 0.2
    h: float = 0.1
    rot: float = 0.0
    opacity: float = 1.0           # 0..1 (blur: strength of the blended blur)
    # visibility window in seconds; end <= 0 means "until the end of the video"
    start: float = 0.0
    end: float = 0.0
    # image objects (logo / watermark)
    path: str = ""
    # text objects (text / subtitle)
    text: str = ""
    font_file: str = ""
    font_name: str = ""
    size: int = 54                 # font size on a 1080-px-high frame
    color: str = "#FFFFFF"
    outline: bool = True
    outline_color: str = "#000000"
    bold: bool = False
    scroll: bool = False           # text only: right-to-left running text (ticker)
    scroll_speed: float = 0.25     # frame widths per second
    # blur
    blur_percent: int = 60

    # ---------------- helpers ----------------
    def label(self):
        base = {"logo": "Logo", "watermark": "Watermark", "text": "Text", "subtitle": "Khmer subtitle", "blur": "Blur area"}.get(self.kind, self.kind)
        return self.name or base

    def window(self, duration):
        """(start, end) in seconds, with the 'until the end' convention resolved."""
        e = self.end if self.end > 0 else (duration if duration and duration > 0 else 1e9)
        return max(0.0, self.start), max(max(0.0, self.start), e)

    def visible_at(self, t, duration=0.0):
        a, b = self.window(duration)
        return self.enabled and a <= t <= b

    def rect(self):
        """(left, top, width, height) normalised."""
        return self.cx - self.w / 2, self.cy - self.h / 2, self.w, self.h

    def set_rect(self, left, top, w, h):
        self.w, self.h = max(0.005, w), max(0.005, h)
        self.cx, self.cy = left + self.w / 2, top + self.h / 2

    def clamp(self):
        self.cx, self.cy = _clamp(self.cx, -0.5, 1.5), _clamp(self.cy, -0.5, 1.5)
        self.w, self.h = _clamp(self.w, 0.005, 3.0), _clamp(self.h, 0.005, 3.0)
        self.opacity = _clamp(self.opacity, 0.0, 1.0)
        self.blur_percent = int(_clamp(self.blur_percent, 0, 100))
        self.rot = ((self.rot + 180) % 360) - 180
        return self

    def duplicate(self, dx=0.03, dy=0.03):
        n = copy.deepcopy(self)
        n.id, n.key = new_id(), ""           # a copy is never a "primary" Style-tab object
        n.name = (self.label() + " copy")
        n.cx, n.cy = _clamp(self.cx + dx, 0, 1), _clamp(self.cy + dy, 0, 1)
        return n

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        it = cls(**{k: v for k, v in (d or {}).items() if k in known})
        if it.kind not in KINDS:
            it.kind = "text"
        return it.clamp()


@dataclass
class FrameSettings:
    """Output frame (canvas) and how the source picture sits in it."""
    preset: str = "Original"       # Original | 9:16 | 16:9 | 1:1 | 4:3 | Custom
    custom_w: int = 1080
    custom_h: int = 1920
    fit: str = "fit"               # fit | fill | stretch   ("Auto Fit" = fit/fill chosen at import)
    zoom: float = 1.0              # 1.0 = the fit/fill size; > 1 zooms in (crop), < 1 zooms out
    off_x: float = 0.0             # picture offset, fraction of the frame size
    off_y: float = 0.0
    crop_l: float = 0.0            # crop of the SOURCE picture (fractions), applied before fitting
    crop_t: float = 0.0
    crop_r: float = 0.0
    crop_b: float = 0.0
    auto_fit: bool = True          # on import: choose the frame / fit automatically
    bg: str = "#000000"            # bar colour for 'fit'

    def aspect(self, src_w, src_h):
        """Frame aspect (w/h)."""
        if self.preset == "Custom":
            return max(1, self.custom_w) / max(1, self.custom_h)
        r = FRAME_PRESETS.get(self.preset)
        if r:
            return r[0] / r[1]
        return src_w / src_h if src_h else 16 / 9

    def out_size(self, src_w, src_h, max_long=None):
        """Even output pixel size.  Keeps the source resolution class: the longer side == the source's longer side."""
        if self.preset == "Original" and not self.has_crop():
            w, h = src_w, src_h
        else:
            a = self.aspect(self.cropped_size(src_w, src_h)[0], self.cropped_size(src_w, src_h)[1])
            if self.preset == "Custom":
                w, h = self.custom_w, self.custom_h
            else:
                long = max(src_w, src_h) if self.preset != "Original" else max(self.cropped_size(src_w, src_h))
                w, h = (long, long / a) if a >= 1 else (long * a, long)
        if max_long and max(w, h) > max_long:
            k = max_long / max(w, h)
            w, h = w * k, h * k
        return max(2, int(round(w / 2)) * 2), max(2, int(round(h / 2)) * 2)

    def has_crop(self):
        return any((self.crop_l, self.crop_t, self.crop_r, self.crop_b))

    def cropped_size(self, src_w, src_h):
        return (max(2, src_w * (1 - self.crop_l - self.crop_r)), max(2, src_h * (1 - self.crop_t - self.crop_b)))

    def picture_rect(self, src_w, src_h):
        """Where the (cropped, zoomed) source picture lies inside the frame, normalised to the frame: (l, t, w, h).
        Aspect ratio is always preserved unless fit == 'stretch'."""
        cw, ch = self.cropped_size(src_w, src_h)
        fa = self.aspect(src_w, src_h)
        pa = cw / ch
        if self.fit == "stretch":
            w, h = 1.0, 1.0
        elif (self.fit == "fill") == (pa >= fa):          # fill+wide picture OR fit+tall picture -> match frame HEIGHT
            h = 1.0; w = pa / fa
        else:
            w = 1.0; h = fa / pa
        z = _clamp(self.zoom, 0.1, 8.0)
        w, h = w * z, h * z
        return 0.5 + self.off_x - w / 2, 0.5 + self.off_y - h / 2, w, h

    def is_default(self):
        return (self.preset == "Original" and not self.has_crop() and abs(self.zoom - 1.0) < 1e-6
                and abs(self.off_x) < 1e-6 and abs(self.off_y) < 1e-6 and self.fit == "fit")

    def is_identity(self, src_w, src_h):
        return (self.preset == "Original" and not self.has_crop() and abs(self.zoom - 1.0) < 1e-6
                and abs(self.off_x) < 1e-6 and abs(self.off_y) < 1e-6)

    def auto_choose(self, src_w, src_h):
        """'Auto Fit' on import: keep the chosen frame (9:16, 16:9 ...) and place the new picture so that ALL of it is visible,
        centred, undistorted (no zoom / offset / crop left over from the previous video)."""
        if not src_w or not src_h:
            return self
        self.fit, self.zoom, self.off_x, self.off_y = "fit", 1.0, 0.0, 0.0
        self.crop_l = self.crop_t = self.crop_r = self.crop_b = 0.0
        return self

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


# STEP 2: six independent tracks (keys of the 5 old channels are kept, so old project files load unchanged; "ai_voice" is new)
MIXER_TRACKS = ("orig_vocals", "khmer_voice", "ai_voice", "orig_bg", "bgm", "sfx")
MIXER_LABELS = {"orig_vocals": "Original Audio", "khmer_voice": "Khmer Voice", "ai_voice": "AI Voice",
                "orig_bg": "Background Audio", "bgm": "Music", "sfx": "SFX"}
MIXER_DEFAULT = {"orig_vocals": 0, "orig_bg": 100, "khmer_voice": 100, "ai_voice": 100, "sfx": 100, "bgm": 100}   # % (0..200); dubbing mutes the original voice


@dataclass
class Channel:
    volume: int = 100       # percent 0..200
    mute: bool = False
    solo: bool = False
    enabled: bool = True    # STEP 2: False = the track is left out completely (not played, not exported, its solo flag is ignored)


@dataclass
class Mixer:
    channels: dict = field(default_factory=lambda: {k: Channel(MIXER_DEFAULT[k]) for k in MIXER_TRACKS})
    master: int = 100

    def reset(self):
        self.channels = {k: Channel(MIXER_DEFAULT[k]) for k in MIXER_TRACKS}
        self.master = 100

    def reset_channel(self, key):
        self.channels[key] = Channel(MIXER_DEFAULT[key])

    def gains(self, available=None):
        """-> {track: linear gain}.  Solo: when any channel is soloed only soloed channels play. Mute wins over solo.
        `available` limits the result to tracks that really have audio."""
        solo = any(c.solo and c.enabled for k, c in self.channels.items() if available is None or k in available)
        out = {}
        for k, c in self.channels.items():
            if available is not None and k not in available:
                continue
            on = c.enabled and (not c.mute) and (c.solo or not solo)
            out[k] = (c.volume / 100.0) * (self.master / 100.0) if on else 0.0
        return out

    def to_dict(self):
        return {"master": self.master, "channels": {k: asdict(c) for k, c in self.channels.items()}}

    @classmethod
    def from_dict(cls, d):
        m = cls()
        d = d or {}
        m.master = int(d.get("master", 100))
        for k, v in (d.get("channels") or {}).items():
            if k in m.channels and isinstance(v, dict):
                m.channels[k] = Channel(int(_clamp(v.get("volume", MIXER_DEFAULT[k]), 0, 200)), bool(v.get("mute")), bool(v.get("solo")),
                                        bool(v.get("enabled", True)))
        return m


@dataclass
class Scene:
    """Everything drawn on / done to the picture: frame settings + objects + mirror."""
    frame: FrameSettings = field(default_factory=FrameSettings)
    items: list = field(default_factory=list)
    mirror: bool = False

    # ---- items ----
    def get(self, item_id):
        return next((i for i in self.items if i.id == item_id), None)

    def primary(self, kind):
        key = PRIMARY[kind]
        return next((i for i in self.items if i.key == key), None)

    def add(self, item):
        self.items.append(item.clamp())
        return item

    def remove(self, item_id):
        n = len(self.items)
        self.items = [i for i in self.items if i.id != item_id]
        return len(self.items) != n

    def duplicate(self, item_id):
        it = self.get(item_id)
        if not it:
            return None
        n = it.duplicate()
        self.items.insert(self.items.index(it) + 1, n)
        return n

    def move_order(self, item_id, delta):
        """Bring forward (+1) / send backward (-1) in the drawing order."""
        it = self.get(item_id)
        if not it:
            return
        i = self.items.index(it)
        j = _clamp(i + delta, 0, len(self.items) - 1)
        self.items.insert(j, self.items.pop(i))

    def at_time(self, t, duration=0.0):
        return [i for i in self.items if i.visible_at(t, duration)]

    def copy(self):
        return Scene(copy.deepcopy(self.frame), [copy.deepcopy(i) for i in self.items], self.mirror)

    def has_effects(self):
        return bool(self.items) or self.mirror or not self.frame.is_default()

    def to_dict(self):
        return {"version": 1, "frame": self.frame.to_dict(), "mirror": self.mirror, "items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(FrameSettings.from_dict(d.get("frame")), [Item.from_dict(x) for x in d.get("items", [])], bool(d.get("mirror")))


class History:
    """Undo / redo of Scene snapshots (cheap: a scene is tiny)."""

    def __init__(self, limit=100):
        self.limit = limit
        self._undo, self._redo = [], []

    def push(self, scene):
        snap = json.dumps(scene.to_dict(), sort_keys=True)
        if self._undo and self._undo[-1] == snap:
            return False
        self._undo.append(snap)
        self._undo = self._undo[-self.limit:]
        self._redo.clear()
        return True

    def can_undo(self):
        return len(self._undo) > 1

    def can_redo(self):
        return bool(self._redo)

    def undo(self):
        if not self.can_undo():
            return None
        self._redo.append(self._undo.pop())
        return Scene.from_dict(json.loads(self._undo[-1]))

    def redo(self):
        if not self._redo:
            return None
        snap = self._redo.pop()
        self._undo.append(snap)
        return Scene.from_dict(json.loads(snap))


# ------------------------------------------------------------------ persistence of a whole studio project
def project_file(work_dir_path):
    return Path(work_dir_path) / "studio_project.json"


def save_project(path, scene, mixer, extra=None):
    d = {"version": 1, "scene": scene.to_dict(), "mixer": mixer.to_dict(), "extra": extra or {}}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def load_project(path):
    """-> (Scene, Mixer, extra). A missing / damaged file gives defaults (never raises)."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return Scene.from_dict(d.get("scene")), Mixer.from_dict(d.get("mixer")), d.get("extra") or {}
    except Exception:
        return Scene(), Mixer(), {}
