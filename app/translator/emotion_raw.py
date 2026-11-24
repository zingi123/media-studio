"""
Emotion Raw  -  one STRUCTURED emotion/performance record per script line (kept apart from the Khmer text).

Stored on Cue.emotion_raw as JSON ("" = not created yet; it is then derived from the old Cue.emotion string, so
old projects and old script files keep working). Cue.emotion (the short text written to script files) stays the
exported label and is kept in sync by `legacy_string()`.

Fields: label, intensity, vocal_performance, speed, pitch, loudness, pause_behavior, emphasis, delivery_style, confidence.

Honest limits
  * A label is only written when confidence >= CONF_MIN, otherwise it is "Normal" (never guessed).
  * speed / pitch / loudness are measured from the real audio only when the audio is available (refine_with_audio).
  * `prosody()` returns numbers only for what a provider can really change (rate / pitch / volume). Whether a
    provider turns them into believable emotion is decided in tts_providers.py - this module never claims it.
"""
import json
import re
from dataclasses import dataclass, asdict, field

LABELS = ["Normal", "Happy", "Sad", "Angry", "Fearful", "Excited", "Surprised", "Disgusted", "Worried", "Calm",
          "Crying", "Whispering", "Shouting"]
INTENSITIES = ["Low", "Medium", "High"]
SPEEDS = ["Slow", "Normal", "Fast"]
PITCHES = ["Low", "Normal", "High"]
LOUDNESS = ["Quiet", "Normal", "Loud"]
CONF_MIN = 0.50          # below this the label falls back to Normal (the detector's own threshold is higher)

# names written by emotion_detect / script_builder  ->  label in LABELS
_FROM_DETECTOR = {
    "normal": "Normal", "neutral": "Normal", "": "Normal",
    "angry": "Angry", "sad": "Sad", "happy": "Happy", "scared": "Fearful", "fearful": "Fearful", "surprised": "Surprised",
    "disgusted": "Disgusted", "screaming": "Shouting", "shouting": "Shouting", "crying": "Crying",
    "whimpering": "Crying", "wailing": "Crying", "laughing": "Happy", "whispering": "Whispering",
    "sighing": "Sad", "groaning": "Worried", "excited": "Excited", "worried": "Worried", "calm": "Calm",
}
# label in LABELS -> word written to script files (what script_builder/emotion_detect already use)
_TO_DETECTOR = {"Normal": "normal", "Fearful": "scared", "Shouting": "shouting", "Crying": "crying", "Happy": "happy",
                "Sad": "sad", "Angry": "angry", "Excited": "excited", "Surprised": "surprised", "Disgusted": "disgusted",
                "Worried": "worried", "Calm": "calm", "Whispering": "whispering"}

# default performance per label: (speed, pitch, loudness, pause behaviour, emphasis, delivery style)
_PROFILE = {
    "Normal":     ("Normal", "Normal", "Normal", "natural pauses at punctuation", "light", "natural conversational delivery"),
    "Happy":      ("Normal", "High",   "Normal", "short, bouncy pauses", "on positive words", "bright, smiling delivery"),
    "Sad":        ("Slow",   "Low",    "Quiet",  "longer pauses, trailing endings", "soft", "heavy, subdued delivery"),
    "Angry":      ("Fast",   "Normal", "Loud",   "short, clipped pauses", "strong on key words", "forceful, tense delivery"),
    "Fearful":    ("Fast",   "High",   "Quiet",  "uneven, breathy pauses", "on danger words", "trembling, anxious delivery"),
    "Excited":    ("Fast",   "High",   "Loud",   "very short pauses", "frequent", "energetic, eager delivery"),
    "Surprised":  ("Normal", "High",   "Normal", "a beat before the key word", "on the surprising word", "sudden, wide-eyed delivery"),
    "Disgusted":  ("Slow",   "Low",    "Normal", "drawn-out, deliberate pauses", "on the disliked word", "contemptuous delivery"),
    "Worried":    ("Normal", "Normal", "Quiet",  "hesitant pauses", "on uncertain words", "tense, hesitant delivery"),
    "Calm":       ("Slow",   "Low",    "Quiet",  "relaxed, even pauses", "light", "steady, soothing delivery"),
    "Crying":     ("Slow",   "Normal", "Quiet",  "broken pauses, sobbing breaks", "on emotional words", "voice breaking, tearful delivery"),
    "Whispering": ("Slow",   "Low",    "Quiet",  "soft pauses", "light", "hushed, breathy delivery"),
    "Shouting":   ("Fast",   "High",   "Loud",   "very short pauses", "strong on every key word", "shouted, projected delivery"),
}

