"""Khmer recap script from language-neutral beats. Provider independent (anything with .generate(prompt, system=, json_mode=)).

Reuses the Translator's tolerant JSON parser and timing heuristic (khmer_prompt) instead of duplicating them.
"""
import json
import re
import unicodedata

from ..ai.khmer_prompt import CPS, parse_json_array
from .model import Segment

SYSTEM_RECAP = (
    "You are a professional movie-recap narrator writing for Cambodian audiences. You receive numbered STORY BEATS "
    "(language-neutral). Write each beat as natural, spoken Cambodian Khmer (ភាសាខ្មែរ), the way a storyteller narrates a movie "
    "summary - vivid but not literal, not bookish. Rules:\n"
    "1. One output item per beat, same id, same order. Never merge, split, drop or add beats.\n"
    "2. Keep every important plot fact of the beat; add no events that are not in the beats.\n"
    "3. Each item must be speakable in about the given 'sec' seconds (about 'chars' Khmer characters). Shorten naturally.\n"
    "4. Use the requested narration style; keep character names spelled the same everywhere.\n"
    "5. Write numbers as Khmer words, not digits.\n"
    "6. Output ONLY a JSON array of {\"id\": number, \"km\": string}."
)


def target_chars(seconds, cps=CPS):
    return int(max(0.0, seconds) * cps)


def normalize_khmer(text):
    """NFC, no BOM, no non-breaking / doubled spaces. Zero-width spaces are kept (they mark word breaks on purpose)."""
    t = unicodedata.normalize("NFC", str(text or "")).replace("\ufeff", "")
    return re.sub(r"\s+", " ", t).strip()


def tts_warnings(text):
    """Things a Khmer voice model commonly reads badly. Heuristics only - always listen to the audio."""
    out = []
    digits = re.findall(r"[0-9]+", text or "")
    if digits:
        out.append(f"Digits ({', '.join(digits[:3])}) may be misread - write numbers in Khmer words.")
    return out


def build_prompt(beats, target_seconds, style="neutral", characters="", glossary=""):
    total = sum(max(0.0, b.weight) for b in beats) or 1.0
    rows = []
    for b in beats:
        sec = target_seconds * max(0.0, b.weight) / total
        rows.append({"id": b.id, "beat": b.text, "sec": round(sec, 1), "chars": target_chars(sec)})
    p = f"Narration style: {style}. Total target length: about {int(target_seconds)} seconds.\n"
    if characters:
        p += f"Characters: {characters}\n"
    if glossary:
        p += f"Names / glossary (use exactly): {glossary}\n"
    return p + "Beats:\n" + json.dumps(rows, ensure_ascii=False)


def parse_segments(text, beats):
    try:
        by_id = parse_json_array(text)
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise ValueError(f"The AI answer is not the expected JSON array ({e}).") from e
    missing = [b.id for b in beats if not by_id.get(b.id, "").strip()]
    if missing:
        raise ValueError("The AI answer is missing beat(s): " + ", ".join(str(i) for i in missing))
    return [Segment(id=b.id, beat_id=b.id, km=normalize_khmer(by_id[b.id])) for b in beats]


def generate_script(provider, beats, target_seconds, style="neutral", characters="", glossary="", retries=1):
    prompt, last = build_prompt(beats, target_seconds, style, characters, glossary), None
    for attempt in range(retries + 1):
        p = prompt if last is None else prompt + f"\nYour previous answer was invalid ({last}). Return ONLY the JSON array."
        reply = provider.generate(p, system=SYSTEM_RECAP, json_mode=True, temperature=0.5)
        try:
            return parse_segments(reply, beats)
        except ValueError as e:
            last = str(e)
    raise ValueError(last)


def regenerate_segment(provider, beat, segment, instruction="", style="neutral", seconds=None, retries=1):
    """New Khmer text for ONE paragraph. Returns the text; the segment is NOT modified (the caller applies it),
    so a failed call can never lose the existing text."""
    b = type(beat)(id=beat.id, text=beat.text, weight=beat.weight)
    prompt = build_prompt([b], seconds if seconds else 8.0, style)
    prompt += f"\nCurrent Khmer text (rewrite it, do not repeat it): {segment.km}\n"
    if instruction:
        prompt += f"Instruction from the editor: {instruction}\n"
    last = None
    for _ in range(retries + 1):
        p = prompt if last is None else prompt + f"Your previous answer was invalid ({last}). Return ONLY the JSON array.\n"
        reply = provider.generate(p, system=SYSTEM_RECAP, json_mode=True, temperature=0.7)
        try:
            return parse_segments(reply, [b])[0].km
        except ValueError as e:
            last = str(e)
    raise ValueError(last)
