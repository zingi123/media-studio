import json, threading, http.server, os
from app.ai import providers as P
from app.ai import khmer_prompt as KP
from app.shared import secrets as SEC


class FakeKeyring:
    def __init__(s): s.d = {}
    def get_password(s, svc, k): return s.d.get((svc, k))
    def set_password(s, svc, k, v): s.d[(svc, k)] = v
    def delete_password(s, svc, k): s.d.pop((svc, k), None)


def serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def make_handler(routes, seen):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _do(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode() if n else ""
            seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            r = routes.get((self.command, self.path.split("?")[0]))
            if callable(r): r = r(body, self.headers)
            code, data = r if r else (404, {"error": "nf"})
            raw = json.dumps(data).encode()
            self.send_response(code); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        do_GET = do_POST = _do
    return H


def test_secrets_store_and_redaction():
    SEC._backend_override = FakeKeyring()
    try:
        SEC.set_secret("claude_key", "sk-ant-SECRETSECRET123456")
        assert SEC.get_secret("claude_key") == "sk-ant-SECRETSECRET123456" and SEC.backend() == "OS credential store"
        msg = SEC.redact("failed with key sk-ant-SECRETSECRET123456 and hf_abcdefghijklmnop and AIzaSyA1234567890123456789012")
        assert "SECRETSECRET" not in msg and "hf_abcdefghijklmnop" not in msg and "AIzaSyA12345" not in msg
        os.environ["HF_TOKEN"] = "hf_envtoken123456"
        assert SEC.get_secret("hf_token") == "hf_envtoken123456"
    finally:
        os.environ.pop("HF_TOKEN", None); SEC._backend_override = None


def test_settings_migrates_plaintext_keys_out_of_json():
    import importlib, json as js
    from app.shared import paths
    SEC._backend_override = FakeKeyring()
    try:
        f = paths.data_dir() / "settings.json"
        f.write_text(js.dumps({"gemini_key": "AIzaOLDPLAINTEXTKEY00000", "hf_token": "hf_oldtoken1234567", "split_silence_s": 1.5}))
        from app.shared import settings as S
        st = S.Settings()
        assert st.get("gemini_key") == "AIzaOLDPLAINTEXTKEY00000" and st.get("split_silence_s") == 3.0
        txt = f.read_text()
        assert "OLDPLAINTEXT" not in txt and "hf_oldtoken" not in txt          # no secret left in settings.json
        st.set("claude_key", "sk-ant-abcdefghijk"); st.save()
        assert "sk-ant-abcdefghijk" not in f.read_text()
    finally:
        SEC._backend_override = None


def test_gemini_claude_ollama_against_mock_server():
    seen = []
    routes = {
        ("GET", "/models"): (200, {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                                              {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}]}),
        ("POST", "/models/gemini-2.5-flash:generateContent"): (200, {"candidates": [{"content": {"parts": [{"text": "[{\"id\":1,\"km\":\"សួស្តី\"}]"}]}}]}),
        ("GET", "/v1/models"): (200, {"data": [{"id": "claude-sonnet-5-5"}]}),
        ("POST", "/v1/messages"): (200, {"content": [{"type": "text", "text": "ok"}]}),
        ("GET", "/api/version"): (200, {"version": "0.9"}),
        ("GET", "/api/tags"): (200, {"models": [{"name": "qwen2.5:7b"}]}),
        ("POST", "/api/chat"): (200, {"message": {"content": "hi"}}),
    }
    srv, url = serve(make_handler(routes, seen))
    try:
        g = P.GeminiProvider("AIzaFAKEKEY0000000000000", "gemini-2.5-flash", url)
        t = g.test_connection(); assert t.ok and t.models == ["gemini-2.5-flash"]
        assert "km" in g.generate("x", json_mode=True)
        assert any(h.get("x-goog-api-key") == "AIzaFAKEKEY0000000000000" for _, _, h, _ in seen)       # key in header, never in URL
        assert not any("AIzaFAKE" in p for _, p, _, _ in seen)
        c = P.ClaudeProvider("sk-ant-FAKEFAKEFAKE", "claude-sonnet-5-5", url)
        assert c.test_connection().ok and c.generate("x", system="s") == "ok"
        assert any(h.get("x-api-key") == "sk-ant-FAKEFAKEFAKE" and h.get("anthropic-version") for _, _, h, _ in seen)
        assert not c.__class__(" ", "").configured()[0]
        o = P.OllamaProvider("", "qwen2.5:7b", url)
        st = o.status(); assert st["running"] and st["models"] == ["qwen2.5:7b"] and o.is_model_downloaded("qwen2.5:7b")
        assert o.generate("x") == "hi"
        o2 = P.OllamaProvider("", "llama3", url)
        assert not o2.test_connection().ok and "ollama pull llama3" in o2.test_connection().message
        try: o2.generate("x"); assert False
        except P.ProviderError as e: assert "not downloaded" in str(e)
    finally:
        srv.shutdown()


