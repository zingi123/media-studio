# CLONE VOICE (VoxCPM2 zero-shot) - what was added

## New files
- `app/translator/voice_clone.py` - storage, audio preparation, GPU check, VoxCPM2 engine (no Qt).
- `app/ui/voice_clone_dialog.py` - CLONE VOICE panel, delete confirmation, and `AddVoiceTypeCloneDialog`
  (a subclass of the existing `AddVoiceTypeDialog`; `voice_type_dialog.py` is untouched).
- `requirements_voice_clone.txt` - extra dependencies (only needed for cloning).

## Small additive edits to existing files
- `app/ui/translator_page.py`: ADD VOICE TYPE button, "Selected Voice Types" panel, Voice Type column dropdown
  also lists Selected Voice Types, `_gen_pass` routes a row with a selected/cloned voice to its own WAV.
  No function was renamed or removed.
- `app/: new default `voxcpm_model`.  `app/shared/hardware.py`: tool report lists VoxCPM2.

## Setup (cloning only; the rest of the app is unchanged)
NVIDIA GPU + CUDA 12+, Python 3.10-3.12. Follow `requirements_voice_clone.txt`
(install the CUDA build of PyTorch first, then `pip install -r requirements_voice_clone.txt`).
First clone downloads the ~5 GB model once. No GPU -> it automatically runs on the CPU (slow, but works; 16 GB RAM recommended, CPU build of PyTorch is enough).
Settings (`settings.json`): `voxcpm_device` = auto|cpu|cuda, `voxcpm_steps` = fewer steps = faster (default 10 GPU / 6 CPU),
`voxcpm_model` = local model folder (copy it with the app for a PC without internet).

## Use
Translator -> Fun -> **ADD VOICE TYPE** -> **CLONE VOICE** -> Import MP3/WAV -> Play reference -> Nickname ->
START CLONE -> hear the sample -> SAVE CLONED VOICE. Then pick it in ADD VOICE TYPE > "Cloned Voices" (ADD TO SELECTED
VOICE TYPES). Set it per row in the Voice Type column (or press "Use" on its chip), then **5. Generate Voice (all)**.

## Where voices live
`~/MediaStudio/cloned_voices/<id>/` (`reference.wav`, `original.*`, `voice.json`) - permanent, never deleted automatically;
only the DELETE button (with Confirm) removes one voice. The list of Selected Voice Types is kept in `settings.json`.
