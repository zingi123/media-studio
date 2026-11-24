"""Chinese -> spoken Khmer: the translation prompt, JSON parsing and a timing-fit check (provider independent)."""
import json
import re

SYSTEM = (
    "You are a professional dubbing translator from Chinese to Khmer (ភាសាខ្មែរ) for Cambodian audiences. "
    "Write NATURAL SPOKEN Cambodian Khmer, the way real people talk in a drama - not literal, not bookish, not formal "
    "written Khmer. Rules:\n"
    "1. Keep the full meaning, the emotional tone (use the given emotion) and the relationship between the speakers.\n"
    "2. Choose Khmer pronouns and politeness level from the relationship (family, friends, boss/employee, lovers, "
    "elder/younger, strangers). Keep the SAME pronoun and the SAME name spelling for a character through the whole video.\n"
    "3. Keep the dialogue order. One output line per input line. Never merge, split, drop or add lines.\n"
    "4. Each line must be speakable in about the given 'sec' seconds (about 10 Khmer characters per second). "
    "Shorten naturally instead of rushing; never remove important meaning.\n"
    "5. Interjections and fillers (ah, hmm, haha) stay as short natural Khmer sounds. Do not translate stage notes.\n"
    "6. Output ONLY JSON."
)

CPS = 10.0   # heuristic characters per second a natural Khmer voice can speak


def build_prompt(batch, ctx=None, glossary="", characters=""):
    """batch: [{id, text, sec, speaker?, emotion?}]  ctx: previous original lines (not to be translated)."""
    p = "Translate the lines below.\n"
    if glossary:
        p += f"Glossary / names (use exactly): {glossary}\n"
    if characters:
        p += f"Characters and relationships: {characters}\n"
    if ctx:
        p += "Earlier lines for context only (do NOT translate): " + json.dumps(ctx, ensure_ascii=False) + "\n"
    p += ('Return ONLY a JSON array of objects {"id": number, "km": string}, one per input line, same ids.\n'
          "Lines:\n" + json.dumps(batch, ensure_ascii=False))
    return p


def parse_json_array(text):
    """Tolerant parser: strips ``` fences / prose around the array. -> {id: km}. Raises ValueError when unusable."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        arr = json.loads(t)
    except ValueError:
        m = re.search(r"\[.*\]", t, re.S)
        if not m:
            raise ValueError("no JSON array in the response")
        arr = json.loads(m.group(0))
    if isinstance(arr, dict):
        arr = arr.get("lines") or arr.get("translations") or list(arr.values())[0]
    return {int(o["id"]): str(o["km"]).strip() for o in arr}


def fit_warning(khmer, seconds, cps=CPS):
    """'' when the line should fit, else a warning (a heuristic - listen to the audio to be sure)."""
    n = len(re.sub(r"\s+", "", khmer or ""))
    if seconds > 0 and n / seconds > cps * 1.6:
        return f"Khmer text is long for {seconds:.1f}s ({n / seconds:.0f} chars/s) - the voice would be rushed; shorten it."
    return ""
