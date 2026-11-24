"""Pure geometry for the on-screen editor (no Qt): hit-testing, move / resize / rotate of a rotated rectangle.

All inputs are in FRAME PIXELS (the canvas scene is the frame, so scene units == frame units); item geometry
is normalised (model.Item) and converted here.  Rotation is clockwise degrees, y axis points down (screen coordinates).
"""
import math

HANDLES = ("tl", "t", "tr", "r", "br", "b", "bl", "l")
_SIGN = {"tl": (-1, -1), "t": (0, -1), "tr": (1, -1), "r": (1, 0), "br": (1, 1), "b": (0, 1), "bl": (-1, 1), "l": (-1, 0)}
ROT_OFFSET = 30.0        # distance of the rotate handle above the top edge (px, in frame units)
MIN_PX = 12.0


def rot_vec(x, y, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def to_local(item, x, y, FW, FH):
    """frame-pixel point -> item-local coordinates (origin = item centre, axes rotated with the item)."""
    cx, cy = item.cx * FW, item.cy * FH
    return rot_vec(x - cx, y - cy, -item.rot)


def to_global(item, lx, ly, FW, FH):
    gx, gy = rot_vec(lx, ly, item.rot)
    return item.cx * FW + gx, item.cy * FH + gy


def size_px(item, FW, FH):
    return item.w * FW, item.h * FH


def corners(item, FW, FH):
    w, h = size_px(item, FW, FH)
    return [to_global(item, sx * w / 2, sy * h / 2, FW, FH) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def handle_points(item, FW, FH):
    """{'tl':(x,y), ..., 'rot':(x,y)} in frame pixels."""
    w, h = size_px(item, FW, FH)
    out = {}
    for name, (sx, sy) in _SIGN.items():
        out[name] = to_global(item, sx * w / 2, sy * h / 2, FW, FH)
    out["rot"] = to_global(item, 0, -h / 2 - ROT_OFFSET, FW, FH)
    return out


def hit_handle(item, x, y, FW, FH, radius=14.0, allow_edges=True):
    """Name of the handle under (x, y) or None.  Corners and the rotate handle win over edges."""
    pts = handle_points(item, FW, FH)
    order = ["rot", "tl", "tr", "br", "bl"] + (["t", "r", "b", "l"] if allow_edges else [])
    best, bd = None, radius
    for name in order:
        px, py = pts[name]
        d = math.hypot(x - px, y - py)
        if d <= bd:
            best, bd = name, d
            if d < radius * 0.5:
                break
    return best


def contains(item, x, y, FW, FH, pad=0.0):
    lx, ly = to_local(item, x, y, FW, FH)
    w, h = size_px(item, FW, FH)
    return abs(lx) <= w / 2 + pad and abs(ly) <= h / 2 + pad


def move(item, dx, dy, FW, FH):
    item.cx += dx / FW
    item.cy += dy / FH
    return item


def resize(item, handle, x, y, FW, FH, keep_aspect=False, from_center=False, min_px=MIN_PX):
    """Drag `handle` to the frame-pixel point (x, y).  The opposite side stays fixed (or the centre, with from_center)."""
    sx, sy = _SIGN[handle]
    w0, h0 = size_px(item, FW, FH)
    lx, ly = to_local(item, x, y, FW, FH)
    if from_center:
        nw = max(min_px, 2 * sx * lx) if sx else w0
        nh = max(min_px, 2 * sy * ly) if sy else h0
    else:
        nw = max(min_px, sx * lx + w0 / 2) if sx else w0
        nh = max(min_px, sy * ly + h0 / 2) if sy else h0
    if keep_aspect and w0 > 0 and h0 > 0:
        if sx and sy:
            s = max(nw / w0, nh / h0)
        elif sx:
            s = nw / w0
        else:
            s = nh / h0
        s = max(s, min_px / min(w0, h0))
        nw, nh = w0 * s, h0 * s
    ox = 0.0 if from_center else sx * (nw - w0) / 2
    oy = 0.0 if from_center else sy * (nh - h0) / 2
    gx, gy = rot_vec(ox, oy, item.rot)
    item.cx += gx / FW
    item.cy += gy / FH
    item.w, item.h = nw / FW, nh / FH
    return item


def rotate_to(item, x, y, FW, FH, snap=False, step=15.0):
    cx, cy = item.cx * FW, item.cy * FH
    ang = math.degrees(math.atan2(x - cx, -(y - cy)))
    if snap:
        ang = round(ang / step) * step
    item.rot = ((ang + 180) % 360) - 180
    return item


def rect_from_drag(x0, y0, x1, y1, FW, FH):
    """Two frame-pixel corners -> normalised (left, top, w, h), clamped to the frame."""
    l, r = sorted((max(0.0, min(FW, x0)), max(0.0, min(FW, x1))))
    t, b = sorted((max(0.0, min(FH, y0)), max(0.0, min(FH, y1))))
    return l / FW, t / FH, (r - l) / FW, (b - t) / FH


def image_aspect_h(item_w, img_w, img_h, FW, FH):
    """Normalised height of an image placed with normalised width item_w (its own aspect kept)."""
    if not img_w or not img_h:
        return item_w
    return item_w * FW * (img_h / img_w) / FH


def preset_position(position, w, h, margin=0.02):
    """Legacy position names ('Top Left', 'Top Center', 'Top Right', 'Center', 'Bottom ...') -> (cx, cy) for a w x h object."""
    pos = position or "Center"
    if pos == "Center":
        return 0.5, 0.5
    v, _, hz = pos.partition(" ")
    cx = {"Left": margin + w / 2, "Center": 0.5, "Right": 1 - margin - w / 2}.get(hz, 0.5)
    cy = {"Top": margin + h / 2, "Bottom": 1 - margin - h / 2}.get(v, 0.5)
    return cx, cy


FIT_MARGIN_PX = 14.0     # empty gutter (device pixels) kept around the frame when the preview is fitted to the workspace


def fit_scale(view_w, view_h, frame_w, frame_h, margin=FIT_MARGIN_PX, min_scale=0.01):
    """Uniform scale that makes a frame_w x frame_h frame fit completely inside a view_w x view_h viewport
    (minus `margin` device pixels on every side). One scale for both axes -> the aspect ratio is never changed and
    nothing is cropped. Degenerate sizes (a viewport that is not laid out yet) give 1.0."""
    if view_w <= 0 or view_h <= 0 or frame_w <= 0 or frame_h <= 0:
        return 1.0
    aw = max(1.0, view_w - 2.0 * margin)
    ah = max(1.0, view_h - 2.0 * margin)
    return max(min_scale, min(aw / frame_w, ah / frame_h))
