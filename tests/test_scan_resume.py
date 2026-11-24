"""Ordinary OCR/Whisper scans persist completed stage output for safe resume."""
import json
from pathlib import Path

from app.translator import scan
from app.translator.subtitles import Cue
from app.shared.ffmpeg_util import Cancelled


def test_voice_scan_reuses_completed_result_after_restart(tmp_path, monkeypatch):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"same source video")
    monkeypatch.setattr(scan, "work_dir", lambda name: tmp_path / "work" / name)
    calls = []

    def transcribe(*args, **kwargs):
        calls.append("whisper")
        return [Cue(1.25, 2.5, "你好")]

    monkeypatch.setattr("app.translator.speech_recognition.transcribe", transcribe, raising=False)
    cues, note = scan.scan_script(str(video), method="voice", lang="zh", model="base")
    assert len(cues) == 1 and cues[0].start == 1.25
    assert "voice" in note
    cues2, _ = scan.scan_script(str(video), method="voice", lang="zh", model="base")
    assert len(cues2) == 1 and cues2[0].text == "你好"
    assert calls == ["whisper"]


def test_voice_result_invalidates_when_model_changes(tmp_path, monkeypatch):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"same source video")
    monkeypatch.setattr(scan, "work_dir", lambda name: tmp_path / "work" / name)
    calls = []

    def transcribe(*args, **kwargs):
        calls.append(kwargs.get("model"))
        return [Cue(0.0, 1.0, str(kwargs.get("model")))]

    monkeypatch.setattr("app.translator.speech_recognition.transcribe", transcribe, raising=False)
    scan.scan_script(str(video), method="voice", lang="zh", model="base")
    scan.scan_script(str(video), method="voice", lang="zh", model="small")
    assert calls == ["base", "small"]


def test_cancelled_stage_is_not_marked_complete(tmp_path, monkeypatch):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"same source video")
    monkeypatch.setattr(scan, "work_dir", lambda name: tmp_path / "work" / name)

    def transcribe(*args, **kwargs):
        raise Cancelled()

    monkeypatch.setattr("app.translator.speech_recognition.transcribe", transcribe, raising=False)
    try:
        scan.scan_script(str(video), method="voice", lang="zh", model="base")
        assert False, "expected cancellation"
    except Cancelled:
        pass
    state_file = next((tmp_path / "work").rglob("scan_project.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["steps"]["whisper"]["status"] == "pending"


def test_auto_scan_caches_empty_voice_result_and_reuses_ocr_result(tmp_path, monkeypatch):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"same source video")
    monkeypatch.setattr(scan, "work_dir", lambda name: tmp_path / "work" / name)
    calls = []

    def transcribe(*args, **kwargs):
        calls.append("whisper")
        return []

    def ocr_extract(*args, **kwargs):
        calls.append("ocr")
        return [Cue(2.0, 3.0, "字幕")]

    monkeypatch.setattr("app.translator.speech_recognition.transcribe", transcribe, raising=False)
    monkeypatch.setattr("app.translator.ocr_subtitles.extract", ocr_extract, raising=False)
    first, _ = scan.scan_script(str(video), method="auto", lang="zh", model="base")
    second, _ = scan.scan_script(str(video), method="auto", lang="zh", model="base")
    assert first[0].text == second[0].text == "字幕"
    assert calls == ["whisper", "ocr"]