def test_ollama_running_without_models_is_not_claimed_ready():
    routes = {("GET", "/api/version"): (200, {"version": "0.9"}), ("GET", "/api/tags"): (200, {"models": []})}
    srv, url = serve(make_handler(routes, []))
    try:
        r = P.OllamaProvider("", "", url).test_connection()
        assert not r.ok and "ollama pull" in r.message
    finally:
        srv.shutdown()


def test_ollama_not_running_message():
    r = P.OllamaProvider("", "", "http://127.0.0.1:9").status()
    assert not r["running"] and r["message"]


def test_http_errors_never_leak_key_and_are_actionable():
    key = "sk-ant-LEAKLEAKLEAK1234"
    routes = {("GET", "/v1/models"): (401, {"error": {"message": f"invalid x-api-key {key}"}})}
    srv, url = serve(make_handler(routes, []))
    try:
        r = P.ClaudeProvider(key, "", url).test_connection()
        assert not r.ok and "LEAKLEAK" not in r.message and "authentication" in r.message
    finally:
        srv.shutdown()


def test_retry_on_429_then_success():
    state = {"n": 0}
    def route(body, h):
        state["n"] += 1
        return (429, {}) if state["n"] < 3 else (200, {"content": [{"type": "text", "text": "done"}]})
    srv, url = serve(make_handler({("POST", "/v1/messages"): route}, []))
    try:
        c = P.ClaudeProvider("sk-ant-RETRYKEY123456", "m", url); c.sleep = lambda s: None
        assert c.generate("x") == "done" and state["n"] == 3
    finally:
        srv.shutdown()


def test_hf_token_and_gated_access_messages():
    from app.translator import diarization as D
    seen = []
    # whoami ok, model file 403 -> clear "accept the conditions" message
    routes = {("GET", "/api/whoami-v2"): (200, {"name": "tester"}),
              ("GET", "/pyannote/speaker-diarization-community-1/resolve/main/config.yaml"): (403, {})}
    srv, url = serve(make_handler(routes, seen))
    old = D.HF_BASE; D.HF_BASE = url
    try:
        r = D.check_model_access("hf_faketoken123456")
        assert not r.ok and r.stage == "access" and "accept" in r.message and r.user == "tester"
        routes[("GET", "/pyannote/speaker-diarization-community-1/resolve/main/config.yaml")] = (200, {})
        assert D.check_model_access("hf_faketoken123456").ok
        routes[("GET", "/api/whoami-v2")] = (401, {})
        assert D.check_model_access("hf_bad").stage == "token"
    finally:
        D.HF_BASE = old; srv.shutdown()


def test_khmer_prompt_parse_and_fit():
    assert KP.parse_json_array('```json\n[{"id":1,"km":"ក"},{"id":2,"km":"ខ"}]\n```') == {1: "ក", 2: "ខ"}
    assert KP.parse_json_array('Here you go: [{"id": 3, "km": "គ"}] thanks') == {3: "គ"}
    assert KP.fit_warning("ក" * 100, 2.0) and not KP.fit_warning("ក" * 10, 2.0)
    pr = KP.build_prompt([{"id": 1, "text": "你好", "sec": 1.0, "speaker": "S1"}], ["前文"], "李明=លី មីង", "S1 (Women)")
    assert "你好" in pr and "លី មីង" in pr and "ONLY" in pr


def test_translate_cues_with_mock_provider_keeps_timestamps_and_batches():
    from app.translator.translator_engine import translate_cues
    from app.translator.subtitles import Cue
    calls = []
    class Fake(P.AIProvider):
        id, label, needs_key = "fake", "Fake", False
        def generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=1):
            calls.append(prompt)
            ids = [o["id"] for o in json.loads(prompt.split("Lines:\n")[1])]
            return json.dumps([{"id": i, "km": f"ខ្មែរ{i}"} for i in ids])
    cues = [Cue(i * 2.0, i * 2.0 + 1.5, f"中文{i}", speaker="S1" if i % 2 else "S2", emotion="angry") for i in range(5)]
    cues[1].translation = "manual"        # user edit is kept (only_empty)
    translate_cues(cues, provider=Fake(), batch_size=2)
    assert cues[1].translation == "manual" and cues[0].translation == "ខ្មែរ0" and cues[4].translation == "ខ្មែរ4"
    assert [(c.start, c.end) for c in cues] == [(i * 2.0, i * 2.0 + 1.5) for i in range(5)]
    assert len(calls) == 2 and "angry" in calls[0]
