"""Timeline editing logic (no Qt): move / trim / split / duplicate / delete / merge for dialogue lines and audio clips,
snapping, and one undo/redo history for the whole studio state (cues + clips + scene).
"""
import copy
import json
import unicodedata
from dataclasses import asdict, fields

from ..translator.subtitles import Cue
from .audio import AudioClip
from .model import Scene

MIN_LEN = 0.10
COEng = "\u17d2"


# ------------------------------------------------------------------ snapping
def snap_points(cues, clips, playhead=None, duration=None, exclude_cues=(), exclude_clips=()):
    pts = [0.0]
    if duration:
        pts.append(float(duration))
    if playhead is not None:
        pts.append(float(playhead))
    for i, c in enumerate(cues):
        if i not in exclude_cues:
            pts += [c.start, c.end]
    for k in clips:
        if k.id not in exclude_clips:
            pts += [k.start, k.end]
    return sorted(set(round(p, 3) for p in pts))


def snap(t, points, tol):
    """Nearest point within tol seconds, else t."""
    best, bd = t, tol
    for p in points:
        d = abs(p - t)
        if d <= bd:
            best, bd = p, d
    return best


def snap_span(start, length, points, tol):
    """Snap a moving block by whichever edge is closer to a point. -> new start."""
    a = snap(start, points, tol)
    b = snap(start + length, points, tol)
    da, db = abs(a - start), abs(b - (start + length))
    if a != start and (b == start + length or da <= db):
        return a
    if b != start + length:
        return b - length
    return start


# ------------------------------------------------------------------ text splitting (Khmer-safe)
def _can_cut(s, i):
    if i <= 0 or i >= len(s):
        return False
    prev, nxt = s[i - 1], s[i]
    if prev == COEng:                                     # coeng + consonant = one subscript cluster
        return False
    if unicodedata.category(nxt).startswith("M") or nxt == COEng:    # never before a combining sign / coeng
        return False
    return True


