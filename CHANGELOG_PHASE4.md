# MediaStudio Phase 4 — Movie Summary gap implementation

Date: 2026-11-22

## Added
- Gemini Vision scene-frame analysis (`app/summary/intelligence.py`) with structured captions, keywords and generic entities.
- Scene matcher uses AI visual summaries and entities as additional text evidence.
- Story-beat drafting from reviewed scene evidence (`app/summary/story_intelligence.py`) with source-time hints and anti-hallucination constraints.
- Local WAV voice-profile registry (`app/summary/voice_profiles.py`) and Summary UI add/remove/select controls using the existing clone adapter.
- Embedded source-video preview through optional Qt Multimedia.
- Read-only EDL timeline overview showing source/output timing, speed and freeze.
- Export audio graph that can align and mix source audio, narration and optional looping background music.
- Focused tests for visual response parsing, visual matching, story-beat parsing, voice-profile CRUD, and a real FFmpeg audio-mix export.

## Verification
- Python compileall: PASS.
- Focused regression suites: see DEVELOPMENT_REPORT.md for exact run and count.
- The real Gemini API, real voice-cloning model, real PySide6 playback on Windows, and full multi-hour movie processing are not testable in this sandbox and remain NOT TESTED.

## Still not complete
- Full dialogue/subtitle-aware automatic plot understanding and character tracking.
- A fully editable multi-track Summary timeline integrated with the Translator Studio timeline.
- Calibrated visual match confidence and human-in-the-loop review workflow beyond current scene/beat tables.
- Real-device end-to-end validation with the user's API keys, models, media and Windows environment.
