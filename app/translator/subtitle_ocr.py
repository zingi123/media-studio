"""OCR for burned-in (hard) subtitles. Different from ASR: it reads text drawn on the video frames."""
import difflib
import shutil
from pathlib import Path
from ..shared.ffmpeg_util import Cancelled, probe, run_ffmpeg
from ..shared.paths import work_dir
from .subtitles import Cue


def _read_text(ocr, path, min_score=0.6):
    """Reads one frame. Supports RapidOCR (default) and PaddleOCR 2.x / 3.x."""
    if getattr(ocr, "_is_rapid", False):
        res, _ = ocr(path)
        items = [(float(b[0][1]), t, float(sc)) for b, t, sc in (res or [])]
        items = [i for i in items if i[2] >= min_score and i[1].strip()]
        items.sort(key=lambda i: i[0])
        return " ".join(i[1].strip() for i in items)
    try:
        res = ocr.predict(path)
        texts = []
        for r in res:
            texts += list(r.get("rec_texts", []) if hasattr(r, "get") else r["rec_texts"])
        return " ".join(t.strip() for t in texts if t.strip())
    except AttributeError:
        res = ocr.ocr(path, cls=False)
        lines = res[0] if res and res[0] else []
        return " ".join(l[1][0] for l in lines if l and l[1][0].strip())


def _load_ocr(lang):
    """RapidOCR (onnxruntime, Chinese/English) first; PaddleOCR only as a fallback."""
    try:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR()
        ocr._is_rapid = True
        return ocr
    except ImportError:
        pass
    try:
        from paddleocr import PaddleOCR
        return PaddleOCR(lang=lang)
    except Exception as e:
        raise RuntimeError("No working OCR engine. Run: pip install rapidocr-onnxruntime --no-deps "
                           "(Paddle failed to load: %s)" % e) from e


def ocr_video(video, lang="ch", interval=0.5, bottom_fraction=0.35, progress=None, cancel=None):
    ocr = _load_ocr(lang)
    info = probe(video)
    tmp = Path(work_dir(Path(video).stem)) / "ocr_frames"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    vf = f"fps={1 / interval},crop=iw:ih*{bottom_fraction}:0:ih*{1 - bottom_fraction}"
    run_ffmpeg(["-y", "-i", video, "-vf", vf, str(tmp / "f_%06d.jpg")], duration=info["duration"],
               cancel=cancel)
    files = sorted(tmp.glob("f_*.jpg"))
    cues, cur = [], None
    for i, f in enumerate(files):
        if cancel and cancel():
            raise Cancelled()
        t = i * interval
        text = _read_text(ocr, str(f))
        if cur and text and difflib.SequenceMatcher(None, cur.text, text).ratio() > 0.8:
            cur.end = t + interval
        else:
            if cur:
                cues.append(cur)
            cur = Cue(t, t + interval, text) if text else None
        if progress:
            progress(int((i + 1) * 100 / len(files)))
    if cur:
        cues.append(cur)
    shutil.rmtree(tmp, ignore_errors=True)
    return cues
