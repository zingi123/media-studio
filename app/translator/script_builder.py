"""
Smart script builder  (speech words + speakers + emotion  ->  dubbing script lines).

RULES (as requested):
 1. A NEW LINE (new time) starts when the speaker / voice / EMOTION changes (emotion = separate Emotion field, never inside the text).
    The SAME character also starts a new line (new time) after a LONG silence (default 3 s) - one TTS clip is
    placed at the start time of its line, so a long pause inside a line would make the voice drift away from the
    picture; the new line starts exactly when the character speaks again. A very long line (default 15 s) is also cut,
    at its biggest pause. Both limits can be changed (0 = off).
 2. The text of a line is continuous. Spaces and punctuation are NOT guessed: they come from the REAL silence
    between the words in the video (word timestamps), so the TTS pauses where the character paused:
        gap < 0.15 s  nothing            0.4 - 0.8 s   ,
        0.15 - 0.4 s  space              0.8 - 1.5 s   end mark  (។ for Khmer, . otherwise)
        > 1.5 s       end mark + [pause Ns]
    The end mark becomes  !  (loud / shouted),  ?  (rising pitch)  or  ...  (trailing off) when the audio says so.
 3. An emotion is written ONLY when the model is confident (default >= 70 %; screaming/crying >= 50 % because those
    scores are naturally lower). When it is not confident the line stays "normal" and only a light description of
    HOW it is said is added, e.g. (normal, soft voice) / (normal, speaking fast). Weak guesses never make the story messy.
    When the emotion changes, a new line starts - the tag lives only in the Emotion field, the text stays clean.

Everything here is plain Python (no model needed); the audio-based extras (loudness / pitch) are optional.
"""
import re
from dataclasses import dataclass, field
from ..shared.utils import fmt_time, parse_time
from .subtitles import Cue

# ---------------------------------------------------------------- tunables
DEFAULT_SILENCE_S = 3.0   # s   same speaker silent this long -> a new row (user-changeable; 0 = off)
GAP_SPACE = 0.15      # s   silence that becomes a space
GAP_COMMA = 0.40      # s   silence that becomes ","
GAP_END = 0.80        # s   silence that becomes an end mark
GAP_LONG = 1.50       # s   silence that also gets a [pause Ns] tag
MIN_TURN = 0.6        # s   a speaker "blip" shorter than this (<= 2 words) between the SAME speaker is merged
EMO_MIN = 0.70        # emotion label is written only at/above this score (0..1)
EVENT_MIN_TAG = 0.50  # screaming / crying ... are written only at/above this score (AST scores run lower)
VERY = 0.85           # at/above this the word "very" is added
MIN_EMO_RUN = 0.5     # s   an emotion that lasts less than this inside a line is ignored (flicker)
SPACED_LANGS = {"en", "vi", "ko", "fr", "de", "es", "id", "ms"}   # languages whose words are separated by spaces

TAG_RE = re.compile(r"\([^()]*\)|\[pause [0-9.]+s\]", re.I)


# ---------------------------------------------------------------- data
@dataclass
class Word:
    text: str
    start: float
    end: float
    speaker: str = ""
    energy: str = ""      # "high" | "low" | ""   (from the audio, optional)
    rise: bool = False    # pitch rises at the end of the word (question)
    trail: bool = False   # voice fades out (unfinished / weak)
    speaker_conf: float = 1.0      # 0..1 how sure the speaker assignment is (diarization overlap share)
    uncertain: bool = False        # True = needs manual review (low confidence / overlapping speech / no diarization turn)
    overlap_with: tuple = ()       # other speakers talking at the same time


@dataclass
class EmotionSpan:
    start: float
    end: float
    label: str = "normal"                     # normal angry sad happy scared surprised disgusted
    score: float = 0.0                        # 0..1
    events: dict = field(default_factory=dict)  # {"screaming": 0.3, "crying": 0.35, ...}
    voice: list = field(default_factory=list)   # light descriptions of HOW it is said: ["soft voice", "speaking slowly"]


def plain_text(text: str) -> str:
    """Script text WITHOUT emotion tags / pause tags (what a translator or a subtitle should see)."""
    t = TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", t).strip()


