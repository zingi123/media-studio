"""List every third-party package the app code imports (incl. lazy imports inside functions).
PyInstaller cannot see them because app/ is NOT frozen, so build_exe.py feeds this list to it."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# imported by string / subprocess, invisible to static scanning
EXTRA = {"demucs", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSvg", "imageio_ffmpeg",
         "yt_dlp", "edge_tts", "sqlite3"}
# packages whose data files / dlls must come along entirely
COLLECT_ALL = {"PySide6", "whisper", "faster_whisper", "torch", "torchaudio", "pyannote", "cv2", "demucs",
               "ctranslate2", "tokenizers", "imageio_ffmpeg", "edge_tts", "yt_dlp", "paddleocr", "paddle", "rapidocr_onnxruntime"}


def third_party():
    stdlib = set(sys.stdlib_module_names)
    found = set()
    for f in (ROOT / "app").rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                names = [n.module]
            else:
                continue
            for nm in names:
                top = nm.split(".")[0]
                if top not in stdlib and top != "app":
                    found.add(top)
    return sorted(found)


if __name__ == "__main__":
    print("\n".join(third_party()))
