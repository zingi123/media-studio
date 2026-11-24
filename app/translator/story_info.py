"""
Story info for a script: short TOPIC / SUMMARY of the story + the CHARACTERS (name, voice, a few words about them).
Written as a header at the TOP of the Smart Script file (lines that start with "#"), so the script reads like a
proper dubbing sheet, and so the translator gets the story context + the names (consistent names in the translation).

  * With a Gemini key (Settings) the AI reads the script and writes the title / summary / names. Only a SHORT
    text of the script is sent (the same key and service the app already uses for translation).
  * Without a key nothing is invented: the title is the video name, the summary is left empty for you to type,
    and the characters are listed as Speaker N (voice type, number of lines, seconds spoken).

The header is plain text, so you can edit it by hand; "B. Script -> Voice" reads it back.
"""
import json
import re
import time
import urllib.error
import urllib.request
from ..shared.utils import fmt_time

MAX_CHARS = 6000          # how much script text is sent to the AI (start + end are kept)


# ---------------------------------------------------------------- data
def empty_story(title=""):
    return {"title": title, "summary": "", "characters": []}   # characters: [{"id","name","voice","desc","lines","secs"}]


def label_of(ch):
    """The name shown on every line: the character's name when known, else 'Speaker N'."""
    return (ch.get("name") or "").strip() or ch["id"]


def offline_story(cues, title=""):
    """No AI: only facts that are really known (who speaks, voice type, how much)."""
    chars, order = {}, []
    for c in cues:
        sid = c.speaker or "Speaker 1"
        if sid not in chars:
            chars[sid] = {"id": sid, "name": "", "voice": c.voice_desc or "", "desc": "", "lines": 0, "secs": 0.0}
            order.append(sid)
        chars[sid]["lines"] += 1
        chars[sid]["secs"] += max(0.0, c.end - c.start)
    st = empty_story(title)
    st["characters"] = [chars[k] for k in order]
    return st


# ---------------------------------------------------------------- AI (Gemini) - optional
def _script_sample(cues):
    from .script_builder import plain_text
    rows = [f"{c.speaker or 'Speaker 1'}" + (f" [{c.emotion}]" if c.emotion and c.emotion != "normal" else "") +
            f": {plain_text(c.text)}" for c in cues if c.text.strip()]
    txt = "\n".join(rows)
    if len(txt) > MAX_CHARS:
        h = MAX_CHARS // 2
        txt = txt[:h] + "\n...\n" + txt[-h:]
    return txt


def _story_provider(api_key, model):
    """Provider chosen for the 'story' task in Settings (Gemini by default; the UI passes the Gemini key/model)."""
    from ..ai.providers import task_provider, GeminiProvider
    try:
        p = task_provider("story")
    except Exception:  # noqa
        p = None
    if p is None or p.id == "gemini":
        return GeminiProvider(api_key, model)
    return p


def _gemini_json(prompt, key, model):
    """Same provider layer as translation (no duplicated HTTP code). Returns the parsed JSON object."""
    from ..ai.providers import ProviderError
    p = _story_provider(key, model)
    try:
        return json.loads(p.generate(prompt, json_mode=True, temperature=0.2, timeout=90))
    except (ProviderError, ValueError) as e:
        raise RuntimeError(f"{p.label} failed: {e}") from e


def ai_story(cues, base, api_key, model="gemini-3.8-flash", out_lang="Khmer"):
    """Fill title / summary / character names with Gemini. `base` = offline_story() (keeps the facts). Raises on failure."""
    ids = [c["id"] for c in base["characters"]]
    prompt = (
        f"Below is a short dubbing script: each line is 'Speaker [emotion]: text'. Speakers: {ids}.\n"
        f"Write in {out_lang}. Return ONLY JSON: {{\"title\": short story topic (max 8 words), "
        f"\"summary\": 2-3 sentences, what happens, "
        f"\"characters\": [{{\"id\": one of the speaker ids, \"name\": the character's name ONLY if the dialogue "
        f"really says it (people calling each other), otherwise null, \"desc\": 3-8 words about who they are or how "
        f"they behave, only from the dialogue}}]}}.\n"
        f"Do not invent facts that are not in the script.\n\nSCRIPT:\n{_script_sample(cues)}")
    r = _gemini_json(prompt, api_key, model)
    st = {"title": str(r.get("title") or base["title"]).strip(), "summary": str(r.get("summary") or "").strip(),
          "characters": [dict(c) for c in base["characters"]]}
    by = {c["id"]: c for c in st["characters"]}
    for o in r.get("characters", []) or []:
        c = by.get(str(o.get("id")))
        if c:
            c["name"] = re.sub(r"\s+", " ", str(o.get("name") or "")).strip()[:40]
            c["desc"] = re.sub(r"\s+", " ", str(o.get("desc") or "")).strip()[:80]
    return st