def gender_of(voice_type: str) -> str:
    v = (voice_type or "").lower()
    if any(k in v for k in ("women", "woman", "girl", "female")):
        return "female"
    if any(k in v for k in ("men", "man", "boy", "male")):
        return "male"
    return ""


# ---------------------------------------------------------------- 1. speakers -> turns
def assign_speakers(words, segments, default="Speaker 1", uncertain_below=0.5):
    """segments = [(start, end, 'Speaker 1'), ...] from diarization (overlaps allowed).
    Each word gets the speaker with the largest TEMPORAL OVERLAP with the word (not the nearest timestamp).
    Fills word.speaker_conf / word.uncertain / word.overlap_with. A word that overlaps no diarization turn
    (a pause between turns) keeps the previous speaker but is marked uncertain, so it is visible for review."""
    from .diarization import Turn, match_interval
    turns = [Turn(a, b, s) for a, b, s in (segments or [])]
    prev = default
    for w in words:
        if not turns:
            w.speaker, w.speaker_conf, w.uncertain, w.overlap_with = default, 1.0, False, ()
            continue
        m = match_interval(w.start, w.end, turns, uncertain_below=uncertain_below)
        if m.speaker:
            w.speaker, w.speaker_conf, w.uncertain, w.overlap_with = m.speaker, m.confidence, m.uncertain, tuple(m.overlap_with)
        else:
            w.speaker, w.speaker_conf, w.uncertain, w.overlap_with = prev, 0.0, True, ()
        prev = w.speaker
    return words


def smooth_speakers(words, min_turn=MIN_TURN):
    """A speaker that appears for < min_turn seconds (<= 2 words) between two runs of the SAME other speaker is a
    diarization glitch -> give it to that speaker. Keeps real short interruptions that are not sandwiched."""
    runs = []
    for i, w in enumerate(words):
        if runs and runs[-1][0] == w.speaker:
            runs[-1][2] = i
        else:
            runs.append([w.speaker, i, i])
    for k in range(1, len(runs) - 1):
        sp, a, b = runs[k]
        if runs[k - 1][0] == runs[k + 1][0] != sp and (b - a + 1) <= 2 and words[b].end - words[a].start < min_turn:
            for i in range(a, b + 1):
                words[i].speaker = runs[k - 1][0]
                words[i].uncertain = True            # glitch-fix is a heuristic -> keep it visible for review
    return words


def _split_long(turn, max_secs):
    """A line longer than max_secs is cut at its biggest pause (the cut must leave >= 30% of the line on each side)."""
    if max_secs <= 0 or len(turn) < 2 or turn[-1].end - turn[0].start <= max_secs:
        return [turn]
    total = turn[-1].end - turn[0].start
    best, gap = None, -1.0
    for i in range(1, len(turn)):
        left = turn[i - 1].end - turn[0].start
        if 0.3 * total <= left <= 0.7 * total and turn[i].start - turn[i - 1].end > gap:
            best, gap = i, turn[i].start - turn[i - 1].end
    if best is None:                                   # no pause in the middle zone: cut at the word nearest the middle
        best = min(range(1, len(turn)), key=lambda i: abs((turn[i - 1].end - turn[0].start) - total / 2))
    return _split_long(turn[:best], max_secs) + _split_long(turn[best:], max_secs)


def split_turns(words, speaker_types=None, break_on_silence=DEFAULT_SILENCE_S, max_secs=0.0):
    """-> list of word lists. New turn when the speaker or its voice type changes, or (same character) when the
    silence between two words is >= break_on_silence seconds, or a turn is longer than max_secs (0 = off)."""
    speaker_types = speaker_types or {}
    turns = []
    for w in sorted(words, key=lambda x: x.start):
        if turns:
            p = turns[-1][-1]
            same = (p.speaker == w.speaker and speaker_types.get(p.speaker, "") == speaker_types.get(w.speaker, ""))
            if same and not (break_on_silence and w.start - p.end >= break_on_silence):
                turns[-1].append(w)
                continue
        turns.append([w])
    return [part for t in turns for part in _split_long(t, max_secs)]


