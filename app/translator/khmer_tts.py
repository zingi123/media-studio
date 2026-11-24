"""
Khmer text-to-speech.

edge-tts uses Microsoft's online neural voices (km-KH-PisethNeural, km-KH-SreymomNeural). It is free and needs
no key, BUT: it requires internet, relies on an unofficial endpoint that Microsoft can change or block, and
its terms are not an open licence. Treat it as the default "best available free" option, not a guarantee.
Quality for Khmer must be judged by listening - it has NOT been tested by the developer of this scaffold.
"""
import asyncio
from pathlib import Path

VOICES = {
    "Sreymom (female) - Microsoft online": "km-KH-SreymomNeural",
    "Piseth (male) - Microsoft online": "km-KH-PisethNeural",
}


GENDER = {"male": "km-KH-PisethNeural", "female": "km-KH-SreymomNeural"}  # ភេទ → សំឡេង


def synthesize(text, out_path, voice="km-KH-SreymomNeural", speed=1.0, pitch_hz=0, volume_pct=0):
    """Generate one MP3. speed 1.0 = normal; 1.2 = 20% faster. pitch_hz / volume_pct (0 = untouched) are real edge-tts
    prosody parameters; they are only sent when non-zero so older edge-tts versions keep working."""
    try:
        import edge_tts
    except ImportError as e:
        raise RuntimeError("edge-tts is not installed. Run: pip install edge-tts") from e
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty text")
    rate = f"{int(round((speed - 1.0) * 100)):+d}%"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    extra = {}
    if int(pitch_hz):
        extra["pitch"] = f"{int(pitch_hz):+d}Hz"
    if int(volume_pct):
        extra["volume"] = f"{int(volume_pct):+d}%"

    async def go():
        await edge_tts.Communicate(text, voice, rate=rate, **extra).save(str(out_path))

    try:
        asyncio.run(go())
    except Exception as e:  # noqa
        raise RuntimeError(f"TTS failed (internet needed for edge-tts): {e}") from e
    if not Path(out_path).exists() or Path(out_path).stat().st_size == 0:
        raise RuntimeError("TTS produced no audio.")
    return str(out_path)