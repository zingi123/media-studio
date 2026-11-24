import json
import re
from dataclasses import dataclass, asdict
from ..shared.utils import fmt_time, parse_time


@dataclass
class Cue:
    start: float
    end: float
    text: str = ""          # original
    translation: str = ""   # Khmer
    audio: str = ""         # generated TTS file (not part of SRT)
    gender: str = ""        # male / female ក្នុងបន្ទាត់នីមួយៗ
    voice_type: str = ""    # ឈ្មោះ Voice Type (Adult Man ...)
    use_voice: bool = True  # ✔ = បង្កើតសំឡេងខ្មែរ, ☐ = រំលង
    emotion: str = ""       # NEW (Smart Script): "angry, a bit screaming → sad"  ("" = not analysed)
    speaker: str = ""       # NEW: "Speaker 1"
    voice_desc: str = ""    # NEW: detected voice type text, e.g. "Women" / "Girl"
    action: str = ""        # NEW: what the character does on screen, e.g. "face on screen, mouth moving, big movement"
    emotion_raw: str = ""   # NEW: structured Emotion Raw as JSON (see emotion_raw.py). "" = derive from `emotion`. Never part of the translation.
    speaker_conf: float = 1.0        # NEW: 0..1 confidence of the speaker assignment (diarization overlap)
    speaker_uncertain: bool = False  # NEW: True = needs manual review (low confidence / overlapping speech)
    overlap_with: str = ""           # NEW: other speakers talking at the same time, comma separated
    speed: float = 1.0               # NEW: per-row speed of the GENERATED Khmer voice (the video speed is never changed)
    ocr_text: str = ""               # NEW: burned-in subtitle text that DIFFERS from the speech text (suggestion, never applied silently)
    ocr_sim: float = 0.0             # NEW: similarity 0..1 between the speech text and the OCR text
    text_conflict: bool = False      # NEW: True = speech and subtitle disagree -> needs a decision (OCR conflict review)
    voice_name: str = ""             # NEW: TTS voice used for this row (kept the same for a speaker by the Speaker Manager)
    audio_sig: str = ""              # STUDIO: fingerprint of the text/voice/emotion/speed the audio was generated from ("" = unknown / legacy)
    voice_dur: float = 0.0           # STUDIO: real length (s) of the generated voice clip (0 = not measured)
    prosody: str = ""                # NEW: measured performance plan JSON {rate,pitch,vol,want_*} (prosody_match.py); "" = label-based emotion only
    match_score: float = -1.0        # NEW: 0-100 how closely the Khmer voice follows the original performance (-1 = not checked)
    match_note: str = ""             # NEW: why the score is what it is


_TIME = r"((?:\d+:)?\d{1,2}:\d{2}[,.]\d{1,3})"
_RANGE = re.compile(_TIME + r"\s*-->\s*" + _TIME)


def _parse(text: str):
    text = text.replace("\r\n", "\n").replace("\ufeff", "")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.split("\n")]
        for i, l in enumerate(lines):
            m = _RANGE.search(l)
            if m:
                body = "\n".join(x for x in lines[i + 1:] if x.strip())
                body = re.sub(r"<[^>]+>", "", body)
                cues.append(Cue(parse_time(m[1]), parse_time(m[2]), body.strip()))
                break
    return cues


_LINE = re.compile(r"^\s*" + _TIME + r"\s*-\s*" + _TIME + r"\s*\|\s*(.*?)\s*(?:\|\s*(male|female)\s*)?$", re.I)  # start-end| text |gender


def parse_txt(text: str):  # អានឯកសារ script .txt
    cues = []  # បញ្ជីបន្ទាត់
    for l in text.replace("\ufeff", "").splitlines():  # រត់ម្តងមួយបន្ទាត់
        if "||" in l:  # NEW format (Smart Script): start-end || text || emotion || speaker, voice
            from .script_builder import parse_script_line
            c = parse_script_line(l)
            if c:
                cues.append(c)
                continue
        m = _LINE.match(l)  # ផ្គូផ្គងទ្រង់ទ្រាយ
        if m:  # បើត្រូវទ្រង់ទ្រាយ
            cues.append(Cue(parse_time(m[1]), parse_time(m[2]), m[3], gender=(m[4] or "").lower()))  # បង្កើត cue មានភេទ
    return cues  # បន្ទាត់ខុសទ្រង់ទ្រាយត្រូវរំលង