def merge_tiny_turns(turns, min_secs=0.6, min_words=2, max_gap=1.0):
    """Avoid tiny rows: a turn shorter than min_secs AND with fewer than min_words words is joined to the
    neighbouring turn of the SAME speaker when the gap between them is < max_gap. Different speakers are NEVER merged,
    and turns separated by a long silence (>= max_gap) stay separate."""
    out = []
    for t in turns:
        small = (t[-1].end - t[0].start) < min_secs and len(t) < min_words
        if small and out and out[-1][-1].speaker == t[0].speaker and t[0].start - out[-1][-1].end < max_gap:
            out[-1] = out[-1] + t
        else:
            out.append(t)
    # a tiny FIRST turn of a speaker can also be absorbed by the NEXT turn of the same speaker
    res, i = [], 0
    while i < len(out):
        t = out[i]
        small = (t[-1].end - t[0].start) < min_secs and len(t) < min_words
        if small and i + 1 < len(out) and out[i + 1][0].speaker == t[0].speaker and out[i + 1][0].start - t[-1].end < max_gap:
            out[i + 1] = t + out[i + 1]
        else:
            res.append(t)
        i += 1
    return res


def split_by_emotion(turns, emotion_spans, emo_min=None, event_min=None):
    """turn នីមួយៗ → កាត់ជាជួរថ្មី (ពេលថ្មី) ពេល emotion/សំឡេងប្តូរ → ១ ជួរ = ១ emotion"""
    out = []   # លទ្ធផល
    for tw in turns:   # រាល់ turn
        sp = tw[0].speaker   # អ្នកនិយាយ
        spans = emotion_spans.get(sp, []) if isinstance(emotion_spans, dict) else (emotion_spans or [])   # span របស់គាត់
        tags = _word_tags(tw, spans, emo_min, event_min, voice=False)   # emotion only: "soft voice"/"speaking fast" notes never split a line
        cur = [tw[0]]   # ជួរបច្ចុប្បន្ន
        for i in range(1, len(tw)):   # ពាក្យបន្ទាប់
            if tags[i] != tags[i - 1]:   # emotion ប្តូរ
                out.append(cur); cur = []   # បិទជួរ ចាប់ថ្មី
            cur.append(tw[i])   # ដាក់ពាក្យ
        out.append(cur)   # ជួរចុងក្រោយ
    return out


# ---------------------------------------------------------------- 2. optional prosody (loud / question / fading)
def annotate_prosody(y, words, sr=16000):
    """Fill energy / rise / trail from the real audio y (float numpy, mono). Silent no-op if librosa is missing.
    Thresholds are heuristics - they only decide ! ? ... at sentence ends, and can be tuned."""
    try:
        import numpy as np
        import librosa
    except Exception:
        return words
    rms_db = {}
    for i, w in enumerate(words):
        seg = y[int(w.start * sr):int(w.end * sr)]
        if len(seg) < 200:
            continue
        rms_db[i] = 20 * np.log10(float(np.sqrt(np.mean(seg ** 2))) + 1e-8)
    base = {}
    for sp in {w.speaker for w in words}:
        v = [rms_db[i] for i, w in enumerate(words) if w.speaker == sp and i in rms_db]
        base[sp] = float(np.median(v)) if v else None
    for i, w in enumerate(words):
        if i not in rms_db or base.get(w.speaker) is None:
            continue
        d = rms_db[i] - base[w.speaker]
        w.energy = "high" if d >= 6 else "low" if d <= -6 else ""
        seg = y[int(w.start * sr):int(w.end * sr)]
        n = len(seg)
        if n >= int(0.15 * sr):
            third = n // 3
            w.trail = (20 * np.log10(float(np.sqrt(np.mean(seg[-third:] ** 2))) + 1e-8)
                       < 20 * np.log10(float(np.sqrt(np.mean(seg[:third] ** 2))) + 1e-8) - 8) and w.energy == "low"
            try:
                f0 = librosa.yin(seg.astype("float32"), fmin=70, fmax=500, sr=sr, frame_length=1024)
                if len(f0) >= 6:
                    k = len(f0) // 3
                    a, b = float(np.median(f0[:2 * k])), float(np.median(f0[-k:]))
                    w.rise = b > a * 1.15
            except Exception:
                pass
    return words


