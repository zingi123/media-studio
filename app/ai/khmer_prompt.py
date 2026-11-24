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


# ------------------------------------------------------------------ language-aware rules, review and shorten passes
LANG_NAMES = {"zh": "Chinese", "ko": "Korean", "ja": "Japanese", "vi": "Vietnamese", "th": "Thai", "en": "English"}
LANG_RULES = {
    "zh": "Chinese: kinship/social titles (哥, 姐, 老公, 师父, 老板, 少爷) map to natural Khmer terms (បង, អូន, ប្តី, គ្រូ, ថៅកែ ...). "
          "Idioms (成语) are translated by meaning, never word by word.",
    "ko": "Korean: speech level (존댓말 vs 반말) and titles (오빠, 형, 누나, 언니, 선배, 사장님) decide the Khmer pronoun and politeness. "
          "Subjects are often omitted - infer who speaks to whom from the earlier lines.",
    "ja": "Japanese: -san/-sama/-kun/-chan and keigo vs casual speech decide politeness. Subjects and pronouns are often omitted - infer "
          "them from context; do not translate every あなた/私.",
    "vi": "Vietnamese: pronouns (anh, chị, em, cô, chú, bác, con, mình, tôi) encode age and relationship - map them to the matching Khmer "
          "terms (បង, អូន, ពូ, មីង, ម្តាយ, ឪពុក ...) and keep them stable for each pair of speakers.",
    "th": "Thai: particles (ครับ/ค่ะ) mark politeness or gender - express them through Khmer politeness, never literally.",
}


def system_for(src_lang="auto"):
    """SYSTEM prompt for the given source language code (auto -> the model detects it). Keeps the original rules."""
    if src_lang in LANG_NAMES and src_lang in LANG_RULES:
        s = SYSTEM.replace("from Chinese to Khmer", f"from {LANG_NAMES[src_lang]} to Khmer")
        return s + "\n" + LANG_RULES[src_lang]
    s = SYSTEM.replace("from Chinese to Khmer", "from Chinese, Korean, Japanese, Vietnamese or Thai (detect it per line) to Khmer")
    return s + "\n" + "\n".join(LANG_RULES[k] for k in ("zh", "ko", "ja", "vi"))


REVIEW_SYSTEM = (
    "You are a senior Khmer dubbing editor. You receive source lines with a draft Khmer translation. Fix ONLY real problems: "
    "wrong or missing meaning, unnatural / bookish / literal Khmer, wrong pronoun or politeness for the relationship, inconsistent "
    "name spelling or pronoun for the same character, leftover foreign words. Do not rewrite lines that are already good. "
    "A fixed line must NOT be longer than the draft (it has to fit the same speaking time). Output ONLY JSON."
)


def build_review_prompt(batch, ctx=None, glossary="", characters=""):
    """batch: [{id, text, km, sec, speaker?, emotion?}]  -> the model returns ONLY the lines that need a fix."""
    p = "Review the draft Khmer below against the source.\n"
    if glossary:
        p += f"Glossary / names (use exactly): {glossary}\n"
    if characters:
        p += f"Characters and relationships: {characters}\n"
    if ctx:
        p += "Earlier source lines for context only: " + json.dumps(ctx, ensure_ascii=False) + "\n"
    p += ('Return ONLY a JSON array of {"id": number, "km": string} for the lines that need a fix (use [] if none). '
          "Lines:\n" + json.dumps(batch, ensure_ascii=False))
    return p


SHORTEN_SYSTEM = (
    "You are a Khmer dubbing editor. Each Khmer line is too long to be spoken in its time slot. Rewrite each one SHORTER, natural spoken "
    "Khmer, keeping the meaning, emotion, pronouns and name spelling. Never add information. Respect max_chars. Output ONLY JSON."
)


def build_shorten_prompt(batch, glossary=""):
    """batch: [{id, source, km, sec, max_chars, speaker?}] -> JSON array of {id, km}."""
    p = "Shorten these Khmer dubbing lines so each fits its time slot (max_chars is a hard limit).\n"
    if glossary:
        p += f"Glossary / names (use exactly): {glossary}\n"
    p += 'Return ONLY a JSON array of {"id": number, "km": string}, same ids.\nLines:\n' + json.dumps(batch, ensure_ascii=False)
    return p
