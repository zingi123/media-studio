"""Narration styles and adapters to the voice engines that ALREADY exist in the app (nothing is re-implemented).

The six recap styles are mapped onto the Translator's EmotionRaw (used for edge-tts prosody) and to a natural-language style
phrase (used by VoxCPM2-family cloned voices). Honest limits:
  * edge-tts has NO emotion styles for Khmer: styles only change speed / pitch / volume a little (limitation() says so).
  * For cloned voices the style phrase is only an instruction; the VoxPocket card says styles change cloned voices less than
    designed voices, and 'Suspenseful' is not one of its documented styles. Judge by listening.
"""
from pathlib import Path

from ..translator import emotion_raw as ER
from ..translator import tts_providers as TP

# style -> (EmotionRaw label, intensity, overrides, clone style phrase)
STYLES = {
    "Neutral":     ("Normal", "Medium", {}, ""),
    "Serious":     ("Normal", "Medium", {"speed": "Slow", "pitch": "Low"}, "serious and clear news-reader tone"),
    "Excited":     ("Excited", "Medium", {}, "excited and energetic"),
    "Suspenseful": ("Worried", "Medium", {"speed": "Slow", "loudness": "Quiet"}, "tense, quiet and suspenseful tone"),
    "Sad":         ("Sad", "Medium", {}, "sad, low and slow tone"),
    "Calm":        ("Calm", "Medium", {}, "calm, slow and steady"),
}


def _key(style):
    k = str(style or "").strip().lower()
    return next((n for n in STYLES if n.lower() == k), "Neutral")


def emotion_for(style):
    label, inten, over, _ = STYLES[_key(style)]
    return ER.make(label, inten, **over)


def clone_style(style):
    return STYLES[_key(style)][3]


def limitation(kind, style):
    """'' when nothing needs saying, else what the engine cannot do (shown next to the style selector)."""
    return TP.limitation(kind, emotion_for(style))


def edge_synth(voice="km-KH-SreymomNeural"):
    def synth(text, out_path, emotion="Neutral", speed=1.0):
        path = str(Path(out_path).with_suffix(".mp3"))               # edge-tts writes MP3
        TP.synth_edge(text, path, voice, speed, raw=emotion_for(emotion))
        return path
    return synth


def clone_synth(reference_wav):
    def synth(text, out_path, emotion="Neutral", speed=1.0):
        from ..translator import voice_clone as VC                    # imported late: the module patches / loads heavy stuff
        return VC.synthesize_clone(text, out_path, reference_wav, speed=speed, style=clone_style(emotion))
    return synth
