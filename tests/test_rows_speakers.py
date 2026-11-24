from app.translator import script_builder as SB
from app.translator.script_builder import Word
from app.translator.subtitles import Cue
from app.translator.speakers import SpeakerBook, SpeakerError


def W(t, a, b, sp): 
    w = Word(t, a, b); w.speaker = sp; return w


def test_default_silence_is_3s():
    assert SB.DEFAULT_SILENCE_S == 3.0
    from app.shared.settings import DEFAULTS
    assert DEFAULTS["split_silence_s"] == 3.0


def test_silence_threshold_splits_only_at_3s():
    ws = [W("a", 0, 1, "A"), W("b", 3.5, 4, "A"), W("c", 7.5, 8, "A")]   # gaps 2.5 s and 3.5 s
    assert [len(t) for t in SB.split_turns(ws)] == [2, 1]
    assert [len(t) for t in SB.split_turns(ws, break_on_silence=2.0)] == [1, 1, 1]   # user-changeable
    assert [len(t) for t in SB.split_turns(ws, break_on_silence=0)] == [3]           # off


def test_speaker_change_always_new_row_and_never_merged():
    ws = [W("a", 0, .3, "A"), W("b", .4, .6, "B"), W("c", .7, .9, "A")]
    turns = SB.merge_tiny_turns(SB.split_turns(ws))
    assert [t[0].speaker for t in turns] == ["A", "B", "A"]        # tiny rows of different speakers are NOT merged


def test_tiny_rows_of_same_speaker_are_merged_but_not_across_long_gap():
    ws = [W("a", 0, 1.5, "A"), W("b", 1.6, 1.7, "A")]
    # force a split between them via a 0.1 s threshold, then merge the tiny one back
    parts = SB.split_turns(ws, break_on_silence=0.05)
    assert len(parts) == 2
    assert len(SB.merge_tiny_turns(parts)) == 1
    far = [W("a", 0, 1.5, "A"), W("b", 6, 6.1, "A")]
    assert len(SB.merge_tiny_turns(SB.split_turns(far, break_on_silence=3.0))) == 2


def test_timestamps_preserved_in_cues():
    ws = [W("hello", 1.25, 1.75, "A"), W("world", 1.8, 2.3, "A"), W("yo", 9.0, 9.4, "B")]
    ws[0].uncertain = True; ws[0].speaker_conf = 0.3
    cues = SB.build_lines(SB.split_turns(ws), lang="en")
    assert (cues[0].start, cues[0].end) == (1.25, 2.3) and (cues[1].start, cues[1].end) == (9.0, 9.4)
    assert cues[0].speaker_uncertain and cues[0].speaker_conf == 0.3 and not cues[1].speaker_uncertain


def mk():
    cues = [Cue(0, 1, "a", speaker="S1"), Cue(1, 2, "b", speaker="S2"), Cue(2, 3, "c", speaker="S1"), Cue(3, 4, "d", speaker="S1")]
    b = SpeakerBook().sync_from_cues(cues)
    return b, cues


def test_rename_merge_split_reassign():
    b, cues = mk()
    b.rename("S1", "Lin"); assert b.get("S1").label() == "Lin"
    try: b.rename("S2", "Lin"); assert False
    except SpeakerError: pass
    nid = b.split("S1", [3], cues)
    assert cues[3].speaker == nid and cues[0].speaker == "S1"
    assert b.reassign([1], "S1", cues) == 1 and cues[1].speaker == "S1" and not cues[1].speaker_uncertain
    assert b.merge(nid, "S1", cues) == 1 and nid not in b.speakers and all(c.speaker == "S1" for c in cues)


def test_lock_blocks_destructive_actions():
    b, cues = mk()
    b.lock("S1")
    for fn in (lambda: b.merge("S1", "S2", cues), lambda: b.split("S1", [0], cues), lambda: b.rename("S1", "x"),
               lambda: b.reassign([0], "S2", cues)):
        try: fn(); assert False
        except SpeakerError: pass
    b.unlock("S1"); b.merge("S1", "S2", cues)


def test_voice_identity_consistent_and_restore():
    import tempfile, os
    b, cues = mk()
    b.set_voice("S1", voice="km-KH-PisethNeural", gender="male", voice_profile={"speed": 1.1})
    cues[2].voice_name = "km-KH-SreymomNeural"          # a row that drifted
    assert b.voice_conflicts(cues) == [2]
    b.apply_to_cues(cues)
    assert {c.voice_name for c in cues if c.speaker == "S1"} == {"km-KH-PisethNeural"} and not b.voice_conflicts(cues)
    p = os.path.join(tempfile.mkdtemp(), "sp.json"); b.save(p)
    b2 = SpeakerBook.load(p)
    assert b2.get("S1").voice == "km-KH-PisethNeural" and b2.get("S1").voice_profile == {"speed": 1.1}


def test_similar_speakers_are_only_suggested():
    b, cues = mk()
    b.get("S1").embedding = [1, 0.1]; b.get("S2").embedding = [1, 0.12]
    assert b.suggest_merges() and set(b.speakers) == {"S1", "S2"}      # still two speakers


def test_rescan_keeps_locked_speaker_settings():
    b, _ = mk()
    b.set_voice("S1", voice="v1"); b.get("S1").voice_type = "Adult Men"; b.lock("S1")
    b.apply_detection(["S1"], embeddings={"S1": [9, 9]}, voice_types={"S1": "Girl"})
    assert b.get("S1").voice_type == "Adult Men" and b.get("S1").embedding == []
