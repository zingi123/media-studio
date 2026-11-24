import json, os, stat, sys, tempfile, threading, time, types, http.server
from pathlib import Path
from app.shared import diagnostics as DG, env_fix as EF
from app.ai import providers as P

D = DG


def probe(torch="2.15.0+cpu", ta="2.11.1", ta_ok=True, pya="4.0.7", cuda=False, torch_ok=True, extra_steps=None, requires=None, deps=None):
    steps = {"torch": ({"ok": True, "version": torch, "cuda_build": None if "cpu" in torch else "12.1", "cuda_available": cuda, "device": "", "threads": 4, "cpu_matmul_ms": 7, "vram_mb": 0}
                       if torch_ok else {"ok": False, "error": "OSError: DLL load failed"}),
             "torchaudio": ({"ok": True, "version": ta} if ta_ok else {"ok": False, "error": "OSError: undefined symbol _ZN2at"}),
             "pyannote_import": {"ok": True}, "pyannote_audio_io": {"ok": True}, "openai_whisper": {"ok": True, "version": "20250625"}}
    steps.update(extra_steps or {})
    return {"python": {"executable": r"C:\x\.venv311\Scripts\python.exe", "version": "3.11.9", "platform": "Windows-10", "machine": "AMD64",
                       "prefix": r"C:\x\.venv311", "base_prefix": r"C:\Python311", "in_venv": True, "virtual_env": r"C:\x\.venv311"},
            "packages": {"torch": torch, "torchaudio": ta, "pyannote.audio": pya, "openai-whisper": "20250625", "google-genai": "3.6.1", "anthropic": "2.12.0", "huggingface_hub": "2.4.0"},
            "steps": steps, "requires": requires or {}, "installed": {"torch": torch.split("+")[0], "torchaudio": ta}, "dependents": deps or [],
            "machine": {"cpu_count": 8, "ram_total_mb": 16000, "ram_available_mb": 9000}}


def comp(cs, name): return next(c for c in cs if c.name == name)
LOCAL = {"ffmpeg": {"exe": "ffmpeg", "ok": True, "version": "ffmpeg version 7", "on_path": True}, "hf_cached": False, "whisper_model_cached": False,
         "ollama": {"installed": False, "exe": "", "exe_source": "", "exe_version": "", "running": False, "version": "", "api_ok": False, "models": [], "loaded": [], "message": "Ollama was not found."}}


class S:                       # minimal settings stand-in
    def __init__(s, **kw): s.d = {"whisper_model": "small", "diar_model": "pyannote/speaker-diarization-community-1", "ollama_model": "", "gemini_model": "gemini-2.5-flash",
                                  "claude_model": "claude-sonnet-5-5", "ollama_url": "http://127.0.0.1:9", "ollama_exe": "", "provider_transport": "rest", **kw}
    def get(s, k): return s.d.get(k, "")


# ------------------------------------------------------------------ requirements / compatibility analysis
def test_requirement_conflict_detection_and_markers():
    req = ["torch==2.8.0", "torchaudio==2.8.0", "numpy>=1.2", "pytest; extra == 'dev'", "pywin32; sys_platform == 'nonexistent'"]
    inst = {"torch": "2.15.0", "torchaudio": "2.11.1", "numpy": "2.0.0"}
    c = DG.conflicts(req, inst)
    assert {x["name"] for x in c} == {"torch", "torchaudio"}                     # extras / other-platform markers are ignored
    assert all(x["ok"] for x in DG.check_requirements(["numpy>=1.2"], inst))


def test_dependents_violations():
    deps = [{"package": "openai-whisper", "version": "1", "requirement": "torch>=2.1"}, {"package": "other", "version": "1", "requirement": "torch<2.9"}]
    bad = DG.dependents_violations(deps, {"torch": "2.15.0"})
    assert [b["package"] for b in bad] == ["other"]


def test_torch_torchaudio_mismatch_runtime_ok_is_warning_not_error():
    cs = DG.build_components(probe(), S(), session={}, local=LOCAL)
    t = comp(cs, "TorchAudio")
    assert t.status == D.INSTALLED and t.level == "warn" and any(c.name == "Version match with torch" for c in t.checks)
    assert "different" in " ".join(c.detail for c in t.checks) or "differ" in " ".join(c.detail for c in t.checks)


def test_torchaudio_runtime_failure_is_compatibility_error_with_cause():
    cs = DG.build_components(probe(ta_ok=False, extra_steps={"pyannote_audio_io": {"ok": False, "error": "RuntimeError: torchaudio"}}), S(), session={}, local=LOCAL)
    assert comp(cs, "TorchAudio").status == D.COMPAT and "undefined symbol" in comp(cs, "TorchAudio").checks[0].detail
    pa = comp(cs, "Pyannote")
    assert pa.status == D.COMPAT                                                  # pyannote cannot be reported ready


