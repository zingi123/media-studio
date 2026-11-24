import sys, types, tempfile, wave, os, json
from pathlib import Path
import numpy as np
from app.shared import model_manager as MMod
from app.shared.model_manager import ModelManager, InsufficientMemory, check_memory
from app.translator import speech_recognition as SR
from app.shared.ffmpeg_util import Cancelled


def wav(seconds, tone_at=()):
    a = np.zeros(int(seconds * 16000), dtype=np.float32)
    for t in tone_at:
        i = int(t * 16000); a[i:i + 8000] = 0.4 * np.sin(np.linspace(0, 400 * np.pi, 8000))
    p = Path(tempfile.mkdtemp()) / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes((a * 32767).astype(np.int16).tobytes())
    return p


# ---------------------------------------------------------------- model manager
def test_only_one_heavy_model_at_a_time_and_unload():
    m = ModelManager(max_heavy=1); freed = []
    class Obj:
        def __init__(s, n): s.n = n
        def __del__(s): freed.append(s.n)
    m.get("a", lambda: Obj("a"), 100, avail_mb=10**6)
    assert m.loaded() == ["a"]
    m.get("b", lambda: Obj("b"), 100, avail_mb=10**6)
    assert m.loaded() == ["b"]                         # a was evicted before b loaded
    m.get("small", lambda: Obj("s"), 10, heavy=False, avail_mb=10**6)
    assert set(m.loaded()) == {"b", "small"}
    m.unload_all(); assert m.loaded() == []
    m2 = ModelManager(max_heavy=2)
    m2.get("a", lambda: 1, 1, avail_mb=10**6); m2.get("b", lambda: 2, 1, avail_mb=10**6)
    assert len(m2.loaded()) == 2                       # configurable workers: user opted in to two


def test_cached_model_is_not_reloaded_and_external_unloaders_run():
    m = ModelManager(); calls = []; ext = []
    m.register_external("pyannote", lambda: ext.append(1))
    m.get("a", lambda: calls.append(1) or object(), 1, avail_mb=10**6); m.get("a", lambda: calls.append(1), 1)
    assert len(calls) == 1
    m.unload_all(); assert ext


def test_memory_block_warn_ok():
    assert check_memory(5000, 8000)[0] == "ok"
    assert check_memory(5000, 5500)[0] == "warn"
    assert check_memory(5000, 2000)[0] == "block"
    m = ModelManager()
    try: m.get("big", lambda: object(), 10000, avail_mb=1000); assert False
    except InsufficientMemory as e: assert "free" in str(e)
    assert m.loaded() == []                            # nothing loaded when refused
    msgs = []; m.warn = msgs.append
    m.get("mid", lambda: object(), 5000, avail_mb=5500); assert msgs


def test_estimates_only_from_measurements():
    assert MMod.estimate_seconds("asr", "x|y|cpu", 600) is None            # no measurement -> no estimate
    MMod.record_speed("asr", "x|y|cpu", 100, 150); MMod.record_speed("asr", "x|y|cpu", 100, 250)
    assert MMod.estimate_seconds("asr", "x|y|cpu", 600) == 1200            # mean factor 2.0


def test_pick_device_never_assumes_cuda():
    dev, why = MMod.pick_device("cuda")
    assert dev == "cpu" and why                                             # no torch/CUDA here -> CPU with a reason
    assert MMod.pick_device("auto")[0] == "cpu" and MMod.pick_device("cpu") == ("cpu", "")


# ---------------------------------------------------------------- whisper layer
def fake_whisper(calls):
    mod = types.ModuleType("whisper")
    class M:
        def transcribe(self, audio, language=None, word_timestamps=True, fp16=False, verbose=None):
            calls.append((len(audio) / 16000, language, fp16))
            return {"language": "zh", "segments": [{"start": 1.0, "end": 2.0, "text": " 你好 ",
                    "words": [{"word": " 你", "start": 1.0, "end": 1.4}, {"word": "好", "start": 1.4, "end": 2.0}]}]}
    mod.load_model = lambda name, device="cpu": M()
    return mod


