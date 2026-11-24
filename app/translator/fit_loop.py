"""Timing-fit loop (no Qt): after the Khmer voice exists, lines whose voice is longer than their slot are re-written SHORTER by the AI,
their voice is regenerated, and the loop repeats (max `rounds`).  Timestamps are never touched; only cue.translation (and its audio) change.
Target length comes from THIS video's own TTS (real chars/second of the clip), not a fixed guess.
"""
import re

from ..shared.ffmpeg_util import Cancelled
from ..ai import khmer_prompt as KP
from .script_builder import plain_text

TOL = 1.12            # a clip up to 12 % longer than its slot is fine (voice_synchronizer speeds it up slightly, still natural)
MIN_SLOT = 0.3
_FOREIGN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u1100-\u11ff\uf900-\ufaff]")
_KHMER = re.compile(r"[\u1780-\u17ff]")


def _chars(t):
    return len(re.sub(r"\s+", "", t or ""))


def _slot(cues, order, k):
    c = cues[order[k]]
    nxt = cues[order[k + 1]].start if k + 1 < len(order) else None
    end = max(c.end, nxt - 0.05) if nxt is not None else c.end
    return max(MIN_SLOT, end - c.start)


def _probe(path):
    from ..shared.ffmpeg_util import probe
    return float(probe(path)["duration"])


def find_long(cues, measure=None, tol=TOL):
    """-> [(index, voice_seconds, slot_seconds)] for spoken lines whose voice does not fit its slot (by more than tol)."""
    from pathlib import Path
    measure = measure or _probe
    order = sorted((i for i, c in enumerate(cues) if c.audio and Path(c.audio).exists() and (c.translation or "").strip()),
                   key=lambda i: cues[i].start)
    out = []
    for k, i in enumerate(order):
        try:
            d = measure(cues[i].audio)
        except Exception:  # noqa - unreadable clip is reported by the QA step
            continue
        s = _slot(cues, order, k)
        if d > s * tol:
            out.append((i, d, s))
    return out


def _accept(old, new):
    new = (new or "").strip()
    return bool(new) and _chars(new) < _chars(old) and not _FOREIGN.search(new) and bool(_KHMER.search(new))


def run(cues, provider, regen, measure=None, rounds=3, glossary="", batch_size=30, tol=TOL, progress=None, cancel=None):
    """regen(idxs) must regenerate the voice of those rows.  -> report [{line, before, after, ok}] (1-based lines, last state per line)."""
    say = progress or (lambda *_: None)
    report = {}
    for rnd in range(1, rounds + 1):
        if cancel and cancel():
            raise Cancelled()
        long_ = find_long(cues, measure, tol)
        if not long_:
            say(f"   Timing fit: every voice fits its slot (round {rnd}).")
            break
        say(f"   Timing fit round {rnd}/{rounds}: {len(long_)} line(s) too long - asking AI to shorten ...")
        changed = []
        for b in range(0, len(long_), batch_size):
            if cancel and cancel():
                raise Cancelled()
            part = long_[b:b + batch_size]
            batch = []
            for i, d, s in part:
                c = cues[i]
                n = _chars(c.translation)
                mx = max(2, int(n * (s / d) * 0.97))          # real chars/second of this very clip x the slot
                batch.append({"id": i, "source": plain_text(c.text), "km": c.translation, "sec": round(s, 1), "max_chars": mx,
                              **({"speaker": c.speaker} if c.speaker else {})})
            try:
                txt = provider.generate(KP.build_shorten_prompt(batch, glossary), system=KP.SHORTEN_SYSTEM, json_mode=True, temperature=0.3)
                got = KP.parse_json_array(txt)
            except Exception as e:  # noqa - a failed AI call must never break the dub; the line keeps its text
                say(f"   (shorten skipped for {len(part)} line(s): {e})")
                continue
            for i, d, s in part:
                old, new = cues[i].translation, got.get(i, "")
                if _accept(old, new):
                    first = report.get(i + 1, {}).get("before", old)
                    cues[i].translation = new.strip()
                    cues[i].audio, cues[i].audio_sig, cues[i].voice_dur = "", "", 0.0
                    report[i + 1] = {"line": i + 1, "before": first, "after": cues[i].translation, "ok": False}
                    changed.append(i)
        if not changed:
            say("   Timing fit: no usable shorter text returned - stopping.")
            break
        regen(changed)
    left = {i + 1 for i, _, _ in find_long(cues, measure, tol)}
    for ln, r in report.items():
        r["ok"] = ln not in left
    if left:
        say(f"   Timing fit: {len(left)} line(s) still long (voice will be sped up / trimmed) - see the quality report.")
    return sorted(report.values(), key=lambda r: r["line"])
