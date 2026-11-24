from pathlib import Path
from PIL import Image
from ..shared.utils import unique_path

FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}


def convert_image(src, out_dir, fmt="jpg", max_side=0, quality=90, progress=None, cancel=None):
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = unique_path(out_dir / f"{src.stem}.{fmt}")
    with Image.open(src) as im:
        im.load()
        if max_side and max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        if FORMATS[fmt] == "JPEG" and im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        kw = {"quality": int(quality)} if FORMATS[fmt] in ("JPEG", "WEBP") else {}
        im.save(out, FORMATS[fmt], **kw)
    if progress:
        progress(100)
    return str(out)
