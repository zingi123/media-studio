"""
Speaker Manager (backend, no Qt).  One SpeakerProfile per character; the cues keep the stable speaker ID in `cue.speaker`.

 * rename / merge / split / reassign / lock / unlock / set voice / preview text / save / restore
 * voice identity: apply_to_cues() gives every row of a speaker the SAME voice (profile.voice) - voices never change at random
 * merging is ALWAYS an explicit user action. suggest_merges() only proposes pairs (embedding similarity); it never merges.
 * a LOCKED speaker is frozen: it cannot be merged away, split, renamed, or have rows reassigned away from it
   (use force=True from a confirmed dialog), and a re-scan does not overwrite its settings.
"""
import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np


class SpeakerError(ValueError):
    pass


@dataclass
class SpeakerProfile:
    id: str
    name: str = ""                  # custom display name ("" = use id)
    locked: bool = False
    gender: str = ""                # "male" | "female" | ""
    voice_type: str = ""            # e.g. "Adult Men" (metadata from Voice Type presets / detection)
    voice: str = ""                 # selected Khmer TTS voice, e.g. "km-KH-PisethNeural"
    voice_profile: dict = field(default_factory=dict)   # {"speed": 1.0, "pitch_hz": 0, "volume_pct": 0, ...}
    ref_audio: str = ""             # optional reference audio for cloned voices
    notes: str = ""
    embedding: list = field(default_factory=list)       # optional, from diarization (used only to SUGGEST merges)

    def label(self):
        return self.name or self.id