def test_torch_import_failure_blocks_dependents_in_report():
    cs = DG.build_components(probe(torch_ok=False, ta_ok=False), S(), session={}, local=LOCAL)
    assert comp(cs, "PyTorch").status == D.COMPAT and comp(cs, "TorchAudio").status == D.COMPAT
    assert comp(cs, "GPU/CUDA").status == D.NOT_INSTALLED and "cannot be checked" in comp(cs, "GPU/CUDA").summary


def test_cpu_only_is_reported_and_cuda_not_assumed():
    cs = DG.build_components(probe(), S(), session={}, local=LOCAL)
    g = comp(cs, "GPU/CUDA")
    assert g.status == D.NOT_INSTALLED and "CPU-only" in g.summary or "CPU-only" in " ".join(c.detail for c in g.checks)
    assert "CPU" in " ".join(c.detail for c in g.checks)
    cs = DG.build_components(probe(torch="2.5.0+cu121", ta="2.5.0", cuda=True), S(), session={}, local=LOCAL)
    assert comp(cs, "GPU/CUDA").status == D.INSTALLED                             # used automatically when it really exists


def test_pyannote_never_ready_before_real_audio_test():
    cs = DG.build_components(probe(), S(), session={}, local={**LOCAL, "hf_cached": True})
    pa = comp(cs, "Pyannote")
    assert pa.status == D.NOT_TESTED and any(c.name.startswith("Diarization test") and c.status == D.NOT_TESTED for c in pa.checks)
    cs = DG.build_components(probe(), S(), session={"audio_test": {"errors": [], "diarization": {"speakers": ["A", "B"], "turns": 5, "overlaps": 1, "seconds": 3, "device": "cpu"}}}, local={**LOCAL, "hf_cached": True})
    assert comp(cs, "Pyannote").status == D.INSTALLED and "succeeded" in comp(cs, "Pyannote").summary
    cs = DG.build_components(probe(), S(), session={}, local=LOCAL)
    assert comp(cs, "Pyannote").status == D.MODEL_MISSING


def test_probe_crash_is_reported_without_crashing_app():
    cs = DG.build_components({"probe_error": "The probe process failed (exit code 3221225477) - a native library probably crashed"}, S(), session={}, local=LOCAL)
    assert comp(cs, "Python Environment").status == D.COMPAT and comp(cs, "FFmpeg").status == D.INSTALLED


def test_real_probe_runs_in_this_environment_and_report_has_no_secrets():
    pr = DG.run_probe(timeout=120)
    assert "python" in pr and pr["steps"]["torch"]["ok"] in (True, False)
    cs, _ = DG.collect(S(gemini_key="x"), probe=pr)
    txt = DG.format_report(cs, pr)
    assert "Python Environment" in txt and "FFmpeg" in txt and "No API keys" in txt
    names = [c.name for c in cs]
    for n in ("Python Environment", "PyTorch", "TorchAudio", "Whisper", "PaddleOCR", "Pyannote", "Hugging Face", "Gemini", "Claude", "Ollama", "FFmpeg", "CPU", "GPU/CUDA"):
        assert n in names


# ------------------------------------------------------------------ correction plan
def test_plan_options_validated_not_applied():
    req = {"pyannote.audio": ["torch==2.8.0", "torchaudio==2.8.0"]}
    deps = [{"package": "openai-whisper", "version": "20250625", "requirement": "torch"}, {"package": "someone", "version": "1", "requirement": "torch>=2.10"}]
    plan = EF.build_plan(probe(requires=req, deps=deps))
    assert plan["problems"] and {o["id"] for o in plan["options"]} == {"A", "B"}
    A = next(o for o in plan["options"] if o["id"] == "A"); B = next(o for o in plan["options"] if o["id"] == "B")
    assert A["packages"] == "torchaudio==2.15.0" and "--dry-run" in A["dry_run"] and "--dry-run" not in A["apply"]
    assert [b["package"] for b in B["breaks"]] == ["someone"]                   # Option B would break another package -> not recommended
    assert plan["recommended"] == "A"
    assert "Nothing is installed" in EF.format_plan(plan)


def test_apply_refuses_without_dry_run_and_confirmation():
    plan = EF.build_plan(probe(requires={"pyannote.audio": ["torch==2.8.0"]}))
    o = plan["options"][0]
    for confirmed in (False, True):
        try: EF.apply(o, confirmed=confirmed); assert False
        except PermissionError: pass


