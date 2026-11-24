"""
Project state, checkpoints, pause / resume / cancel and chunk planning for long videos.

 * ProjectState keeps per-step status + a fingerprint of the parameters. A step that finished with the SAME
   parameters is skipped on resume (`is_done`); changing a parameter invalidates that step and the ones after it.
 * Saved atomically (write temp + os.replace) after EVERY change, so a crash / restart loses at most the running step.
 * Chunks: plan_chunks() cuts a long video with offsets; merge_chunk_cues() shifts timestamps back to the full
   timeline and keeps speaker IDs per chunk ("c1:Speaker 1") - IDs of independently processed chunks are NOT assumed
   to be the same person. link_speakers() only SUGGESTS links from embeddings; the user confirms in the Speaker Manager.
"""
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

STEPS = ["import", "extract_audio", "ocr", "whisper", "diarization", "speakers", "script", "translation",
         "voice", "sync", "export"]
PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"


def fingerprint(video) -> str:
    """Cheap identity of a video file: size + mtime + hash of the first 1 MB (a moved file is still recognised)."""
    p = Path(video)
    st = p.stat()
    h = hashlib.sha1(f"{st.st_size}|{int(st.st_mtime)}".encode())
    with open(p, "rb") as f:
        h.update(f.read(1 << 20))
    return h.hexdigest()[:20]


def params_hash(params) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:12]


from ..shared.ffmpeg_util import Cancelled   # the same exception the rest of the app raises


class RunControl:
    """Pause / resume / cancel flag shared with a worker thread. Call `check()` inside loops."""

    def __init__(self):
        self._run = threading.Event(); self._run.set()
        self._cancel = threading.Event()

    def pause(self):
        self._run.clear()

    def resume(self):
        self._run.set()

    def cancel(self):
        self._cancel.set(); self._run.set()

    @property
    def paused(self):
        return not self._run.is_set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def check(self):
        self._run.wait()                 # blocks while paused
        if self._cancel.is_set():
            raise Cancelled()


@dataclass
class StepRecord:
    status: str = PENDING
    params: str = ""
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    artifacts: dict = field(default_factory=dict)      # name -> file path (cache of intermediate results)
    progress: dict = field(default_factory=dict)       # e.g. {"chunks_done": [0,1,2]} for resumable loops


class ProjectState:
    def __init__(self, path, video="", video_fp=""):
        self.path = Path(path)
        self.video, self.video_fp = str(video), video_fp
        self.steps = {s: StepRecord() for s in STEPS}
        self.chunks = []                                # [{"index":0,"start":0.0,"end":600.0,"file":""}]
        self.meta = {}
        self.updated = time.time()

    # ---------------------------------------------------------- persistence
    def to_dict(self):
        return {"version": 1, "video": self.video, "video_fp": self.video_fp, "chunks": self.chunks, "meta": self.meta,
                "steps": {k: asdict(v) for k, v in self.steps.items()}, "updated": self.updated}

    def save(self):
        self.updated = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path):
        p = Path(path)
        d = json.loads(p.read_text(encoding="utf-8"))
        st = cls(p, d.get("video", ""), d.get("video_fp", ""))
        st.chunks, st.meta = d.get("chunks", []), d.get("meta", {})
        known = set(StepRecord.__dataclass_fields__)
        for k, v in d.get("steps", {}).items():
            st.steps[k] = StepRecord(**{a: b for a, b in v.items() if a in known})
        for rec in st.steps.values():                  # a step still "running" = the app closed mid-step -> redo it
            if rec.status == RUNNING:
                rec.status = PENDING
        return st

    @classmethod
    def open_or_create(cls, path, video):
        fp = fingerprint(video)
        if Path(path).exists():
            try:
                st = cls.load(path)
                if st.video_fp == fp:
                    st.video = str(video)
                    return st
            except Exception:
                pass                                   # damaged / other video -> start clean (old file kept as .bak)
            try:
                os.replace(path, str(path) + ".bak")
            except OSError:
                pass
        st = cls(path, video, fp)
        st.save()
        return st

    # ---------------------------------------------------------- steps
    def is_done(self, step, params=None) -> bool:
        r = self.steps[step]
        if r.status != DONE:
            return False
        if params is not None and r.params != params_hash(params):
            return False                                # parameters changed -> must be redone
        return all(Path(p).exists() for p in r.artifacts.values())   # cached file deleted -> redo

    def start(self, step, params=None):
        r = self.steps[step]
        r.status, r.started, r.error = RUNNING, time.time(), ""
        r.params = params_hash(params) if params is not None else ""
        self.save()

    def finish(self, step, artifacts=None, progress=None):
        r = self.steps[step]
        r.status, r.finished = DONE, time.time()
        if artifacts:
            r.artifacts.update({k: str(v) for k, v in artifacts.items()})
        if progress is not None:
            r.progress = progress
        self.save()

    def fail(self, step, error):
        from ..shared.secrets import redact
        r = self.steps[step]
        r.status, r.error = FAILED, redact(error)[:500]
        self.save()

    def invalidate_from(self, step):
        """Reset `step` and every later step (their cached results are stale)."""
        reset = False
        for s in STEPS:
            reset = reset or s == step
            if reset:
                self.steps[s] = StepRecord()
        self.save()

    def run_step(self, step, fn, params=None, control: RunControl = None):
        """Run fn() unless it is already done with the same params. Returns (result, skipped)."""
        if self.is_done(step, params):
            return None, True
        self.start(step, params)
        try:
            res = fn()
        except Cancelled:
            self.steps[step].status = PENDING; self.save(); raise
        except Exception as e:  # noqa
            self.fail(step, e); raise
        self.finish(step)
        return res, False

    def next_step(self):
        for s in STEPS:
            if self.steps[s].status != DONE:
                return s
        return None

    def summary(self):
        return {s: r.status for s, r in self.steps.items()}


