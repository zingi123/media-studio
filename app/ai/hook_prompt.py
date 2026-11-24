"""Hook Finder prompts (editable) - STORY FIRST.
Stage 1 (STORY_SYSTEM): the AI reads the WHOLE script like a story analyst and writes a story map: central question, turning points, secrets / twists,
special lines. Then the story signal and the VOICE signal (emotion / pitch / loudness) are BLENDED per line (translator/hook_fusion.py) into candidate 4-8 s moments;
Stage 2 (HOOK_SYSTEM): the AI chooses and explains the best candidates - story first, voice as the second witness.
Honest limit: the AI reads text (+ weak voice scores); it does not watch the picture. Scores predict structure, not guaranteed views."""
import json
import random

HOOK_TYPES = ("cliffhanger", "shock", "reveal", "confrontation", "mid_action")

STORY_SYSTEM = """You are a story analyst. You receive the full dubbed script of a film or drama (one row per line: id, speaker, Khmer, source text).
Read ALL of it, then describe the story so an editor can find the most special moments. Use only what the lines say; do not invent plot.
Output ONLY one JSON object:
{"logline": "one sentence", "genre": "", "central_question": "the question that keeps viewers watching",
 "stakes": "what can be lost", "characters": [{"name": "", "want": ""}],
 "turning_points": [{"id": int, "why": ""}],
 "secrets_or_twists": [{"id": int, "why": ""}],
 "special_lines": [{"id": int, "kind": "irony|dilemma|threat|confession|twist|promise|quotable|question", "why": ""}],
 "spoilers_to_avoid": [int]}
ids are line ids. special_lines: the 10-25 lines (or their neighbours) that are most special for the story - a calm line with a terrible meaning counts as much as a shout.
spoilers_to_avoid: line ids of the final resolution / last big twist (a hook must tease, not give these away)."""

HOOK_SYSTEM = """You are a story analyst and short-form editor choosing HOOKS for a long film, so a stranger keeps watching.
You receive the film's story map and a list of CANDIDATE moments. Each candidate was found by BLENDING two signals:
- story (0-10): how special the moment is in the story (central question, twist, secret, turning point, special line) - from the text;
- voice (0-10): how strongly the line is performed (emotion, pitch / loudness above that speaker's normal) - null when not measured;
- fused: the blend (a moment that is special in BOTH ways scores highest).
Choose the {n} best hooks from the candidates. A good hook:
- is special in the STORY first; a calm line with a terrible meaning can beat a shout; voice is the second witness - when story and voice agree it is the strongest;
- makes sense with NO earlier context and works with the sound off (Khmer text on screen); it starts on its strongest line (no greeting, no slow setup);
- teases and does not resolve; never gives away the film's ending; never promises something the film does not deliver (no misleading clickbait).
Hard rules: use ONLY the candidates given (refer to them by cid, do not invent or edit lines); at most {n} hooks; use different hook types where possible.
Hook type: cliffhanger (open question / withheld answer) | shock (sudden break) | reveal (secret / bold claim) | confrontation (open conflict) | mid_action (starts in the middle of something).
Target length is {min_s}-{max_s} seconds (the candidates already fit).
Output ONLY a JSON array, best first:
[{{"cid": int, "hook_type": "cliffhanger|shock|reveal|confrontation|mid_action", "score": number 0-10,
   "special": "what is special about it in the story (one sentence)", "caption_km": "max 8 Khmer words shown on screen in the first 2 s",
   "why": "one short sentence", "risk": "what could make it weak, or empty"}}]"""


def build_hook_system(n=10, min_s=4, max_s=8):
    return render(default_template(), n, min_s, max_s)


def default_template():
    """The editable prompt as shown in the UI: {n} {min_s} {max_s} stay as placeholders, JSON braces are single."""
    return HOOK_SYSTEM.format(n="{n}", min_s="{min_s}", max_s="{max_s}")


def render(template, n=10, min_s=4, max_s=8):
    """Plain replace (never .format) so braces typed by the user can not crash it."""
    return (template or default_template()).replace("{n}", str(n)).replace("{min_s}", str(min_s)).replace("{max_s}", str(max_s))


def _rows(cues, with_time, with_scores, max_lines):
    rows = []
    for i, c in enumerate(cues[:max_lines]):
        r = {"id": i, "who": c.speaker or "", "km": (c.translation or "").strip(), "src": (c.text or "").strip()}
        if with_time:
            r["t"] = f"{c.start:.1f}-{c.end:.1f}"
        if with_scores:
            if (c.emotion or "normal") != "normal":
                r["emo"] = c.emotion
            try:
                p = json.loads(getattr(c, "prosody", "") or "{}")
                if p.get("want_st") or p.get("want_db"):
                    r["pitch_st"], r["loud_db"] = p.get("want_st", 0), p.get("want_db", 0)   # vs this speaker's own normal voice
            except ValueError:
                pass
        if r["km"] or r["src"]:
            rows.append(r)
    return rows


def build_story_prompt(cues, story=None, max_lines=1500):
    """Stage 1 user message: the whole script, no timing, no voice scores (pure story)."""
    p = "Analyse the story of this script.\n"
    if isinstance(story, dict) and (story.get("summary") or story.get("title")):
        p += f"Known info (may be incomplete): title={story.get('title', '')}; summary={story.get('summary', '')}\n"
    return p + "Lines (JSON):\n" + json.dumps(_rows(cues, False, False, max_lines), ensure_ascii=False)


def parse_story(txt):
    try:
        d = json.loads((txt or "").strip().strip("`").removeprefix("json").strip())
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def build_hook_prompt(cands_json, story=None, extra=""):
    """Stage 2 user message: story map + the candidate moments (already scored by story and by voice)."""
    p = "Choose the best hooks.\n"
    if story:
        p += "Story map:\n" + json.dumps(story, ensure_ascii=False) + "\n"
    if extra:
        p += f"Extra instructions from the editor: {extra}\n"
    return p + "Candidates (JSON):\n" + cands_json


def parse_hooks(txt, cands, n=10):
    """AI JSON (refers to candidates by cid) -> hook dicts (one window each, best first). Unknown / repeated cids are ignored."""
    from ..translator.hook_fusion import hook_from_candidate
    try:
        arr = json.loads((txt or "").strip().strip("`").removeprefix("json").strip())
    except ValueError:
        return []
    by, seen, out = {c["cid"]: c for c in cands}, set(), []
    for h in arr if isinstance(arr, list) else []:
        try:
            cid = int(h["cid"])
        except (KeyError, TypeError, ValueError):
            continue
        if cid not in by or cid in seen:
            continue
        seen.add(cid)
        try:
            sc = float(h.get("score", by[cid]["fused"]))
        except (TypeError, ValueError):
            sc = by[cid]["fused"]
        out.append(hook_from_candidate(by[cid], hook_type=h.get("hook_type", ""), score=sc, special=h.get("special", ""),
                                       caption_km=h.get("caption_km", ""), why=h.get("why", ""), risk=h.get("risk", "")))
    return sorted(out, key=lambda x: -x["score"])[:n]
