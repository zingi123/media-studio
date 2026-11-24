import sys, types, time, tempfile, os, threading, json, http.server
from pathlib import Path
from app.ai import providers as P
from app.translator import diarization as D
from app.translator import smart_script as SS
from app.translator.script_builder import Word
from app.translator import project_state as PS
from app.shared.ffmpeg_util import Cancelled
from test_environment import serve, handler            # reuse mock server helpers


def O(**kw): return types.SimpleNamespace(**kw)


# ---------------------------------------------------------------- error classification over REST
def test_error_kinds_over_rest():
    srv, url = serve({("GET", "/v1/models"): (401, {"error": "invalid x-api-key sk-ant-LEAKLEAK123456"}),
                      ("GET", "/models"): (404, {"error": "models/foo is not found"})})
    try:
        try: P.ClaudeProvider("sk-ant-LEAKLEAK123456", "", url).list_models(); assert False
        except P.ProviderError as e: assert e.kind == "auth" and "LEAKLEAK" not in str(e) and "authentication" in str(e)
        try: P.GeminiProvider("k" * 10, "", url).list_models(); assert False
        except P.ProviderError as e: assert e.kind == "model"
    finally: srv.shutdown()


def test_quota_error_is_clear_and_retried():
    n = {"c": 0}
    srv, url = serve({("POST", "/v1/messages"): lambda: (n.__setitem__("c", n["c"] + 1) or (429, {"error": {"message": "Your credit balance is too low"}}))})
    try:
        c = P.ClaudeProvider("sk-ant-RATERATE123456", "m", url); c.sleep = lambda s: None
        try: c.generate("x"); assert False
        except P.ProviderError as e: assert e.kind == "quota" and "quota" in str(e) and "credit" in str(e)
        assert n["c"] == 4                                           # 1 try + 3 retries, then a clear message
    finally: srv.shutdown()


def test_timeout_and_no_network():
    srv, url = serve({("GET", "/slow"): lambda: (time.sleep(1.2) or (200, {}))})
    try:
        try: P._http("GET", url + "/slow", timeout=0.3); assert False
        except P.ProviderError as e: assert e.kind == "timeout" and "Timed out" in str(e)
    finally: srv.shutdown()
    try: P._http("GET", "http://127.0.0.1:9/x", timeout=2); assert False
    except P.ProviderError as e: assert e.kind == "network" and "Cannot connect" in str(e)


def test_missing_key_and_missing_model_messages():
    r = P.GeminiProvider("", "").test_connection()
    assert not r.ok and "No Gemini key" in r.message and not P.ClaudeProvider("", "").test_request().ok
    srv, url = serve({("GET", "/v1/models"): (200, {"data": [{"id": "claude-sonnet-5-5"}]})})
    try:
        r = P.ClaudeProvider("sk-ant-ABCDEFGH123456", "claude-nonexistent", url).test_connection()
        assert not r.ok and "NOT in the list" in r.message
    finally: srv.shutdown()


# ---------------------------------------------------------------- SDK transports (fake SDK modules mirror the documented API)
class RateLimitError(Exception):
    status_code = 429
class AuthenticationError(Exception):
    status_code = 401


def fake_anthropic(behaviour="ok", calls=None):
    m = types.ModuleType("anthropic")
    class Models:
        def list(self, limit=20): return O(data=[O(id="claude-sonnet-5-5"), O(id="claude-haiku-5-5")])
    class Messages:
        def create(self, **kw):
            if calls is not None: calls.append(kw)
            if behaviour == "quota": raise RateLimitError("rate limit exceeded")
            if behaviour == "auth": raise AuthenticationError("invalid x-api-key sk-ant-SDKLEAK1234567")
            if behaviour == "mismatch": raise AttributeError("'Messages' object has no attribute 'create'")
            return O(content=[O(type="text", text="OK-from-sdk")])
    class Anthropic:
        def __init__(self, api_key=None, timeout=None, max_retries=None):
            self.models, self.messages = Models(), Messages(); self.timeout = timeout
    m.Anthropic = Anthropic
    return m


