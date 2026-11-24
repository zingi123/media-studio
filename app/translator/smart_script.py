"""
Smart Script:  video  ->  script lines  (time || text || emotion || speaker, voice type).

  1. Whisper words with real times            (speech_recognition.transcribe_words)
  2. who speaks + voice type                   (voice_detect.detect_voice_types - existing, unchanged)
  3. emotion / scream / cry per 3-5 s window   (emotion_detect)
  4. loud / question / fading per word         (script_builder.annotate_prosody)
  4b. character movement from the picture      (motion_detect) - written in the "action" field;
      runs in a second thread AT THE SAME TIME as steps 1-3, so the total time is the longer of the two, not the sum
  5b. story info: title, summary, character names (story_info) - header of the script file
  5. a NEW line when the speaker / voice changes, or the same character is silent >= break_on_silence s (default 3.0),
     or a line is longer than max_line_secs (default 0 = no length limit); spaces + punctuation from the real silences

Each stage is optional: if one fails the others still run and the reason is returned in `notes`.
"""
import threading
from pathlib import Path
import numpy as np
from ..shared.ffmpeg_util import Cancelled
from ..shared.paths import work_dir
from . import script_builder as SB
from .speech_recognition import transcribe_words
from .audio_extractor import extract_audio
from . import project_state as PS
import dataclasses
import json
from ..shared.model_manager import MM, pick_device, configure_cpu

SR = 16000


class CueList(list):
    """A normal list of Cue (so `cues, note = result` and every old use keeps working) that also carries `.story`
    and `.diarization` (method, merge suggestions, overlaps, embeddings - for the Speaker Manager)."""
    story = None
    diarization = None


def _save_words(path, words):
    Path(path).write_text(json.dumps([dataclasses.asdict(w) for w in words], ensure_ascii=False), encoding="utf-8")


def _load_words(path):
    known = {f.name for f in dataclasses.fields(SB.Word)}
    out = []
    for d in json.loads(Path(path).read_text(encoding="utf-8")):
        d = {k: v for k, v in d.items() if k in known}
        if isinstance(d.get("overlap_with"), list):
            d["overlap_with"] = tuple(d["overlap_with"])
        out.append(SB.Word(**d))
    return out


def _check_cancel(cancel):
    """Cooperative pause/cancel checkpoint between non-streaming pipeline stages."""
    if cancel and cancel():
        raise Cancelled()


def _stage(progress, lo, hi):
    def f(p):
        if progress is None:
            return
        progress(lo + int(max(0, min(100, p)) * (hi - lo) / 100) if isinstance(p, (int, float)) else p)
    return f


