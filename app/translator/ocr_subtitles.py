"""Burned-in subtitle OCR (separate from subtitle_ocr.py). Video -> sample -> ROI -> change check -> OCR -> merge."""
import difflib
import re
import numpy as np
from ..shared.ffmpeg_util import Cancelled
from .subtitles import Cue

_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")  # តួអក្សរ CJK


_EASY = {"ch": ["ch_sim", "en"], "en": ["en"], "ja": ["ja", "en"], "ko": ["ko", "en"],
         "th": ["th", "en"], "vi": ["vi", "en"]}   # EasyOCR language lists


def _rapid():
    from rapidocr_onnxruntime import RapidOCR
    r = RapidOCR()
    return lambda img: [(b, t, float(s)) for b, t, s in (r(img)[0] or [])]  # [(box, text, score)]


def _easy(lang):
    import easyocr
    rd = easyocr.Reader(_EASY.get(lang, ["en"]))
    return lambda img: [(b, t, float(s)) for b, t, s in rd.readtext(img)]  # same format


_PADDLE = {"ch": "ch", "en": "en", "ja": "japan", "ko": "korean", "th": "th", "vi": "vi"}


def _paddle(lang):
    """Optional PaddleOCR engine (Settings > ocr_engine = paddle). Handles the 2.x and 3.x result formats. CPU works."""
    from paddleocr import PaddleOCR
    code = _PADDLE.get(lang, "en")
    try:
        eng = PaddleOCR(use_angle_cls=False, lang=code, show_log=False)              # 2.x
    except (TypeError, ValueError):
        eng = PaddleOCR(lang=code, use_textline_orientation=False)                   # 3.x

    def run(img):
        try:
            res = eng.ocr(img, cls=False)
        except TypeError:
            res = eng.ocr(img)
        out = []
        for page in res or []:
            if page is None:
                continue
            if isinstance(page, dict):                                               # 3.x: dict of parallel lists
                boxes, texts, scores = page.get("rec_polys") or page.get("dt_polys") or [], page.get("rec_texts") or [], page.get("rec_scores") or []
                for b, t, sc in zip(boxes, texts, scores):
                    out.append(([list(map(float, p)) for p in b], t, float(sc)))
            else:                                                                    # 2.x: [[box, (text, score)], ...]
                for line in page:
                    box, (t, sc) = line
                    out.append((box, t, float(sc)))
        return out
    return run


def load_engine(lang="ch"):
    """Pick the OCR engine for a language (lang: ch | en | ja | ko | th | vi).
    RapidOCR (fast, Chinese + English) first for ch/en; EasyOCR first for ja/ko/th/vi because RapidOCR's default
    model is not trained for those scripts. The other engine is the fallback."""
    order = (_easy, _rapid) if lang in ("ja", "ko", "th", "vi") else (_rapid, _easy)
    try:
        from ..shared.settings import settings
        pref = settings().get("ocr_engine") or "auto"
    except Exception:
        pref = "auto"
    pick = {"paddle": _paddle, "rapid": _rapid, "easy": _easy}.get(pref)
    if pick is not None:                                                              # user chose an engine: try it first
        order = (pick,) + tuple(m for m in order if m is not pick)
    for mk in order:
        try:
            return mk(lang) if mk in (_easy, _paddle) else mk()
        except ImportError:
            continue
    raise RuntimeError("No OCR engine installed. Run: pip install rapidocr-onnxruntime  "
                       "(Japanese/Korean/Thai/Vietnamese also need: pip install easyocr)")


def _join(parts):  # ភ្ជាប់បន្ទាត់ដោយមិនបំបែកប្រយោគ
    out = ""
    for p in parts:
        if out and not (_CJK.search(out[-1]) and _CJK.search(p[0])):  # CJK មិនដាក់ដកឃ្លា
            out += " "
        out += p
    return out


def read_roi(ocr, roi, min_score=0.6):  # អានតែអត្ថបទ subtitle ក្នុង ROI
    h, w = roi.shape[:2]
    items = []
    for box, text, sc in ocr(roi):
        text = (text or "").strip()
        cx = sum(p[0] for p in box) / len(box)  # កណ្តាលផ្ដេកនៃប្រអប់
        if sc >= min_score and text and 0.2 * w <= cx <= 0.8 * w:  # រំលង logo ជ្រុង និងពិន្ទុទាប
            items.append((sum(p[1] for p in box) / len(box), text, sc))
    items.sort(key=lambda i: i[0])  # តាមលំដាប់ពីលើចុះក្រោម
    return _join([i[1] for i in items]), (sum(i[2] for i in items) / len(items) if items else 0.0)


