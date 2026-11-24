# MediaStudio — Windows run and verification guide

## Start the application

Open PowerShell in the extracted project folder, activate the existing Python 3.11 virtual environment, then run:

```powershell
.\.venv311\Scripts\Activate.ps1
python run.py
```

`run.py` launches the existing `app.ui.main_window.MainWindow` and existing `Context`; it does not replace the application architecture.

## Runtime requirements

- Windows 10/11, Python 3.11 64-bit
- Existing project's PySide6 build with Qt Multimedia
- NumPy
- FFmpeg (FFprobe recommended) available on `PATH`, or configured in MediaStudio Settings
- Optional: Demucs and PyTorch for stem separation; install/use the versions already validated in the user's environment

Do not upgrade the virtual environment just to satisfy this ZIP. Check the existing `.venv311` first.

## Verify environment

```powershell
python tests/windows_runtime_check.py
```

This diagnostic does not install packages, download models, or modify settings. Resolve any missing item relevant to the features you intend to use.

## Automated tests

```powershell
python -m pytest -q tests/test_step1_logic.py tests/test_step2_audio.py tests/test_step2_controller.py tests/test_step3_timeline.py
```

The tests cover the pure logic, the six-track PCM mixing engine, real FFmpeg track preparation/mixing, timeline editing/history, and a stubbed playback controller. They do not substitute for actual Qt audio hardware testing.

## GUI and manual acceptance tests

```powershell
python tests/gui_smoke_step1.py
```

Then, in the application:

1. Import a video with audio and play/pause/seek/stop repeatedly.
2. Verify the video and mixed tracks remain aligned during a 10-minute playback.
3. Test each of the six tracks separately, then enable all six simultaneously.
4. Change each track's volume while playing; test mute, solo, enable/disable, and master volume.
5. Run Demucs and verify Original Audio and Background Audio are separate, with no accidental double playback.
6. Regenerate a Khmer Voice line and confirm the old audio is replaced, not duplicated.
7. Test split, trim, move, duplicate, delete, merge, undo, and redo on editable dialogue/audio clips.
8. Export with different mixer states and compare the output against the preview.
9. Play the exported MP4 from beginning to end and verify audio/video synchronization.

## Known scope limitation

Original Audio and Background Audio source lanes are source-locked to the video/stems. Their mixer controls are independent, but those two source lanes are not yet independent draggable/trim-editable timeline clips. Khmer Voice, AI Voice, Music, and SFX clips support timeline editing.

Do not mark the build production-ready until the Windows GUI, physical audio device, Demucs, and rendered MP4 checks above pass.
