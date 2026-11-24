# MediaStudio — targeted completion notes

## Changes made in this ZIP

- Added separate Original Audio and Background Audio source lanes to the existing studio timeline. The original lane uses the source video's audio until Demucs stems exist; after Demucs, it displays the vocals stem and the Background lane displays `no_vocals.wav`.
- Source-derived lanes stay aligned to the video source. They are intentionally not draggable/trim-editable as independent files; their real-time gain, mute, solo and enable controls remain in the existing six-channel Mixer. User-added Khmer Voice, AI Voice, SFX and Music clips continue to use the existing timeline editing operations.
- Timeline clip/dialogue edits, voice regeneration, Demucs completion, and undo/redo now invalidate the playback render cache. If an edit happens during playback, the mixer is rebuilt and playback continues from the current video position.
- Playback cache fingerprints now use nanosecond file modification times to reduce stale-cache cases when a regenerated audio file is replaced quickly.
- Added a root `run.py` launcher because the supplied ZIP had no root-level entry point, plus `RUN_WINDOWS.md`.
- Added `tests/test_step3_timeline.py` for move, trim, split, duplicate, delete, merge, and history behavior.

## Existing functionality retained

The supplied ZIP already contained the Step 1 accordion/preview work and Step 2 six-channel audio engine/mixer. Those implementations were retained rather than replaced.

## Validation performed in this environment

- Step 1 logic tests: 14 passed.
- Step 2 audio tests: 18 passed.
- Step 2 playback-controller tests: 10 passed.
- Step 3 timeline model tests: 6 passed.
- Python source compilation: passed.
- GUI/runtime playback tests: not run; PySide6 is not installed in this execution environment.
- Python 3.11 compatibility: not directly tested here; this environment provides Python 3.13.5.
- FFmpeg is available; NumPy is available.
