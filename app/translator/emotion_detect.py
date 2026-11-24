"""
Emotion + voice-event detection for a speaker's speech (used by Smart Script).

Two kinds of model, both optional (each stage degrades gracefully, like voice_detect.py):
  * emotion2vec  (funasr: iic/emotion2vec_plus_large)  -> normal / angry / sad / happy / scared / surprised / disgusted
  * AST AudioSet (MIT/ast-finetuned-audioset-10-10-0.4593) -> screaming, shouting, crying, whimpering, laughing, whispering ...

Honest limits: both were trained mostly on English / Chinese and on acted speech. On real Khmer speech the labels
are a GUIDE, not a fact - the user can edit every tag in the table. Without any model installed nothing is guessed:
every window is "normal" (score 0) and a note says so.
"""
import wave
import tempfile
from pathlib import Path
import numpy as np
from ..shared.ffmpeg_util import Cancelled
from .script_builder import EmotionSpan

SR = 16000
WIN = 4.0        # s   analysis window (3-5 s is the reliable range)
MIN_WIN = 1.5    # s   a last piece shorter than this is merged into the previous window
EVENT_MIN = 0.2  # below this an event is not reported

EMO_MAP = {"angry": "angry", "anger": "angry", "sad": "sad", "happy": "happy", "joy": "happy", "neutral": "normal",
           "fearful": "scared", "fear": "scared", "surprised": "surprised", "surprise": "surprised",
           "disgusted": "disgusted", "disgust": "disgusted"}
EVENT_MAP = {"Screaming": "screaming", "Shout": "shouting", "Yell": "shouting", "Children shouting": "shouting",
             "Battle cry": "shouting", "Crying, sobbing": "crying", "Baby cry, infant cry": "crying",
             "Whimper": "whimpering", "Wail, moan": "wailing", "Laughter": "laughing", "Giggle": "laughing",
             "Whispering": "whispering", "Sigh": "sighing", "Groan": "groaning"}

_M = {}


def make_windows(spans, win=WIN, min_win=MIN_WIN):
    """spans = [(start, end, speaker)] (one per speaker turn) -> [(start, end, speaker)] windows of about `win` s."""
    out = []
    for a, b, sp in spans:
        n = max(1, int(round((b - a) / win)))
        step = (b - a) / n
        cuts = [(a + i * step, a + (i + 1) * step) for i in range(n)]
        if len(cuts) > 1 and cuts[-1][1] - cuts[-1][0] < min_win:
            cuts[-2] = (cuts[-2][0], cuts[-1][1]); cuts.pop()
        out += [(x, y, sp) for x, y in cuts]
    return out


def _write_wav(path, y):
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())


def _emotion_model(device):
    if "emo" in _M:
        return _M["emo"]
    m = None
    try:
        from funasr import AutoModel
        for kw in ({"model": "emotion2vec/emotion2vec_plus_large", "hub": "hf"}, {"model": "iic/emotion2vec_plus_large"}):
            try:
                m = AutoModel(device=device, disable_update=True, **kw)
                break
            except Exception:
                m = None
    except Exception:
        m = None
    _M["emo"] = m
    return m


def _event_model(device):
    if "ev" in _M:
        return _M["ev"]
    m = None
    try:
        from transformers import pipeline
        m = pipeline("audio-classification", model="MIT/ast-finetuned-audioset-10-10-0.4593",
                     device=0 if device == "cuda" else -1, top_k=30)
    except Exception:
        m = None
    _M["ev"] = m
    return m


def _emo_of(model, y, tmpdir):
    p = Path(tmpdir) / "w.wav"
    _write_wav(p, y)
    res = model.generate(str(p), granularity="utterance", extract_embedding=False)
    r = res[0] if isinstance(res, list) else res
    best, score = "normal", 0.0
    for lab, sc in zip(r.get("labels", []), r.get("scores", [])):
        key = str(lab).split("/")[-1].strip().lower()
        if key in EMO_MAP and float(sc) > score:
            best, score = EMO_MAP[key], float(sc)
    return best, score


def _events_of(model, y):
    out = {}
    for d in model(y.astype("float32")):
        cat = EVENT_MAP.get(d["label"])
        if cat and d["score"] >= EVENT_MIN:
            out[cat] = max(out.get(cat, 0.0), float(d["score"]))
    return out