def test_claude_sdk_path_and_error_mapping():
    old = P.dist_version; P.dist_version = lambda n: "2.12.0"
    try:
        calls = []
        sys.modules["anthropic"] = fake_anthropic("ok", calls)
        c = P.ClaudeProvider("sk-ant-SDKKEY123456", "claude-sonnet-5-5", transport="auto")
        assert c.sdk_version() == "2.12.0" and c._use_sdk()
        assert c.list_models() == ["claude-sonnet-5-5", "claude-haiku-5-5"]
        assert c.generate("hi", system="s", json_mode=True) == "OK-from-sdk"
        assert calls[0]["model"] == "claude-sonnet-5-5" and "JSON" in calls[0]["system"]
        assert P.ClaudeProvider("k", "m", transport="rest")._use_sdk() is False
        sys.modules["anthropic"] = fake_anthropic("auth")
        try: c.generate("x"); assert False
        except P.ProviderError as e: assert e.kind == "auth" and "SDKLEAK" not in str(e)
        sys.modules["anthropic"] = fake_anthropic("quota"); c.sleep = lambda s: None
        try: c.generate("x"); assert False
        except P.ProviderError as e: assert e.kind == "quota"
        # SDK whose API differs from what we expect -> graceful fallback to the built-in HTTPS client
        sys.modules["anthropic"] = fake_anthropic("mismatch")
        orig = P._http
        P._http = lambda *a, **k: {"content": [{"type": "text", "text": "OK-from-rest"}]}
        try: assert c.generate("x") == "OK-from-rest"
        finally: P._http = orig
    finally:
        P.dist_version = old; sys.modules.pop("anthropic", None)


def fake_genai(behaviour="ok"):
    google = types.ModuleType("google"); genai = types.ModuleType("google.genai"); types_m = types.ModuleType("google.genai.types")
    class APIError(Exception):
        def __init__(s, code, message): super().__init__(message); s.code, s.message = code, message
    types_m.HttpOptions = lambda timeout=None: O(timeout=timeout)
    types_m.GenerateContentConfig = lambda **kw: O(**kw)
    class Models:
        def list(self): return [O(name="models/gemini-2.5-flash", supported_actions=["generateContent"]), O(name="models/embed", supported_actions=["embedContent"])]
        def generate_content(self, model, contents, config):
            if behaviour == "quota": raise APIError(429, "RESOURCE_EXHAUSTED: You exceeded your current quota")
            return O(text="OK-gemini-sdk", cfg=config)
    class Client:
        def __init__(self, api_key=None, http_options=None): self.models = Models()
    genai.Client, genai.types = Client, types_m
    google.genai = genai
    return {"google": google, "google.genai": genai, "google.genai.types": types_m}


def test_gemini_sdk_path_and_quota_mapping():
    old = P.dist_version; P.dist_version = lambda n: "3.6.1"
    try:
        sys.modules.update(fake_genai("ok"))
        g = P.GeminiProvider("AIzaSDKKEY00000000000000", "gemini-2.5-flash", transport="auto"); g.sleep = lambda s: None
        assert g.list_models() == ["gemini-2.5-flash"]
        assert g.generate("hi", system="sys", json_mode=True) == "OK-gemini-sdk"
        sys.modules.update(fake_genai("quota"))
        try: g.generate("x"); assert False
        except P.ProviderError as e: assert e.kind == "quota" and "quota" in str(e)
    finally:
        P.dist_version = old
        for k in ("google", "google.genai", "google.genai.types"): sys.modules.pop(k, None)


# ---------------------------------------------------------------- pyannote 4 API usage + failures (fake pyannote/torch)
def fake_torch():
    t = types.ModuleType("torch")
    t.cuda = O(is_available=lambda: False)
    t.device = lambda s: s
    class Tensor:
        def __init__(s, a): s.a = a
        def unsqueeze(s, i): return s
    t.from_numpy = lambda a: Tensor(a)
    return t


def fake_pyannote(calls, mode="ok"):
    pa = types.ModuleType("pyannote"); pau = types.ModuleType("pyannote.audio")
    class Seg:
        def __init__(s, a, b): s.start, s.end = a, b
    class Ann:
        def __init__(s, it): s.it = it
        def itertracks(s, yield_label=False): return [(Seg(a, b), None, l) for a, b, l in s.it]
    class Out:
        speaker_diarization = Ann([(0.0, 3.0, "SPEAKER_00"), (2.5, 6.0, "SPEAKER_01")])
        exclusive_speaker_diarization = Ann([(0.0, 3.0, "SPEAKER_00"), (3.0, 6.0, "SPEAKER_01")])
        speaker_embeddings = [[1.0, 0.0], [0.0, 1.0]]
    class Pipe:
        def to(self, d): calls.append(("to", d)); return self
        def __call__(self, inp, **kw): calls.append(("call", sorted(inp), kw)); return Out()
    class Pipeline:
        @staticmethod
        def from_pretrained(model, token=None):                     # use_auth_token= would raise TypeError here, like the real pyannote 4
            calls.append(("from_pretrained", model, token))
            if mode == "fail": raise OSError("Cannot download model: connection reset")
            if mode == "none": return None
            return Pipe()
    pau.Pipeline = Pipeline; pa.audio = pau
    return {"pyannote": pa, "pyannote.audio": pau}


def wavfile(sec=7):
    import wave, numpy as np
    p = Path(tempfile.mkdtemp()) / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(np.zeros(16000 * sec, dtype=np.int16).tobytes())
    return p