# (rate %, pitch Hz, volume %) at MEDIUM intensity. Small on purpose: the same voice must stay recognisable.
_PROSODY = {
    "Normal": (0, 0, 0), "Happy": (6, 4, 8), "Sad": (-12, -5, -10), "Angry": (10, 3, 22), "Fearful": (8, 6, -4),
    "Excited": (14, 6, 15), "Surprised": (5, 7, 10), "Disgusted": (-6, -4, 0), "Worried": (-3, 2, -6), "Calm": (-8, -2, -6),
    "Crying": (-15, -3, -6), "Whispering": (-10, -2, -35), "Shouting": (12, 5, 40),
}
_INT_SCALE = {"Low": 0.5, "Medium": 1.0, "High": 1.5}
MAX_RATE, MAX_PITCH, MAX_VOL = 25, 10, 50     # hard limits (identity safety)
_STEP = {"Slow": -10, "Fast": 10, "Quiet": -8, "Loud": 8}   # manual speed/loudness fields move the numbers a little


@dataclass
class EmotionRaw:
    label: str = "Normal"
    intensity: str = "Medium"
    vocal_performance: str = ""
    speed: str = "Normal"
    pitch: str = "Normal"
    loudness: str = "Normal"
    pause_behavior: str = ""
    emphasis: str = ""
    delivery_style: str = ""
    confidence: float = 0.0
    source: str = "auto"              # auto | manual

    # ---- text for providers that understand a spoken-style instruction (VoxCPM2 / future LLM-TTS)
    def instruction(self) -> str:
        if self.label == "Normal" and self.speed == self.pitch == self.loudness == "Normal":
            return ""
        parts = []
        if self.label != "Normal":
            parts.append(f"{self.intensity.lower() + ' ' if self.intensity == 'High' else ''}{self.label.lower()}")
        for v, n in ((self.loudness, "voice"), (self.pitch, "pitch"), (self.speed, "pace")):
            if v != "Normal":
                parts.append(f"{v.lower()} {n}")
        if self.delivery_style and self.label != "Normal":
            parts.append(self.delivery_style)
        return ", ".join(parts)

    def tts_instruction(self) -> str:
        """Full English instruction (shown in the UI, sent to providers that accept natural-language style)."""
        if not self.instruction():
            return "Generate natural Khmer speech with normal, conversational delivery."
        s = (f"Generate Khmer speech with {self.label.lower()} emotion ({self.intensity.lower()} intensity): "
             f"{self.delivery_style or 'natural delivery'}; speed {self.speed.lower()}, pitch {self.pitch.lower()}, "
             f"loudness {self.loudness.lower()}; {self.pause_behavior or 'natural pauses'}; emphasis {self.emphasis or 'light'}.")
        return s

    def prosody(self):
        """-> (rate_percent, pitch_hz, volume_percent) a prosody-capable provider can apply. Always inside the hard limits."""
        r, p, v = _PROSODY.get(self.label, (0, 0, 0))
        k = _INT_SCALE.get(self.intensity, 1.0) if self.label != "Normal" else 0.0
        r, p, v = r * k, p * k, v * k
        r += _STEP.get(self.speed, 0) * 0.5 if self.speed != _PROFILE[self.label][0] else 0       # manual / measured change
        p += {"Low": -3, "High": 3}.get(self.pitch, 0) if self.pitch != _PROFILE[self.label][1] else 0
        v += _STEP.get(self.loudness, 0) if self.loudness != _PROFILE[self.label][2] else 0
        clamp = lambda x, m: int(round(max(-m, min(m, x))))  # noqa
        return clamp(r, MAX_RATE), clamp(p, MAX_PITCH), clamp(v, MAX_VOL)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _norm(v, allowed, default):
    for a in allowed:
        if str(v).strip().lower() == a.lower():
            return a
    return default