def parse_srt(text: str):
    return _parse(text)


def parse_vtt(text: str):
    return _parse(text)


def load_subtitle_file(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        data = f.read()  # អានអត្ថបទទាំងអស់
    return parse_txt(data) if str(path).lower().endswith(".txt") else _parse(data)  # .txt ប្រើ parser ផ្ទាល់


def _field(c: Cue, which: str):
    return c.translation if which == "translation" else c.text


def to_srt(cues, which="translation") -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{fmt_time(c.start)} --> {fmt_time(c.end)}\n{_field(c, which)}\n")
    return "\n".join(out)


def to_vtt(cues, which="translation") -> str:
    out = ["WEBVTT\n"]
    for c in cues:
        out.append(f"{fmt_time(c.start).replace(',', '.')} --> {fmt_time(c.end).replace(',', '.')}\n"
                   f"{_field(c, which)}\n")
    return "\n".join(out)


def to_txt(cues, which="translation", default="female") -> str:  # សរសេរ script .txt
    return "\n".join(f"{fmt_time(c.start)}-{fmt_time(c.end)}| {' '.join(_field(c, which).split())} |{c.gender or default}" for c in cues)  # start-end| text |gender


def save_subtitle_file(path, cues, which="translation", default="female"):
    p = str(path).lower()  # ឈ្មោះអក្សរតូច
    data = to_vtt(cues, which) if p.endswith(".vtt") else to_txt(cues, which, default) if p.endswith(".txt") else to_srt(cues, which)  # ជ្រើសទ្រង់ទ្រាយ
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)


def save_project(path, video, cues, voices=None):  # voices = {ឈ្មោះ: preset} ដែលបានជ្រើស
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 2, "video": video, "cues": [asdict(c) for c in cues], "voices": voices or {}}, f,
                  ensure_ascii=False, indent=1)


def load_project(path):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    from .voice_presets import normalize_voices  # ឈ្មោះចាស់ → ថ្មី, setting Human ជាប់
    _known = set(Cue.__dataclass_fields__)
    return d.get("video", ""), [Cue(**{k: v for k, v in c.items() if k in _known}) for c in d.get("cues", [])], normalize_voices(d.get("voices", {}))  # ត្រឡប់ voices ផងដែរ


_T2 = r"(\d+(?::\d+){1,2}(?:[.,]\d+)?)"  # mm:ss ឬ hh:mm:ss[,ms]
_SPAN = re.compile(r"^\s*" + _T2 + r"\s*[-–]\s*" + _T2 + r"\s*$")  # Start-End
_NO = {"☐", "0", "false", "no", "n", "off"}  # តម្លៃមានន័យថាមិនធីក


def rows_to_cues(rows):  # rows: [[Start-End, Chinese, Khmer, VoiceType, Voice], ...]
    cues = []  # លទ្ធផល
    for r in rows:  # រត់ម្តងមួយជួរ
        r = [str(x).strip() if x is not None else "" for x in r] + [""] * 5  # បំពេញឱ្យគ្រប់ 5 ជួរឈរ
        m = _SPAN.match(r[0])  # 00:00-00:04
        if not m:  # មិនមែនជួរ time (header ជាដើម)
            continue  # រំលង
        cues.append(Cue(parse_time(m[1]), parse_time(m[2]), r[1], r[2], voice_type=r[3],
                        use_voice=r[4].lower() not in _NO))  # បង្កើត cue
    return cues  # ត្រឡប់បញ្ជី


def import_table(path):  # CSV ឬ Excel
    import csv
    if str(path).lower().endswith((".xlsx", ".xlsm")):  # Excel
        try:
            from openpyxl import load_workbook
        except ImportError as e:
            raise RuntimeError("openpyxl is not installed. Run: pip install openpyxl") from e
        ws = load_workbook(path, read_only=True, data_only=True).active  # សន្លឹកដំបូង
        return rows_to_cues(list(ws.iter_rows(values_only=True)))  # ជួរទាំងអស់
    with open(path, "r", encoding="utf-8-sig", newline="") as f:  # CSV
        return rows_to_cues(list(csv.reader(f)))  # អាន csv


def parse_pasted(text):  # Copy/Paste (tab ពី Excel ឬ comma)
    import csv
    first = text.strip().splitlines()[0] if text.strip() else ""  # ជួរដំបូង
    delim = "\t" if "\t" in first else ","  # ជ្រើស delimiter
    return rows_to_cues(list(csv.reader(text.strip().splitlines(), delimiter=delim)))  # បំលែងជា cue


