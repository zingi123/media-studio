"""Sync planner: the VIDEO adapts to the NARRATION (the opposite of dubbing, where the voice adapts to the video).

For every narration segment of measured length d the planner picks source footage inside its matched scene:
  1. enough unused footage            -> plain clip
  2. only slightly short              -> slow motion (never slower than max_slow)
  3. footage already consumed         -> start the scene again (flagged 'reused')
  4. scene too short                  -> slow motion + frozen last frame (<= max_freeze), then footage after the scene (flagged 'spill')
Every clip's output length equals its narration length, so audio and picture stay in step without drift.
Nothing is hidden: slowed / freeze / reused / spill are returned as issues for the Quality Check and the UI.
"""
from .model import Clip

EPS = 1e-6


def plan(project, max_slow=0.8, max_freeze=2.0):
    clips, issues = [], []
    cursor = {s.id: s.start for s in project.scenes}
    for g in project.segments:
        d = float(g.dur or 0.0)
        if d <= 0:
            issues.append({"kind": "no_audio", "level": "warning", "segment": g.id,
                           "detail": f"Segment {g.id} has no narration audio - it is left out of the plan."})
            continue
        sc = project.scene(g.scene_id)
        if sc is None:
            issues.append({"kind": "no_scene", "level": "warning", "segment": g.id,
                           "detail": f"Segment {g.id} has no scene - it is left out of the plan."})
            continue

        a = max(sc.start, cursor[sc.id])
        avail = sc.end - a
        speed, freeze, b = 1.0, 0.0, a + d
        if avail >= d - EPS:
            pass
        elif avail / d >= max_slow:
            speed, b = avail / d, sc.end
            issues.append({"kind": "slowed", "level": "info", "segment": g.id,
                           "detail": f"Segment {g.id}: scene footage slowed to x{speed:.2f} to fill {d:.1f}s."})
        elif sc.length >= d - EPS:
            a, b = sc.start, sc.start + d
            issues.append({"kind": "reused", "level": "warning", "segment": g.id,
                           "detail": f"Segment {g.id}: part of scene {sc.id} is shown again (unused footage ran out)."})
        else:
            if cursor[sc.id] > sc.start:
                issues.append({"kind": "reused", "level": "warning", "segment": g.id,
                               "detail": f"Segment {g.id}: scene {sc.id} is shown again from its start."})
            a, speed = sc.start, max(max_slow, sc.length / d)
            b = sc.end
            out = (b - a) / speed
            if out < d - EPS:
                freeze = min(max_freeze, d - out)
                out += freeze
            if out < d - EPS:                                              # still short -> footage after the scene
                b = min(project.duration or 1e9, sc.end + (d - out) * speed)
                out = (b - a) / speed + freeze
                issues.append({"kind": "spill", "level": "warning", "segment": g.id,
                               "detail": f"Segment {g.id}: scene {sc.id} is too short for {d:.1f}s; footage after it is used. "
                                         "Pick a longer scene or shorten the text."})
                if out < d - EPS:
                    freeze += d - out
            if speed < 0.98:
                issues.append({"kind": "slowed", "level": "info", "segment": g.id,
                               "detail": f"Segment {g.id}: footage slowed to x{speed:.2f}."})
            if freeze > EPS:
                issues.append({"kind": "freeze", "level": "info", "segment": g.id,
                               "detail": f"Segment {g.id}: last frame held for {freeze:.1f}s."})
        clips.append(Clip(segment_id=g.id, src_start=round(a, 4), src_end=round(b, 4), speed=round(speed, 4), freeze=round(freeze, 4)))
        for s2 in project.scenes:                                          # footage used up to b is consumed
            if b > s2.start and a < s2.end:
                cursor[s2.id] = max(cursor[s2.id], min(b, s2.end))
    return clips, issues


def timeline(project):
    """[(segment, start, end)] on the OUTPUT timeline (clips back to back)."""
    out, t = [], 0.0
    for c in project.clips:
        g = project.segment(c.segment_id)
        out.append((g, t, t + c.out_len))
        t += c.out_len
    return out


def starts(project):
    """{segment_id: output start}. Uses the plan when it exists, else narration lengths back to back."""
    if project.clips:
        return {g.id: s for g, s, _e in timeline(project)}
    t, out = 0.0, {}
    for g in project.segments:
        if g.dur and g.dur > 0:
            out[g.id] = t
            t += g.dur
    return out
