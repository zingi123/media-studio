"""
Speaker diarization with pyannote.audio 4.x  (pyannote/speaker-diarization-community-1)  + speaker<->ASR matching.

Pyannote 4 differences from 3.x that this module handles:
  * Pipeline.from_pretrained(..., token=HF_TOKEN)           (3.x used use_auth_token=)
  * the pipeline returns a DiarizeOutput object:
        .speaker_diarization            full result, OVERLAPPING speech kept
        .exclusive_speaker_diarization  one speaker at a time (meant for matching with ASR word timestamps)
        .speaker_embeddings             one embedding per speaker (optional)
    (3.x returned a bare Annotation - handled too, so an old install gives a clear result instead of a crash)
  * file-path input needs torchcodec; we pass an in-memory waveform {"waveform","sample_rate"} instead.

Nothing here ever falls back silently: every failure raises DiarizationError with a message the user can act on.
"""
import hashlib
import json
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from ..shared.secrets import redact

DEFAULT_MODEL = "pyannote/speaker-diarization-community-1"
HF_BASE = "https://huggingface.co"


class DiarizationError(RuntimeError):
    """Actionable error (token / model access / install / runtime)."""


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


@dataclass
class DiarizationResult:
    turns: list                                   # all turns, overlaps kept
    exclusive: list                               # non-overlapping turns (for ASR matching)
    embeddings: dict = field(default_factory=dict)  # {speaker: list[float]} when the model provides them
    model: str = DEFAULT_MODEL
    pyannote_version: str = ""
    device: str = "cpu"

    def speakers(self):
        return sorted({t.speaker for t in self.turns})

    def overlaps(self):
        """[(start, end, [speakers])] where 2+ speakers talk at once."""
        ev = sorted({t.start for t in self.turns} | {t.end for t in self.turns})
        out = []
        for a, b in zip(ev, ev[1:]):
            act = sorted({t.speaker for t in self.turns if t.start < b and t.end > a})
            if len(act) > 1 and b - a > 1e-3:
                if out and out[-1][1] == a and out[-1][2] == act:
                    out[-1] = (out[-1][0], b, act)
                else:
                    out.append((a, b, act))
        return out

    def to_json(self):
        return {"model": self.model, "pyannote_version": self.pyannote_version, "device": self.device,
                "turns": [asdict(t) for t in self.turns], "exclusive": [asdict(t) for t in self.exclusive],
                "embeddings": self.embeddings}

    @staticmethod
    def from_json(d):
        return DiarizationResult([Turn(**t) for t in d["turns"]], [Turn(**t) for t in d["exclusive"]],
                                 d.get("embeddings", {}), d.get("model", DEFAULT_MODEL),
                                 d.get("pyannote_version", ""), d.get("device", "cpu"))


# ------------------------------------------------------------------ access / environment checks
@dataclass
class AccessReport:
    ok: bool
    stage: str            # "token" | "access" | "network" | "ok"
    message: str
    user: str = ""


def check_model_access(token: str, model: str = DEFAULT_MODEL, timeout=15) -> AccessReport:
    """Validate the HF token and that the account may download the (gated) model. Network only, no torch needed."""
    token = (token or "").strip()
    if not token:
        return AccessReport(False, "token", "No Hugging Face token. Create a READ token at "
                            "huggingface.co/settings/tokens and save it in Settings > AI Providers.")

    def get(url, headers=None):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", **(headers or {})})
        return urllib.request.urlopen(req, timeout=timeout)

    try:
        with get(f"{HF_BASE}/api/whoami-v2") as r:
            who = json.loads(r.read())
        user = who.get("name", "")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return AccessReport(False, "token", "Hugging Face rejected the token (invalid, expired or revoked).")
        return AccessReport(False, "network", f"Hugging Face returned HTTP {e.code} while checking the token.")
    except Exception as e:  # noqa
        return AccessReport(False, "network", f"Cannot reach Hugging Face: {redact(e)}")

    try:   # config.yaml of a gated repo: 200 = access granted, 403 = terms not accepted
        with get(f"{HF_BASE}/{model}/resolve/main/config.yaml", {"Range": "bytes=0-0"}):
            pass
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return AccessReport(False, "access", f"Token is valid (user '{user}') but has NO ACCESS to {model}. "
                                f"Open {HF_BASE}/{model}, accept the conditions, and make sure the token type is READ "
                                "(or a fine-grained token with read access to gated repos).", user)
        if e.code == 404:
            return AccessReport(False, "access", f"Model '{model}' was not found on Hugging Face.", user)
        return AccessReport(False, "network", f"Hugging Face returned HTTP {e.code} for {model}.", user)
    except Exception as e:  # noqa
        return AccessReport(False, "network", f"Cannot reach Hugging Face: {redact(e)}", user)
    return AccessReport(True, "ok", f"OK - user '{user}' can use {model}.", user)


