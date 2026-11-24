"""Hook fusion (no Qt): ONE special place = story signal + voice signal blended per line.
 story  (0-10): from the AI story map - special lines, twists / secrets, turning points (neighbour lines get a decaying share). Text meaning, not loudness.
 voice  (0-10): emotion label, pitch / loudness above the speaker's own normal (Performance plan), '?' '!' - how the line is PERFORMED.
 fused  = (1-w)*story + w*voice + 0.25*min(story, voice)   -> a line that is special in BOTH ways wins; w = voice weight (default 0.4).
Candidate windows (4-8 s, whole lines) START on a strong fused line, never contain a spoiler line, and do not overlap."""
import json

from . import hook_export as HE

KIND_W = {"special_lines": 10.0, "secrets_or_twists": 9.0, "turning_points": 8.0}
NEIGHBOUR = {1: 0.55, 2: 0.3}


def _ids(v):
    out = []
    for x in v or []:
        try:
            out.append(int(x["id"] if isinstance(x, dict) else x))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def avoid_ids(smap):
    return set(_ids((smap or {}).get("spoilers_to_avoid")))


def story_scores(cues, smap):
    """-> [0..10 per line] or None when there is no usable story map."""
    if not smap:
        return None
    n, sc = len(cues), [0.0] * len(cues)
    hit = False
    for key, w in KIND_W.items():
        for i in _ids(smap.get(key)):
            if 0 <= i < n:
                hit = True
                for d, f in ((0, 1.0), (-1, NEIGHBOUR[1]), (1, NEIGHBOUR[1]), (-2, NEIGHBOUR[2]), (2, NEIGHBOUR[2])):
                    if 0 <= i + d < n:
                        sc[i + d] = max(sc[i + d], w * f)
    return sc if hit else None


def voice_scores(cues):
    """-> [0..10 per line] or None when nothing was measured (no emotion labels / Performance plan yet)."""
    raw = [HE._line_score(c) for c in cues]
    pos = sorted(r for r in raw if r > 0)
    if not pos:
        return None
    ref = pos[int(0.95 * (len(pos) - 1))] or 1.0
    return [min(10.0, 10.0 * r / ref) for r in raw]


def fuse(story, voice, w_voice=0.4):
    n = len(story or voice or [])
    if story is None and voice is None:
        return [0.0] * n
    if story is None:
        return list(voice)
    if voice is None:
        return list(story)
    return [min(10.0, (1 - w_voice) * s + w_voice * v + 0.25 * min(s, v)) for s, v in zip(story, voice)]


def candidates(cues, fused, story, voice, avoid, n=12, min_s=4.0, max_s=8.0):
    """Best non-overlapping 4-8 s windows -> [{cid, start_id, end_id, start, end, dur, fused, story, voice, km, src}]."""
    N, raw = len(cues), []
    for a in range(N):
        if a in avoid or not ((cues[a].translation or "").strip() or (cues[a].text or "").strip()):
            continue
        b = a
        while b + 1 < N and (b + 1) not in avoid and cues[b + 1].end - cues[a].start <= max_s:
            b += 1
        if cues[b].end - cues[a].start < min_s:
            continue
        peak = max(fused[a:b + 1])
        if peak <= 0 or fused[a] < 0.5 * peak:          # the hook must START on something strong
            continue
        raw.append((0.6 * fused[a] + 0.4 * peak, a, b))
    out, used = [], []
    for sc, a, b in sorted(raw, reverse=True):
        s, e = cues[a].start, cues[b].end
        if any(s < u1 and e > u0 for u0, u1 in used):
            continue
        used.append((s, e))
        out.append({"cid": len(out), "start_id": a, "end_id": b, "start": s, "end": e, "dur": round(e - s, 1), "fused": round(sc, 1),
                    "story": round(story[a], 1) if story else None, "voice": round(voice[a], 1) if voice else None,
                    "km": " / ".join((cues[i].translation or "").strip() for i in range(a, b + 1)),
                    "src": " / ".join((cues[i].text or "").strip() for i in range(a, b + 1))})
        if len(out) >= n:
            break
    return out


def hook_from_candidate(c, **extra):
    """Candidate -> the hook dict the exporter uses (one window)."""
    h = {"segments": [{"start": c["start"], "end": c["end"], "start_id": c["start_id"], "end_id": c["end_id"]}], "start": c["start"],
         "end": c["end"], "dur": c["dur"], "hook_type": "", "score": c["fused"], "story": c["story"], "voice": c["voice"], "special": "",
         "caption_km": "", "why": "", "risk": ""}
    h.update(extra)
    return h


def top_hooks(cands, n=8):
    """No AI ranking: the best fused windows as they are."""
    return [hook_from_candidate(c, hook_type="fused", why="story + voice score (no AI ranking)") for c in cands[:n]]


def describe(h):
    bits = []
    if h.get("story") is not None:
        bits.append(f"story {h['story']:.1f}")
    if h.get("voice") is not None:
        bits.append(f"voice {h['voice']:.1f}")
    return " · ".join(bits)


def candidates_json(cands):
    return json.dumps([{"cid": c["cid"], "t": f"{c['start']:.1f}-{c['end']:.1f}", "dur": c["dur"], "fused": c["fused"], "story": c["story"],
                        "voice": c["voice"], "km": c["km"], "src": c["src"]} for c in cands], ensure_ascii=False)