# ---------------------------------------------------------------- Khmer-only script import
_KC = "\u1780-\u17FF\u19E0-\u19FF"                                     # Khmer + Khmer symbols
_KH = re.compile(rf"[{_KC}\u200b\u200c]+(?:[ \t]+[{_KC}\u200b\u200c]+)*")
_KCORE = re.compile(rf"[{_KC}]")
_T3 = r"(\d+(?::\d+){1,2}(?:[.,]\d+)?)"
_RNG3 = re.compile(_T3 + r"\s*(?:-->|[-\u2013])\s*" + _T3)
_GEN3 = re.compile(r"\b(male|female)\b", re.I)


def khmer_only(line: str) -> str:
    """Keep ONLY Khmer words of a line (numbers, timestamps, Latin/Chinese text, symbols are dropped)."""
    parts = [m.group(0).strip(" \t\u200b\u200c") for m in _KH.finditer(line)]
    return " ".join(p for p in parts if _KCORE.search(p)).strip()


def parse_khmer_script(text: str):
    """Any .txt/.srt/.vtt text -> [(khmer, start|None, end|None, 'male'|'female'|'')], one per line that has Khmer.
    Timestamps / gender tags are only READ (never copied into the Khmer text)."""
    items, pend = [], (None, None)
    for raw in text.replace("\ufeff", "").replace("\r", "").split("\n"):
        if raw.lstrip().startswith("#"):  # NEW: "# TITLE / SUMMARY / CHARACTERS" header of a Smart Script - never a voice line
            continue
        if "||" in raw:  # NEW format: keep the spaces / ។ , ! ? ... the script was built with; drop only (tags) and [pause]
            from .script_builder import parse_script_line, plain_text
            c = parse_script_line(raw)
            if c and _KCORE.search(c.text):
                kh = plain_text(c.text)
                items.append((kh, c.start, c.end, c.gender))
                pend = (None, None)
                continue
        m = _RNG3.search(raw)
        st, en = (parse_time(m[1]), parse_time(m[2])) if m else (None, None)
        kh = khmer_only(raw)
        if not kh:
            if m:
                pend = (st, en)      # srt/vtt: time line comes before its text line
            continue
        if st is None:
            st, en = pend
        pend = (None, None)
        g = _GEN3.search(raw)
        items.append((kh, st, en, g[1].lower() if g else ""))
    return items


# ---------------------------------------------------------------- NEW: SRT / VTT with names (plain SRT / VTT above are unchanged)
def _who(c):
    return c.speaker or ""


def to_srt_rich(cues, which="translation", names=True, emotion=False):
    """SRT where each line starts with the speaker ("Sreyneang: text"); emotion=True adds [angry] before the text."""
    out = []
    for i, c in enumerate(cues, 1):
        t = _field(c, which)
        if which != "translation":
            from .script_builder import plain_text
            t = plain_text(t)
        pre = (f"{_who(c)}: " if names and _who(c) else "") + (f"[{c.emotion.split('→')[0].strip()}] " if emotion and c.emotion not in ("", "normal") else "")
        out.append(f"{i}\n{fmt_time(c.start)} --> {fmt_time(c.end)}\n{pre}{t}\n")
    return "\n".join(out)


def to_vtt_rich(cues, which="translation", story=None):
    """WebVTT with a NOTE header (title / summary / characters) and <v Name> speaker tags (both are standard WebVTT)."""
    out = ["WEBVTT\n"]
    if story:
        note = [f"TITLE: {story.get('title', '')}", f"SUMMARY: {story.get('summary', '')}"] + \
               [f"{c['id']} = {c.get('name') or '-'} ({c.get('voice') or '-'}) {c.get('desc') or ''}".strip()
                for c in story.get("characters", [])]
        out.append("NOTE\n" + "\n".join(x.replace("-->", "->") for x in note) + "\n")
    for c in cues:
        t = _field(c, which)
        if which != "translation":
            from .script_builder import plain_text
            t = plain_text(t)
        v = _who(c).replace(">", "")
        body = f"<v {v}>{t}" if v else t
        out.append(f"{fmt_time(c.start).replace(',', '.')} --> {fmt_time(c.end).replace(',', '.')}\n{body}\n")
    return "\n".join(out)
