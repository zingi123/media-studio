"""
Compare Whisper text with burned-in subtitle OCR (ADD-ON, off unless switched on).

REVIEW WORKFLOW (default): where speech and subtitle DISAGREE the cue gets `ocr_text`, `ocr_sim` and `text_conflict=True`;
the speech text is kept. The user decides per row (accept OCR / keep Whisper) in the OCR review dialog - neither source
silently replaces the other. `auto_apply=True` (Settings > `ocr_fix_auto_apply`) restores the old behaviour: only
similar-enough text (>= MIN_SIM) is replaced automatically; everything else is still left for review.

Rules (the reason it is safe):
  * Whisper stays the base: Cue.start / Cue.end / speaker / emotion are NEVER changed.
  * Only Cue.text can change, and only when OCR text for the same time span is SIMILAR to Whisper's (>= MIN_SIM).
    A subtitle that is a paraphrase / shortened / another language is much less similar -> Whisper text is kept.
  * Leading (emotion) and trailing tags are kept. A line with a tag in the MIDDLE is left unchanged (flagged),
    because the OCR text cannot say where those tags belong.
  * Sees no subtitles in the picture -> does nothing. OCR never raises into Smart Script: every failure becomes a note.
"""
import difflib
import re
from ..shared.ffmpeg_util import Cancelled

MIN_SIM = 0.70                       # similarity (0-1) between Whisper text and OCR text needed to accept the OCR text
MIN_OVERLAP = 0.4                    # seconds (or half the line, whichever is smaller) an OCR cue must overlap the line
_TAG = r"(?:\([^()]*\)|\[pause\s*[0-9.]+s\])"
_LEAD = re.compile(r"^(\s*" + _TAG + r")+")
_TRAIL = re.compile(r"(" + _TAG + r"\s*)+$")
_ANY_TAG = re.compile(_TAG)
_NORM = re.compile(r"[\W_]+", re.U)

# Whisper language code -> OCR engine language code used by ocr_subtitles.load_engine
OCR_LANG = {"zh": "ch", "en": "en", "ja": "ja", "ko": "ko", "th": "th", "vi": "vi"}


def ocr_lang_for(whisper_lang):
    """None when the OCR engines cannot read this language (e.g. Khmer, Russian, Arabic...)."""
    return OCR_LANG.get((whisper_lang or "").lower()[:2])


def _norm(s):
    return _NORM.sub("", _ANY_TAG.sub(" ", s or "")).lower()


