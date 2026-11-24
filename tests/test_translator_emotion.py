"""Tests for the Translator: new-line timing, Emotion Raw, provider adapter, timing/sync.
Run from the project root:   python -m pytest tests -q
No GPU, no internet and no AI model is needed (ffmpeg is needed only for the sync test)."""
import json
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from app.translator import script_builder as SB
from app.translator import emotion_raw as ER
from app.translator import tts_providers as TP
from app.translator import subtitles as S
from app.translator.script_builder import Word, EmotionSpan


def W(text, a, b, sp="Speaker 1"):
    return Word(text, a, b, sp)


def lines(words, spans=None, types=None, **kw):
    split_kw = {k: kw.pop(k) for k in ("break_on_silence", "max_secs") if k in kw}
    turns = SB.split_turns(words, types or {}, split_kw.get("break_on_silence", 0.0), split_kw.get("max_secs", 0.0))
    if spans:
        turns = SB.split_by_emotion(turns, spans)
    return SB.build_lines(turns, spans, types or {}, "zh"), turns


def all_words(turns):
    return [w for t in turns for w in t]


ANGRY = lambda a, b: EmotionSpan(a, b, "angry", 0.9)        # noqa
SAD = lambda a, b: EmotionSpan(a, b, "sad", 0.8)            # noqa
NORMAL = lambda a, b: EmotionSpan(a, b, "normal", 0.0)      # noqa


# ---------------------------------------------------------------- 1-6 new-line timing
def test_1_same_speaker_same_emotion_keeps_one_line():
    ws = [W("你", 0.0, 0.3), W("好", 0.35, 0.6), W("吗", 0.65, 1.0)]
    cues, _ = lines(ws, {"Speaker 1": [NORMAL(0, 4)]})
    assert len(cues) == 1 and (cues[0].start, cues[0].end) == (0.0, 1.0)


def test_2_speaker_change_new_line_real_timestamp():
    ws = [W("你", 0.0, 0.4), W("好", 0.45, 0.9), W("我", 1.2, 1.5, "Speaker 2"), W("是", 1.55, 2.0, "Speaker 2")]
    cues, turns = lines(ws)
    assert [c.speaker for c in cues] == ["Speaker 1", "Speaker 2"]
    assert cues[1].start == 1.2 and cues[1].end == 2.0          # first / last real word time, nothing invented


def test_3_voice_type_change_new_line():
    # voice type is a property of a speaker, so a different voice type always arrives with a different speaker id
    ws = [W("a", 0.0, 0.3, "S1"), W("b", 0.35, 0.6, "S2")]
    cues, _ = lines(ws, types={"S1": "Adult Man", "S2": "Girl"})
    assert len(cues) == 2 and [c.voice_desc for c in cues] == ["Adult Man", "Girl"]
    # same speaker, same voice type: stays one line
    assert len(SB.split_turns([W("a", 0.0, 0.3, "S1"), W("b", 0.35, 0.6, "S1")], {"S1": "Adult Man"})) == 1


def test_4_emotion_change_new_line_and_timestamp():
    ws = [W("a", 0.0, 0.5), W("b", 0.6, 1.2), W("c", 2.0, 2.6), W("d", 2.7, 3.3)]
    spans = {"Speaker 1": [NORMAL(0, 1.6), ANGRY(1.6, 4.0)]}
    cues, turns = lines(ws, spans)
    assert len(cues) == 2
    assert cues[1].start == 2.0 and cues[1].end == 3.3
    assert "angry" in cues[1].emotion and cues[0].emotion == "normal"


def test_5_same_speaker_resumes_after_long_silence():
    ws = [W("a", 0.0, 0.5), W("b", 0.6, 1.0), W("c", 4.0, 4.4)]
    cues, _ = lines(ws, break_on_silence=1.5)
    assert len(cues) == 2 and cues[1].start == 4.0


def test_6_emotion_changes_inside_a_sentence_splits_at_the_word():
    ws = [W("w1", 0.0, 0.6), W("w2", 0.65, 1.3), W("w3", 1.35, 2.0), W("w4", 2.05, 2.7), W("w5", 2.75, 3.4), W("w6", 3.45, 4.1)]
    spans = {"Speaker 1": [NORMAL(0, 2.02), ANGRY(2.02, 5.0)]}          # change starts exactly before w4 (2.05)
    cues, turns = lines(ws, spans)
    assert [t[0].text for t in turns] == ["w1", "w4"]
    assert cues[1].start == 2.05


def test_words_never_lost_duplicated_or_overlapping():
    ws = [W(f"w{i}", i * 0.7, i * 0.7 + 0.6, "S1" if i < 5 else "S2") for i in range(12)]
    spans = {"S1": [NORMAL(0, 2), ANGRY(2, 4)], "S2": [SAD(4, 6), NORMAL(6, 9)]}
    cues, turns = lines(ws, spans, break_on_silence=1.5)
    got = [w.text for w in all_words(turns)]
    assert got == [w.text for w in ws]                                   # same words, same order
    assert all(cues[i].end <= cues[i + 1].start + 1e-6 for i in range(len(cues) - 1))