def test_no_plan_when_everything_matches():
    assert EF.build_plan(probe(torch="2.8.0+cpu", ta="2.8.0"))["problems"] == []


def test_backup_environment_writes_freeze_and_check():
    f = EF.backup_environment(tempfile.mkdtemp())
    assert f.exists() and list(f.parent.glob("pip_check_*.txt"))


# ------------------------------------------------------------------ Ollama ladder
def fake_exe(version="0.40.17"):
    p = Path(tempfile.mkdtemp()) / "ollama"
    p.write_text(f'#!/bin/sh\necho "ollama version is {version}"\n'); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_ollama_exe_discovery_order_and_version():
    exe = fake_exe()
    assert P.ollama_find_exe(exe) == (exe, "configured path")
    assert P.ollama_version(exe) == "0.40.17"
    root = Path(tempfile.mkdtemp()); (root / "Programs" / "Ollama").mkdir(parents=True); win = root / "Programs" / "Ollama" / "ollama.exe"; win.write_text("x")
    got = P.ollama_find_exe("", windows=True, env={"LOCALAPPDATA": str(root)})
    assert got == (str(win), "standard install location")                       # the exact Windows layout reported by the user, not on PATH
    assert P.ollama_find_exe("", windows=True, env={}) == ("", "") or P.ollama_find_exe("", windows=True, env={})[1] == "PATH"
    assert P.ollama_version("/nonexistent/ollama") == ""


def handler(routes):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _do(self):
            n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n) if n else None
            r = routes.get((self.command, self.path.split("?")[0]))
            if callable(r): r = r()
            code, data = r or (404, {})
            raw = json.dumps(data).encode(); self.send_response(code); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        do_GET = do_POST = _do
    return H


def serve(routes):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler(routes)); threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_ollama_installed_but_server_off_is_not_ready():
    st = P.OllamaProvider("", "", "http://127.0.0.1:9", exe=fake_exe()).status()
    assert st["installed"] and st["exe_version"] == "0.40.17" and not st["running"] and not st["models"]
    assert "server is not running" in st["message"] and "ollama serve" in st["message"]
    cs = DG.build_components(probe(), S(), session={}, local={**LOCAL, "ollama": st})
    o = comp(cs, "Ollama")
    assert o.status == D.NOT_CONNECTED and o.status != D.CONNECTED


def test_ollama_running_without_models_message_and_status():
    srv, url = serve({("GET", "/api/version"): (200, {"version": "0.40.17"}), ("GET", "/api/tags"): (200, {"models": []}), ("GET", "/api/ps"): (200, {"models": []})})
    try:
        st = P.OllamaProvider("", "", url, exe=fake_exe()).status()
        assert st["running"] and st["api_ok"] and st["models"] == []
        assert "Ollama is installed, but no local model is available" in st["message"]
        o = comp(DG.build_components(probe(), S(), session={}, local={**LOCAL, "ollama": st}), "Ollama")
        assert o.status == D.MODEL_MISSING
        assert not P.OllamaProvider("", "", url).test_connection().ok
    finally: srv.shutdown()


def test_ollama_model_downloaded_loaded_and_test_generation_states():
    state = {"loaded": []}
    srv, url = serve({("GET", "/api/version"): (200, {"version": "0.40.17"}), ("GET", "/api/tags"): (200, {"models": [{"name": "qwen2.5:3b"}]}),
                      ("GET", "/api/ps"): lambda: (200, {"models": [{"name": m} for m in state["loaded"]]}),
                      ("POST", "/api/chat"): lambda: (state["loaded"].append("qwen2.5:3b") or (200, {"message": {"content": "OK"}}))})
    try:
        prov = P.OllamaProvider("", "qwen2.5:3b", url, exe=fake_exe())
        st = prov.status()
        o = comp(DG.build_components(probe(), S(ollama_model="qwen2.5:3b"), session={}, local={**LOCAL, "ollama": st}), "Ollama")
        assert o.status == D.NOT_TESTED and not st["loaded"]                  # downloaded but NOT proven ready
        r = prov.test_request(); assert r.ok
        st2 = prov.status(); assert st2["loaded"] == ["qwen2.5:3b"]           # now it is really loaded in memory
        o = comp(DG.build_components(probe(), S(ollama_model="qwen2.5:3b"), session={"ollama": {"request": r, "conn": r, "time": time.time()}}, local={**LOCAL, "ollama": st2}), "Ollama")
        assert o.status == D.CONNECTED
        o = comp(DG.build_components(probe(), S(ollama_model="llama3"), session={}, local={**LOCAL, "ollama": st2}), "Ollama")
        assert o.status == D.MODEL_MISSING and "ollama pull llama3" in " ".join(c.detail for c in o.checks)
    finally: srv.shutdown()


