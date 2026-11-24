"""Khmer review pass (no Qt): a second AI pass over the finished translation. The model returns ONLY lines that need a fix.
A fix is accepted only if it is Khmer, has no CJK/Hangul/kana, and is not longer than the draft (+15 %), so timing never gets worse.
Timestamps are never touched. Skipped automatically when the translations are unchanged since the last review (`sig`)."""
import hashlib
import re

from ..shared.ffmpeg_util import Cancelled
from ..ai import khmer_prompt as KP
from .script_builder import plain_text
from .translator_engine import _characters_text

_FOREIGN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u1100-\u11ff\uf900-\ufaff]")
_KHMER = re.compile(r"[\u1780-\u17ff]")


def signature(cues):
    return hashlib.md5("\n".join((c.translation or "") for c in cues).encode("utf-8")).hexdigest()


def _n(t):
    return len(re.sub(r"\s+", "", t or ""))


def _ok(old, new):
    new = (new or "").strip()
    return bool(new) and new != (old or "").strip() and not _FOREIGN.search(new) and bool(_KHMER.search(new)) \
        and _n(new) <= _n(old) * 1.15 + 3


def review_cues(cues, provider, glossary="", batch_size=40, progress=None, cancel=None):
    """Fix cue.translation in place -> [{line, before, after}] (1-based)."""
    idxs = [i for i, c in enumerate(cues) if (c.text or "").strip() and (c.translation or "").strip()]
    chars, changes = _characters_text(cues), []
    for b in range(0, len(idxs), batch_size):
        if cancel and cancel():
            raise Cancelled()
        part = idxs[b:b + batch_size]
        batch = [{"id": i, "text": plain_text(cues[i].text), "km": cues[i].translation,
                  "sec": round(cues[i].end - cues[i].start, 1),
                  **({"speaker": cues[i].speaker} if cues[i].speaker else {}),
                  **({"emotion": cues[i].emotion} if cues[i].emotion and cues[i].emotion != "normal" else {})} for i in part]
        ctx = [plain_text(cues[i].text) for i in range(max(0, part[0] - 3), part[0])]
        try:
            txt = provider.generate(KP.build_review_prompt(batch, ctx, glossary, chars), system=KP.REVIEW_SYSTEM, json_mode=True, temperature=0.2)
            got = KP.parse_json_array(txt) if (txt or "").strip() not in ("", "[]") else {}
        except Exception as e:  # noqa - the review is an extra; the draft stays if the AI fails
            if progress:
                progress(f"   (review skipped for lines {part[0] + 1}-{part[-1] + 1}: {e})")
            continue
        for i in part:
            new = got.get(i)
            if new is not None and _ok(cues[i].translation, new):
                changes.append({"line": i + 1, "before": cues[i].translation, "after": new.strip()})
                cues[i].translation = new.strip()
                cues[i].audio, cues[i].audio_sig, cues[i].voice_dur = "", "", 0.0
        if progress:
            progress(min(100, int((b + len(part)) * 100 / len(idxs))))
    return changes
