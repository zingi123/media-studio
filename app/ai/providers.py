"""
AI Provider Manager - ONE interface for Gemini, Claude, Hugging Face and Ollama.

    p = get_provider("claude")            # credentials come from shared.secrets (never from settings.json)
    p.test_connection()  -> TestResult(ok, message, models)
    p.list_models()      -> [str]
    p.generate(prompt, system=..., json_mode=True) -> str

Transport: Gemini / Claude use the official SDKs (google-genai, anthropic) when installed, and the built-in HTTPS client
otherwise (or when the SDK's API does not match what this code expects). `provider_transport` setting: auto | sdk | rest.
Rules: no provider is required; keys never appear in messages/logs (secrets.redact); Ollama is "ready" only when it is
installed AND its server answers AND a model is downloaded (and a test request succeeded).
"""
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from ..shared import secrets as SEC

TIMEOUT = 120
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProviderError(RuntimeError):
    """kind: auth | quota | model | network | timeout | bad_response | missing | other"""

    def __init__(self, msg, kind="other"):
        super().__init__(msg)
        self.kind = kind


@dataclass
class TestResult:
    ok: bool
    message: str
    models: list = field(default_factory=list)


_QUOTA_WORDS = ("quota", "resource_exhausted", "rate limit", "rate_limit", "credit balance", "billing", "usage limit", "too many requests")


def classify(code, name, text):
    """HTTP status / exception name / message -> (kind, user message)."""
    low = f"{name} {text}".lower()
    if code in (401, 403) or "authentication" in low or "permissiondenied" in low or "api key not valid" in low or "invalid x-api-key" in low:
        return "auth", "authentication failed - the key/token is invalid, expired, or has no permission"
    if code == 429 or any(w in low for w in _QUOTA_WORDS):
        return "quota", "quota / rate limit reached - wait a minute, check your plan or billing, or choose another provider"
    if code == 404 or "notfound" in low or "not found" in low or "is not found for api version" in low:
        return "model", "not found - wrong model name (open the model list and pick one)"
    if "timeout" in low or "timed out" in low:
        return "timeout", "the request timed out - check the connection or try again"
    if "connection" in low or "network" in low or "getaddrinfo" in low or "name or service" in low:
        return "network", "cannot reach the server - check the internet connection / firewall"
    if code and code >= 500:
        return "server", "the provider reported a server error - try again later"      # kind ថ្មី "server" ដើម្បី retry បាន
    return "other", ""


