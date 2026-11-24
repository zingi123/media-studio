"""Optional cloud-assisted visual scene understanding for Movie Summary.

Only one representative JPEG per scene is sent to Gemini; the source movie/audio is not uploaded.
Descriptions are suggestions, stored separately from user captions, and can be reviewed/edited.
"""
import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from ..shared import secrets as SEC
from ..shared.ffmpeg_util import Cancelled

API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"


def _parse(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("Vision model returned no JSON scene description.")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("Vision model response must be a JSON object.")
    caption = str(data.get("caption", "")).strip()
    if not caption:
        raise ValueError("Vision model returned an empty caption.")
    keywords = data.get("keywords", [])
    entities = data.get("entities", [])
    if not isinstance(keywords, list) or not isinstance(entities, list):
        raise ValueError("Vision model keywords/entities must be lists.")
    return caption[:1200], [str(x).strip()[:80] for x in keywords if str(x).strip()][:20], [str(x).strip()[:100] for x in entities if str(x).strip()][:12]


def describe_frame(image_path, api_key=None, model=DEFAULT_MODEL, timeout=90):
    key = (api_key or SEC.get_secret("gemini_key")).strip()
    if not key:
        raise ValueError("Gemini API key is missing. Add it in Settings > AI Providers.")
    path = Path(image_path)
    if not path.is_file():
        raise ValueError(f"Scene thumbnail is missing: {path}")
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    payload = {
        "contents": [{"parts": [
            {"text": "Describe only visible evidence in this movie frame for a scene-matching system. Do not identify real people or guess names. Return JSON only: {\\\"caption\\\": concise English sentence, \\\"keywords\\\": [visual keywords], \\\"entities\\\": [generic visible people/objects/places]. Mention actions, setting, and important objects. If uncertain, say so."},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(path.read_bytes()).decode("ascii")}}
        ]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }
    url = f"{API}/models/{model}:generateContent?key={key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as e:
        detail = SEC.redact(e.read().decode("utf-8", "replace")[:250], [key])
        raise RuntimeError(f"Gemini vision request failed (HTTP {e.code}): {detail}") from None
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"Gemini vision returned an unusable response: {SEC.redact(e, [key])}") from None
    return _parse(text)


def describe_scenes(project, progress=None, cancel=None, model=DEFAULT_MODEL, overwrite=False):
    """Fill visual_summary/keywords/entities from scene thumbnails; retain user-authored captions."""
    todo = [s for s in project.scenes if s.thumb and (overwrite or not s.visual_summary)]
    if not todo:
        raise ValueError("No scene thumbnails need analysis. Run Detect scenes first, or enable overwrite.")
    updated, errors = [], []
    for i, scene in enumerate(todo):
        if cancel and cancel():
            raise Cancelled()
        try:
            caption, keywords, entities = describe_frame(scene.thumb, model=model)
            scene.visual_summary = caption
            scene.keywords = list(dict.fromkeys((scene.keywords or []) + keywords))
            scene.entities = entities
            scene.visual_confidence = 0.65  # provider response is a suggestion, not calibrated confidence
            updated.append(scene.id)
        except Exception as e:
            errors.append(f"Scene {scene.id}: {SEC.redact(e)}")
        if progress:
            progress(int((i + 1) * 100 / len(todo)))
    # Preserve partial successful results even if individual frames failed.
    if not updated and errors:
        raise RuntimeError("No scenes were described. " + " | ".join(errors[:3]))
    return {"updated": updated, "errors": errors}