def with_pyannote(mode, fn):
    calls = []; saved = {k: sys.modules.get(k) for k in ("torch", "pyannote", "pyannote.audio")}
    sys.modules["torch"] = fake_torch(); sys.modules.update(fake_pyannote(calls, mode))
    oldv, oldc = D.pyannote_version, D.check_model_access
    D.pyannote_version = lambda: "4.0.7"; D.check_model_access = lambda t, m=D.DEFAULT_MODEL, timeout=15: D.AccessReport(True, "ok", "ok", "u")
    D._PIPE.clear()
    try: return fn(calls)
    finally:
        D.pyannote_version, D.check_model_access = oldv, oldc; D._PIPE.clear()
        for k, v in saved.items():
            if v is None: sys.modules.pop(k, None)
            else: sys.modules[k] = v


def test_pyannote4_called_with_token_keyword_and_waveform_dict():
    def run(calls):
        r = D.diarize(wavfile(), "hf_TESTTOKEN123456", device="cpu")
        assert calls[0] == ("from_pretrained", "pyannote/speaker-diarization-community-1", "hf_TESTTOKEN123456")
        assert ("to", "cpu") in calls and next(c for c in calls if c[0] == "call")[1] == ["sample_rate", "waveform"]
        assert r.speakers() == ["SPEAKER_00", "SPEAKER_01"] and r.overlaps() == [(2.5, 3.0, ["SPEAKER_00", "SPEAKER_01"])]
        assert set(r.embeddings) == {"SPEAKER_00", "SPEAKER_01"} and [t.speaker for t in r.exclusive] == ["SPEAKER_00", "SPEAKER_01"]
    with_pyannote("ok", run)


def test_pyannote_model_download_failure_and_no_access_messages_without_token_leak():
    def f1(calls):
        try: D.diarize(wavfile(), "hf_SECRETTOKEN123456"); assert False
        except D.DiarizationError as e: assert "Could not load" in str(e) and "SECRETTOKEN" not in str(e)
    with_pyannote("fail", f1)
    def f2(calls):
        try: D.diarize(wavfile(), "hf_SECRETTOKEN123456"); assert False
        except D.DiarizationError as e: assert "no access" in str(e).lower() or "no pipeline" in str(e).lower()
    with_pyannote("none", f2)
    def f3(calls):
        old = D.pyannote_version; D.pyannote_version = lambda: "3.1.1"
        try: D.diarize(wavfile(), "hf_x123456789"); assert False
        except D.DiarizationError as e: assert ">= 4.0" in str(e)
    with_pyannote("ok", f3)


def test_whisper_model_download_failure_surfaces_and_nothing_stays_loaded():
    from app.translator import speech_recognition as SR
    from test_models_asr import wav
    m = types.ModuleType("whisper")
    def load(name, device="cpu"): raise OSError("download failed: connection reset")
    m.load_model = load; sys.modules["whisper"] = m
    old = SR._dist_version; SR._dist_version = lambda n: "1" if n == "openai-whisper" else None
    try:
        try: SR.run_asr(wav(5), "small", None, "cpu", "openai-whisper"); assert False
        except OSError as e: assert "download failed" in str(e)
        assert SR.MM.loaded() == []
    finally: SR._dist_version = old; sys.modules.pop("whisper", None)


# ---------------------------------------------------------------- interrupted processing -> resume
def fake_video():
    p = Path(tempfile.mkdtemp()) / "ep.mp4"; p.write_bytes(os.urandom(4000)); return p


def test_cancelled_whisper_resumes_without_redoing_extraction():
    calls = []
    SS.extract_audio = lambda v, out, cancel=None: (calls.append("extract"), Path(out).write_bytes(b"RIFF"))[1]
    state = {"boom": True}
    def tw(video, **kw):
        calls.append("whisper")
        if state["boom"]: raise Cancelled()
        return [Word("你", 1.0, 1.3), Word("好", 1.4, 1.7)], "zh"
    SS.transcribe_words = tw
    import app.translator.voice_detect as VD
    VD.detect_voice_types = lambda video, saved, **kw: {"speakers": [{"id": "Speaker 1", "type": "Women"}], "segments": [(0.5, 2.0, "Speaker 1")], "notes": [], "diar_method": "pyannote", "merge_suggestions": [], "overlaps": [], "embeddings": {}}
    v = fake_video(); kw = dict(use_emotion=False, use_motion=False, use_story=False, lang="zh", model="small")
    try: SS.scan_smart_script(v, **kw); assert False
    except Cancelled: pass
    calls.clear(); state["boom"] = False
    cues, _ = SS.scan_smart_script(v, **kw)
    assert "extract" not in calls and calls.count("whisper") == 1 and len(cues) == 1
