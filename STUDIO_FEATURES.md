# MediaStudio - Studio update (the 7 requested feature groups)

Everything is added INSIDE the existing MediaStudio (Translator page). No old feature was removed: the Blur / Subtitle / Logo / Track
tabs, Smart Script, Speaker Manager, voice generation and all exports still work and are now kept in sync with the new on-screen objects.

| # | Request | Where / how |
|---|---|---|
| 1 | Edit directly on the video | Importing a video shows it in the new **canvas** (`app/ui/video_canvas.py`). Click any object to select it - no extra button. Drag = move, corner/edge handles = resize, round handle = rotate (Shift = 15° steps, Alt = resize from centre). Arrow keys nudge, Delete removes, Ctrl+D copies, right-click menu. |
| 2 | Logo, Khmer subtitles, watermark | Tool bar above the video: **+ Logo, + Watermark, + Khmer subtitle, + Text**. Inspector (right): position, size, rotation, **opacity**, **show from / until** (with "= playhead" buttons), text, font, colour, outline, bold, **running text** (ticker). Copy / delete / bring forward / send backward. |
| 3 | Blur watermark | Tool **▭ Blur area**: drag a rectangle on the video. Move / resize it on screen, set blur level and opacity, and the time window; also visible as a block timeline (show from / until). |
| 4 | Frame + Auto Fit | Inspector → **Video frame**: 9:16, 16:9, 1:1, 4:3, Custom, Original; Fit / Fill / Stretch; **Auto Fit on import** (whole picture, centred, never distorted); zoom in/out, move, crop L/T/R/B, mirror. Tool **✥ Picture**: drag to move the picture, wheel to zoom it. |
| 5 | Demucs + mixer | Export no longer starts Demucs. **Run Demucs** button (Audio mixer tab and Export box). After it: 5 channels (Original Vocals, Background, Khmer AI Voice, Sound Effects, Background Music) with volume, mute, solo, reset, **listen to one channel** and **listen to the whole mix** (30 s from the playhead). Settings are saved with the project and used by Export. |
| 6 | CapCut-style timeline | `app/ui/studio_timeline.py`: VIDEO, KHMER (one block per sentence), AI VOICE, SFX, MUSIC tracks with real waveforms. Drag / trim / split / duplicate / delete / merge, snap, zoom (Ctrl+wheel), scroll, undo/redo (Ctrl+Z / Ctrl+Y), click anywhere to seek. + SFX / + Music add audio files. |
| 7 | Dialogue + AI voice sync | Every line keeps original text, Khmer text, timing, speaker, emotion and voice settings (`Cue`) and is saved with the video (`dialogue.json`). Timing edits on the timeline update table, subtitles and export. **Regenerate edited lines** regenerates only lines whose text / voice / emotion / speed changed (character voice, emotion and speed are kept). After generation each voice is measured and compared with its slot ("Check voice timing", optional "extend End" fix). |

## Keys (timeline focused)
S split · Ctrl+D duplicate · Delete delete · M merge · Ctrl+Z / Ctrl+Y undo / redo · Space play / pause · ←/→ step (Shift = 1 s) · Home

## Files
`app/studio/` model, geometry, render (scene → FFmpeg, same graph for preview and export), audio (Demucs stems, mixer, SFX/music, waveforms), tracks (timeline operations + history), dialogue (voice sync).
`app/ui/` video_canvas, studio_timeline, mixer_panel, studio_panels, studio_integration (wiring into `TranslatorPage`).
Project data per video: `~/MediaStudio/work/<video>/studio_project.json` (scene, mixer, SFX/music clips) and `dialogue.json`.

## Known limits (honest list)
* The VIDEO track is shown (with waveform) and used for seeking, but the video itself cannot be trimmed/split there (that would re-time every other track).
* The canvas shows blur areas as a marked rectangle; the true blur appears in **Preview Effects** and in the export.
* Blur areas are drawn on the finished frame (after frame fit and mirror), i.e. what you see. Old projects that used Blur together with Mirror may need the area moved.
* Export mode "Video + Khmer subtitles" (no voice) keeps the original audio untouched; the mixer applies to the dubbing exports and to audio-only exports.
* Rotation of blur areas is not supported (axis-aligned only).
