"""Video -> script. One entry point for the two ways of reading the ORIGINAL text of a video:

  voice  : speech recognition (faster-whisper) - reads what people SAY
  screen : OCR - reads the subtitles burned into the picture

"auto" tries the voice first and, if it finds nothing, automatically tries the on-screen text, so the table is
not left empty (an empty table is what later produces a silent dub).
"""
from ..shared.ffmpeg_util import Cancelled
from ..shared.paths import work_dir
from . import project_state as PS
from .subtitles import Cue
import json
import os

METHODS = [("Auto (voice, then on-screen text)", "auto"),
           ("Voice only (Whisper)", "voice"),
           ("On-screen text only (OCR)", "screen")]

# label shown in the UI -> code ("auto" = let the engine detect the language)
LANGS = [("Auto-detect", "auto"), ("Chinese", "zh"), ("English", "en"), ("Japanese", "ja"),
         ("Korean", "ko"), ("Thai", "th"), ("Vietnamese", "vi"), ("Khmer", "km")]

OCR_LANG = {"auto": "ch", "zh": "ch", "en": "en", "ja": "ja", "ko": "ko", "th": "th", "vi": "vi"}  # OCR engine codes
MIN_LINES = 1   # fewer lines than this = "nothing found" -> try the other method


def scan_script(video, method="auto", lang="auto", model="base", device="cpu", progress=None, cancel=None):
    """-> (cues, note). Raises RuntimeError with the reason(s) when nothing could be read.
    method: auto | voice | screen.   lang: a code from LANGS."""
    from .ocr_subtitles import extract as ocr_extract
    from .speech_recognition import transcribe

    def say(msg):
        if progress:
            progress(msg)

    order = {"auto": ["voice", "screen"], "voice": ["voice"], "screen": ["screen"]}.get(method, ["voice", "screen"])
    problems = []

    # Keep independent, timestamp-preserving stage artifacts beside the project's other
    # working files. A completed OCR/Whisper stage can be reused after cancellation or
    # application restart; changing the model/language invalidates only that stage.
    state_dir = work_dir(f"{__import__('pathlib').Path(video).stem}_scan")
    state_dir.mkdir(parents=True, exist_ok=True)
    state = PS.ProjectState.open_or_create(state_dir / "scan_project.json", video)

    def read_cached(path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = set(Cue.__dataclass_fields__)
        return [Cue(**{k: v for k, v in item.items() if k in known}) for item in raw]

    def write_cached(path, cues):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps([c.__dict__ for c in cues], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    for step, how in enumerate(order):
        if cancel and cancel():
            raise Cancelled()
        stage = "whisper" if how == "voice" else "ocr"
        params = ({"model": model, "language": lang, "device": device} if how == "voice"
                  else {"language": OCR_LANG.get(lang, "ch"), "top": None})
        artifact = state_dir / ("voice_cues.json" if how == "voice" else "ocr_cues.json")
        if state.is_done(stage, params):
            cues = read_cached(artifact)
            say(f"Resume: cached {'Whisper' if how == 'voice' else 'OCR'} result reused ({len(cues)} lines).")
        else:
            state.start(stage, params)
            try:
                if how == "voice":
                    say("Reading the voice (Whisper)... the first run downloads the model.")
                    cues = transcribe(video, model=model, language=None if lang == "auto" else lang, device=device,
                                      progress=progress, cancel=cancel)
                else:
                    say("Reading the on-screen subtitles (OCR)...")
                    cues = ocr_extract(video, top=None, lang=OCR_LANG.get(lang, "ch"), progress=progress, cancel=cancel)
                write_cached(artifact, cues)
                state.finish(stage, {"cues": artifact})
            except Cancelled:
                state.steps[stage].status = PS.PENDING
                state.save()
                raise
            except Exception as e:  # noqa - keep going: the other method may still work
                state.fail(stage, e)
                problems.append(f"{'Voice' if how == 'voice' else 'On-screen text'}: {e}")
                cues = []
        if len(cues) >= MIN_LINES:
            src = "voice" if how == "voice" else "on-screen text"
            note = f"{len(cues)} lines read from the {src}."
            if step:
                note += " (The voice gave nothing, so the on-screen text was used.)"
            return cues, note
        if not problems or not problems[-1].startswith("Voice" if how == "voice" else "On-screen"):
            problems.append(f"{'Voice' if how == 'voice' else 'On-screen text'}: no text found")
        if step + 1 < len(order):
            say(f"{problems[-1]} -> trying the on-screen text...")
    raise RuntimeError("No script could be read from this video.\n  - " + "\n  - ".join(problems) +
                       "\nTry: choose the correct language, a bigger Whisper accuracy (small/medium), "
                       "or use a video that has speech or clear subtitles.")
