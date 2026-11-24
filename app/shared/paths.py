import os
import sys
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    p = Path(os.environ.get("MEDIASTUDIO_HOME") or (Path.home() / "MediaStudio"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def work_dir(name: str) -> Path:
    p = data_dir() / "work" / name
    p.mkdir(parents=True, exist_ok=True)
    return p