def test_voice_note_alone_does_not_split_a_line():
    """'soft voice' / 'speaking fast' describe HOW it is said, they are not an emotion change."""
    ws = [W(f"w{i}", i * 0.6, i * 0.6 + 0.5) for i in range(6)]
    a, b = EmotionSpan(0, 1.8, "normal", 0.0, voice=["soft voice"]), EmotionSpan(1.8, 5, "normal", 0.0, voice=["speaking fast"])
    cues, turns = lines(ws, {"Speaker 1": [a, b]})
    assert len(cues) == 1


def test_short_emotion_flicker_is_ignored():
    ws = [W(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(8)]
    spans = {"Speaker 1": [NORMAL(0, 1.9), ANGRY(1.9, 2.2), NORMAL(2.2, 5)]}      # 0.3 s blip < MIN_EMO_RUN
    cues, _ = lines(ws, spans)
    assert len(cues) == 1


# ---------------------------------------------------------------- Emotion Raw
def test_emotion_raw_is_separate_from_translation_and_roundtrips():
    r = ER.make("Angry", "High", 0.9)
    c = S.Cue(0, 2, "你好", "សួស្តី")
    ER.set_on_cue(c, r)
    assert c.translation == "សួស្តី" and json.loads(c.emotion_raw)["label"] == "Angry"
    assert c.emotion.startswith("very angry")
    back = ER.of_cue(c)
    assert back.label == "Angry" and back.intensity == "High"
    for f in ("label", "intensity", "vocal_performance", "speed", "pitch", "loudness", "pause_behavior", "emphasis", "delivery_style", "confidence"):
        assert f in json.loads(c.emotion_raw)


def test_8_missing_model_or_low_confidence_means_normal():
    assert ER.from_span(None).label == "Normal"
    assert ER.from_span(EmotionSpan(0, 3, "normal", 0.0)).label == "Normal"
    assert ER.from_span(EmotionSpan(0, 3, "angry", 0.4)).label == "Normal"        # below the threshold: never guessed
    assert ER.make("Angry", confidence=0.2).label == "Normal"
    assert ER.from_legacy("").label == "Normal"
    assert ER.of_cue(S.Cue(0, 1, "x")).label == "Normal"
    assert ER.from_json("not json") is None


def test_old_emotion_text_still_works():
    assert ER.from_legacy("very angry, loud voice").label == "Angry"
    assert ER.from_legacy("very angry, loud voice").intensity == "High"
    assert ER.from_legacy("normal, soft voice").loudness == "Quiet"
    assert ER.from_legacy("sad → angry").label == "Sad"                           # first emotion of a chain
    assert ER.from_legacy("screaming").label == "Shouting"
    assert ER.legacy_string(ER.from_legacy("crying")) == "crying"


def test_build_lines_fills_emotion_raw():
    ws = [W("a", 0, .5), W("b", .6, 1.1)]
    cues, _ = lines(ws, {"Speaker 1": [EmotionSpan(0, 3, "angry", 0.9)]})
    assert ER.of_cue(cues[0]).label == "Angry" and cues[0].emotion_raw


def test_manual_raw_is_not_overwritten_by_audio_measures():
    sr = 16000
    rng = np.random.default_rng(0)
    y = np.concatenate([rng.normal(0, a, sr * 2) for a in (0.02, 0.02, 0.02, 0.4)]).astype("float32")   # 4th line much louder
    cues = [S.Cue(i * 2, i * 2 + 2, "one two three", speaker="A") for i in range(4)]
    ER.refine_with_audio(cues, y, sr)
    assert ER.of_cue(cues[3]).loudness == "Loud" and ER.of_cue(cues[0]).loudness == "Normal"
    m = ER.make("Calm"); ER.set_on_cue(cues[3], m, manual=True)
    ER.refine_with_audio(cues, y, sr)
    assert ER.of_cue(cues[3]).label == "Calm" and ER.of_cue(cues[3]).loudness == "Quiet"   # hand edit kept


def test_old_project_without_emotion_raw_loads(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"version": 2, "video": "v.mp4", "cues": [{"start": 0, "end": 1, "text": "a", "emotion": "angry"}], "voices": {}}))
    _, cues, _ = S.load_project(p)
    assert cues[0].emotion_raw == "" and ER.of_cue(cues[0]).label == "Angry"
    S.save_project(p, "v.mp4", cues)
    assert "emotion_raw" in p.read_text()