def split_text(text, ratio):
    """Cut `text` near `ratio` (0..1) of its length: at a space if one is close, otherwise at a safe character boundary."""
    s = text or ""
    if len(s) < 2:
        return s, ""
    target = int(round(len(s) * max(0.0, min(1.0, ratio))))
    window = max(2, len(s) // 4)
    best = None
    for d in range(0, window + 1):
        for i in (target - d, target + d):
            if 0 < i < len(s) and (s[i - 1].isspace() or s[i].isspace()) and _can_cut(s, i):
                best = i
                break
        if best is not None:
            break
    if best is None:
        for d in range(0, len(s)):
            for i in (target - d, target + d):
                if _can_cut(s, i):
                    best = i
                    break
            if best is not None:
                break
    if best is None:
        return s, ""
    return s[:best].rstrip(), s[best:].lstrip()


# ------------------------------------------------------------------ dialogue lines (cues)
def _reset_voice(c):
    c.audio = ""
    if hasattr(c, "voice_dur"):
        c.voice_dur = 0.0
    if hasattr(c, "audio_sig"):
        c.audio_sig = ""


def sort_cues(cues, keep=()):
    """Sort in place by start time (stable). `keep` = cue objects whose new indices are wanted. -> list of indices."""
    cues.sort(key=lambda c: (c.start, c.end))
    return [i for i, c in enumerate(cues) if any(c is k for k in keep)]


def cue_move(cues, i, new_start, duration=None):
    c = cues[i]
    length = c.end - c.start
    s = max(0.0, new_start)
    if duration:
        s = min(s, max(0.0, duration - length))
    c.start, c.end = s, s + length
    return c


def cue_trim_start(cues, i, new_start):
    c = cues[i]
    c.start = max(0.0, min(new_start, c.end - MIN_LEN))
    return c


def cue_trim_end(cues, i, new_end, duration=None):
    c = cues[i]
    e = max(new_end, c.start + MIN_LEN)
    if duration:
        e = min(e, max(duration, c.start + MIN_LEN))
    c.end = e
    return c


def cue_split(cues, i, t):
    """Split line i at time t. The text is divided in proportion to the time; voice / speaker / emotion / speed are kept for both
    halves, the generated audio is cleared (each half needs its own voice).  -> (index_left, index_right) or None."""
    c = cues[i]
    if not (c.start + MIN_LEN <= t <= c.end - MIN_LEN):
        return None
    ratio = (t - c.start) / (c.end - c.start)
    n = copy.deepcopy(c)
    c.text, n.text = split_text(c.text, ratio)
    c.translation, n.translation = split_text(c.translation, ratio)
    n.start, c.end = t, t
    for x in (c, n):
        _reset_voice(x)
        if hasattr(x, "text_conflict"):
            x.text_conflict, x.ocr_text, x.ocr_sim = False, "", 0.0
    cues.insert(i + 1, n)
    return i, i + 1


def cue_duplicate(cues, idxs):
    """Copy each selected line right after its end (CapCut style). -> indices of the copies."""
    made = []
    for i in sorted(set(idxs)):
        c = cues[i]
        n = copy.deepcopy(c)
        length = c.end - c.start
        n.start, n.end = c.end, c.end + length
        made.append(n)
    for n in made:
        cues.append(n)
    return sort_cues(cues, made)


def cue_delete(cues, idxs):
    for i in sorted(set(idxs), reverse=True):
        if 0 <= i < len(cues):
            del cues[i]


def cue_merge(cues, idxs):
    """Join the selected lines into the first one (time = earliest start .. latest end). The text is joined in time order;
    the voice must be generated again. -> index of the merged line or None."""
    idxs = sorted(set(i for i in idxs if 0 <= i < len(cues)))
    if len(idxs) < 2:
        return None
    rows = [cues[i] for i in idxs]
    rows.sort(key=lambda c: (c.start, c.end))
    first = rows[0]
    first.text = " ".join(x.text.strip() for x in rows if x.text.strip())
    first.translation = " ".join(x.translation.strip() for x in rows if x.translation.strip())
    first.start, first.end = min(x.start for x in rows), max(x.end for x in rows)
    _reset_voice(first)
    if hasattr(first, "text_conflict"):
        first.text_conflict, first.ocr_text, first.ocr_sim = False, "", 0.0
    for x in rows[1:]:
        for j in range(len(cues) - 1, -1, -1):
            if cues[j] is x:
                del cues[j]
    return next(j for j, c in enumerate(cues) if c is first)


# ------------------------------------------------------------------ audio clips (SFX / music)
def clip_move(clip, new_start, duration=None):
    s = max(0.0, new_start)
    if duration:
        s = min(s, max(0.0, duration - clip.dur))
    clip.start = s
    return clip


def clip_trim_start(clip, new_start):
    """Drag the left edge: the clip shortens / lengthens at the front (the sound itself does not move)."""
    new_start = max(0.0, min(new_start, clip.end - MIN_LEN))
    delta = new_start - clip.start
    if clip.src_in + delta < 0:                           # cannot go before the beginning of the file
        delta = -clip.src_in
        new_start = clip.start + delta
    clip.src_in += delta
    clip.start = new_start
    clip.dur = max(MIN_LEN, clip.dur - delta)
    return clip


def clip_trim_end(clip, new_end):
    e = max(new_end, clip.start + MIN_LEN)
    if clip.src_len:
        e = min(e, clip.start + max(MIN_LEN, clip.src_len - clip.src_in))
    clip.dur = e - clip.start
    return clip


def clip_split(clips, k, t):
    c = clips[k]
    if not (c.start + MIN_LEN <= t <= c.end - MIN_LEN):
        return None
    n = copy.deepcopy(c)
    n.id = AudioClip().id
    cut = t - c.start
    n.start, n.src_in, n.dur = t, c.src_in + cut, c.dur - cut
    c.dur = cut
    clips.insert(k + 1, n)
    return k, k + 1


def clip_duplicate(clips, ks):
    made = []
    for k in sorted(set(ks)):
        c = clips[k]
        n = copy.deepcopy(c)
        n.id = AudioClip().id
        n.start = c.end
        made.append(n)
    clips.extend(made)
    return [clips.index(n) for n in made]


def clip_delete(clips, ks):
    for k in sorted(set(ks), reverse=True):
        if 0 <= k < len(clips):
            del clips[k]


def clip_merge(clips, ks):
    """Join clips that are neighbours of the SAME file and continue each other. -> index or None."""
    ks = sorted(set(ks), key=lambda k: clips[k].start)
    if len(ks) < 2:
        return None
    rows = [clips[k] for k in ks]
    for a, b in zip(rows, rows[1:]):
        if a.path != b.path or a.track != b.track or abs(a.end - b.start) > 0.05 or abs((a.src_in + a.dur) - b.src_in) > 0.05:
            return None
    first = rows[0]
    first.dur = rows[-1].end - first.start
    for x in rows[1:]:
        clips.remove(x)
    return clips.index(first)


# ------------------------------------------------------------------ one history for the whole studio
def _cue_dict(c):
    return asdict(c)


def _cue_from(d):
    known = {f.name for f in fields(Cue)}
    return Cue(**{k: v for k, v in d.items() if k in known})


class StudioHistory:
    """Undo / redo of (dialogue lines + audio clips + scene).  `capture` BEFORE changing something;
    `undo` / `redo` take the current state so redo works."""

    def __init__(self, limit=200):
        self.limit = limit
        self._undo, self._redo = [], []

    @staticmethod
    def snapshot(cues, clips, scene):
        return json.dumps({"cues": [_cue_dict(c) for c in cues], "clips": [k.to_dict() for k in clips],
                           "scene": scene.to_dict() if scene is not None else None}, ensure_ascii=False, sort_keys=True)

    def capture(self, cues, clips, scene):
        s = self.snapshot(cues, clips, scene)
        if self._undo and self._undo[-1] == s:
            return False
        self._undo.append(s)
        self._undo = self._undo[-self.limit:]
        self._redo.clear()
        return True

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def _apply(self, snap, cues, clips):
        d = json.loads(snap)
        cues[:] = [_cue_from(x) for x in d["cues"]]
        clips[:] = [AudioClip.from_dict(x) for x in d["clips"]]
        return Scene.from_dict(d["scene"]) if d.get("scene") is not None else None

    def undo(self, cues, clips, scene):
        """-> restored Scene (or None when the scene was not tracked / nothing to undo). cues / clips are changed IN PLACE."""
        if not self._undo:
            return False, None
        self._redo.append(self.snapshot(cues, clips, scene))
        return True, self._apply(self._undo.pop(), cues, clips)

    def redo(self, cues, clips, scene):
        if not self._redo:
            return False, None
        self._undo.append(self.snapshot(cues, clips, scene))
        return True, self._apply(self._redo.pop(), cues, clips)
