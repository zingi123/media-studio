import os, tempfile
from pathlib import Path
from app.translator import smart_script as SS
from app.translator.script_builder import Word
from app.translator.diarization import DiarizationError


def fake_video():
    p = Path(tempfile.mkdtemp()) / "episode1.mp4"; p.write_bytes(os.urandom(4000)); return p


def words():
    return ([Word("你", 1.0, 1.3), Word("好", 1.4, 1.7), Word("吗", 5.0, 5.3)] +      # same speaker, 3.3 s pause -> new row
            [Word("好", 5.4, 5.7), Word("的", 5.8, 6.0)])                              # other speaker right after


def patch(calls, diar_error=None):
    SS.extract_audio = lambda v, out, cancel=None: (calls.append("extract"), Path(out).write_bytes(b"RIFF"))[1]
    def tw(video, **kw):
        calls.append("whisper"); return words(), "zh"
    SS.transcribe_words = tw
    def dv(video, saved, **kw):
        calls.append(("diar", kw.get("diar_mode")))
        if diar_error: raise diar_error
        return {"speakers": [{"id": "Speaker 1", "type": "Women"}, {"id": "Speaker 2", "type": "Adult Men"}],
                "segments": [(0.9, 5.35, "Speaker 1"), (5.35, 6.1, "Speaker 2")], "notes": [], "diar_method": "pyannote",
                "merge_suggestions": [("Speaker 1", "Speaker 2", 0.8)], "overlaps": [], "embeddings": {}}
    import app.translator.voice_detect as VD
    VD.detect_voice_types = dv


KW = dict(use_emotion=False, use_motion=False, use_story=False, lang="zh", model="small")


def test_pipeline_rows_speakers_timing_and_resume():
    calls = []; patch(calls); v = fake_video()
    cues, note = SS.scan_smart_script(v, **KW)
    assert [(c.speaker, c.start, c.end) for c in cues] == [("Speaker 1", 1.0, 1.7), ("Speaker 1", 5.0, 5.3), ("Speaker 2", 5.4, 6.0)]
    assert cues.diarization["merge_suggestions"] and "Speaker 1" in {c.speaker for c in cues}   # suggestion only, still 2 speakers
    assert calls.count("whisper") == 1
    calls.clear(); patch(calls)
    cues2, _ = SS.scan_smart_script(v, **KW)                                # second run (like after an app restart)
    assert "whisper" not in calls and "extract" not in calls                  # completed steps are NOT repeated
    assert [(c.start, c.end) for c in cues2] == [(c.start, c.end) for c in cues]
    calls.clear(); patch(calls)
    SS.scan_smart_script(v, **{**KW, "model": "medium"})                      # parameter changed -> Whisper redone
    assert "whisper" in calls


def test_user_threshold_changes_rows():
    calls = []; patch(calls); v = fake_video()
    cues, _ = SS.scan_smart_script(v, break_on_silence=4.0, **KW)
    assert len(cues) == 2                                                     # 3.3 s pause no longer splits


def test_diarization_failure_is_raised_not_hidden():
    calls = []; patch(calls, DiarizationError("Token has NO ACCESS to model")); v = fake_video()
    try:
        SS.scan_smart_script(v, **KW); assert False, "must not continue with a fake single speaker"
    except DiarizationError as e:
        assert "NO ACCESS" in str(e)


def test_explicit_off_mode_is_passed_through():
    calls = []; patch(calls); v = fake_video()
    SS.scan_smart_script(v, diar_mode="off", **KW)
    assert ("diar", "off") in calls
