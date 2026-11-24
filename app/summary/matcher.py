"""Narration segment -> scene matching.

Evidence per (segment, scene):
  * time  : overlap between the beat's source-time hint and the scene (strongest; comes from transcript / OCR timestamps)
  * text  : word overlap between the beat text and the scene caption (only when a describer produced captions)
Story order is assumed by default (recaps mostly follow the film): a dynamic programme finds the best NON-DECREASING scene
sequence. Pass monotonic=False for flashback-heavy films. Locked segments (user choice) are never moved.
Scores are evidence strength, not a measure of truth: low ones are reported so a person can check them.
"""
import re

_STOP = {"the", "a", "an", "of", "to", "and", "in", "on", "is", "are", "was", "were", "at", "with", "for", "his", "her", "it"}
_CJK = re.compile(r"[\u4e00-\u9fff]")
NEG = -1e9


def tokens(text):
    t = (text or "").lower()
    words = {w for w in re.findall(r"[^\W\d_]+", t) if w not in _STOP and not _CJK.fullmatch(w)}
    cjk = _CJK.findall(t)
    words |= {a + b for a, b in zip(cjk, cjk[1:])}
    return words


def time_score(scene, hint):
    s, e = hint
    ov = min(scene.end, e) - max(scene.start, s)
    den = min(scene.length, max(0.0, e - s)) or 1.0
    return max(0.0, min(1.0, ov / den))


def text_score(beat_text, scene):
    a, b = tokens(beat_text), tokens(scene.caption + " " + scene.visual_summary + " " + " ".join(scene.keywords) + " " + " ".join(scene.entities))
    return len(a & b) / len(a | b) if a and b else 0.0


def pair_score(beat, scene):
    has_t = bool(beat and beat.t_hint)
    has_c = bool(scene.caption or scene.visual_summary or scene.keywords or scene.entities) and bool(beat and beat.text)
    t = time_score(scene, beat.t_hint) if has_t else 0.0
    c = text_score(beat.text, scene) if has_c else 0.0
    if has_t and has_c:
        return 0.65 * t + 0.35 * c
    return t if has_t else (c if has_c else 0.0)


def assign(project, monotonic=True, min_score=0.3):
    scenes, segs = project.scenes, project.segments
    if not scenes:
        raise ValueError("No scenes - run scene detection first.")
    ids = [s.id for s in scenes]
    pos = {sid: j for j, sid in enumerate(ids)}
    m, n = len(scenes), len(segs)
    S = [[pair_score(project.beat(g.beat_id), sc) for sc in scenes] for g in segs]
    allowed = []
    for g in segs:
        allowed.append({pos[g.scene_id]} if g.locked and g.scene_id in pos else set(range(m)))
    choice = [None] * n
    if monotonic:
        prev = [0.0] * m
        back = []
        for i in range(n):
            cur, bk, run, arg = [NEG] * m, [0] * m, NEG, 0
            for j in range(m):
                if prev[j] > run:                       # best previous score with scene index <= j
                    run, arg = prev[j], j
                if j in allowed[i] and run > NEG / 2:
                    cur[j], bk[j] = run + S[i][j], arg
            prev = cur
            back.append(bk)
        j = max(range(m), key=lambda k: prev[k]) if n else 0
        for i in range(n - 1, -1, -1):
            choice[i] = j
            j = back[i][j]
    else:
        for i in range(n):
            choice[i] = max(allowed[i], key=lambda k: (S[i][k], -k))
    issues = []
    for i, g in enumerate(segs):
        j = choice[i]
        g.scene_id, g.match_score = ids[j], round(S[i][j], 3)
        order = sorted((k for k in range(m) if k != j), key=lambda k: -S[i][k])
        g.alternatives = [[ids[k], round(S[i][k], 3)] for k in order[:3] if S[i][k] > 0]
        if not g.locked and g.match_score < min_score:
            why = "no timing or caption evidence" if S[i][j] == 0 else "weak evidence"
            issues.append({"kind": "low_confidence", "level": "warning", "segment": g.id,
                           "detail": f"Segment {g.id}: scene {g.scene_id} chosen with {why} (score {g.match_score:.2f}). Please check."})
    return issues
