"""Make the GitHub Release files:  release/update.zip  +  release/update.zip.sha256  +  release/NOTES.txt
Usage:  python tools/release.py 1.1.0 "what changed"      (also bumps app/version.py)
Then upload the two files to a NEW GitHub Release whose tag is v1.1.0  (release.bat can do it with `gh`)."""
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    vf = ROOT / "app" / "version.py"
    arg = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if arg == "auto":                      # next patch number: 1.0.0 -> 1.0.1
        cur = re.search(r'__version__ = "([^"]+)"', vf.read_text(encoding="utf-8")).group(1)
        a, b, c = (int(x) for x in cur.split("."))
        arg = f"{a}.{b}.{c + 1}"
    if not re.fullmatch(r"\d+\.\d+\.\d+", arg):
        sys.exit('Usage: python tools/release.py [1.1.0|auto] "release notes"')
    ver, notes = arg, (sys.argv[2] if len(sys.argv) > 2 else "update")
    vf.write_text(re.sub(r'__version__ = "[^"]*"', f'__version__ = "{ver}"', vf.read_text(encoding="utf-8")), encoding="utf-8")
    sys.path.insert(0, str(ROOT / "tools"))
    out = ROOT / "release"
    out.mkdir(exist_ok=True)
    zp = out / "update.zip"
    # min_launcher: raise this number ONLY when the new code needs a library that the installed exe does not contain.
    meta = {"version": ver, "min_launcher": 1}
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((ROOT / "app").rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                z.write(f, f.relative_to(ROOT).as_posix())
        z.writestr("release.json", json.dumps(meta))
    (out / "update.zip.sha256").write_text(hashlib.sha256(zp.read_bytes()).hexdigest() + "  update.zip\n")
    (out / "VERSION.txt").write_text(ver)
    (out / "NOTES.txt").write_text(notes or f"MediaStudio {ver}", encoding="utf-8")
    print(f"OK {ver}: {zp} ({zp.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