def scan_smart_script(video, lang="auto", model="small", device="cpu", hf_token="", min_conf=45, saved_presets=None,
                      use_emotion=True, break_on_silence=SB.DEFAULT_SILENCE_S, max_line_secs=0.0, emo_min_pct=70, event_min_pct=50, use_motion=True,
                      use_story=True, split_on_emotion=True, ocr_fix=False, diar_mode="pyannote", diar_model=None, gemini_key="", gemini_model="gemini-3.8-flash", progress=None, cancel=None):
    """-> (cues, note). Raises RuntimeError when no speech is found."""
    say = (lambda m: progress(m)) if progress else (lambda m: None)
    notes = []
    _check_cancel(cancel)
    try:
        from ..shared.settings import settings as _st
        workers = max(1, int(_st().get("processing_workers") or 1))
        auto_ocr = bool(_st().get("ocr_fix_auto_apply"))
        configure_cpu(int(_st().get("cpu_threads") or 0))
    except Exception:
        workers, auto_ocr = 1, False
    device, why = pick_device(device)                                 # CUDA only when it really exists; CPU otherwise
    if why:
        say(why)
        notes.append(why)

    box = {}
    vt = None
    if use_motion:                    # the picture does not depend on the audio -> analyse it at the same time (saves minutes)
        def _video():
            try:
                from .motion_detect import sample_frames
                box["samples"] = sample_frames(video, cancel=cancel)
            except BaseException as e:  # noqa - reported after the audio work
                box["err"] = e
        vt = threading.Thread(target=_video, daemon=True)
        vt.start()
        say("Smart Script: video picture is analysed at the same time as the audio...")

    wdir = Path(work_dir(Path(video).stem))
    state = PS.ProjectState.open_or_create(wdir / "project.json", video)       # checkpoints: a finished step is not redone
    wav = wdir / "audio16k.wav"
    if state.is_done("extract_audio", {"sr": 16000}):
        say("Resume: audio already extracted - reused.")
    else:
        state.start("extract_audio", {"sr": 16000})
        extract_audio(video, wav, cancel=cancel)  # extract តែម្តង មុន thread ទាំងអស់
        state.finish("extract_audio", {"audio": wav})
    _check_cancel(cancel)

    vbox = {}                                                        # លទ្ធផល voice detect ពី thread
    def _voice():                                                    # voice detect រត់ស្របគ្នានឹង Whisper
        try:
            from .voice_detect import detect_voice_types
            vbox["r"] = detect_voice_types(video, saved_presets or {}, hf_token=hf_token, device=device,
                                           min_conf=float(min_conf) / 100.0, progress=None, cancel=cancel,
                                           diar_mode=diar_mode, diar_model=diar_model)
        except BaseException as e:  # noqa - រាយការណ៍ក្រោយ
            vbox["err"] = e
    vth = None
    if workers > 1:                                                  # user allowed several models at once
        vth = threading.Thread(target=_voice, daemon=True)
        vth.start()
    else:                                                            # CPU-safe default: ONE heavy model in memory at a time
        say("Smart Script: speaker detection first (one model at a time to save memory)...")
        _voice()
        MM.unload_all()

    pth = None
    if use_emotion and workers > 1:                                                  # load model emotion ទុកជាមុន ពេលរង់ចាំ Whisper
        def _preload():
            try:
                from .emotion_detect import _emotion_model, _event_model
                _emotion_model(device); _event_model(device)         # ដាក់ក្នុង cache ខាងក្នុងរបស់វា
            except BaseException:  # noqa - បើបរាជ័យ analyze_windows នឹងរាយការណ៍ខ្លួនឯង
                pass
        pth = threading.Thread(target=_preload, daemon=True)         # thread preload
        pth.start()                                                  # ចាប់ផ្តើម

    say("Smart Script 1/4: reading the words and their exact times (Whisper)... (voice detect runs at the same time)")
    wparams = {"model": model, "lang": lang}
    wfile = wdir / "whisper_words.json"
    if state.is_done("whisper", wparams) and state.meta.get("whisper_det") is not None:
        words, det = _load_words(wfile), state.meta["whisper_det"] or None
        say("Resume: Whisper result from the previous run is reused (same video, model and language).")
        if progress:
            progress(65)
    else:
        state.start("whisper", wparams)
        words, det = transcribe_words(video, model=model, language=None if lang == "auto" else lang, device=device,
                                      progress=_stage(progress, 0, 65), cancel=cancel)
        if words:
            _save_words(wfile, words)
            state.meta["whisper_det"] = det or ""
            state.finish("whisper", {"words": wfile})
    _check_cancel(cancel)
    if not words:
        raise RuntimeError("No speech found in this video (Whisper returned no words). "
                           "Choose the correct language or a bigger accuracy (small/medium).")
    lang_used = lang if lang != "auto" else (det or "auto")
    if workers == 1:
        MM.unload_all()                                              # free Whisper before the emotion models load

    say("Smart Script 2/4: who is speaking and what voice type (waiting for the detector)...")
    segments, types = [], {}
    if vth:
        vth.join()                                                   # រង់ចាំ voice detect (បើនៅមិនទាន់ចប់)
    e = vbox.get("err")                                              # កំហុសពី thread
    if isinstance(e, Cancelled):                                     # cancel → បោះបង់
        raise e
    if e is not None:                                              # NEVER continue with a wrong/silent single-speaker result
        raise e
    if "r" not in vbox:
        raise RuntimeError("Speaker detection returned nothing.")
    else:
        r = vbox["r"]                                                # លទ្ធផលដូចដើម
        segments = r["segments"]
        types = {s_["id"]: s_["type"] for s_ in r["speakers"] if s_["type"] != "Unknown"}
        notes += r["notes"]
        diar_info = {"method": r.get("diar_method"), "merge_suggestions": r.get("merge_suggestions", []),
                     "overlaps": r.get("overlaps", []), "embeddings": r.get("embeddings", {}),
                     "voice_types": {s_["id"]: s_["type"] for s_ in r["speakers"]}}
        state.finish("diarization")
    _check_cancel(cancel)
    SB.assign_speakers(words, segments)
    SB.smooth_speakers(words)

    y = None
    try:
        import librosa
        y, _ = librosa.load(str(Path(work_dir(Path(video).stem)) / "audio16k.wav"), sr=SR, mono=True)
        SB.annotate_prosody(y, words, SR)
    except Exception as e:  # noqa
        notes.append(f"Loudness/pitch marks (! ? ...) skipped: {str(e)[:80]}")

    _check_cancel(cancel)
    turns = SB.split_turns(words, types, break_on_silence, max_line_secs)
    turns = SB.merge_tiny_turns(turns)
    spans = {}
    if use_emotion and y is not None:
        _check_cancel(cancel)
        say("Smart Script 3/4: emotion, screaming, crying...")
        if pth is not None:                                          # ប្រាកដថា model load ចប់ មុនប្រើ
            pth.join()
        from .emotion_detect import make_windows, analyze_windows
        wins = make_windows([(t[0].start, t[-1].end, t[0].speaker) for t in turns])
        spans, n2 = analyze_windows(y, wins, device, progress=_stage(progress, 65, 90), cancel=cancel, words=words)
        notes += n2

    if split_on_emotion and spans:   # emotion ប្តូរ → ជួរថ្មី + ពេលថ្មី
        turns = SB.split_by_emotion(turns, spans, emo_min_pct / 100.0, event_min_pct / 100.0)   # កាត់តាម emotion
    say(f"Smart Script 4/4: building lines (an emotion is written only at {emo_min_pct}% or more)...")
    cues = SB.build_lines(turns, spans, types, lang_used, emo_min=emo_min_pct / 100.0, event_min=event_min_pct / 100.0)

    if y is not None:                                # Emotion Raw: speed / loudness measured from the real audio, per speaker
        try:
            from .emotion_raw import refine_with_audio
            refine_with_audio(cues, y, SR)
        except Exception as e:  # noqa
            notes.append(f"Emotion Raw audio measures skipped: {str(e)[:60]}")

    _check_cancel(cancel)
    if ocr_fix:                                      # NEW (optional): correct the words with burned-in subtitles, timing stays Whisper's
        say("Smart Script: checking burned-in subtitles to correct the words (OCR)...")
        from .ocr_fix import fix_with_ocr
        notes.append(fix_with_ocr(cues, video, lang_used, progress=say, cancel=cancel, auto_apply=auto_ocr))

    _check_cancel(cancel)
    if vt is not None:                               # what each character does on screen during the line
        say("Smart Script: waiting for the video picture analysis...")
        vt.join()
        err = box.get("err")
        if isinstance(err, Cancelled):
            raise err
        if err is not None or "samples" not in box:
            notes.append(f"Movement analysis skipped ({str(err)[:80]}).")
        else:
            from .motion_detect import describe_ranges
            acts = describe_ranges(box["samples"], [(c.start, c.end) for c in cues])
            for c, a_ in zip(cues, acts):
                c.action = a_
            if not any("face" in (a_ or "") for a_ in acts):
                notes.append("No face marks (face detector unavailable or no face found) - only movement size is described.")

    out = CueList(cues)
    out.diarization = diar_info
    if use_story:                                    # title / summary / character names (header of the script)
        say("Smart Script: story info (title, summary, characters)...")
        from .story_info import make_story, apply_names
        out.story, n3 = make_story(cues, Path(video).stem, gemini_key, gemini_model)
        apply_names(cues, out.story)
        notes.append(n3)
    cues = out
    _check_cancel(cancel)
    if progress:
        progress(100)
    state.finish("script")
    note = f"{len(cues)} lines, {len({c.speaker for c in cues})} speaker(s)."
    n_conf = sum(1 for c in cues if c.text_conflict)
    if n_conf:
        note += f" {n_conf} line(s) differ between speech and burned-in subtitles - review them (Speakers & AI > Review OCR differences)."
    n_unc = sum(1 for c in cues if c.speaker_uncertain)
    if n_unc:
        note += f" {n_unc} line(s) have an UNCERTAIN speaker (marked in the Speaker column) - please review."
    if notes:
        note += " " + " | ".join(dict.fromkeys(notes))
    return cues, note
