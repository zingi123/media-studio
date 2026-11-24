"""Apply new code to MediaStudio from a ZIP file or a download link, with a safety backup and undo.

Works when the app is started with run.bat / the desktop shortcut (source mode).
Also works in the packaged MediaStudio.exe: the exe only holds Python + libraries, the app code lives in the
external 'code\\app' folder next to it, so a small update ZIP can replace it (see main.py).
GitHub Releases: check_github() finds the newest release, download_zip() + apply_update() install it.

Command line (if the app no longer starts):
    python -m app.shared.app_updater rollback        # undo the last update
    python -m app.shared.app_updater apply my.zip    # apply a ZIP
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ALLOWED_SUFFIX = {".py", ".txt", ".md", ".json", ".bat", ".qss", ".png", ".svg", ".ico"}
ALLOWED_DIRS = ("app/", "tests/")
ALLOWED_ROOT_FILES = {"main.py", "requirements.txt", "requirements-ai.txt", "run.bat", "make_app.bat",
                      "build_exe.bat", "rollback.bat", "make_shortcut.bat", "README.md"}
MAX_DOWNLOAD = 60 * 1024 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


class UpdateError(Exception):
    pass


def is_frozen() -> bool:
    """True only when the app CODE is bundled inside the exe (cannot be replaced). With the external 'code' folder
    layout (main.py launcher) the code is a normal folder, so updates work even though the exe is frozen."""
    if not getattr(sys, "frozen", False):
        return False
    meipass = getattr(sys, "_MEIPASS", None)
    try:
        return bool(meipass) and Path(__file__).resolve().is_relative_to(Path(meipass).resolve())
    except Exception:
        return False


def default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _allowed(rel: str) -> bool:
    if rel.startswith("/") or ".." in rel.split("/") or ":" in rel or "__pycache__" in rel:
        return False
    if not (rel in ALLOWED_ROOT_FILES or rel.startswith(ALLOWED_DIRS)):
        return False
    return Path(rel).suffix.lower() in ALLOWED_SUFFIX


def _map_names(names):
    """zip member name -> path inside the app folder. Handles a zip wrapped in one extra folder."""
    names = [n for n in names if n and not n.endswith("/") and not n.startswith("__MACOSX")]
    norm = {n: n.replace("\\", "/").lstrip("./") for n in names}
    if not any(r in ALLOWED_ROOT_FILES or r.startswith(ALLOWED_DIRS) for r in norm.values()):
        firsts = {r.split("/", 1)[0] for r in norm.values() if "/" in r}
        if len(firsts) == 1 and all("/" in r for r in norm.values()):
            strip = firsts.pop() + "/"
            norm = {n: r[len(strip):] for n, r in norm.items()}
    return norm


def apply_update(zip_path, root=None, progress=None, cancel=None) -> str:
    if is_frozen():
        raise UpdateError("This is the packaged .exe. It cannot replace its own code.\n"
                          "Start the app with run.bat (or the desktop shortcut) to use Update, "
                          "or rebuild the .exe with make_app.bat.")
    root = Path(root) if root else default_root()
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise UpdateError("This file is not a valid ZIP.")
    with zipfile.ZipFile(zip_path) as z:
        mapping = _map_names(z.namelist())
        files, skipped = {}, []
        for zn, rel in mapping.items():
            (files.__setitem__(rel, zn) if _allowed(rel) else skipped.append(rel))
        if not files:
            raise UpdateError("The ZIP has no app files. Expected a folder named 'app' (and optionally main.py).")
        data = {rel: z.read(zn) for rel, zn in files.items()}

    # Safety: refuse the whole update if any Python file has a syntax error (it would break the app).
    for rel, raw in data.items():
        if rel.endswith(".py"):
            try:
                compile(raw, rel, "exec")
            except SyntaxError as e:
                raise UpdateError(f"Update NOT applied. Syntax error in {rel}, line {e.lineno}: {e.msg}")

    # Launcher gate: the update may need libraries that only a new MediaStudio.exe / Setup can bring.
    try:
        meta = json.loads(zf_read(zip_path, "release.json") or b"{}")
    except Exception:
        meta = {}
    need = int(meta.get("min_launcher", 0) or 0)
    have = int(os.environ.get("MEDIASTUDIO_LAUNCHER", "0") or 0)
    if getattr(sys, "frozen", False) and need > have:
        raise UpdateError("This update needs a newer MediaStudio program (new libraries).\n"
                          "Download and run the new MediaStudio_Setup.exe from the Releases page.")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bdir = root / "backups" / f"update_{stamp}"
    replaced, added, removed = [], [], []
    for rel, raw in data.items():
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            raise UpdateError(f"Unsafe path in ZIP: {rel}")
        if target.exists():
            b = bdir / rel
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, b)
            replaced.append(rel)
        else:
            added.append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    # Full snapshot (contains app/version.py): delete old app code that no longer exists in the new version.
    if "app/version.py" in data:
        for f in sorted((root / "app").rglob("*.py")):
            rel = f.relative_to(root).as_posix()
            if rel not in data and "__pycache__" not in rel:
                b = bdir / rel
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, b)
                f.unlink()
                removed.append(rel)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "manifest.json").write_text(json.dumps({"replaced": replaced, "added": added, "removed": removed}, indent=1), "utf-8")

    msg = [f"Updated {len(replaced)} file(s), added {len(added)} new file(s)" + (f", removed {len(removed)} old file(s)." if removed else "."),
           f"Backup (for Undo): {bdir}"]
    if skipped:
        msg.append(f"Ignored {len(skipped)} file(s) that are not app code (for safety).")
    if "requirements.txt" in data and not getattr(sys, "frozen", False):
        p = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(root / "requirements.txt")],
                           capture_output=True, text=True,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        msg.append("Packages installed." if p.returncode == 0 else
                   "Warning: pip failed:\n" + (p.stderr or p.stdout)[-400:])
    msg.append("Restart the app to use the new code.")
    return "\n".join(msg)


def zf_read(zip_path, name):
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            if n.replace("\\", "/").lstrip("./").split("/")[-1] == name and n.count("/") <= 1:
                return z.read(n)
    return None


def current_version() -> str:
    try:
        from ..version import __version__
        return __version__
    except Exception:
        return "0.0.0"


def _vtuple(v):
    out = []
    for p in str(v).lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def check_github(repo=None, progress=None, cancel=None) -> dict:
    """-> {"available": bool, "version", "current", "notes", "url", "sha256_url"}  (never raises on 'no release yet')."""
    if repo is None:
        from ..version import GITHUB_REPO as repo
    if not repo or "/" not in repo or repo.startswith("YOUR_"):
        raise UpdateError("Update source is not set. Edit GITHUB_REPO in app/version.py (owner/repo).")
    cur = current_version()
    try:
        rel = _get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"available": False, "current": cur, "version": cur, "notes": "", "url": "", "sha256_url": ""}
        raise UpdateError(f"Could not check for updates (HTTP {e.code}).")
    except Exception as e:  # noqa
        raise UpdateError(f"Could not check for updates: {e}")
    ver = str(rel.get("tag_name", "")).lstrip("vV")
    assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets", [])}
    url = assets.get("update.zip", "")
    return {"available": bool(url) and _vtuple(ver) > _vtuple(cur), "version": ver, "current": cur,
            "notes": (rel.get("body") or "").strip(), "url": url, "sha256_url": assets.get("update.zip.sha256", "")}


def download_zip(url, progress=None, cancel=None, sha256_url="") -> str:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise UpdateError("Paste a full link that starts with https://")
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=40) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_DOWNLOAD:
                    raise UpdateError("The file is too large to be an update.")
                f.write(chunk)
                h.update(chunk)
                if progress and total:
                    progress(int(got * 100 / total))
        if sha256_url:
            want = urllib.request.urlopen(urllib.request.Request(sha256_url, headers={"User-Agent": UA}),
                                          timeout=20).read().decode().split()[0].lower()
            if want != h.hexdigest():
                raise UpdateError("Download is corrupted (checksum mismatch). Try again.")
    except UpdateError:
        _rm(tmp)
        raise
    except Exception as e:  # noqa
        _rm(tmp)
        raise UpdateError(f"Could not download: {e}")
    if not zipfile.is_zipfile(tmp):
        raise UpdateError("The link is not a direct ZIP download (use a link that downloads a .zip file).")
    return tmp


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def apply_from_url(url, progress=None, cancel=None, sha256_url="") -> str:
    tmp = download_zip(url, progress=progress, sha256_url=sha256_url)
    try:
        return apply_update(tmp)
    finally:
        _rm(tmp)


def rollback_last(root=None, progress=None, cancel=None) -> str:
    if is_frozen():
        raise UpdateError("Undo is only available when started with run.bat.")
    root = Path(root) if root else default_root()
    base = root / "backups"
    cands = sorted(d for d in base.glob("update_*") if (d / "manifest.json").exists()) if base.exists() else []
    if not cands:
        raise UpdateError("No update to undo.")
    d = cands[-1]
    man = json.loads((d / "manifest.json").read_text("utf-8"))
    for rel in man.get("replaced", []):
        src = d / rel
        if src.exists():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / rel)
    for rel in man.get("removed", []):
        src = d / rel
        if src.exists():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / rel)
    for rel in man.get("added", []):
        try:
            (root / rel).unlink()
        except OSError:
            pass
    d.rename(d.with_name("undone_" + d.name[len("update_"):]))
    return (f"Undone: restored {len(man.get('replaced', []))} file(s), removed {len(man.get('added', []))} "
            "new file(s).\nRestart the app.")


def restart_app():
    root = default_root()
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    if getattr(sys, "frozen", False):                      # MediaStudio.exe: start itself again
        cmd, cwd = [sys.executable], str(Path(sys.executable).parent)
    else:
        cmd, cwd = [sys.executable, str(root / "main.py")], str(root)
    subprocess.Popen(cmd, cwd=cwd, creationflags=flags, close_fds=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 2 and sys.argv[1] == "rollback":
            print(rollback_last())
        elif len(sys.argv) >= 3 and sys.argv[1] == "apply":
            print(apply_update(sys.argv[2]))
        else:
            print(__doc__)
    except UpdateError as e:
        print("ERROR:", e)
        sys.exit(1)