# MediaStudio update

## Workspace layout and live playback

### Changed files
- `app/ui/translator_page.py`
  - Made workflow panel visibility follow the exclusive accordion state; closing the active rail item hides the whole workflow panel.
  - Timeline rail now hides/shows the complete bottom workspace (timeline, edit toolbar, Dialogue table and Audio mixer).
  - Preserved Big view and Inspector toggles, restored splitter dimensions, and added splitter-move canvas refitting.
  - Added persistence for active workflow section and panel/splitter visibility dimensions.
- `app/shared/settings.py`
  - Added new `translator_*` workspace defaults without renaming or removing existing keys.
- `app/studio/live_playback.py` (new)
  - Added per-channel cache fingerprinting, mixer gain mapping, 80 ms drift decision, and a coordinator for the existing synchronized playback engine.
  - The implementation intentionally uses one shared `QAudioSink` fed by independent per-channel WAV sources, rather than five independent `QMediaPlayer` clocks. This keeps all channels on one hardware clock while retaining live per-channel gain/mute/solo.
- `app/ui/playback.py`
  - Connected the coordinator, added 400 ms debouncing for timeline-triggered render rebuilds, status updates, and an 80 ms drift threshold.
  - Existing worker-based source preparation, channel caching, live gain changes, video-audio muting and hot-swap behaviour are retained.
- `app/ui/main_window.py`
  - Stops playback timers, pending preparation and mapped channel sources during application shutdown.
- `app/ui/mixer_panel.py`
  - Added a small audio update status line.
- `main.py` (new)
  - Added the requested root-level launcher (`python main.py`).
- `tests/test_live_playback.py` (new)
  - Added tests for channel hashing, gain/solo/mute mapping and drift correction.

## Manual checklist
1. Click each workflow rail item: only its section remains open; click it again to hide the workflow panel.
2. Toggle Timeline: the whole bottom workspace hides/shows. Dialogue table and Audio mixer are available when the bottom workspace is shown.
3. Toggle Inspector and Big view; confirm video area expands and prior layout is restored.
4. Split/duplicate/delete/move/trim a Khmer voice cue or SFX/music clip; press Play and confirm updated audio is rebuilt and synchronized.
5. Move mixer sliders during playback; confirm gain changes live. Test mute and solo.
6. Regenerate a line and confirm playback uses the new voice.
7. Compare preview mixer values with export output.
8. Close and reopen the app; confirm workspace visibility and splitter dimensions are restored.

## Verification caveat
The ZIP did not contain a prior `/tests` directory or root `main.py`. A new focused pure-logic test suite and launcher were added. GUI playback, QtMultimedia device behaviour, FFmpeg rendering, and export parity still require manual verification on the target Windows machine with the project's runtime dependencies installed.