# ---------------------------------------------------------------- 7 / 9 providers
def test_7_same_character_sad_to_angry_keeps_the_same_voice():
    sad, angry = ER.make("Sad", "High", 0.9), ER.make("Angry", "High", 0.9)
    for r in (sad, angry):
        rate, pitch, vol = r.prosody()
        assert abs(rate) <= ER.MAX_RATE and abs(pitch) <= ER.MAX_PITCH and abs(vol) <= ER.MAX_VOL     # bounded -> identity safe
    assert sad.prosody() != angry.prosody()                                       # but the delivery really differs
    assert TP.edge_args(sad, 1.0)[0] < 1.0 < TP.edge_args(angry, 1.0)[0]
    calls = []
    import app.translator.khmer_tts as K
    orig = K.synthesize
    K.synthesize = lambda text, out, voice, speed=1.0, pitch_hz=0, volume_pct=0: calls.append((voice, speed, pitch_hz, volume_pct)) or out
    try:
        TP.synth_edge("x", "o.mp3", "km-KH-PisethNeural", 1.0, sad)
        TP.synth_edge("x", "o.mp3", "km-KH-PisethNeural", 1.0, angry)
    finally:
        K.synthesize = orig
    assert {c[0] for c in calls} == {"km-KH-PisethNeural"}                          # voice name never changes
    assert TP.clone_style(sad) != TP.clone_style(angry)                           # clone: same reference, only the style text differs


def test_9_provider_without_emotion_support_says_so():
    msg = TP.limitation("edge", ER.make("Angry", "High", 0.9))
    assert "cannot act" in msg and "Angry" in msg
    assert TP.limitation("edge", ER.make()) == ""                                 # Normal: nothing to warn about
    assert TP.limitation("clone", ER.make("Angry", "High", 0.9)) == ""
    assert TP.EDGE.emotion_style is False and TP.CLONE.emotion_style is True
    assert TP.edge_args(ER.make("Angry", "High", .9), 1.0, enabled=False) == (1.0, 0, 0)      # switched off = unchanged


def test_normal_emotion_leaves_the_request_unchanged():
    assert TP.edge_args(ER.make(), 1.2) == (1.2, 0, 0)
    assert TP.edge_args(None, 1.0) == (1.0, 0, 0)


def test_edge_tts_receives_pitch_and_volume(monkeypatch):
    import app.translator.khmer_tts as K
    seen = {}
    class FakeComm:
        def __init__(self, text, voice, **kw): seen.update(kw)
        async def save(self, path): Path(path).write_bytes(b"x" * 10)
    import edge_tts
    monkeypatch.setattr(edge_tts, "Communicate", FakeComm)
    K.synthesize("សួស្តី", "/tmp/_t.mp3", "km-KH-SreymomNeural", 1.1, pitch_hz=4, volume_pct=-10)
    assert seen == {"rate": "+10%", "pitch": "+4Hz", "volume": "-10%"}
    seen.clear()
    K.synthesize("សួស្តី", "/tmp/_t.mp3", "km-KH-SreymomNeural", 1.0)
    assert seen == {"rate": "+0%"}                                                 # old behaviour untouched


# ---------------------------------------------------------------- 10 export sync
def _tone(path, secs, f=440, sr=24000):
    t = np.arange(int(secs * sr)) / sr
    pcm = (0.5 * np.sin(2 * np.pi * f * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
def test_10_export_every_clip_starts_at_its_line_and_overruns_are_reported(tmp_path):
    from app.translator import voice_synchronizer as VS
    cues = [S.Cue(1.0, 3.0, "a"), S.Cue(4.0, 5.0, "b"), S.Cue(6.0, 6.6, "c")]
    for i, (c, d) in enumerate(zip(cues, (1.5, 0.8, 3.0))):                          # 3rd clip is far too long for its slot
        c.audio = str(tmp_path / f"c{i}.wav"); _tone(c.audio, d)
    out = tmp_path / "track.wav"
    over = VS.build_voice_track(cues, 8.0, out)
    with wave.open(str(out), "rb") as w:
        y = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float); sr = w.getframerate()
    for c in cues:
        a, b = int(c.start * sr), int((c.start + 0.3) * sr)
        assert np.abs(y[a:b]).mean() > 1000                                          # voice present right at the line start
        assert np.abs(y[int((c.start - 0.3) * sr):int((c.start - 0.05) * sr)]).mean() < 100   # silence before it
    assert 2 in over and VS.LAST_REPORT and VS.LAST_REPORT[0]["line"] == 3
    warns = VS.timing_report(cues, 8.0)
    assert any("Line 3" in w for w in warns)


def test_timing_report_flags_overlap_and_past_end():
    from app.translator import voice_synchronizer as VS
    w = VS.timing_report([S.Cue(0, 3, "a"), S.Cue(2, 4, "b"), S.Cue(9, 12, "c")], 10.0)
    assert any("overlap" in x for x in w) and any("after the video" in x for x in w)