def pyannote_version() -> str:
    try:
        import importlib.metadata as md
        return md.version("pyannote.audio")
    except Exception:
        return ""


def check_environment() -> list:
    """-> [(name, ok, detail)] for pyannote / torch / CUDA. Used by verify_env.py and the UI."""
    out = []
    v = pyannote_version()
    if not v:
        out.append(("pyannote.audio", False, "not installed: pip install -r requirements-ai.txt"))
    else:
        major = int(v.split(".")[0])
        out.append(("pyannote.audio", major >= 4, f"{v}" + ("" if major >= 4 else "  (need >= 4.0 for community-1)")))
    try:
        import torch
        out.append(("torch", True, torch.__version__))
        out.append(("CUDA", torch.cuda.is_available(), "available" if torch.cuda.is_available() else "not available (CPU will be used)"))
    except Exception as e:  # noqa
        out.append(("torch", False, f"not installed ({type(e).__name__})"))
    return out


# ------------------------------------------------------------------ running the pipeline
def read_wav_mono(path):
    """16-bit PCM WAV -> (float32 array [-1,1], sample_rate). The extracted audio16k.wav always has this format."""
    with wave.open(str(path), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise DiarizationError(f"Unsupported WAV sample width {sw * 8}-bit (expected 16-bit PCM).")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def _annotation_turns(ann):
    return [Turn(float(t.start), float(t.end), str(s)) for t, _, s in ann.itertracks(yield_label=True)]


def parse_pipeline_output(out):
    """Pyannote 4 DiarizeOutput or 3.x Annotation -> (turns, exclusive_turns, embeddings dict)."""
    if hasattr(out, "speaker_diarization"):                       # pyannote 4
        turns = _annotation_turns(out.speaker_diarization)
        ex = getattr(out, "exclusive_speaker_diarization", None)
        exclusive = _annotation_turns(ex) if ex is not None else turns
        emb = {}
        se = getattr(out, "speaker_embeddings", None)
        if se is not None:
            labels = sorted({t.speaker for t in turns})
            try:
                for lab, vec in zip(labels, np.asarray(se)):
                    emb[lab] = [float(x) for x in vec]
            except Exception:
                emb = {}
        return turns, exclusive, emb
    if hasattr(out, "itertracks"):                                # 3.x Annotation
        t = _annotation_turns(out)
        return t, t, {}
    raise DiarizationError(f"Unexpected pyannote output type {type(out).__name__}; unsupported pyannote version?")


_PIPE = {}
from ..shared.model_manager import MM as _MM
_MM.register_external("pyannote", _PIPE.clear)


def load_pipeline(token, model=DEFAULT_MODEL, device="cpu"):
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError as e:
        raise DiarizationError("pyannote.audio / torch are not installed. Run: pip install -r requirements-ai.txt "
                               f"(detail: {e})") from e
    v = pyannote_version()
    if v and int(v.split(".")[0]) < 4:
        raise DiarizationError(f"pyannote.audio {v} is installed but '{model}' needs pyannote.audio >= 4.0. "
                               "Run: pip install -U \"pyannote.audio>=4.0.7\"")
    if device == "cuda" and not torch.cuda.is_available():
        raise DiarizationError("CUDA was selected but is not available in this PyTorch build. Choose CPU, or install a CUDA build of torch.")
    key = (model, device)
    if key in _PIPE:
        return _PIPE[key]
    try:
        pipe = Pipeline.from_pretrained(model, token=token)
    except Exception as e:  # noqa
        raise DiarizationError(f"Could not load '{model}': {redact(e)}. Check the token / accepted model terms "
                               "(Settings > AI Providers > Hugging Face > Test).") from e
    if pipe is None:
        raise DiarizationError(f"Hugging Face returned no pipeline for '{model}' - the token has no access to the gated model.")
    try:
        pipe.to(torch.device(device))
    except Exception as e:  # noqa
        raise DiarizationError(f"Could not move the pipeline to {device}: {redact(e)}") from e
    _PIPE[key] = pipe
    return pipe


def _cache_path(wav, model, num_speakers, cache_dir):
    st = Path(wav).stat()
    h = hashlib.sha1(f"{Path(wav).name}|{st.st_size}|{model}|{num_speakers}".encode()).hexdigest()[:16]
    return Path(cache_dir) / f"diar_{h}.json"


def diarize(wav, token, model=DEFAULT_MODEL, device="cpu", num_speakers=None, min_speakers=None, max_speakers=None,
            cache_dir=None, preflight=True) -> DiarizationResult:
    """Run pyannote on a 16 kHz WAV. Timestamps are those of the audio (never altered). Cached per file/model."""
    cp = _cache_path(wav, model, (num_speakers, min_speakers, max_speakers), cache_dir) if cache_dir else None
    if cp and cp.exists():
        try:
            return DiarizationResult.from_json(json.loads(cp.read_text(encoding="utf-8")))
        except Exception:
            pass
    if preflight:
        rep = check_model_access(token, model)
        if not rep.ok:
            raise DiarizationError(rep.message)
    pipe = load_pipeline(token, model, device)
    audio, sr = read_wav_mono(wav)
    import torch
    kw = {k: v for k, v in (("num_speakers", num_speakers), ("min_speakers", min_speakers), ("max_speakers", max_speakers)) if v}
    try:
        out = pipe({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": sr}, **kw)
    except Exception as e:  # noqa
        raise DiarizationError(f"Diarization failed: {redact(e)}") from e
    turns, exclusive, emb = parse_pipeline_output(out)
    if not turns:
        raise DiarizationError("Pyannote found no speech in this audio.")
    res = DiarizationResult(sorted(turns, key=lambda t: t.start), sorted(exclusive, key=lambda t: t.start), emb,
                            model, pyannote_version(), device)
    if cp:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(res.to_json()), encoding="utf-8")
    return res


# ------------------------------------------------------------------ speaker <-> ASR matching (temporal overlap)
@dataclass
class Match:
    speaker: str            # best speaker ("" when nothing overlaps)
    confidence: float       # 0..1  = share of the segment covered by that speaker, scaled by the margin to the runner-up
    uncertain: bool         # needs manual review
    overlap_with: list      # other speakers also talking during the segment (>= overlap_min of it)
    coverage: float         # share of the segment that has ANY speaker


def match_interval(start, end, turns, uncertain_below=0.5, overlap_min=0.2) -> Match:
    """Speaker of [start, end] by TEMPORAL OVERLAP with the diarization turns (never by nearest timestamp)."""
    dur = max(end - start, 1e-3)
    per = {}
    for t in turns:
        o = min(end, t.end) - max(start, t.start)
        if o > 0:
            per[t.speaker] = per.get(t.speaker, 0.0) + o
    if not per:
        return Match("", 0.0, True, [], 0.0)
    ranked = sorted(per.items(), key=lambda kv: -kv[1])
    best, bo = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    share = min(1.0, bo / dur)
    margin = (bo - second) / max(bo, 1e-3)
    conf = round(share * (0.5 + 0.5 * margin), 3)
    others = [s for s, o in ranked[1:] if o / dur >= overlap_min]
    covered = min(1.0, sum(per.values()) / dur) if not others else min(1.0, share + sum(o for _, o in ranked[1:]) / dur)
    return Match(best, conf, conf < uncertain_below or bool(others), others, round(min(1.0, covered), 3))


def match_segments(segments, result: DiarizationResult, use_exclusive=False, **kw):
    """segments: iterable of objects/tuples with start,end (Whisper segments / cues) -> [Match] (same order).
    use_exclusive=True matches against one-speaker-at-a-time turns (no overlap info)."""
    turns = result.exclusive if use_exclusive else result.turns
    out = []
    for s in segments:
        a, b = (s.start, s.end) if hasattr(s, "start") else (s[0], s[1])
        out.append(match_interval(a, b, turns, **kw))
    return out
