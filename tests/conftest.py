import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MEDIASTUDIO_HOME"] = tempfile.mkdtemp(prefix="ms_test_home_")
for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "HF_TOKEN"):
    os.environ.pop(k, None)