# ---------------------------------------------------------------- 3. punctuation from real silence
def end_mark(lang_style, word):
    """End mark for a sentence that finishes on `word` (a Word, may carry prosody)."""
    base = "។" if lang_style == "km" else "。" if lang_style == "cjk" else "."
    if word.energy == "high":
        return "!"
    if word.rise:
        return "?"
    if word.trail:
        return "..."
    return base


def gap_mark(gap, prev, lang_style, spaced):
    """-> text put between two words. `prev` = the word before the silence."""
    if gap < GAP_SPACE:
        return " " if spaced else ""
    if gap < GAP_COMMA:
        return " "
    if gap < GAP_END:
        return ", "
    m = end_mark(lang_style, prev) + " "
    if gap >= GAP_LONG:
        m = m.rstrip() + f" [pause {gap:.1f}s] "
    return m


def _lang_style(lang):
    lang = (lang or "").lower()
    return "km" if lang.startswith("km") else "cjk" if lang[:2] in ("zh", "ja") else "other"


# ---------------------------------------------------------------- 4. emotion tags
def intensity(score):
    """score -> '' or 'very '. (Anything below the confidence thresholds is not written at all.)"""
    return "very " if score >= VERY else ""


def tag_of(span, emo_min=None, event_min=None, voice=True):
    """EmotionSpan -> 'angry, loud voice' | 'normal, soft voice' | ''  (plain normal speech has no tag)."""
    emo_min = EMO_MIN if emo_min is None else emo_min
    event_min = EVENT_MIN_TAG if event_min is None else event_min
    if span is None:
        return ""
    parts = []
    if span.label not in ("normal", "neutral", "") and span.score >= emo_min:
        parts.append(f"{intensity(span.score)}{span.label}")
    for ev, sc in sorted(span.events.items(), key=lambda t: -t[1]):
        if sc >= event_min and len(parts) < 3:
            parts.append(f"{intensity(sc)}{ev}")
    if parts:                                           # confident: emotion (+ at most one voice note)
        return ", ".join(parts + (span.voice[:1] if voice else []))
    if not voice:                                       # splitting key: a mere voice note ("soft voice") is NOT an emotion change
        return ""
    return ("normal, " + ", ".join(span.voice[:2])) if span.voice else ""   # not confident: normal + how it sounds


def _span_at(spans, t):
    for s in spans or []:
        if s.start <= t < s.end:
            return s
    return min(spans, key=lambda s: min(abs(t - s.start), abs(t - s.end)), default=None) if spans else None


def _word_tags(words, spans, emo_min=None, event_min=None, voice=True):
    """Tag per word, with flicker removed (a tag that lasts < MIN_EMO_RUN is replaced by the previous one)."""
    tags = [tag_of(_span_at(spans, (w.start + w.end) / 2), emo_min, event_min, voice) for w in words]
    runs = []
    for i, t in enumerate(tags):
        if runs and runs[-1][0] == t:
            runs[-1][2] = i
        else:
            runs.append([t, i, i])
    if len(runs) > 1 and words[runs[0][2]].end - words[runs[0][1]].start < MIN_EMO_RUN:
        runs[0][0] = runs[1][0]   # run ដំបូងខ្លីពេក ប្រើ emotion បន្ទាប់
    for k in range(1, len(runs)):
        t, a, b = runs[k]
        if words[b].end - words[a].start < MIN_EMO_RUN:
            runs[k][0] = runs[k - 1][0]
    out = []
    for t, a, b in runs:
        out += [t] * (b - a + 1)
    return out


# ---------------------------------------------------------------- 5. lines
def _join_words(words, tags, lang_style, spaced):
    text = ""   # អត្ថបទដើម
    for i, w in enumerate(words):
        text += w.text.strip()   # អត្ថបទសុទ្ធ — emotion ទៅជួរ Emotion វិញ
        if i + 1 < len(words):
            text += gap_mark(words[i + 1].start - w.end, w, lang_style, spaced)
        else:
            if not re.search(r"[.!?។。…]$", text):
                text += end_mark(lang_style, w)
    text = re.sub(r"[ ]{2,}", " ", text).strip()
    return text