def make_story(cues, title="", api_key="", model="gemini-3.8-flash", out_lang="Khmer"):
    """-> (story, note). Never raises: the offline story is the fallback."""
    base = offline_story(cues, title)
    prov = _story_provider(api_key, model)
    if not prov.configured()[0]:
        return base, f"Story info: no {prov.label} key -> only the facts (speakers/voices); type the summary yourself if you want one."
    try:
        return ai_story(cues, base, api_key, model, out_lang), f"Story info: title, summary and names written by {prov.label} (please check)."
    except Exception as e:  # noqa
        return base, f"Story info: AI failed ({str(e)[:70]}) -> only the facts."


def apply_names(cues, story):
    """Put the character names on the lines (Cue.speaker = name, or 'Speaker N' when no name). Returns the cues."""
    m = {c["id"]: label_of(c) for c in story["characters"]}
    for c in cues:
        c.speaker = m.get(c.speaker, c.speaker)
    return cues


def glossary_text(story):
    """Context for the translator: story summary + the names (passed as the `glossary` of translate_cues)."""
    if not story:
        return ""
    bits = []
    if story.get("title"):
        bits.append(f"story: {story['title']}")
    if story.get("summary"):
        bits.append(f"summary: {story['summary']}")
    for c in story.get("characters", []):
        if (c.get("name") or "").strip():
            bits.append(f"character {c['id']} is named {c['name']}" + (f" ({c['desc']})" if c.get("desc") else ""))
    return "; ".join(bits)


# ---------------------------------------------------------------- header text  (# lines)
def header_text(story, cues=None, lang=""):
    one = lambda s: re.sub(r"\s+", " ", str(s or "")).strip()  # noqa
    out = [f"# TITLE: {one(story.get('title'))}", f"# SUMMARY: {one(story.get('summary'))}"]
    if lang or cues:
        dur = max((c.end for c in cues), default=0.0) if cues else 0.0
        out.append(f"# INFO: language={lang or '-'} | lines={len(cues or [])} | length={fmt_time(dur)}")
    out.append("# CHARACTERS:")
    for c in story.get("characters", []):
        extra = []
        if c.get("lines"):
            extra.append(f"{c['lines']} line(s), {c.get('secs', 0):.0f}s")
        out.append(f"#   {c['id']} = {one(c.get('name')) or '-'} | {one(c.get('voice')) or '-'} | {one(c.get('desc')) or '-'}"
                   + (f" | {', '.join(extra)}" if extra else ""))
    out.append("# ---")
    return "\n".join(out)


_CH = re.compile(r"^#\s{1,3}(.+?)\s=\s(.*)$")


def parse_header(text):
    """Header lines of a script file -> story dict (or None when there is no header)."""
    st, got, in_ch = empty_story(), False, False
    for raw in text.replace("\ufeff", "").replace("\r", "").split("\n"):
        l = raw.strip()
        if not l.startswith("#"):
            if l:
                break
            continue
        m = re.match(r"^#\s*(TITLE|SUMMARY):\s*(.*)$", l, re.I)
        if m:
            st[m[1].lower()] = m[2].strip(); got = True; in_ch = False; continue
        if re.match(r"^#\s*CHARACTERS:", l, re.I):
            in_ch = True; got = True; continue
        if in_ch:
            m = _CH.match(raw.rstrip())
            if m:
                p = [x.strip() for x in m[2].split("|")]
                g = lambda i: "" if i >= len(p) or p[i] == "-" else p[i]  # noqa
                st["characters"].append({"id": m[1].strip(), "name": g(0), "voice": g(1), "desc": g(2), "lines": 0, "secs": 0.0})
    return st if got else None