def make(label="Normal", intensity="Medium", confidence=0.0, **kw) -> EmotionRaw:
    """Build a record; unset performance fields come from the label's default profile. Low confidence -> Normal."""
    label = _norm(label, LABELS, "Normal")
    if label != "Normal" and confidence and confidence < CONF_MIN:
        label = "Normal"
    sp, pi, lo, pause, emph, style = _PROFILE[label]
    r = EmotionRaw(label=label, intensity=_norm(intensity, INTENSITIES, "Medium"), speed=sp, pitch=pi, loudness=lo,
                   pause_behavior=pause, emphasis=emph, delivery_style=style, confidence=round(float(confidence or 0), 2))
    for k, v in kw.items():
        if v not in (None, "") and hasattr(r, k):
            setattr(r, k, v)
    r.speed, r.pitch, r.loudness = _norm(r.speed, SPEEDS, "Normal"), _norm(r.pitch, PITCHES, "Normal"), _norm(r.loudness, LOUDNESS, "Normal")
    if not r.vocal_performance:
        r.vocal_performance = r.instruction() or "normal speech"
    return r


def from_json(s) -> "EmotionRaw | None":
    try:
        d = json.loads(s) if isinstance(s, str) else dict(s)
        if not isinstance(d, dict):
            return None
        r = make(d.get("label", "Normal"), d.get("intensity", "Medium"), 1.0 if d.get("label", "Normal") == "Normal" else float(d.get("confidence", 1.0) or 0),
                 **{k: d.get(k) for k in ("vocal_performance", "speed", "pitch", "loudness", "pause_behavior", "emphasis", "delivery_style")})
        r.confidence = round(float(d.get("confidence", 0) or 0), 2)
        r.source = d.get("source", "auto") if d.get("source") in ("auto", "manual") else "auto"
        return r
    except Exception:  # noqa - a damaged value must never break a project
        return None


_VOICE_NOTES = {"loud voice": ("loudness", "Loud"), "soft voice": ("loudness", "Quiet"), "high voice": ("pitch", "High"),
                "low voice": ("pitch", "Low"), "speaking fast": ("speed", "Fast"), "speaking slowly": ("speed", "Slow")}


def from_legacy(text, confidence=None) -> EmotionRaw:
    """The short Cue.emotion string ('very angry, loud voice' / 'normal, soft voice' / 'sad → angry') -> EmotionRaw.
    Only the FIRST emotion of a 'a → b' chain is used (one line = one emotion; the rest was split into other lines)."""
    first = re.split(r"\s*(?:→|->)\s*", str(text or ""))[0]
    parts = [p.strip().lower() for p in first.split(",") if p.strip()]
    label, intensity, extra = "Normal", "Medium", {}
    for p in parts:
        if p in _VOICE_NOTES:
            k, v = _VOICE_NOTES[p]; extra[k] = v; continue
        very = p.startswith("very ")
        w = _FROM_DETECTOR.get(p[5:] if very else p)
        if w and label == "Normal":
            label, intensity = w, "High" if very else "Medium"
        elif w and w != "Normal" and label != "Normal" and w != label:
            pass                                               # a 2nd emotion in one line: ignored, never mixed
    if confidence is None:
        confidence = 0.0 if label == "Normal" else (0.9 if intensity == "High" else 0.7)
    r = make(label, intensity, confidence, **extra)
    return r


def of_cue(c) -> EmotionRaw:
    """The Emotion Raw of a Cue (stored JSON first, else derived from the old emotion text). Never raises."""
    r = from_json(getattr(c, "emotion_raw", "") or "") if getattr(c, "emotion_raw", "") else None
    return r or from_legacy(getattr(c, "emotion", ""))