def _describe(y, res, words):
    """Fill span.voice with LIGHT descriptions of how it is said, relative to the speaker's own normal voice:
    loud / soft, high / low pitch, speaking fast / slowly. Used when the emotion is not confident enough to be written."""
    allsp = [(sp, s) for sp, L in res.items() for s in L]
    if not allsp:
        return

    def rms_db(sp_):
        seg = y[int(sp_.start * SR):int(sp_.end * SR)]
        return 20 * np.log10(float(np.sqrt(np.mean(seg ** 2))) + 1e-8) if len(seg) > 400 else None

    def rate(sp_):
        n = sum(1 for w in (words or []) if sp_.start <= (w.start + w.end) / 2 < sp_.end)
        return n / max(sp_.end - sp_.start, 1e-3) if words else None

    def f0(sp_):
        try:
            import librosa
            seg = y[int(sp_.start * SR):int(sp_.end * SR)].astype("float32")
            v = librosa.yin(seg, fmin=70, fmax=500, sr=SR, frame_length=1024)
            return float(np.median(v))
        except Exception:
            return None

    feats = {id(s): (rms_db(s), rate(s), f0(s)) for _, s in allsp}
    med = lambda xs: float(np.median([x for x in xs if x is not None])) if any(x is not None for x in xs) else None  # noqa
    g_rms, g_rate = med([f[0] for f in feats.values()]), med([f[1] for f in feats.values()])
    for sp, L in res.items():
        own = len(L) >= 3                                   # a personal baseline needs a few windows
        b_rms = med([feats[id(s)][0] for s in L]) if own else g_rms
        b_rate = med([feats[id(s)][1] for s in L]) if own else g_rate
        b_f0 = med([feats[id(s)][2] for s in L]) if own else None
        for s in L:
            r, rt, p = feats[id(s)]
            cand = []                                       # (strength, text)
            if r is not None and b_rms is not None:
                d = r - b_rms
                if d >= 5: cand.append((d / 5, "loud voice"))
                elif d <= -5: cand.append((-d / 5, "soft voice"))
            if p and b_f0:
                q = p / b_f0
                if q >= 1.15: cand.append(((q - 1) / 0.15, "high voice"))
                elif q <= 0.87: cand.append(((1 - q) / 0.13, "low voice"))
            if rt and b_rate:
                q = rt / b_rate
                if q >= 1.3: cand.append(((q - 1) / 0.3, "speaking fast"))
                elif q <= 0.7: cand.append(((1 - q) / 0.3, "speaking slowly"))
            s.voice = [t for k, t in sorted(cand, reverse=True)[:2] if k >= 1.3]   # only a CLEAR difference from their normal voice


def analyze_windows(y, windows, device="cpu", progress=None, cancel=None, words=None):
    """y = float32 mono 16 kHz audio of the whole video; windows from make_windows().
    -> ({speaker: [EmotionSpan]}, notes)"""
    notes = []
    emo, ev = _emotion_model(device), _event_model(device)
    if emo is None:
        notes.append("Emotion model not available (pip install funasr modelscope) -> all lines 'normal'.")
    if ev is None:
        notes.append("Audio-event model not available (transformers + torch) -> screaming/crying not detected.")
    res = {}
    with tempfile.TemporaryDirectory() as td:
        for n, (a, b, sp) in enumerate(windows):
            if cancel and cancel():
                raise Cancelled()
            seg = y[int(a * SR):int(b * SR)]
            label, score, events = "normal", 0.0, {}
            if len(seg) >= int(0.8 * SR):                      # shorter pieces give unreliable scores
                try:
                    if emo is not None:
                        label, score = _emo_of(emo, seg, td)
                except Exception as e:  # noqa
                    notes.append(f"emotion failed at {a:.1f}s: {str(e)[:60]}"); emo = None
                try:
                    if ev is not None:
                        events = _events_of(ev, seg)
                except Exception as e:  # noqa
                    notes.append(f"events failed at {a:.1f}s: {str(e)[:60]}"); ev = None
            res.setdefault(sp, []).append(EmotionSpan(a, b, label, score, events))
            if progress:
                progress(int((n + 1) * 100 / max(1, len(windows))))
    try:
        _describe(y, res, words)
    except Exception as e:  # noqa - the light descriptions are optional
        notes.append(f"voice description skipped: {str(e)[:60]}")
    return res, notes


from ..shared.model_manager import MM as _MM          # lets the model manager free these models between stages
_MM.register_external("emotion", _M.clear)