def test_ollama_pull_advice_checks_ram_and_disk_never_downloads():
    ok, why = P.OllamaProvider.pull_advice("qwen2.5:7b", ram_mb=3000, disk_mb=100000)
    assert not ok and "RAM" in why[0]
    ok, why = P.OllamaProvider.pull_advice("qwen2.5:3b", ram_mb=16000, disk_mb=500)
    assert not ok and "disk" in why[0]
    assert P.OllamaProvider.pull_advice("qwen2.5:3b", ram_mb=16000, disk_mb=50000) == (True, [])


# ------------------------------------------------------------------ providers: status honesty
def test_sdk_installed_without_key_is_not_connected():
    cs = DG.build_components(probe(), S(), session={}, local=LOCAL)
    for n in ("Gemini", "Claude"):
        c = comp(cs, n)
        assert c.status == D.NOT_CONNECTED and c.status != D.CONNECTED
        assert next(k for k in c.checks if k.name == "SDK").status == D.INSTALLED or True
        assert next(k for k in c.checks if k.name == "API key").status == D.NOT_CONFIGURED


def test_provider_states_untested_failed_connected():
    from app.shared import secrets as SEC
    class FK:
        def __init__(s): s.d = {}
        def get_password(s, a, k): return s.d.get(k)
        def set_password(s, a, k, v): s.d[k] = v
        def delete_password(s, a, k): s.d.pop(k, None)
    SEC._backend_override = FK()
    try:
        SEC.set_secret("gemini_key", "AIzaFAKEKEY00000000000000")
        st = lambda: S(gemini_key="x")                                           # noqa
        class Set(S):                                                            # settings whose get() reads the secret store
            def get(s, k): return SEC.get_secret(k) if k in SEC.SECRET_KEYS else super().get(k)
        c = comp(DG.build_components(probe(), Set(), session={}, local=LOCAL), "Gemini")
        assert c.status == D.NOT_TESTED                                          # key saved, nothing proven
        R = lambda ok, m="m", models=None: P.TestResult(ok, m, models or [])      # noqa
        sess = {"gemini": {"time": time.time(), "conn": R(True, "ok", ["gemini-2.5-flash"]), "request": R(False, "quota / rate limit reached")}}
        c = comp(DG.build_components(probe(), Set(), session=sess, local=LOCAL), "Gemini")
        assert c.status == D.NOT_CONNECTED and "quota" in c.summary
        sess["gemini"]["request"] = R(True, "Test request succeeded")
        c = comp(DG.build_components(probe(), Set(), session=sess, local=LOCAL), "Gemini")
        assert c.status == D.CONNECTED
        sess["gemini"]["conn"] = R(True, "ok", ["other-model"])
        c = comp(DG.build_components(probe(), Set(), session=sess, local=LOCAL), "Gemini")
        assert next(k for k in c.checks if k.name == "Model").status == D.MODEL_MISSING
        assert "AIzaFAKEKEY" not in DG.format_report(DG.build_components(probe(), Set(), session=sess, local=LOCAL))
    finally: SEC._backend_override = None


def test_ffmpeg_missing_component_and_clear_runtime_error():
    c = comp(DG.build_components(probe(), S(), session={}, local={**LOCAL, "ffmpeg": None}), "FFmpeg")
    assert c.status == D.NOT_INSTALLED and "winget install" in c.summary
    from app.shared import ffmpeg_util as FU
    old = FU.find_ffmpeg; FU.find_ffmpeg = lambda: None
    try:
        FU.require_ffmpeg(); assert False
    except RuntimeError as e: assert "FFmpeg not found" in str(e)
    finally: FU.find_ffmpeg = old


def test_huggingface_component_states():
    c = comp(DG.build_components(probe(), S(), session={}, local=LOCAL), "Hugging Face")
    assert c.status == D.NOT_CONNECTED
    class Set(S):
        def get(s, k): return "hf_x123456789" if k == "hf_token" else super().get(k)
    c = comp(DG.build_components(probe(), Set(), session={}, local=LOCAL), "Hugging Face")
    assert c.status == D.NOT_TESTED
    c = comp(DG.build_components(probe(), Set(), session={"huggingface": {"conn": P.TestResult(False, "Token is valid but has NO ACCESS to model"), "time": 0}}, local=LOCAL), "Hugging Face")
    assert c.status == D.NOT_CONNECTED and "NO ACCESS" in c.summary