class SpeakerBook:
    def __init__(self):
        self.speakers = {}

    # ------------------------------------------------------------ basic
    def get(self, sid) -> SpeakerProfile:
        if sid not in self.speakers:
            raise SpeakerError(f"Unknown speaker '{sid}'.")
        return self.speakers[sid]

    def ensure(self, sid) -> SpeakerProfile:
        if sid and sid not in self.speakers:
            self.speakers[sid] = SpeakerProfile(sid)
        return self.speakers[sid]

    def sync_from_cues(self, cues):
        """Create a profile for every speaker ID used by the rows (existing profiles are kept untouched)."""
        for c in cues:
            if c.speaker:
                self.ensure(c.speaker)
        return self

    def rows_of(self, sid, cues):
        return [i for i, c in enumerate(cues) if c.speaker == sid]

    def _free(self, sp, force):
        if sp.locked and not force:
            raise SpeakerError(f"Speaker '{sp.label()}' is locked. Unlock it first.")

    # ------------------------------------------------------------ actions
    def rename(self, sid, name, force=False):
        sp = self.get(sid)
        self._free(sp, force)
        name = (name or "").strip()
        if name and any(o.name == name and o.id != sid for o in self.speakers.values()):
            raise SpeakerError(f"Another speaker is already named '{name}'.")
        sp.name = name

    def lock(self, sid):
        self.get(sid).locked = True

    def unlock(self, sid):
        self.get(sid).locked = False

    def set_voice(self, sid, voice=None, gender=None, voice_type=None, voice_profile=None, ref_audio=None, force=False):
        sp = self.get(sid)
        self._free(sp, force)
        if voice is not None:
            sp.voice = voice
        if gender is not None:
            sp.gender = gender
        if voice_type is not None:
            sp.voice_type = voice_type
        if voice_profile is not None:
            sp.voice_profile = dict(voice_profile)
        if ref_audio is not None:
            sp.ref_audio = ref_audio

    def merge(self, src, dst, cues, force=False):
        """Move every row of `src` to `dst`; `src` disappears; `dst` keeps ITS voice/profile. Explicit user action only."""
        if src == dst:
            raise SpeakerError("Choose two different speakers.")
        s, d = self.get(src), self.get(dst)
        self._free(s, force)
        n = 0
        for c in cues:
            if c.speaker == src:
                c.speaker, n = dst, n + 1
        if not d.notes and s.notes:
            d.notes = s.notes
        del self.speakers[src]
        return n

    def split(self, src, row_indices, cues, new_id=None, new_name="", force=False):
        """Move the chosen rows of `src` to a NEW speaker (copy of its voice settings). Returns the new speaker id."""
        s = self.get(src)
        self._free(s, force)
        rows = [i for i in row_indices if 0 <= i < len(cues) and cues[i].speaker == src]
        if not rows:
            raise SpeakerError("Select at least one row that belongs to this speaker.")
        if len(rows) == len(self.rows_of(src, cues)):
            raise SpeakerError("Selecting every row would just rename the speaker. Use Rename instead.")
        nid = new_id or self._new_id()
        if nid in self.speakers:
            raise SpeakerError(f"Speaker ID '{nid}' already exists.")
        self.speakers[nid] = SpeakerProfile(nid, new_name, False, s.gender, s.voice_type, s.voice,
                                            dict(s.voice_profile), s.ref_audio, "", [])
        for i in rows:
            cues[i].speaker = nid
        return nid

    def reassign(self, row_indices, to_id, cues, force=False):
        """Change the speaker of the selected rows (manual correction). The row's uncertainty flag is cleared."""
        self.ensure(to_id)
        n = 0
        for i in row_indices:
            c = cues[i]
            if c.speaker == to_id:
                continue
            if c.speaker in self.speakers:
                self._free(self.speakers[c.speaker], force)
            c.speaker, c.speaker_uncertain, c.speaker_conf, c.overlap_with = to_id, False, 1.0, ""
            n += 1
        return n

    def _new_id(self):
        k = len(self.speakers) + 1
        while f"Speaker {k}" in self.speakers:
            k += 1
        return f"Speaker {k}"

    # ------------------------------------------------------------ voice identity
    def apply_to_cues(self, cues):
        """Every row of a speaker gets that speaker's voice / gender / voice type -> a character never changes voice.
        Rows of speakers without a selected voice are left untouched. Returns the number of rows changed."""
        n = 0
        for c in cues:
            sp = self.speakers.get(c.speaker)
            if not sp:
                continue
            ch = False
            if sp.voice and c.voice_name != sp.voice:
                c.voice_name, ch = sp.voice, True
            if sp.gender and c.gender != sp.gender:
                c.gender, ch = sp.gender, True
            if sp.voice_type and c.voice_type != sp.voice_type:
                c.voice_type, ch = sp.voice_type, True
            n += ch
        return n

    def voice_conflicts(self, cues):
        """Rows whose voice differs from their speaker's voice (should be empty after apply_to_cues)."""
        return [i for i, c in enumerate(cues) if (sp := self.speakers.get(c.speaker)) and sp.voice and c.voice_name and c.voice_name != sp.voice]

    # ------------------------------------------------------------ suggestions (never automatic)
    def suggest_merges(self, threshold=0.75):
        """[(id_a, id_b, cosine)] for speakers with embeddings that sound alike. A hint for the user - nothing is merged."""
        ids = [i for i, s in self.speakers.items() if s.embedding]
        out = []
        for k, a in enumerate(ids):
            va = np.asarray(self.speakers[a].embedding, float)
            for b in ids[k + 1:]:
                vb = np.asarray(self.speakers[b].embedding, float)
                c = float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-9))
                if c >= threshold:
                    out.append((a, b, round(c, 3)))
        return sorted(out, key=lambda t: -t[2])

    def apply_detection(self, speaker_ids, embeddings=None, voice_types=None):
        """After a (re-)scan: add new speakers; LOCKED and already-configured speakers keep their settings."""
        for sid in speaker_ids:
            sp = self.ensure(sid)
            if embeddings and sid in embeddings and not sp.locked:
                sp.embedding = list(embeddings[sid])
            if voice_types and sid in voice_types and not sp.locked and not sp.voice_type:
                sp.voice_type = voice_types[sid]

    # ------------------------------------------------------------ persistence
    def to_dict(self):
        return {"version": 1, "speakers": [asdict(s) for s in self.speakers.values()]}

    @classmethod
    def from_dict(cls, d):
        b = cls()
        known = set(SpeakerProfile.__dataclass_fields__)
        for s in d.get("speakers", []):
            sp = SpeakerProfile(**{k: v for k, v in s.items() if k in known})
            b.speakers[sp.id] = sp
        return b

    def save(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    @classmethod
    def load(cls, path):
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:  # noqa
            raise SpeakerError(f"Speaker settings file is damaged: {e}") from e
