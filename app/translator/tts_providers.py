"""
TTS provider adapters  -  decide HOW an Emotion Raw reaches each voice engine, and say honestly what it can do.

  edge      Microsoft edge-tts Khmer voices (default + preset voices)
            Real controls: rate, pitch, volume (prosody).  NO emotion "styles" exist for km-KH voices.
            -> the emotion is approximated with small, bounded prosody changes. It is NOT emotional acting,
               and the UI/log must say so (see limitation()).
  clone     VoxCPM2 cloned voices
            Accepts a natural-language style instruction in front of the text (existing behaviour).
            Real emotional control is model-dependent and not guaranteed either; speed is applied after generation.

Voice identity: the voice name / reference file is NEVER changed by an emotion. Prosody is clamped (emotion_raw.MAX_*),
so a character stays recognisable from calm to angry.
"""
from dataclasses import dataclass
from . import emotion_raw as ER


@dataclass(frozen=True)
class Capabilities:
    name: str
    emotion_style: bool        # accepts an emotion / style instruction that changes HOW it is acted
    rate: bool
    pitch: bool
    volume: bool
    note: str


EDGE = Capabilities("Microsoft edge-tts (Khmer)", False, True, True, True,
                    "No emotion styles for Khmer voices. Emotion is approximated with bounded speed / pitch / volume changes only.")
CLONE = Capabilities("VoxCPM2 cloned voice", True, False, False, False,
                     "Style instruction is passed to the model; how strongly it is followed depends on the model.")
PROVIDERS = {"edge": EDGE, "clone": CLONE}


def limitation(kind, raw: ER.EmotionRaw) -> str:
    """'' when nothing needs saying, else a plain-language limitation (shown in the log / Emotion tab)."""
    if raw is None or raw.label == "Normal":
        return ""
    cap = PROVIDERS[kind]
    if cap.emotion_style:
        return ""
    return (f"{cap.name} cannot act '{raw.label}'. Applied only: speed {raw.prosody()[0]:+d}%, pitch {raw.prosody()[1]:+d} Hz, "
            f"volume {raw.prosody()[2]:+d}%. For real emotional delivery use a cloned voice.")


def edge_args(raw: ER.EmotionRaw, speed=1.0, enabled=True):
    """-> (speed, pitch_hz, volume_pct) for khmer_tts.synthesize. enabled=False or Normal -> the plain request unchanged."""
    if not enabled or raw is None or raw.label == "Normal" and raw.speed == raw.pitch == raw.loudness == "Normal":
        return speed, 0, 0
    r, p, v = raw.prosody()
    return max(0.5, min(2.0, speed * (1 + r / 100.0))), p, v


def clone_style(raw: ER.EmotionRaw) -> str:
    """Style text for VoxCPM2 ('' for plain Normal speech)."""
    return raw.instruction() if raw is not None else ""


def synth_edge(text, out_path, voice, speed, raw=None, enabled=True):
    """edge-tts with the emotion's prosody. Returns (path, limitation_text)."""
    from .khmer_tts import synthesize
    sp, pitch, vol = edge_args(raw, speed, enabled)
    path = synthesize(text, out_path, voice, sp, pitch_hz=pitch, volume_pct=vol)
    return path, (limitation("edge", raw) if enabled else "")
