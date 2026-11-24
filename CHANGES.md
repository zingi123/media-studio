# Translator update - Emotion Raw, emotion-aware TTS adapter, timing report

## Already in the project (verified in the code, left as is)
Whisper word timestamps, speaker + gender detection, emotion/scream/cry detection, new line on speaker / voice / emotion change and
after a configurable silence, flicker removal, VoxCPM2 style prefix, speech-rate matching, sync + export.

## New
| File | What |
|---|---|
| `app/translator/emotion_raw.py` (new) | Structured Emotion Raw per line: label, intensity, vocal performance, speed, pitch, loudness, pause behavior, emphasis, delivery style, confidence. Low confidence -> Normal. Measures speed/loudness from the real audio relative to each speaker. Hand-edited values are never overwritten automatically. |
| `app/translator/tts_providers.py` (new) | Provider adapter. edge-tts: bounded rate/pitch/volume only, with an explicit limitation message (Khmer voices have NO emotion styles). Clone voice: style text from Emotion Raw. Voice name / reference is never changed by an emotion. |
| `subtitles.py` | `Cue.emotion_raw` (JSON, default ""). Old projects load unchanged; unknown keys are ignored. |
| `khmer_tts.py` | `synthesize(..., pitch_hz=0, volume_pct=0)` - sent to edge-tts only when non-zero. |
| `script_builder.py` | A pure voice note ("soft voice", "speaking fast") no longer splits a line; only an emotion change does. Lines carry Emotion Raw. |
| `smart_script.py` | Fills speed/loudness of Emotion Raw from the audio after the lines are built. |
| `voice_synchronizer.py` | `timing_report()` (overlaps, past-end, clips that cannot fit), overrun details in `LAST_REPORT`, 80 ms fade on trimmed clips. Return value of `build_voice_track` unchanged. |
| `ui/translator_page.py` | New "Emotion Raw" column; Emotion Raw panel (edit, Apply, Re-analyse this row, preview Emotional / Normal, Regenerate row); switch "Apply emotion to default / preset voices"; timing warnings before export. |
| `shared/settings.py` | Default `emotion_tts_prosody = True`. |
| `tests/` | 21 tests, `python -m pytest tests -q`. |

## Honest limits
* edge-tts Khmer cannot act emotions. The default/preset voices only get small speed/pitch/volume changes (switch off in the Emotion tab). For real emotional delivery use a cloned voice, and even there it depends on VoxCPM2.
* Emotion labels come from models trained mostly on English/Chinese; they are a guide, every field is editable.
* Not tested here: real Whisper/emotion models, VoxCPM2, a real edge-tts call (no internet/GPU), and a full video export. The Qt page was exercised headless only.
