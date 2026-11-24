# MediaStudio Translator redesign notes

## What changed
- The active application shell now opens directly into the Translator workspace.
- Removed Dashboard, Downloader, Converter, OCR/other-page and More Settings navigation from the active UI.
- Added a compact MediaStudio header and focused Translator workspace layout.
- Kept the existing `TranslatorPage`, its processing methods, worker wiring, voice dialogs, timeline, preview, subtitle editor, and export controls intact.

## Important preservation decision
Legacy converter/downloader source files remain in the archive but are not registered in the active application shell. They were not physically deleted because the supplied archive has no visible application entry point/dependency manifest, and `app/context.py` still constructs shared download infrastructure. Deleting those modules without inspecting the external launcher and dependency setup could break startup or shared imports.

## Existing Translator capabilities retained
Video import/preview, script extraction and Smart Script, script editing/import/export, translation, voice generation, voice presets/cloning, voice preview, speed/emotion/style controls, effects, subtitle generation, timeline/cue table, progress/log, and export.

## Not implemented by this UI-only pass
A true editable multi-track video timeline, project autosave/version history, dependency-aware selective regeneration, and advanced lip-sync remain development tasks. The current CueTimeline is a cue visualization/editor aid, not a complete NLE timeline.

## Validation
The Python source files were syntax-compiled after packaging. Runtime launch still depends on the original app launcher and installed PySide6/FFmpeg/AI dependencies, which were not included in the supplied ZIP.

## Figma implementation pass (Translator UI, file BvYZbXwUEGtXV8AXEFp9CB)

Implemented from the approved Figma file (pages "Design System" and "Translator UI").

### Modified files
- `app/ui/theme.py` - rewritten around a token table `T` (colors, radii, sizes from the Figma Design System). The QSS is a
  template that uses `@token` placeholders (`render_qss()`); every original selector is kept, plus new ones for the rail,
  SectionBox, status dots and the Figma button/input/table styling. `apply_theme()` and `QSS` keep their names.
- `app/ui/translator_widgets.py` - new `SectionBox` (same `.btn` / `.inner` / `_title` surface as `Collapsible`);
  `CueTimeline` now reads its colors from `T` (geometry and behaviour unchanged, min height 112 per Figma).
- `app/ui/translator_page.py` - the six groups use `SectionBox`; rail rebuilt (66x58 items, active highlight follows the
  open group); table header 32 + alternating rows; display-only status dots; one stale hint text corrected.
- `app/ui/main_window.py` - header 52, rail width 84, 8px gutters (values from `T`).

### Not changed
`translator/*`, `shared/*`, `workers.py`, all handlers, signals, slots, settings keys, and every control. Sub-sections
(Output location, Advanced scan settings, Voice type / Speed / Emotion, Audio mix) are still `Collapsible`.

### Known gaps vs. Figma
- Icons are the existing Unicode glyphs, not the Figma SVG icons (no asset download was done).
- Primary-button pressed color, chip pressed/selected and disabled states were not inspected in Figma (tool limit was
  reached); they reuse the original project values.
- Sub-section headers are styled as bordered bars; they are not wrapped in a bordered box as in the Figma spec.
- Status-dot rules (ready / warning / idle per group) are an interpretation; Figma only shows the dot.
- Nothing was launched: PySide6 was not available in the build environment.
