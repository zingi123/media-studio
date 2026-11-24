# MediaStudio – STEP 1 notes

## Modified files
- app/ui/translator_page.py   – accordion wired in `_build_left`; rail `_goto` toggles; preview uses `TransportBar`; slightly roomier preview padding
- app/ui/translator_widgets.py – new `TransportBar` (adaptive one-row / two-row playback controls)
- app/ui/video_canvas.py      – `fit_view` rewritten (uniform scale, centred, scrollbars only when zoomed), `showEvent`, min size, native-size correction
- app/studio/geometry.py      – new pure `fit_scale()`
- NEW app/ui/accordion.py     – Qt-free accordion logic
- NEW tests/test_step1_logic.py (run: `python tests/test_step1_logic.py -v`, no Qt needed)
- NEW tests/gui_smoke_step1.py (needs PySide6; NOT yet run by the author)
- STEP1_changes.diff          – full diff against your original

Backup of the original ZIP: MediaStudio_ORIGINAL_backup.zip (delivered separately).
Unchanged: main_window.py, theme.py, every other page, all non-UI modules.