def with_openai(fn):
    calls = []
    sys.modules["whisper"] = fake_whisper(calls)
    old = SR._dist_version
    SR._dist_version = lambda n: "20250625" if n == "openai-whisper" else None
    SR.MM.unload_all()
    try:
        return fn(calls)
    finally:
        SR._dist_version = old; sys.modules.pop("whisper", None); SR.MM.unload_all()


def test_backend_selection():
    old = SR._dist_version
    try:
        SR._dist_version = lambda n: "1" if n == "openai-whisper" else None
        assert SR.pick_backend("auto") == "openai-whisper" and SR.pick_backend("faster-whisper") == "openai-whisper"
        SR._dist_version = lambda n: "1"
        assert SR.pick_backend("auto") == "faster-whisper" and SR.pick_backend("openai-whisper") == "openai-whisper"
        SR._dist_version = lambda n: None
        try: SR.pick_backend("auto"); assert False
        except RuntimeError as e: assert "pip install" in str(e)
    finally:
        SR._dist_version = old


def test_openai_whisper_short_audio_words_and_language():
    def run(calls):
        segs, lang, dur = SR.run_asr(wav(10), "tiny", None, "cpu", "openai-whisper")
        assert lang == "zh" and abs(dur - 10) < 0.01 and len(calls) == 1 and calls[0][2] is False      # fp16 off on CPU
        assert [(w[0], w[1], w[2]) for w in segs[0].words] == [("你", 1.0, 1.4), ("好", 1.4, 2.0)]
    with_openai(run)


def test_long_audio_pieces_keep_exact_global_timestamps_and_split_at_silence():
    def run(calls):
        a = wav(500, tone_at=[179.0, 361.0])                 # quiet everywhere except near the nominal cut points
        segs, lang, _ = SR.run_asr(a, "tiny", None, "cpu", "openai-whisper")
        assert len(calls) == 3 and calls[1][1] == "zh" and calls[2][1] == "zh"      # language detected once, then fixed
        starts = [round(s.start, 1) for s in segs]
        assert starts[0] == 1.0 and starts[1] > 150 and starts[2] > 330 and starts == sorted(starts)
        ends = [s.words[-1][2] for s in segs]
        assert all(e - s.start <= 1.01 for e, s in zip(ends, segs))               # words stay with their own segment
        # pieces cover the whole audio with no gap / overlap
        import app.translator.diarization as D
        audio, sr = D.read_wav_mono(a)
        pts = SR.split_points(audio, sr)
        assert pts[0][0] == 0 and pts[-1][1] == len(audio) and all(x[1] == y[0] for x, y in zip(pts, pts[1:]))
    with_openai(run)


def test_cancel_between_pieces():
    def run(calls):
        n = {"k": 0}
        def cancel():
            n["k"] += 1; return n["k"] > 2
        try: SR.run_asr(wav(500), "tiny", None, "cpu", "openai-whisper", cancel=cancel); assert False
        except Cancelled: pass
        assert len(calls) < 3
    with_openai(run)


def test_transcribe_words_and_cues_end_to_end_with_fake_model():
    def run(calls):
        v = Path(tempfile.mkdtemp()) / "v.mp4"; v.write_bytes(b"x")
        w = wav(10)
        SR._wav_for = lambda video, cancel: w
        words, lang = SR.transcribe_words(v, "tiny", None, "cpu")
        assert [(x.text, x.start, x.end) for x in words] == [("你", 1.0, 1.4), ("好", 1.4, 2.0)] and lang == "zh"
        cues = SR.transcribe(v, "tiny", "zh", "cpu")
        assert len(cues) == 1 and (cues[0].start, cues[0].end) == (1.0, 2.0) and cues[0].text == "你好"
    with_openai(run)


def test_model_too_big_for_ram_gives_clear_error_not_a_crash():
    def run(calls):
        old = MMod.available_ram_mb
        MMod._mem = lambda: (8000, 1500)
        try:
            SR.run_asr(wav(5), "large-v3", None, "cpu", "openai-whisper"); assert False
        except InsufficientMemory as e:
            assert "RAM" in str(e) and "smaller model" in str(e)
        finally:
            import importlib; importlib.reload(MMod)
    with_openai(run)