def build_lines(turns, emotion_spans=None, speaker_types=None, lang="auto", emo_min=None, event_min=None):
    """turns (from split_turns) -> [Cue]. Cue.text holds the continuous script with punctuation + emotion tags.
    emotion_spans: {speaker: [EmotionSpan]} or a plain list (applies to everybody)."""
    speaker_types = speaker_types or {}
    style = _lang_style(lang)
    spaced = (lang or "").lower()[:2] in SPACED_LANGS
    cues = []
    for tw in turns:
        sp = tw[0].speaker
        spans = emotion_spans.get(sp, []) if isinstance(emotion_spans, dict) else (emotion_spans or [])
        tags = _word_tags(tw, spans, emo_min, event_min)
        text = _join_words(tw, tags, style, spaced)
        seen = []
        for t in tags:
            if (t or "normal") not in seen:
                seen.append(t or "normal")
        vt = speaker_types.get(sp, "")
        from . import emotion_raw as ER                     # structured Emotion Raw of the line (the middle word's span)
        mid = tw[len(tw) // 2]
        raw = ER.from_span(_span_at(spans, (mid.start + mid.end) / 2), EMO_MIN if emo_min is None else emo_min,
                           EVENT_MIN_TAG if event_min is None else event_min)
        ov = sorted({o for w in tw for o in getattr(w, "overlap_with", ())})
        cues.append(Cue(tw[0].start, max(tw[-1].end, tw[0].start + 0.2), text, gender=gender_of(vt),
                        emotion=" → ".join(seen) if seen else "normal", speaker=sp, voice_desc=vt, emotion_raw=raw.to_json(),
                        speaker_conf=round(min(getattr(w, "speaker_conf", 1.0) for w in tw), 3),
                        speaker_uncertain=any(getattr(w, "uncertain", False) for w in tw), overlap_with=",".join(ov)))
    return cues


# ---------------------------------------------------------------- 6. script file  (start-end || text || emotion || speaker, voice)
def _one_line(s):
    return re.sub(r"\s+", " ", str(s or "").replace("||", "|")).strip()


def to_script_txt(cues, which="text", story=None, lang=""):
    """`which`: 'text' (original, with tags) | 'translation'. One line per cue.
    story (story_info dict) -> a "# TITLE / SUMMARY / CHARACTERS" header is written first (lines starting with #)."""
    out = []
    for c in cues:
        body = c.translation if which == "translation" else c.text
        who = ", ".join(x for x in (c.speaker, c.voice_desc) if x)
        act = _one_line(getattr(c, "action", ""))
        out.append(f"{fmt_time(c.start)}-{fmt_time(c.end)} || {_one_line(body)} || {_one_line(c.emotion) or 'normal'}"
                   + (f" || {_one_line(who) or '-'}" if (who or act) else "") + (f" || {act}" if act else ""))
    head = ""
    if story:
        from .story_info import header_text
        head = header_text(story, cues, lang) + "\n"
    return head + "\n".join(out)


_T = r"(\d+(?::\d+){1,2}(?:[.,]\d+)?)"
SCRIPT_LINE = re.compile(r"^\s*" + _T + r"\s*[-\u2013]\s*" + _T + r"\s*\|\|(.*)$")


def parse_script_line(line):
    """'start-end || text || emotion || speaker, voice'  ->  Cue, or None when the line is not in this format."""
    m = SCRIPT_LINE.match(line.replace("\ufeff", ""))
    if not m:
        return None
    parts = [p.strip() for p in m[3].split("||")]
    text = parts[0] if parts else ""
    emotion = parts[1] if len(parts) > 1 else ""
    who = [x.strip() for x in parts[2].split(",") if x.strip() != "-"] if len(parts) > 2 else []
    action = parts[3] if len(parts) > 3 else ""
    speaker = who[0] if who else ""
    voice = ", ".join(who[1:])
    return Cue(parse_time(m[1]), parse_time(m[2]), text, emotion=emotion if emotion.lower() != "normal" else "normal",
               speaker=speaker, voice_desc=voice, action=action, gender=gender_of(voice) or gender_of(speaker))


def parse_script_txt(text):
    return [c for c in (parse_script_line(l) for l in text.replace("\r", "").split("\n")) if c]


def is_script_format(text):
    return any(SCRIPT_LINE.match(l.replace("\ufeff", "")) for l in text.replace("\r", "").split("\n"))
