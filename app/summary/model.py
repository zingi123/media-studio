"""Data model of a movie-summary project. Plain dataclasses -> one JSON file (atomic save, versioned).

Language-neutral BEATS are the backbone: a beat is one story point (with an optional time hint in the source movie).
The Khmer script is generated FROM beats, so beat -> scene links survive editing / regenerating the Khmer text.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

SCHEMA = 1


@dataclass
class Scene:
    id: int
    start: float
    end: float
    caption: str = ""                       # what is visible (any language the describer used); "" = not described
    keywords: list = field(default_factory=list)
    thumb: str = ""
    visual_summary: str = ""              # AI description grounded in the sampled frame
    entities: list = field(default_factory=list)  # visible people/objects/places; descriptive, not identity claims
    visual_confidence: float = 0.0

    @property
    def length(self):
        return max(0.0, self.end - self.start)


@dataclass
class Beat:
    id: int
    text: str                               # language-neutral story point (English / Chinese / ...)
    t_hint: tuple = None                    # (start, end) seconds in the SOURCE movie where it happens, if known
    weight: float = 1.0                     # share of the narration time


@dataclass
class Segment:
    id: int
    beat_id: int
    km: str = ""
    emotion: str = "normal"
    audio: str = ""
    dur: float = 0.0                        # measured length of the narration audio (0 = not generated)
    audio_sig: str = ""                     # fingerprint of what the audio was generated from
    scene_id: int = None
    match_score: float = 0.0
    locked: bool = False                    # user fixed the scene: the matcher never moves it
    alternatives: list = field(default_factory=list)   # [[scene_id, score], ...] suggestions


@dataclass
class Clip:
    segment_id: int
    src_start: float
    src_end: float
    speed: float = 1.0                      # < 1 = slow motion
    freeze: float = 0.0                     # seconds of the last frame held after the clip

    @property
    def out_len(self):
        return (self.src_end - self.src_start) / (self.speed or 1.0) + self.freeze


@dataclass
class SummaryProject:
    source: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 25.0
    scenes: list = field(default_factory=list)
    beats: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    clips: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)

    def scene(self, sid):
        return next((s for s in self.scenes if s.id == sid), None)

    def beat(self, bid):
        return next((b for b in self.beats if b.id == bid), None)

    def segment(self, sid):
        return next((s for s in self.segments if s.id == sid), None)


def voice_sig(seg, voice_key, speed=1.0):
    """Fingerprint of everything the narration audio depends on. Timing / scene matching are NOT part of it."""
    parts = [(seg.km or "").strip(), seg.emotion or "normal", voice_key or "", f"{float(speed):.3f}"]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _build(cls, d):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (d or {}).items() if k in known})


def _load_dict(d):
    p = _build(SummaryProject, {k: v for k, v in d.items() if k not in ("scenes", "beats", "segments", "clips")})
    p.scenes = [_build(Scene, x) for x in d.get("scenes", [])]
    p.beats = []
    for x in d.get("beats", []):
        b = _build(Beat, x)
        b.t_hint = tuple(b.t_hint) if b.t_hint else None
        p.beats.append(b)
    p.segments = [_build(Segment, x) for x in d.get("segments", [])]
    p.clips = [_build(Clip, x) for x in d.get("clips", [])]
    return p


def save_project(p, path):
    """Atomic: a crash never leaves a half-written project."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(p)
    data["schema"] = SCHEMA
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_project(path):
    """ValueError for a damaged / unreadable / newer-schema file (the caller shows it; nothing is deleted)."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not a project object")
        if int(d.get("schema", 1)) > SCHEMA:
            raise ValueError(f"project was saved by a newer MediaStudio (schema {d.get('schema')})")
        return _load_dict(d)
    except ValueError:
        raise
    except Exception as e:  # noqa
        raise ValueError(f"Damaged project file: {e}") from e
