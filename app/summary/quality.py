"""Quality check before export. Reports facts that can be measured; it never claims a Khmer text 'sounds right'."""
from pathlib import Path

from ..ai.khmer_prompt import fit_warning
from .script import tts_warnings

MIN_SCORE = 0.3


def check(project, min_score=MIN_SCORE):
    out = []

    def add(kind, level, seg, detail):
        out.append({"kind": kind, "level": level, "segment": seg, "detail": detail})

    for g in project.segments:
        if not (g.km or "").strip():
            add("empty_text", "warning", g.id, f"Segment {g.id} has no Khmer text.")
            continue
        for w in tts_warnings(g.km):
            add("tts_digits", "warning", g.id, f"Segment {g.id}: {w}")
        if not (g.audio and Path(g.audio).exists()):
            add("no_audio", "warning", g.id, f"Segment {g.id} has no narration audio file yet.")
        if g.dur > 0:
            w = fit_warning(g.km, g.dur)
            if w:
                add("text_too_long", "warning", g.id, f"Segment {g.id}: {w}")
        if g.scene_id is None:
            add("no_scene", "warning", g.id, f"Segment {g.id} is not matched to a scene.")
        elif not g.locked and g.match_score < min_score:
            add("low_confidence", "warning", g.id,
                f"Segment {g.id}: scene match score {g.match_score:.2f} is low - check or replace the scene.")
    if not project.clips:
        add("no_plan", "error", None, "There is no sync plan yet.")
    else:
        spoken = sum(g.dur for g in project.segments if g.dur > 0 and any(c.segment_id == g.id for c in project.clips))
        planned = sum(c.out_len for c in project.clips)
        if abs(spoken - planned) > 0.1:
            add("drift", "error", None, f"Picture is {planned:.1f}s but narration is {spoken:.1f}s - run the plan again.")
    return out
