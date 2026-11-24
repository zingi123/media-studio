"""Credential storage. API keys / tokens are NEVER written to settings.json or logs.

Lookup order for a secret:  1) environment variable   2) OS credential store (python `keyring`:
Windows Credential Manager / macOS Keychain / Linux Secret Service)   3) only if keyring is unavailable:
a user-only (0600) fallback file, and `backend()` then reports "file (NOT encrypted)" so the UI can warn.
"""
import json
import os
import re
from .paths import data_dir

SERVICE = "MediaStudio"
SECRET_KEYS = {
    "gemini_key": "GEMINI_API_KEY",
    "claude_key": "ANTHROPIC_API_KEY",
    "hf_token": "HF_TOKEN",
}
_backend_override = None      # tests can inject an object with get_password/set_password/delete_password


def _keyring():
    if _backend_override is not None:
        return _backend_override
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def backend() -> str:
    return "OS credential store" if _keyring() is not None else "file (NOT encrypted)"


def _file():
    return data_dir() / "secrets.json"


def _read_file() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_file(d: dict):
    p = _file()
    p.write_text(json.dumps(d), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def get_secret(name: str) -> str:
    env = SECRET_KEYS.get(name)
    if env and os.environ.get(env, "").strip():
        return os.environ[env].strip()
    kr = _keyring()
    if kr is not None:
        try:
            return (kr.get_password(SERVICE, name) or "").strip()
        except Exception:
            return ""
    return str(_read_file().get(name, "")).strip()


def set_secret(name: str, value: str):
    value = (value or "").strip()
    kr = _keyring()
    if kr is not None:
        try:
            if value:
                kr.set_password(SERVICE, name, value)
            else:
                try:
                    kr.delete_password(SERVICE, name)
                except Exception:
                    pass
            return
        except Exception:
            pass
    d = _read_file()
    if value:
        d[name] = value
    else:
        d.pop(name, None)
    _write_file(d)


def redact(text, extra=()) -> str:
    """Remove anything that looks like a key/token from a message before it is logged or shown."""
    s = str(text)
    for v in list(extra) + [get_secret(k) for k in SECRET_KEYS]:
        if v and len(v) >= 6:
            s = s.replace(v, "***")
    s = re.sub(r"hf_[A-Za-z0-9]{10,}", "hf_***", s)
    s = re.sub(r"sk-ant-[A-Za-z0-9_\-]{10,}", "sk-ant-***", s)
    s = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "AIza***", s)
    s = re.sub(r"(?i)(key|token|authorization)(=|:\s*)[^\s&\"']+", r"\1\2***", s)
    return s