def legacy_string(r: EmotionRaw) -> str:
    """EmotionRaw -> the short text kept in Cue.emotion / script files ('very angry, loud voice' / 'normal, soft voice' / 'normal')."""
    notes = [n for n, (k, v) in _VOICE_NOTES.items() if getattr(r, k) == v and getattr(r, k) != _PROFILE[r.label][("speed", "pitch", "loudness").index(k)]]
    if r.label == "Normal":
        return ("normal, " + ", ".join(notes[:2])) if notes else "normal"
    return ", ".join([("very " if r.intensity == "High" else "") + _TO_DETECTOR[r.label]] + notes[:1])


def set_on_cue(c, r: EmotionRaw, manual=False):
    """Write r to the cue and keep the short text in sync. Returns True when the generated voice is now out of date."""
    if manual:
        r.source = "manual"
    old = (c.emotion_raw, c.emotion)
    c.emotion_raw, c.emotion = r.to_json(), legacy_string(r)
    return (c.emotion_raw, c.emotion) != old


# ---------------------------------------------------------------- measure from the real audio (optional)
def refine_with_audio(cues, y, sr=16000):
    """Fill speed / pitch / loudness of every line's Emotion Raw from the REAL audio y (float mono), relative to the
    same speaker's own median - so 'Loud' means louder than THIS character normally is. Lines whose raw was edited
    by hand (source == 'manual') are never touched. Silent no-op without numpy."""
    try:
        import numpy as np
    except Exception:
        return cues
    rms, rate = {}, {}
    for i, c in enumerate(cues):
        seg = y[int(c.start * sr):int(c.end * sr)]
        if len(seg) > 0.3 * sr:
            rms[i] = 20 * np.log10(float(np.sqrt(np.mean(np.square(seg)))) + 1e-8)
        words = len(re.findall(r"[A-Za-z0-9\u00C0-\u024F']+", re.sub(r"\([^()]*\)|\[pause [0-9.]+s\]", " ", c.text or ""))) \
            + len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", c.text or ""))
        if words and c.end - c.start >= 0.4:
            rate[i] = words / (c.end - c.start)
    by = {}
    for i, c in enumerate(cues):
        by.setdefault(c.speaker or "Speaker", []).append(i)
    for sp, idx in by.items():
        if len(idx) < 3:
            continue                                      # no reliable personal baseline
        b_rms = float(np.median([rms[i] for i in idx if i in rms])) if any(i in rms for i in idx) else None
        b_rate = float(np.median([rate[i] for i in idx if i in rate])) if any(i in rate for i in idx) else None
        for i in idx:
            r = of_cue(cues[i])
            if r.source == "manual":
                continue
            if i in rms and b_rms is not None:
                d = rms[i] - b_rms
                r.loudness = "Loud" if d >= 6 else "Quiet" if d <= -6 else r.loudness
            if i in rate and b_rate:
                q = rate[i] / b_rate
                r.speed = "Fast" if q >= 1.3 else "Slow" if q <= 0.7 else r.speed
            r.vocal_performance = r.instruction() or "normal speech"
            cues[i].emotion_raw = r.to_json()
    return cues


def from_span(span, emo_min=0.70, event_min=0.50) -> EmotionRaw:
    """script_builder.EmotionSpan -> EmotionRaw (label only when confident enough; events such as screaming/crying win
    when they are confident; voice notes become loudness / pitch / speed)."""
    label, conf = "Normal", 0.0
    if span is not None:
        if span.label not in ("normal", "neutral", "") and span.score >= emo_min:
            label, conf = _FROM_DETECTOR.get(span.label, "Normal"), span.score
        for ev, sc in sorted(span.events.items(), key=lambda t: -t[1]):
            if sc >= event_min and _FROM_DETECTOR.get(ev, "Normal") in ("Shouting", "Crying", "Whispering"):
                label, conf = _FROM_DETECTOR[ev], sc
                break
    extra = {}
    for n in (span.voice if span is not None else []):
        if n in _VOICE_NOTES:
            k, v = _VOICE_NOTES[n]; extra[k] = v
    return make(label, "High" if conf >= 0.85 else "Medium", conf, **extra)
