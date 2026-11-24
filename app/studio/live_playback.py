"""Live playback helpers and coordinator for MediaStudio.

The application renders each channel to its own cached WAV, then mixes those channels
through one QAudioSink driven by the video player's clock. A single sink is deliberate:
separate QMediaPlayers have independent device clocks and are harder to keep sample-synchronous.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

DRIFT_LIMIT_MS = 80


def channel_signature(channel: str, items) -> str:
    """Stable hash for cues/clips belonging to one audio channel."""
    payload = []
    for item in items or ():
        if isinstance(item, dict):
            d = item
        elif hasattr(item, "to_dict"):
            d = item.to_dict()
        else:
            d = {k: getattr(item, k, None) for k in ("start", "end", "audio", "path", "volume", "src_in", "dur", "mute")}
        payload.append(d)
    raw = json.dumps({"channel": channel, "items": payload}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mixer_gains(mixer, available=None):
    """Return exactly the linear gain mapping used by studio.model.Mixer.gains/render_mix."""
    return mixer.gains(available=set(available) if available is not None else None)


def should_resync(video_ms: int, audio_ms: int, threshold_ms: int = DRIFT_LIMIT_MS) -> bool:
    """True only when slave audio is more than threshold_ms away from the video clock."""
    return abs(int(video_ms) - int(audio_ms)) > int(threshold_ms)


class LiveMixPlayer:
    """Small coordinator around the existing MixEngine/AudioOutput pair.

    The engine owns independent per-channel sources and live gain/mute/solo state;
    output is the synchronized Qt audio sink. This class centralizes clock operations.
    """
    def __init__(self, engine, output):
        self.engine = engine
        self.output = output

    def play(self, position_seconds: float):
        self.output.start(max(0.0, float(position_seconds)))

    def pause(self):
        self.output.stop()

    def stop(self):
        self.output.stop()
        self.engine.seek(0.0)

    def seek(self, position_seconds: float, resume: bool = False):
        self.output.start(max(0.0, float(position_seconds))) if resume else self.engine.seek(max(0.0, float(position_seconds)))

    def needs_resync(self, video_ms: int) -> bool:
        return self.output.running and should_resync(video_ms, int(self.output.audible_time() * 1000))
