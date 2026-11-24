import json
import wave
from pathlib import Path

import pytest

from app.summary.intelligence import _parse
from app.summary.voice_profiles import VoiceProfileStore
from app.summary.model import SummaryProject, Scene, Beat, Segment
from app.summary.matcher import pair_score


def test_vision_response_parser_handles_fenced_json_and_limits_fields():
    caption, keywords, entities = _parse('```json\n{"caption":"A woman opens a red door", "keywords":["door", "red"], "entities":["woman"]}\n```')
    assert caption == "A woman opens a red door"
    assert keywords == ["door", "red"] and entities == ["woman"]


def test_vision_response_parser_rejects_empty_or_wrong_shapes():
    with pytest.raises(ValueError):
        _parse('{"caption":""}')
    with pytest.raises(ValueError):
        _parse('{"caption":"A street", "keywords":"street"}')


def test_visual_description_contributes_to_scene_matching():
    p = SummaryProject(scenes=[Scene(1, 0, 10, visual_summary="A woman opens a red door")],
                       beats=[Beat(1, "woman opens red door")],
                       segments=[Segment(1, 1)])
    assert pair_score(p.beats[0], p.scenes[0]) > 0.5


def test_voice_profile_store_add_get_remove_and_validation(tmp_path):
    wav = tmp_path / "reference.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 100)
    store = VoiceProfileStore(tmp_path / "profiles")
    profile = store.add("My Voice", str(wav), "test")
    assert store.get(profile["id"])["name"] == "My Voice"
    with pytest.raises(ValueError):
        store.add("my voice", str(wav))
    assert store.remove(profile["id"]) is True
    assert store.list() == []


def test_voice_profile_requires_existing_wav(tmp_path):
    store = VoiceProfileStore(tmp_path / "profiles")
    with pytest.raises(ValueError):
        store.add("Missing", str(tmp_path / "missing.wav"))


def test_render_graph_supports_aligned_original_narration_and_music_tracks(tmp_path):
    import subprocess
    import numpy as np
    from app.shared.ffmpeg_util import require_ffmpeg, probe
    from app.summary.model import Clip
    from app.summary.render import render_edl

    source = tmp_path / "source_with_audio.mp4"
    subprocess.run([require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
                    "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(source)], check=True)
    def write_wav(path, hz):
        import numpy as np
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            t = np.arange(16000) / 16000
            w.writeframes((np.sin(2*np.pi*hz*t)*4000).astype(np.int16).tobytes())
    narration, music = tmp_path / "narr.wav", tmp_path / "music.wav"
    write_wav(narration, 440); write_wav(music, 220)
    p = SummaryProject(source=str(source), duration=2, width=160, height=120, fps=25,
                       scenes=[Scene(1, 0, 2)], beats=[Beat(1, "scene")],
                       segments=[Segment(1, 1, km="សួស្តី", dur=2, audio=str(narration), scene_id=1)],
                       clips=[Clip(1, 0, 2)])
    out = tmp_path / "mixed.mp4"
    render_edl(p, out, narration=str(narration), subtitles=False, work=tmp_path / "render",
               mix_original_audio=True, music=str(music))
    assert out.is_file() and probe(out)["has_audio"]


def test_story_beats_are_built_from_scene_evidence_with_time_hints():
    from app.summary.story_intelligence import parse_beats, build_prompt
    scenes = [Scene(2, 5, 9, visual_summary="A man enters a train station"),
              Scene(3, 9, 13, visual_summary="The man boards a train")]
    prompt = build_prompt(scenes)
    assert "ONLY the visual evidence" in prompt and "train station" in prompt
    beats = parse_beats('[{"scene_ids":[2,3],"text":"A man boards a train."}]', scenes)
    assert len(beats) == 1 and beats[0].t_hint == (5, 13)


def test_story_beat_parser_rejects_unlinked_scene_ids():
    from app.summary.story_intelligence import parse_beats
    with pytest.raises(ValueError):
        parse_beats('[{"scene_ids":[99],"text":"Unknown event"}]', [Scene(1, 0, 3)])


def test_controller_auto_story_beats_accepts_worker_callbacks_and_autosaves(tmp_path):
    from app.summary.controller import SummaryController
    from app.summary.model import Scene

    class Provider:
        def generate(self, prompt, system="", json_mode=False, **kwargs):
            assert "visible_evidence" in prompt
            return '[{"scene_ids":[1],"text":"A traveler enters the city."}]'

    ctl = SummaryController(root=tmp_path / "summary")
    source = tmp_path / "placeholder.mp4"
    source.write_bytes(b"x")
    ctl.work = tmp_path / "work"
    ctl.work.mkdir()
    ctl.project = SummaryProject(source=str(source), scenes=[Scene(1, 2, 5, visual_summary="A traveler enters a city")])
    beats = ctl.extract_story_beats(Provider(), progress=lambda _: None, cancel=lambda: False)
    assert beats[0].t_hint == (2, 5)
    from app.summary.model import load_project
    assert load_project(ctl.work / "project.json").beats[0].text == "A traveler enters the city."