def similarity(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _ocr_text_for(c, ocr_cues):
    """OCR text whose time overlaps cue c (in time order)."""
    need = min(MIN_OVERLAP, (c.end - c.start) / 2)
    hit = [o for o in ocr_cues if min(c.end, o.end) - max(c.start, o.start) >= need]
    hit.sort(key=lambda o: o.start)
    parts = []
    for o in hit:
        t = (o.text or "").strip()
        if t and (not parts or parts[-1] != t):
            parts.append(t)
    return " ".join(parts)


def apply_fix(cues, ocr_cues, min_sim=MIN_SIM):
    """Pure logic (no video): -> (changed, kept_low_similarity, kept_mid_tags, no_ocr_text)."""
    changed = low = mid = none = 0
    for c in cues:
        o = _ocr_text_for(c, ocr_cues)
        if not o:
            none += 1
            continue
        m = _LEAD.match(c.text or "")
        lead = m.group(0) if m else ""
        body = (c.text or "")[len(lead):]
        tm = _TRAIL.search(body)
        trail = tm.group(0).strip() if tm else ""
        if tm:
            body = body[:tm.start()]
        if _ANY_TAG.search(body):                      # a tag in the middle of the line -> cannot place it in OCR text
            mid += 1
            continue
        if similarity(body, o) < min_sim:
            low += 1
            continue
        if _norm(body) != _norm(o):                    # same words already -> keep Whisper's punctuation untouched
            c.text = " ".join(x for x in (lead.strip(), o, trail) if x)
            changed += 1
    return changed, low, mid, none


def collect_conflicts(cues, ocr_cues, min_sim=MIN_SIM, auto_apply=False):
    """Pure logic (no video). Marks disagreements for review; replaces text only when auto_apply and similarity >= min_sim.
    Start/end/speaker/emotion are never touched. -> dict(conflicts, auto_applied, same, no_ocr)"""
    r = {"conflicts": 0, "auto_applied": 0, "same": 0, "no_ocr": 0}
    for c in cues:
        c.ocr_text, c.ocr_sim, c.text_conflict = "", 0.0, False
        o = _ocr_text_for(c, ocr_cues)
        if not o:
            r["no_ocr"] += 1
            continue
        m = _LEAD.match(c.text or "")
        lead = m.group(0) if m else ""
        body = (c.text or "")[len(lead):]
        tm = _TRAIL.search(body)
        trail = tm.group(0).strip() if tm else ""
        if tm:
            body = body[:tm.start()]
        if _norm(body) == _norm(o):
            r["same"] += 1
            continue
        sim = similarity(body, o)
        suggestion = " ".join(x for x in (lead.strip(), o, trail) if x)
        if auto_apply and sim >= min_sim and not _ANY_TAG.search(body):
            c.text = suggestion
            r["auto_applied"] += 1
            continue
        c.ocr_text, c.ocr_sim, c.text_conflict = suggestion, round(sim, 3), True
        r["conflicts"] += 1
    return r


def resolve(cue, use_ocr):
    """User decision for one row. Times are never changed."""
    if use_ocr and cue.ocr_text:
        cue.text = cue.ocr_text
    cue.ocr_text, cue.ocr_sim, cue.text_conflict = "", 0.0, False


def accept_all_similar(cues, min_sim=0.8):
    """Convenience for the review dialog: accept OCR for every pending conflict whose similarity >= min_sim. -> count"""
    n = 0
    for c in cues:
        if c.text_conflict and c.ocr_sim >= min_sim and not _mid_tag(c.text):
            resolve(c, True)
            n += 1
    return n


def _mid_tag(text):
    m = _LEAD.match(text or "")
    body = (text or "")[len(m.group(0)) if m else 0:]
    tm = _TRAIL.search(body)
    if tm:
        body = body[:tm.start()]
    return bool(_ANY_TAG.search(body))


def fix_with_ocr(cues, video, whisper_lang, min_sim=MIN_SIM, progress=None, cancel=None, auto_apply=False):
    """-> note string. Marks disagreements for REVIEW (and replaces similar text only when auto_apply)."""
    lang = ocr_lang_for(whisper_lang)
    if lang is None:
        return f"OCR fix skipped: OCR engines do not read language '{whisper_lang}'."
    try:
        from . import ocr_subtitles as OS
        ocr = OS.load_engine(lang)
        if progress:
            progress("OCR fix: looking for burned-in subtitles...")
        band = OS.auto_roi(video, ocr, cancel=cancel)
        if band is None:
            return "OCR fix skipped: no burned-in subtitles found in the picture."
        top, bottom = band
        if progress:
            progress("OCR fix: reading the subtitles (this can take a few minutes)...")
        ocr_cues = OS.extract(video, top=top, bottom=bottom, lang=lang, ocr=ocr, progress=progress, cancel=cancel)
    except Cancelled:
        raise
    except Exception as e:  # noqa - never break Smart Script
        return f"OCR fix skipped ({str(e)[:100]})."
    if not ocr_cues:
        return "OCR fix skipped: subtitle area found but no text could be read."
    r = collect_conflicts(cues, ocr_cues, min_sim, auto_apply)
    return (f"OCR check: {r['same']} line(s) agree with the subtitles, {r['conflicts']} DIFFER (marked for your review), "
            f"{r['auto_applied']} auto-corrected, {r['no_ocr']} without subtitle.")
