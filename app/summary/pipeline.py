"""Thin orchestration over the engine modules (no Qt). The UI calls these; so do the tests."""
import re
from pathlib import Path

from ..shared.ffmpeg_util import probe
from . import matcher as M, quality as Q, scenes as SC, sync as SY
from .model import Beat, SummaryProject

_TS = r"(\d+(?::\d{1,2}){0,2}(?:\.\d+)?)"
_HINT = re.compile(r"^\s*\[?\s*" + _TS + r"\s*[-–]\s*" + _TS + r"\s*\]?\s+(.*\S)\s*$")


def _secs(s):
    parts = [float(x) for x in s.split(":")]
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def beats_from_text(text):
    """One beat per line; an optional leading '00:01:20-00:01:45' or '[80-105]' says where it happens in the movie."""
    beats = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _HINT.match(line)
        hint, body = (None, line)
        if m:
            a, b = _secs(m[1]), _secs(m[2])
            if b > a:
                hint, body = (a, b), m[3]
        beats.append(Beat(id=len(beats) + 1, text=body, t_hint=hint))
    return beats


def new_project(video):
    info = probe(video)
    if not info["has_video"]:
        raise ValueError("The file has no readable video stream.")
    return SummaryProject(source=str(Path(video).resolve()), duration=info["duration"], width=info["width"],
                          height=info["height"], fps=info["fps"] or 25.0)


def analyse(project, work, threshold=0.3, min_len=1.0, max_len=30.0, progress=None, cancel=None):
    """Scene detection + thumbnails. Replaces project.scenes (segments keep their ids; run plan_all again afterwards)."""
    project.scenes = SC.detect_scenes(project.source, threshold, min_len, max_len, progress=progress, cancel=cancel)
    SC.extract_thumbs(project.source, project.scenes, Path(work) / "thumbs", cancel=cancel)
    return project.scenes


def plan_all(project, monotonic=True, min_score=0.3, max_slow=0.8, max_freeze=2.0):
    """Match segments to scenes, plan the clips, run the quality check. Returns one list of issues (matcher + sync + quality)."""
    issues = M.assign(project, monotonic=monotonic, min_score=min_score)
    project.clips, plan_issues = SY.plan(project, max_slow=max_slow, max_freeze=max_freeze)
    seen = {(i["kind"], i["segment"]) for i in issues + plan_issues}
    extra = [i for i in Q.check(project, min_score) if (i["kind"], i["segment"]) not in seen]
    return issues + plan_issues + extra
