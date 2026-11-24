"""Turn reviewed visual scene notes into an editable, evidence-bounded story-beat draft.

This is not full plot comprehension: it can only use the scene descriptions supplied to it.
"""
import json
import re
from .model import Beat


def build_prompt(scenes, max_beats=18):
    rows = []
    for scene in scenes:
        note = (scene.caption or scene.visual_summary or "").strip()
        if note:
            rows.append({"scene_id": scene.id, "start": round(scene.start, 2), "end": round(scene.end, 2), "visible_evidence": note})
    if not rows:
        raise ValueError("No AI scene descriptions or captions are available. Describe scenes first.")
    return ("Create a concise chronological draft of story beats for a movie recap, using ONLY the visual evidence below. "
            "Do not invent dialogue, motivations, relationships, names, or events not visible in the evidence. "
            "Merge adjacent scenes when they show the same event. Return JSON array only; each item must contain "
            "scene_ids (array of integers) and text (one concise English story beat). Use at most " + str(max(1, min(40, int(max_beats))) ) +
            " beats. Input scenes: " + json.dumps(rows, ensure_ascii=False))


def parse_beats(raw, scenes):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            raise ValueError("AI story analysis returned no JSON array.")
        data = json.loads(m.group(0))
    if not isinstance(data, list) or not data:
        raise ValueError("AI story analysis returned no beats.")
    by_id = {s.id: s for s in scenes}
    beats = []
    for row in data:
        if not isinstance(row, dict):
            continue
        ids = row.get("scene_ids", [])
        valid = [by_id[i] for i in ids if isinstance(i, int) and i in by_id] if isinstance(ids, list) else []
        body = str(row.get("text", "")).strip()
        if not valid or not body:
            continue
        beats.append(Beat(id=len(beats) + 1, text=body[:500], t_hint=(min(s.start for s in valid), max(s.end for s in valid))))
    if not beats:
        raise ValueError("AI story analysis did not link any valid scene IDs to story beats.")
    return beats


def extract_beats(provider, scenes, max_beats=18):
    prompt = build_prompt(scenes, max_beats)
    raw = provider.generate(prompt, system="You are a careful film-scene analyst. Never add unsupported plot facts.", json_mode=True)
    return parse_beats(raw, scenes)
