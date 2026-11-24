# MediaStudio - Chinese → Khmer AI Video Dubbing

Import a video → extract Chinese speech/subtitles → detect speakers → translate to spoken Khmer → generate one Khmer voice per
character → synchronize → export a dubbed video (with or without subtitles).
See `DEVELOPMENT_REPORT.md` for what changed, what is tested and what is NOT tested.

## Install (Windows + VS Code)
Your existing virtual environment (`.venv311`) is reused. Nothing working is reinstalled, upgraded or removed.
1. Open the folder in VS Code, select the interpreter `.venv311` (`Ctrl+Shift+P` > *Python: Select Interpreter*), open a terminal.
2. Install what is missing (asks before the large optional packages; never touches PyTorch):
```
setup_windows.bat
```
3. **Diagnose first, change nothing:**
```
.venv311\Scripts\python verify_env.py --report environment_report.txt
```
4. If PyTorch / TorchAudio / Pyannote show a compatibility problem (e.g. torch 2.15.0+cpu with torchaudio 2.11.1), read the cause and plan:
```
.venv311\Scripts\python verify_env.py --plan
```
`--fix` backs up the environment (`pip freeze`), validates the chosen option with `pip install --dry-run`, and installs only after you type YES.
5. FFmpeg: `winget install Gyan.FFmpeg` (MediaStudio also finds `bin\ffmpeg.exe` or a bundled copy; Whisper receives audio in memory so it does not need FFmpeg on PATH).

## Start
```
run.bat            (or:  .venv311\Scripts\python main.py)
```
The app opens even if Gemini / Claude / Hugging Face are not configured, Ollama is missing, or no local model is downloaded.
Open **Translator > Speakers & AI > System Status** for an honest per-component panel (INSTALLED / NOT INSTALLED / CONNECTED /
NOT CONNECTED / MODEL AVAILABLE / MODEL MISSING / COMPATIBILITY ERROR / NOT TESTED). A provider is CONNECTED only after a real
test request succeeded in the current session.

## CPU-first processing
CUDA is never assumed: it is used only if PyTorch really reports it. Models load on demand, **one heavy model at a time** (Settings:
`processing_workers`, default 1), are unloaded between stages, and a RAM check warns/refuses before a model that cannot fit
(e.g. Whisper large on a small PC). `cpu_threads` sets PyTorch's CPU threads. Time estimates are shown only from speeds measured on your PC.
Speech recognition works with `openai-whisper` or `faster-whisper` (setting `asr_backend`).

## Ollama
Found via PATH, `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`, or a path you set. Reported separately: executable / server /
API / models downloaded / model loaded / test generation. Starting the server is a button, never automatic. No model is ever
downloaded automatically: use `ollama pull qwen2.5:3b` yourself after checking RAM and disk.

## OCR vs Whisper
When burned-in subtitles disagree with the recognized speech, the row is marked (purple) and you choose per row
(*Review OCR vs speech differences*). Neither source silently replaces the other; timestamps always stay Whisper's.

## Speaker detection (Pyannote 4)
1. Create a Hugging Face account → https://huggingface.co/settings/tokens → new token, type **Read**.
2. Open https://huggingface.co/pyannote/speaker-diarization-community-1 and **accept the conditions** with the same account.
3. In the app: *Speakers & AI > AI Providers > Hugging Face* → paste the token → **Test token + model access**.
   The test tells you exactly which step is missing (invalid token / terms not accepted / network).
4. If Pyannote cannot run, the scan STOPS with that message. It does not guess. You may explicitly choose
   *Energy fallback* (less accurate) or *Off* (one speaker) in the same dialog.

## Keys and tokens
Stored in the OS credential store (Windows Credential Manager) via `keyring` (add / edit / test / remove in AI Providers), or read from environment variables
`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `HF_TOKEN` (see `.env.example`). Never written to `settings.json`, logs or messages.
Old plaintext keys in an existing `settings.json` are moved to the credential store on first start.
If `keyring` is not installed, a 0600 file is used and the app/`verify_env.py` says so.

## Providers
Gemini and Claude need an API key. Ollama is local: install it, run `ollama pull qwen2.5:7b`, then *Detect Ollama*.
The app only calls a model "available" when Ollama really lists it. Small local models are weak at Khmer - prefer Gemini/Claude
for final quality and always review the Khmer text before generating voice.

## Tests
```
python tests/run_tests.py       # no extra packages needed      (or: pip install -r requirements-dev.txt && pytest)
```


## Studio update
On-screen editing, frame presets / Auto Fit, manual Demucs + 5-channel mixer, CapCut-style timeline and dialogue/voice sync: see `STUDIO_FEATURES.md`.
Tests: `python tests/run_tests.py` (the Qt smoke test uses a fake Qt and is skipped when PySide6 is installed).

## Processing controls (Pause / Resume / Stop)
The Translator progress strip includes **PAUSE**, **RESUME**, and **STOP ALL**. Pause is cooperative: it takes effect at the next
safe checkpoint exposed by the active stage (for example FFmpeg loops, Whisper chunks, frame sampling, and Smart Script stage
boundaries). A model call or third-party operation that does not expose checkpoints cannot be interrupted mid-call; it will pause
when control returns to the pipeline. Stop releases a paused worker and requests cancellation. Completed project checkpoints are
kept so a later scan can reuse finished extraction / transcription work where its parameters still match.


### Scan resume and saved stage results
The Translator's standard scan now saves completed Whisper and OCR cue results in the per-video work folder. If the app is closed or a later stage is cancelled, rerunning the same scan can reuse a completed stage. Changing the Whisper model, language, device, or OCR language causes the affected stage to run again. A stage interrupted while it is running is marked pending and rerun safely; partial neural inference is not presented as resumable.