# ---------------------------------------------------------------- chunking
def plan_chunks(duration, chunk_s=600.0, overlap_s=0.0, min_tail_s=30.0):
    """-> [{"index","start","end"}]. A tail shorter than min_tail_s is absorbed by the previous chunk (no tiny chunk)."""
    if duration <= 0:
        return []
    if chunk_s <= 0 or duration <= chunk_s + min_tail_s:
        return [{"index": 0, "start": 0.0, "end": float(duration)}]
    out, a = [], 0.0
    while a < duration:
        b = min(duration, a + chunk_s)
        if duration - b < min_tail_s:
            b = duration
        out.append({"index": len(out), "start": round(a, 3), "end": round(b + (overlap_s if b < duration else 0), 3)})
        a = b
    for c in out:
        c["end"] = min(c["end"], float(duration))
    return out


def merge_chunk_cues(chunk_results, chunks, overlap_s=0.0):
    """chunk_results[i] = list of cues with chunk-LOCAL times. Returns one list on the FULL timeline.
    Speaker IDs become "c<i>:<id>" (not assumed identical across chunks). Rows that start inside the overlap of the
    previous chunk (already covered by it) are dropped."""
    out = []
    for ch, cues in zip(chunks, chunk_results):
        off = ch["start"]
        for c in cues:
            a = c.start + off
            if out and ch["index"] > 0 and a < off + overlap_s and any(abs(a - o.start) < 0.4 for o in out[-50:]):
                continue
            c = type(c)(**{**c.__dict__})
            c.start, c.end = c.start + off, c.end + off
            if c.speaker:
                c.speaker = f"c{ch['index']}:{c.speaker}"
            out.append(c)
    return sorted(out, key=lambda c: c.start)


def link_speakers(embeddings_by_chunk, threshold=0.75):
    """SUGGESTIONS for the same person across chunks: [(("c0:Speaker 1"), ("c1:Speaker 2"), cosine)].
    embeddings_by_chunk = {"c0:Speaker 1": [..], ...}. Only the best match per speaker is proposed; the user decides."""
    ids = list(embeddings_by_chunk)
    out = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a.split(":")[0] == b.split(":")[0]:
                continue                                  # same chunk -> already different people by diarization
            va, vb = np.asarray(embeddings_by_chunk[a], float), np.asarray(embeddings_by_chunk[b], float)
            c = float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-9))
            if c >= threshold:
                out.append((a, b, round(c, 3)))
    return sorted(out, key=lambda t: -t[2])
