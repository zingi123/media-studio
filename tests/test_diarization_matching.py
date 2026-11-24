from app.translator.diarization import (Turn, DiarizationResult, match_interval, match_segments,
                                        parse_pipeline_output, check_model_access)
from app.translator.script_builder import Word, assign_speakers, smooth_speakers


def T(a, b, s): return Turn(a, b, s)


def test_overlap_not_nearest():
    turns = [T(0, 4, "A"), T(3.5, 8, "B")]
    m = match_interval(4.2, 6.0, turns)          # inside B only
    assert m.speaker == "B" and not m.uncertain and m.confidence > 0.9


def test_overlapping_speech_flagged():
    turns = [T(0, 5, "A"), T(2, 5, "B")]
    m = match_interval(2.0, 5.0, turns)
    assert m.speaker in ("A", "B") and "A" in (m.speaker, *m.overlap_with) and m.overlap_with and m.uncertain


def test_no_turn_is_uncertain_not_guessed():
    m = match_interval(10, 11, [T(0, 2, "A")])
    assert m.speaker == "" and m.uncertain and m.confidence == 0


def test_short_word_and_pause_words():
    turns = [T(0, 2, "A"), T(5, 7, "B")]
    ws = [Word("a", 0.5, 0.6), Word("p", 3.0, 3.2), Word("b", 5.1, 5.2)]
    assign_speakers(ws, [(t.start, t.end, t.speaker) for t in turns])
    assert [w.speaker for w in ws] == ["A", "A", "B"]
    assert ws[1].uncertain and not ws[0].uncertain and not ws[2].uncertain   # the word in the pause is visible for review


def test_match_segments_order():
    res = DiarizationResult([T(0, 3, "A"), T(3, 6, "B")], [T(0, 3, "A"), T(3, 6, "B")])
    r = match_segments([(0.1, 2.9), (3.1, 5.9)], res)
    assert [m.speaker for m in r] == ["A", "B"]


def test_overlap_regions():
    res = DiarizationResult([T(0, 5, "A"), T(2, 6, "B")], [])
    assert res.overlaps() == [(2, 5, ["A", "B"])]


def test_parse_pyannote4_output_and_3x_annotation():
    class Seg:  # minimal pyannote Segment
        def __init__(s, a, b): s.start, s.end = a, b
    class Ann:
        def __init__(s, items): s.items = items
        def itertracks(s, yield_label=False): return [(Seg(a, b), None, l) for a, b, l in s.items]
    class Out:
        speaker_diarization = Ann([(0, 2, "SPEAKER_00"), (1, 3, "SPEAKER_01")])
        exclusive_speaker_diarization = Ann([(0, 1, "SPEAKER_00"), (1, 3, "SPEAKER_01")])
        speaker_embeddings = [[1, 0], [0, 1]]
    t, ex, emb = parse_pipeline_output(Out())
    assert len(t) == 2 and len(ex) == 2 and set(emb) == {"SPEAKER_00", "SPEAKER_01"}
    t2, ex2, emb2 = parse_pipeline_output(Ann([(0, 1, "X")]))
    assert len(t2) == 1 and emb2 == {}


def test_access_check_without_token_gives_clear_message():
    r = check_model_access("")
    assert not r.ok and r.stage == "token" and "token" in r.message.lower()


def test_smooth_marks_changes_uncertain():
    ws = [Word(str(i), i * 0.3, i * 0.3 + 0.2) for i in range(5)]
    for w, s in zip(ws, ["A", "A", "B", "A", "A"]):
        w.speaker = s
    smooth_speakers(ws)
    if ws[2].speaker == "A":
        assert ws[2].uncertain
