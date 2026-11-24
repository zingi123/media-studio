"""Portable metadata registry for user-owned narration voice references.

This stores paths and labels only; it does not clone voices or upload samples.
"""
import json
import os
import uuid
from pathlib import Path

from ..shared.paths import data_dir


class VoiceProfileStore:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_dir() / "summary" / "voice_profiles"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "profiles.json"

    def list(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def add(self, name, reference_wav, notes=""):
        name = str(name or "").strip()
        wav = Path(reference_wav).expanduser().resolve()
        if not name:
            raise ValueError("Voice profile name is required.")
        if not wav.is_file() or wav.suffix.lower() != ".wav":
            raise ValueError("Choose an existing WAV reference file.")
        rows = self.list()
        if any(x.get("name", "").casefold() == name.casefold() for x in rows):
            raise ValueError("A voice profile with this name already exists.")
        row = {"id": uuid.uuid4().hex[:12], "name": name, "reference_wav": str(wav), "notes": str(notes or "")[:500]}
        rows.append(row)
        self._save(rows)
        return row

    def remove(self, profile_id):
        rows = self.list()
        kept = [x for x in rows if x.get("id") != profile_id]
        if len(kept) == len(rows):
            return False
        self._save(kept)
        return True

    def get(self, profile_id):
        return next((x for x in self.list() if x.get("id") == profile_id), None)

    def _save(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