def auto_roi(video, ocr, samples=24, lo=0.35, pad=0.04, cancel=None):
    """Find WHERE the burned-in subtitles are. OCR `samples` evenly spaced full frames, keep text boxes that are
    centred horizontally and sit in the lower part of the picture, and return (top, bottom) fractions of the band
    where most of that text lies. Returns None when too little text is found (caller falls back to defaults)."""
    import cv2
    cap = cv2.VideoCapture(str(video))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    ys = []
    for k in range(samples):
        if cancel and cancel():
            cap.release()
            raise Cancelled()
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (0.05 + 0.9 * k / max(1, samples - 1))))
        ok, fr = cap.read()
        if not ok:
            continue
        h, w = fr.shape[:2]
        for box, text, sc in ocr(fr):
            cx = sum(p[0] for p in box) / len(box)
            cy = sum(p[1] for p in box) / len(box)
            if sc >= 0.6 and len((text or "").strip()) >= 2 and 0.15 * w <= cx <= 0.85 * w and cy >= lo * h:
                ys.append(cy / h)
    cap.release()
    if len(ys) < 3:
        return None
    hist, edges = np.histogram(ys, bins=20, range=(0.0, 1.0))
    i = int(hist.argmax())
    band = [j for j in range(max(0, i - 1), min(20, i + 2)) if hist[j] >= 0.25 * hist[i]]   # peak + strong neighbours
    top, bottom = edges[min(band)] - pad, edges[max(band) + 1] + pad
    return max(0.0, top), min(1.0, bottom)


def extract(video, top=0.65, bottom=1.0, step=0.25, sim=0.85, min_dur=0.3, diff_thr=2.0, recheck=0.6, lang="ch",
            ocr=None, progress=None, cancel=None):
    """Original on-screen subtitle -> cues. start = the frame where a subtitle APPEARS, end = the frame where it CLOSES
    (or where the next subtitle replaces it). Sampling every `step` s finds the change; a bisect over the frames
    in between then pins the exact frame, so timing is frame-accurate instead of +-step."""
    import cv2
    ocr = ocr or load_engine(lang)
    if top is None:  # auto: look for the subtitle band first, then scan only that band
        if progress:
            progress("OCR: looking for the subtitle area...")
        found = auto_roi(video, ocr, cancel=cancel)
        top, bottom = found if found else (0.6, 1.0)
        if progress:
            progress(f"OCR: subtitle area {int(top * 100)}%-{int(bottom * 100)}% of the picture"
                     + ("" if found else " (not detected, default used)"))
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError("Cannot open video.")
    cap2 = cv2.VideoCapture(str(video))  # second reader for seeking while refining boundaries
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    every = max(1, int(round(step * fps)))

    def roi_of(frame):
        h = frame.shape[0]
        return frame[int(h * top):int(h * bottom)]

    def same(a, b):
        if not a and not b:
            return True
        if not a or not b:
            return False
        return difflib.SequenceMatcher(None, a, b).ratio() >= sim

    def text_at(fn):
        cap2.set(cv2.CAP_PROP_POS_FRAMES, fn)
        ok, fr = cap2.read()
        return read_roi(ocr, roi_of(fr))[0] if ok else ""

    def refine(f0, f1, ref):  # first frame in (f0, f1] whose text differs from ref (the text seen at f0)
        lo, hi = f0, f1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if same(text_at(mid), ref):
                lo = mid
            else:
                hi = mid
        return hi / fps

    cues, cur, prev, last, last_ocr = [], None, None, ("", 0.0), -1e9
    state_text, state_frame = "", 0  # text seen at the previous sample
    n, t = 0, 0.0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if n % every == 0:
            if cancel and cancel():
                raise Cancelled()
            ok, frame = cap.retrieve()
            t = n / fps
            if ok:
                roi = roi_of(frame)
                g = cv2.cvtColor(cv2.resize(roi, (160, 40)), cv2.COLOR_BGR2GRAY)
                did_ocr = prev is None or cv2.absdiff(g, prev).mean() > diff_thr or t - last_ocr >= recheck
                if did_ocr:
                    last = read_roi(ocr, roi)  # OCR again when the picture changed or after `recheck` seconds
                    last_ocr = t
                prev = g
                text, sc = last
                if not did_ocr or same(state_text, text):
                    if cur:
                        cur["end"] = t + step  # still showing: extend tentatively
                        if did_ocr and sc > cur["sc"]:
                            cur["text"], cur["sc"] = text, sc  # keep the most confident reading
                else:
                    # state_frame = the last frame that was REALLY read, so the change is inside (state_frame, n]
                    b = refine(state_frame, n, state_text) if n > state_frame else t
                    if cur:
                        cur["end"] = b  # old subtitle CLOSES here
                        cues.append(cur)
                    cur = {"start": b, "end": t + step, "text": text, "sc": sc} if text else None  # new one starts here
                if did_ocr:
                    state_text, state_frame = text, n
            if progress:
                progress(min(99, int(n * 100 / total)))
        n += 1
    cap.release(); cap2.release()
    if cur:
        cur["end"] = min(cur["end"], n / fps)
        cues.append(cur)
    out = [Cue(c["start"], c["end"], c["text"]) for c in cues if c["end"] - c["start"] >= min_dur]
    if progress:
        progress(100)
    return out
