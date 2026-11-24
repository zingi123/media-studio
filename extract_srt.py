
import argparse
import difflib
import os
import sys


def srt_time(t):
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_srt(segs, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(segs, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (i, srt_time(start), srt_time(end), text))
    print("Saved %d lines -> %s" % (len(segs), path))


# ---------------------------------------------------------------- speech
def extract_speech(video, lang, model_size, device):
    from faster_whisper import WhisperModel

    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
    compute = "float16" if device == "cuda" else "int8"
    print("Loading Whisper '%s' on %s (first run downloads the model)..." % (model_size, device))
    model = WhisperModel(model_size, device=device, compute_type=compute)

    print("Listening to the characters...")
    segments, info = model.transcribe(
        video,
        language=None if lang == "auto" else lang,
        vad_filter=True,
        beam_size=5,
    )
    total = info.duration or 0
    out = []
    for s in segments:
        text = s.text.strip()
        if text:
            out.append((s.start, s.end, text))
            if total:
                print("  %5.1f%%  %s" % (min(100, s.end / total * 100), text[:60]))
    return out


# ------------------------------------------------------------------- ocr
def extract_ocr(video, lang, step, region, min_score):
    """Read only the subtitle area. Returns [(start, end, text)] = when the text is visible."""
    import cv2
    from paddleocr import PaddleOCR

    y0p, y1p = region
    print("Loading PaddleOCR (first run downloads the models)...")
    try:
        ocr = PaddleOCR(lang=lang, use_doc_orientation_classify=False,
                        use_doc_unwarping=False, use_textline_orientation=False)
    except TypeError:  # older PaddleOCR versions
        ocr = PaddleOCR(lang=lang)

    def read_text(img):
        items = []  # (y, text, score)
        try:  # PaddleOCR 3.x
            for r in ocr.predict(img):
                texts = list(r["rec_texts"]) if "rec_texts" in r else []
                scores = list(r["rec_scores"]) if "rec_scores" in r else [1.0] * len(texts)
                polys = r["rec_polys"] if "rec_polys" in r else (r["dt_polys"] if "dt_polys" in r else [])
                for k, (t, sc) in enumerate(zip(texts, scores)):
                    y = float(polys[k][:, 1].mean()) if k < len(polys) else k
                    items.append((y, t, sc))
        except AttributeError:  # PaddleOCR 2.x
            for page in ocr.ocr(img, cls=False) or []:
                for line in page or []:
                    items.append((line[0][0][1], line[1][0], line[1][1]))
        # keep only confident text, top line first
        items = [i for i in items if i[2] >= min_score and i[1].strip()]
        items.sort(key=lambda i: i[0])
        return " ".join(i[1].strip() for i in items)

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frames / fps
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    y0, y1 = int(H * y0p / 100), int(H * y1p / 100)

    samples = []  # (time, text)
    t = 0.0
    while t < duration:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        samples.append((t, read_text(frame[y0:y1, :])))
        if len(samples) % 20 == 0:
            print("  reading subtitles %5.1f%%" % (t / duration * 100 if duration else 0))
        t += step
    cap.release()

    # merge neighbouring frames showing the same text into one line
    out, cur = [], None  # cur = [start, end, text]
    for t, text in samples:
        if not text:
            if cur:
                out.append(tuple(cur)); cur = None
            continue
        if cur and difflib.SequenceMatcher(None, cur[2], text).ratio() > 0.8:
            cur[1] = t + step
            if len(text) > len(cur[2]):
                cur[2] = text  # keep the most complete reading
        else:
            if cur:
                out.append(tuple(cur))
            cur = [t, t + step, text]
    if cur:
        out.append(tuple(cur))
    return out


# ----------------------------------------------------------------- match
def align(sub, speech, tol=0.6):
    """Keep the subtitle TEXT, but move its start/end to when the character speaks."""
    out, matched = [], 0
    for a, b, text in sub:
        ov = [(s, e) for s, e, _ in speech if e > a - tol and s < b + tol]
        if ov:
            s = min(x[0] for x in ov)
            e = max(x[1] for x in ov)
            s = min(max(s, a - tol), b)      # never drift more than 'tol' from the visible text
            e = max(min(e, b + tol), a)
            if e - s >= 0.3:
                out.append((s, e, text)); matched += 1
                continue
        out.append((a, b, text))
    for i in range(len(out) - 1):            # no overlapping lines
        if out[i][1] > out[i + 1][0]:
            out[i] = (out[i][0], out[i + 1][0], out[i][2])
    print("Timing matched to the speaking voice: %d of %d lines" % (matched, len(out)))
    return [o for o in out if o[1] > o[0]]


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Video -> timed .srt")
    ap.add_argument("video")
    ap.add_argument("--mode", choices=["match", "speech", "ocr"], default="match")
    ap.add_argument("--lang", default="auto", help="speech language: auto/zh/en/ja/...")
    ap.add_argument("--ocr-lang", default="ch", help="subtitle language: ch/en/japan/...")
    ap.add_argument("--model", default="small", help="Whisper: tiny/base/small/medium/large-v3")
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    ap.add_argument("--step", type=float, default=0.5, help="seconds between scanned frames")
    ap.add_argument("--region", default="55,92",
                    help="subtitle area: top,bottom in percent of video height")
    ap.add_argument("--min-score", type=float, default=0.6,
                    help="ignore OCR text below this confidence (0-1)")
    ap.add_argument("--out", help="output .srt path (default: next to the video)")
    a = ap.parse_args()

    if not os.path.exists(a.video):
        sys.exit("File not found: " + a.video)
    out = a.out or os.path.splitext(a.video)[0] + ".srt"
    y0, y1 = [float(x) for x in a.region.split(",")]

    if a.mode == "speech":
        segs = extract_speech(a.video, a.lang, a.model, a.device)
    elif a.mode == "ocr":
        segs = extract_ocr(a.video, a.ocr_lang, a.step, (y0, y1), a.min_score)
    else:
        sub = extract_ocr(a.video, a.ocr_lang, a.step, (y0, y1), a.min_score)
        speech = extract_speech(a.video, a.lang, a.model, a.device)
        segs = align(sub, speech)

    if not segs:
        sys.exit("No text found.")
    write_srt(segs, out)


if __name__ == "__main__":
    main()