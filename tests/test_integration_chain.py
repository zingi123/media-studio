"""Whole workflow without Qt: video -> Smart Script (Whisper/diarization STUBBED) -> Speaker Manager -> translation (mock provider)
-> TTS (STUBBED: a distinct tone per voice) -> sync -> export. Proves the stages hand each other correct data and timestamps."""
import json, os, tempfile
from pathlib import Path
import numpy as np
from app.translator import smart_script as SS
from app.translator.script_builder import Word
from app.translator.speakers import SpeakerBook
from app.translator.translator_engine import translate_cues
from app.translator import voice_synchronizer as VS
from app.translator.exporter import export_video
from app.ai import providers as P
from test_state_sync_export import need_ffmpeg, make_video, sine, read, rms, extract_audio, tmp, FF
import subprocess


def stub_pipeline():
    SS.extract_audio = lambda v, out, cancel=None: (Path(out).write_bytes(b"RIFF"))
    ws = [Word("你好", 1.0, 1.8), Word("吗", 1.9, 2.2),            # Speaker A
          Word("我很好", 4.0, 4.9),                                # Speaker B
          Word("再见", 8.0, 8.8)]                                  # Speaker A again after a 3 s+ gap
    SS.transcribe_words = lambda video, **kw: (ws, "zh")
    import app.translator.voice_detect as VD
    VD.detect_voice_types = lambda video, saved, **kw: {"speakers": [{"id": "Speaker 1", "type": "Women"}, {"id": "Speaker 2", "type": "Adult Men"}],
        "segments": [(0.8, 2.5, "Speaker 1"), (3.8, 5.2, "Speaker 2"), (7.8, 9.0, "Speaker 1")], "notes": [], "diar_method": "pyannote",
        "merge_suggestions": [], "overlaps": [], "embeddings": {}}


class FakeLLM(P.AIProvider):
    id, label, needs_key = "fake", "Fake", False
    def generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=1):
        ids = [o["id"] for o in json.loads(prompt.split("Lines:\n")[1])]
        return json.dumps([{"id": i, "km": f"ខ្មែរ {i}"} for i in ids])


def test_full_chain_timing_and_voice_identity():
    need_ffmpeg()
    stub_pipeline()
    d = tmp(); video = d / "ep.mp4"; make_video(video, 12)
    cues, note = SS.scan_smart_script(video, use_emotion=False, use_motion=False, use_story=False, lang="zh", model="tiny")
    assert [(c.speaker, c.start, c.end) for c in cues] == [("Speaker 1", 1.0, 2.2), ("Speaker 2", 4.0, 4.9), ("Speaker 1", 8.0, 8.8)]
    translate_cues(cues, provider=FakeLLM())
    assert all(c.translation for c in cues)
    book = SpeakerBook().sync_from_cues(cues)
    book.set_voice("Speaker 1", voice="voiceA", gender="female"); book.set_voice("Speaker 2", voice="voiceB", gender="male")
    book.apply_to_cues(cues)
    assert [c.voice_name for c in cues] == ["voiceA", "voiceB", "voiceA"]            # a character keeps ONE voice
    freq = {"voiceA": 440, "voiceB": 880}
    for i, c in enumerate(cues):                                                     # TTS stub: tone per voice
        f = d / f"r{i}.wav"; sine(f, freq[c.voice_name], 0.7); c.audio = str(f)
    voice = d / "voice.wav"; rep = []
    VS.build_voice_track(cues, 12, voice, report=rep)
    out = export_video(video, d / "dub.mp4", cues, voice_wav=voice, original_volume=0.0, subs="none")
    a, sr = extract_audio(out, d / "o.wav")
    def dom(t0, t1):
        seg = a[int(t0 * sr):int(t1 * sr)]; sp = np.abs(np.fft.rfft(seg)); fr = np.fft.rfftfreq(len(seg), 1 / sr); return fr[int(np.argmax(sp))]
    assert abs(dom(1.1, 1.6) - 440) < 25 and abs(dom(4.1, 4.6) - 880) < 25 and abs(dom(8.1, 8.6) - 440) < 25   # right voice at the right time
    assert rms(a, sr, 5.5, 7.5) < 0.005 and rms(a, sr, 10, 11.5) < 0.005                # silence between rows is kept
    assert not rep                                                                    # nothing overran its slot
