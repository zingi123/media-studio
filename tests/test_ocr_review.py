from app.translator import ocr_fix as OF
from app.translator.subtitles import Cue


def cues():
    return [Cue(0, 2, "你好吗"), Cue(3, 5, "(angry) 我不去了"), Cue(6, 8, "完全不同的话"), Cue(9, 11, "没有字幕")]


def ocr():
    return [Cue(0, 2, "你好吗?"), Cue(3, 5, "我不去啦"), Cue(6, 8, "这是另一句")]


def test_default_never_replaces_text_only_flags_conflicts_and_keeps_times():
    c = cues(); before = [(x.start, x.end, x.text) for x in c]
    r = OF.collect_conflicts(c, ocr())
    assert [(x.start, x.end, x.text) for x in c] == before                      # NOTHING replaced silently
    assert r == {"conflicts": 2, "auto_applied": 0, "same": 1, "no_ocr": 1}
    assert c[1].text_conflict and c[1].ocr_text == "(angry) 我不去啦" and 0.5 < c[1].ocr_sim < 1   # emotion tag kept in the suggestion
    assert c[2].text_conflict and c[2].ocr_sim < 0.5 and not c[0].text_conflict and not c[3].text_conflict


def test_user_decisions_and_times_preserved():
    c = cues(); OF.collect_conflicts(c, ocr())
    OF.resolve(c[1], use_ocr=True); OF.resolve(c[2], use_ocr=False)
    assert c[1].text == "(angry) 我不去啦" and c[2].text == "完全不同的话" and not c[1].text_conflict and not c[2].text_conflict
    assert (c[1].start, c[1].end) == (3, 5)


def test_auto_apply_only_for_similar_and_rest_stay_for_review():
    c = cues(); r = OF.collect_conflicts(c, ocr(), auto_apply=True)
    assert r["auto_applied"] == 1 and c[1].text == "(angry) 我不去啦" and r["conflicts"] == 1 and c[2].text_conflict


def test_accept_all_similar_skips_low_similarity_and_midline_tags():
    c = cues(); c.append(Cue(12, 14, "我(sigh)不去")); o = ocr() + [Cue(12, 14, "我不去啦")]
    OF.collect_conflicts(c, o)
    n = OF.accept_all_similar(c, 0.5)
    assert n == 1 and c[1].text.endswith("啦") and c[2].text == "完全不同的话" and c[4].text == "我(sigh)不去" and c[4].text_conflict   # tag inside the line -> manual decision