def _http(method, url, headers=None, body=None, timeout=TIMEOUT, retries=0, sleep=time.sleep, secret=""):
    """JSON request -> parsed JSON. Failures become ProviderError(kind) with a redacted, actionable message."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdr = {"Content-Type": "application/json", **(headers or {})}
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = SEC.redact(e.read().decode("utf-8", "replace")[:300], [secret])
            kind, hint = classify(e.code, "", detail)
            if (kind == "quota" or e.code in (500, 502, 503, 529)) and attempt < retries:
                sleep(2 ** (attempt + 1))
                continue
            raise ProviderError(f"HTTP {e.code}: {hint + '. ' if hint else ''}{detail}".strip(), kind) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise ProviderError(f"Timed out after {timeout}s contacting {urllib.parse.urlparse(url).netloc}.", "timeout") from None
            if attempt < retries:
                sleep(2 ** (attempt + 1)); continue
            raise ProviderError(f"Cannot connect to {urllib.parse.urlparse(url).netloc}: {SEC.redact(e.reason, [secret])}", "network") from None
        except TimeoutError:
            raise ProviderError(f"Timed out after {timeout}s contacting {urllib.parse.urlparse(url).netloc}.", "timeout") from None
        except OSError as e:
            raise ProviderError(f"Network error: {SEC.redact(e, [secret])}", "network") from None
        except ValueError:
            raise ProviderError("The server returned a response that is not JSON.", "bad_response") from None


def dist_version(name):
    try:
        import importlib.metadata as md
        return md.version(name)
    except Exception:
        return None


class _SdkMismatch(Exception):
    """The installed SDK does not have the API this code expects -> fall back to the built-in HTTPS client."""


def _map_sdk_error(e, secret=""):
    code = getattr(e, "status_code", None) or getattr(e, "code", None)
    code = code if isinstance(code, int) else None
    msg = SEC.redact(getattr(e, "message", None) or str(e), [secret])[:300]
    kind, hint = classify(code, type(e).__name__, msg)
    return ProviderError(f"{type(e).__name__}{f' ({code})' if code else ''}: {hint + '. ' if hint else ''}{msg}".strip(), kind)


class AIProvider:
    id = ""
    label = ""
    needs_key = True
    default_model = ""
    sdk_dist = ""                      # pip distribution name of the official SDK ("" = none)
    sleep = staticmethod(time.sleep)

    def __init__(self, key="", model="", base_url="", transport="auto"):
        self.key, self.model, self.base_url = (key or "").strip(), (model or "").strip() or self.default_model, base_url
        self.transport = transport

    def sdk_version(self):
        return dist_version(self.sdk_dist) if self.sdk_dist else None

    def _use_sdk(self):
        return self.transport != "rest" and bool(self.sdk_version()) and not self.base_url      # a custom base_url -> REST

    def list_models(self) -> list:
        raise NotImplementedError

    def generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=TIMEOUT) -> str:
        raise NotImplementedError

    def configured(self):
        if self.needs_key and not self.key:
            return False, f"No {self.label} key saved (Settings > AI Providers)."
        return True, ""

    def _with_retry(self, fn, retries=2):
        for attempt in range(retries + 1):
            try:
                return fn()
            except ProviderError as e:
                if e.kind in ("quota", "network", "server") and attempt < retries:     # 5xx (ឧ. 504) ជាបញ្ហាបណ្ដោះអាសន្ន -> retry
                    self.sleep(2 ** (attempt + 1)); continue
                raise

    def test_connection(self) -> TestResult:
        ok, why = self.configured()
        if not ok:
            return TestResult(False, why)
        try:
            models = self.list_models()
        except ProviderError as e:
            return TestResult(False, str(e))
        msg = f"{self.label}: connected, {len(models)} model(s) available."
        if self.model and models and self.model not in models:
            return TestResult(False, msg + f" Selected model '{self.model}' is NOT in the list - choose another.", models)
        return TestResult(True, msg, models)

    def test_request(self) -> TestResult:
        """A real (tiny) generation request - the only proof that key + model + quota work together."""
        ok, why = self.configured()
        if not ok:
            return TestResult(False, why)
        t0 = time.time()
        try:
            out = self.generate("Reply with the single word OK.", temperature=0.0, timeout=45)
        except ProviderError as e:
            return TestResult(False, str(e))
        return TestResult(True, f"Test request succeeded in {time.time() - t0:.1f}s (reply: {SEC.redact(out.strip())[:40]!r}).")


class GeminiProvider(AIProvider):
    id, label, default_model, sdk_dist = "gemini", "Gemini", "gemini-3.8-flash", "google-genai"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    # ---- REST
    def _h(self):
        return {"x-goog-api-key": self.key}

    def _rest_list(self):
        d = _http("GET", f"{self.base_url or self.BASE}/models?pageSize=200", self._h(), secret=self.key)
        return sorted(m["name"].split("/", 1)[-1] for m in d.get("models", [])
                      if "generateContent" in m.get("supportedGenerationMethods", []))

    def _rest_generate(self, prompt, system, json_mode, model, temperature, timeout):
        cfg = {"temperature": temperature}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        d = _http("POST", f"{self.base_url or self.BASE}/models/{model or self.model}:generateContent", self._h(), body,
                  timeout, retries=3, sleep=self.sleep, secret=self.key)
        try:
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise ProviderError("Gemini returned no text (blocked or empty response).", "bad_response") from None

    # ---- SDK (google-genai)
    def _client(self, timeout):
        from google import genai
        from google.genai import types
        return genai.Client(api_key=self.key, http_options=types.HttpOptions(timeout=int(timeout * 1000))), types

    def _sdk(self, fn):
        try:
            return fn()
        except ProviderError:
            raise
        except (AttributeError, TypeError, ImportError) as e:
            raise _SdkMismatch(str(e))
        except Exception as e:  # noqa
            raise _map_sdk_error(e, self.key)

    def list_models(self):
        if self._use_sdk():
            try:
                def f():
                    c, _ = self._client(30)
                    out = []
                    for m in c.models.list():
                        acts = getattr(m, "supported_actions", None)
                        if acts is None or "generateContent" in acts:
                            out.append(m.name.split("/", 1)[-1])
                    return sorted(out)
                return self._sdk(f)
            except _SdkMismatch:
                pass
        return self._rest_list()

    PREFERRED = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-2.5-flash")   # លំដាប់ជម្រើស Model ប្រសិនបើ Model ដែលកំណត់ត្រូវបានដកចេញ

    def generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=TIMEOUT):
        try:
            return self._generate(prompt, system, json_mode, model, temperature, timeout)
        except ProviderError as e:
            if e.kind not in ("model", "server"):                                  # 404 model ឬ 5xx (overload) ប៉ុណ្ណោះ
                raise
            cur = model or self.model
            try:
                avail = self.list_models()                                         # ពិនិត្យបញ្ជី Model របស់ Key នេះ
            except ProviderError:
                raise e
            for alt in (m for m in self.PREFERRED if m != cur and m in avail):     # ព្យាយាមតាមលំដាប់ ទាល់តែជោគជ័យ
                try:
                    out = self._generate(prompt, system, json_mode, alt, temperature, timeout)
                except ProviderError as e2:
                    if e2.kind not in ("model", "server"):
                        raise
                    continue
                if e.kind == "model":
                    self.model = alt                                               # 404: ចងចាំ Model ថ្មី; 503: ប្រើបណ្ដោះអាសន្ន (វិលត្រឡប់មក Model ដើមវិញ)
                return out
            raise e

    def _generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=TIMEOUT):
        if self._use_sdk():
            try:
                def f():
                    c, t = self._client(timeout)
                    kw = {"temperature": temperature}
                    if system:
                        kw["system_instruction"] = system
                    if json_mode:
                        kw["response_mime_type"] = "application/json"
                    r = c.models.generate_content(model=model or self.model, contents=prompt, config=t.GenerateContentConfig(**kw))
                    if not getattr(r, "text", None):
                        raise ProviderError("Gemini returned no text (blocked or empty response).", "bad_response")
                    return r.text
                return self._with_retry(lambda: self._sdk(f))
            except _SdkMismatch:
                pass
        return self._rest_generate(prompt, system, json_mode, model, temperature, timeout)


class ClaudeProvider(AIProvider):
    id, label, default_model, sdk_dist = "claude", "Claude", "claude-sonnet-5-5", "anthropic"
    BASE = "https://api.anthropic.com"

    def _h(self):
        return {"x-api-key": self.key, "anthropic-version": "2023-06-01"}

    def _client(self, timeout):
        import anthropic
        return anthropic.Anthropic(api_key=self.key, timeout=float(timeout), max_retries=0)

    def _sdk(self, fn):
        try:
            return fn()
        except ProviderError:
            raise
        except (AttributeError, TypeError, ImportError) as e:
            raise _SdkMismatch(str(e))
        except Exception as e:  # noqa
            raise _map_sdk_error(e, self.key)

    def list_models(self):
        if self._use_sdk():
            try:
                return self._sdk(lambda: [m.id for m in self._client(30).models.list(limit=100).data])
            except _SdkMismatch:
                pass
        d = _http("GET", f"{self.base_url or self.BASE}/v1/models?limit=100", self._h(), secret=self.key)
        return [m["id"] for m in d.get("data", [])]

    def generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=TIMEOUT):
        if json_mode:
            system = (system + "\n" if system else "") + "Respond with valid JSON only - no prose, no code fences."
        if self._use_sdk():
            try:
                def f():
                    kw = {"model": model or self.model, "max_tokens": 8000, "temperature": temperature,
                          "messages": [{"role": "user", "content": prompt}]}
                    if system:
                        kw["system"] = system
                    r = self._client(timeout).messages.create(**kw)
                    txt = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
                    if not txt:
                        raise ProviderError("Claude returned no text.", "bad_response")
                    return txt
                return self._with_retry(lambda: self._sdk(f))
            except _SdkMismatch:
                pass
        body = {"model": model or self.model, "max_tokens": 8000, "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        d = _http("POST", f"{self.base_url or self.BASE}/v1/messages", self._h(), body, timeout, retries=3,
                  sleep=self.sleep, secret=self.key)
        txt = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        if not txt:
            raise ProviderError("Claude returned no text.", "bad_response")
        return txt


# ------------------------------------------------------------------ Ollama
OLLAMA_SUGGESTIONS = [          # APPROXIMATE download size / RAM - guidance only, nothing is downloaded automatically
    ("qwen2.5:3b", 2000, 4000), ("qwen2.5:7b", 4700, 8000), ("llama3.1:8b", 4900, 8000), ("gemma2:9b", 5500, 10000),
]


def ollama_find_exe(user_path="", windows=None, env=None):
    """-> (path, source) or ("", ""). Order: configured path, PATH, standard install locations
    (Windows: %LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe, %ProgramFiles%\\Ollama)."""
    win = (os.name == "nt") if windows is None else windows
    env = os.environ if env is None else env
    if user_path and os.path.isfile(user_path):
        return user_path, "configured path"
    w = shutil.which("ollama")
    if w:
        return w, "PATH"
    cands = []
    if win:
        for e, sub in (("LOCALAPPDATA", os.path.join("Programs", "Ollama", "ollama.exe")), ("ProgramFiles", os.path.join("Ollama", "ollama.exe")),
                       ("ProgramFiles(x86)", os.path.join("Ollama", "ollama.exe"))):
            if env.get(e):
                cands.append(os.path.join(env[e], sub))
    else:
        cands += ["/usr/local/bin/ollama", "/usr/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"]
    for c in cands:
        if os.path.isfile(c):
            return c, "standard install location"
    return "", ""


def ollama_version(exe, timeout=10):
    """Read the version from `ollama --version` (the exe may be installed but NOT on PATH, and the server may be off)."""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN)
        m = re.search(r"(\d+\.\d+\.\d+)", (r.stdout or "") + " " + (r.stderr or ""))
        return m.group(1) if m else ""
    except Exception:
        return ""


class OllamaProvider(AIProvider):
    id, label, needs_key, default_model = "ollama", "Ollama (local)", False, ""
    BASE = "http://localhost:11434"

    def __init__(self, key="", model="", base_url="", transport="auto", exe=""):
        super().__init__(key, model, base_url, transport)
        self.exe_setting = exe

    def _url(self):
        return (self.base_url or self.BASE).rstrip("/")

    def server_version(self, timeout=3):
        try:
            return _http("GET", f"{self._url()}/api/version", timeout=timeout).get("version", "") or "?"
        except ProviderError:
            return ""

    def loaded_models(self):
        """Models currently loaded in memory (/api/ps)."""
        try:
            return sorted(m.get("name", "") for m in _http("GET", f"{self._url()}/api/ps", timeout=5).get("models", []))
        except ProviderError:
            return []

    def status(self) -> dict:
        """Honest ladder: executable -> server -> API -> models -> loaded. Each rung is checked separately."""
        exe, src = ollama_find_exe(self.exe_setting)
        ver_exe = ollama_version(exe) if exe else ""
        ver_srv = self.server_version()
        running = bool(ver_srv)
        models, api_ok = [], False
        if running:
            try:
                models = self.list_models(); api_ok = True
            except ProviderError:
                pass
        loaded = self.loaded_models() if running else []
        if running and models:
            msg = f"Ollama {ver_srv} is running with {len(models)} downloaded model(s)."
        elif running:
            msg = ("Ollama is installed, but no local model is available. Open a terminal and run e.g.:  ollama pull qwen2.5:3b  "
                   "(small local models are weak at Khmer - prefer Gemini/Claude for final quality).")
        elif exe:
            msg = ("Ollama is installed" + (f" ({src}; not on PATH)" if src != "PATH" else "") +
                   " but its server is not running. Use 'Start Ollama server' or run:  ollama serve")
        else:
            msg = "Ollama was not found. Install it from https://ollama.com/download , then run:  ollama pull qwen2.5:3b"
        return {"installed": bool(exe), "exe": exe, "exe_source": src, "exe_version": ver_exe,
                "running": running, "version": ver_srv or ver_exe, "api_ok": api_ok, "models": models, "loaded": loaded, "message": msg}

    def start_server(self, wait=20):
        """Explicit user action: launch `ollama serve` and wait until it answers. Never called automatically."""
        exe, _ = ollama_find_exe(self.exe_setting)
        if not exe:
            raise ProviderError("Ollama executable not found - install it or set its path.", "missing")
        if self.server_version():
            return self.status()
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | _NOWIN) if os.name == "nt" else 0
        try:
            subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as e:
            raise ProviderError(f"Could not start Ollama: {SEC.redact(e)}", "other") from None
        for _ in range(int(wait * 2)):
            time.sleep(0.5)
            if self.server_version():
                break
        return self.status()

    def list_models(self):
        d = _http("GET", f"{self._url()}/api/tags", timeout=10)
        return sorted(m["name"] for m in d.get("models", []))

    def is_model_downloaded(self, name):
        have = self.list_models()
        return name in have or (":" not in name and f"{name}:latest" in have)

    def test_connection(self):
        st = self.status()
        ok = st["running"] and bool(st["models"])
        if ok and self.model and not self.is_model_downloaded(self.model):
            return TestResult(False, f"Model '{self.model}' is not downloaded. Run:  ollama pull {self.model}", st["models"])
        return TestResult(ok, st["message"], st["models"])

    def test_request(self):
        name = self.model
        if not name:
            return TestResult(False, "No Ollama model selected. " + self.status()["message"])
        t0 = time.time()
        try:
            out = self.generate("Reply with the single word OK.", temperature=0.0, timeout=180)   # first call loads the model
        except ProviderError as e:
            return TestResult(False, str(e))
        return TestResult(True, f"Model '{name}' loaded and answered in {time.time() - t0:.1f}s (reply: {out.strip()[:40]!r}).")

    def generate(self, prompt, system="", json_mode=False, model=None, temperature=0.3, timeout=TIMEOUT):
        name = model or self.model
        if not name:
            raise ProviderError("No Ollama model selected. " + self.status()["message"], "missing")
        if not self.is_model_downloaded(name):
            raise ProviderError(f"Ollama model '{name}' is not downloaded. Run:  ollama pull {name}", "model")
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = {"model": name, "messages": msgs, "stream": False, "options": {"temperature": temperature}}
        if json_mode:
            body["format"] = "json"
        d = _http("POST", f"{self._url()}/api/chat", body=body, timeout=timeout)
        txt = (d.get("message") or {}).get("content", "")
        if not txt:
            raise ProviderError("Ollama returned no text.", "bad_response")
        return txt

    @staticmethod
    def pull_advice(model, ram_mb=None, disk_mb=None):
        """Can this PC reasonably hold `model`? -> (ok, [reasons]). Uses the APPROXIMATE table; never downloads anything."""
        from ..shared.model_manager import available_ram_mb
        row = next((r for r in OLLAMA_SUGGESTIONS if r[0] == model), None)
        if row is None:
            return True, [f"'{model}' size unknown - check its page on ollama.com/library before downloading."]
        ram = available_ram_mb() if ram_mb is None else ram_mb
        if disk_mb is None:
            try:
                disk_mb = shutil.disk_usage(os.path.expanduser("~")).free / 2**20
            except OSError:
                disk_mb = None
        why = []
        if ram is not None and ram < row[2] * 0.7:
            why.append(f"needs about {row[2] / 1024:.0f} GB RAM, only {ram / 1024:.1f} GB free")
        if disk_mb is not None and disk_mb < row[1] * 1.2:
            why.append(f"needs about {row[1] / 1024:.1f} GB disk, only {disk_mb / 1024:.1f} GB free")
        return (not why), why


class HuggingFaceProvider(AIProvider):
    """Token + gated-model access checks (Pyannote, voice models). Not a text-generation provider."""
    id, label, sdk_dist = "huggingface", "Hugging Face", "huggingface_hub"
    BASE = "https://huggingface.co"

    def list_models(self):
        return []

    def generate(self, *a, **k):
        raise ProviderError("Hugging Face is used for speaker detection / voice models, not for translation.", "other")

    def test_connection(self):
        ok, why = self.configured()
        if not ok:
            return TestResult(False, why)
        from ..translator.diarization import check_model_access, DEFAULT_MODEL
        r = check_model_access(self.key, self.model or DEFAULT_MODEL)
        return TestResult(r.ok, r.message)

    def test_request(self):
        return self.test_connection()

    def check_model(self, repo):
        from ..translator.diarization import check_model_access
        return check_model_access(self.key, repo)


PROVIDERS = {c.id: c for c in (GeminiProvider, ClaudeProvider, OllamaProvider, HuggingFaceProvider)}
TRANSLATION_PROVIDERS = ["gemini", "claude", "ollama"]


def get_provider(pid, settings=None):
    """Build a provider from saved settings (keys from the secure store)."""
    if pid not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{pid}'.", "other")
    if settings is None:
        from ..shared.settings import settings as _s
        settings = _s()
    g = settings.get
    tr = g("provider_transport") or "auto"
    if pid == "gemini":
        return GeminiProvider(g("gemini_key"), g("gemini_model"), transport=tr)
    if pid == "claude":
        return ClaudeProvider(g("claude_key"), g("claude_model"), transport=tr)
    if pid == "ollama":
        return OllamaProvider("", g("ollama_model"), g("ollama_url"), exe=g("ollama_exe") or "")
    return HuggingFaceProvider(g("hf_token"), g("diar_model"))


def task_provider(task, settings=None):
    """Provider chosen for a task ('translate' | 'story') in Settings -> provider instance."""
    if settings is None:
        from ..shared.settings import settings as _s
        settings = _s()
    pid = settings.get(f"{task}_provider") or "gemini"
    return get_provider(pid if pid in TRANSLATION_PROVIDERS else "gemini", settings)
