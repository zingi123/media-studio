"""Automatic background removal for logos (used right after "Input Logo").

Strategy (fast -> heavy):
1. Image already has real transparency  -> keep as is.
2. Border is (almost) one solid colour   -> flood-fill that colour from the edges (no extra packages, instant).
   Only background connected to the border is removed, so same-coloured details inside the logo survive.
3. Complex background                    -> `rembg` (AI) if installed:  pip install rembg onnxruntime
   (first run downloads a ~170 MB model; needs internet).
4. Otherwise the logo is returned unchanged and a note says why.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SENTINEL = (255, 0, 254)


def _has_alpha(img, min_transparent=0.01):
    if img.mode not in ("RGBA", "LA"):
        return False
    a = img.getchannel("A").point(lambda v: 255 if v < 250 else 0)
    return (a.getbbox() is not None) and (sum(a.histogram()[255:]) / (img.width * img.height) >= min_transparent)


def _border_colour(rgb, tol=40):
    """Median colour of the 1-px border, or None if the border is not uniform."""
    w, h = rgb.size
    px = rgb.load()
    pts = [px[x, 0] for x in range(0, w, max(1, w // 64))] + [px[x, h - 1] for x in range(0, w, max(1, w // 64))] + \
          [px[0, y] for y in range(0, h, max(1, h // 64))] + [px[w - 1, y] for y in range(0, h, max(1, h // 64))]
    med = tuple(sorted(p[c] for p in pts)[len(pts) // 2] for c in range(3))
    ok = sum(1 for p in pts if sum(abs(p[c] - med[c]) for c in range(3)) <= tol)
    return med if ok / len(pts) >= 0.9 else None


def _floodfill_remove(img, tol=60):
    rgb = img.convert("RGB")
    bg = _border_colour(rgb)
    if bg is None:
        return None
    work = rgb.copy()
    w, h = work.size
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)] + [(x, 0) for x in range(0, w, 8)] + \
            [(x, h - 1) for x in range(0, w, 8)] + [(0, y) for y in range(0, h, 8)] + [(w - 1, y) for y in range(0, h, 8)]
    for s in seeds:
        px = work.getpixel(s)
        if px != SENTINEL and sum(abs(px[c] - bg[c]) for c in range(3)) <= tol:
            ImageDraw.floodfill(work, s, SENTINEL, thresh=tol)
    # background = pixels that turned into the sentinel colour
    import numpy as np
    a_w, a_o = np.asarray(work), np.asarray(rgb)
    sent = np.array(SENTINEL, dtype=np.uint8)
    bgmask = (a_w == sent).all(axis=2) & ~(a_o == sent).all(axis=2)
    mask = Image.fromarray(np.where(bgmask, 0, 255).astype(np.uint8), "L")
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))  # trim halo + soften edge
    if mask.getextrema()[0] > 0:   # nothing removed
        return None
    out = img.convert("RGBA")
    if img.mode == "RGBA":   # keep any existing partial transparency
        from PIL import ImageChops
        mask = ImageChops.multiply(mask, img.getchannel("A"))
    out.putalpha(mask)
    return out


def _rembg_remove(img):
    try:
        from rembg import remove
    except Exception:
        return None
    try:
        return remove(img.convert("RGBA"))
    except Exception:
        return None


def remove_background(src, out_png, progress=None, cancel=None):
    """-> (path_to_use, message). Always returns a usable path (the original if nothing could be done)."""
    img = Image.open(src)
    img.load()
    if _has_alpha(img):
        return str(src), "Logo already has a transparent background."
    res = _floodfill_remove(img)
    how = "solid-colour background removed"
    if res is None:
        res = _rembg_remove(img)
        how = "background removed (AI)"
    if res is None:
        return str(src), ("Could not remove the background automatically (complex background). "
                          "Install the optional AI remover:  pip install rembg onnxruntime   or use a PNG with transparency.")
    bbox = res.getchannel("A").getbbox()
    if bbox:
        res = res.crop(bbox)   # trim empty margins so the logo size % is accurate
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    res.save(out_png, "PNG")
    return str(out_png), "Logo: " + how + "."
