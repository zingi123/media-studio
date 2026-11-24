import subprocess
import sys
import time
from pathlib import Path
from ..shared.ffmpeg_util import Cancelled, run_ffmpeg
from ..shared.paths import work_dir


def separate(video, device="cpu", progress=None, cancel=None):  # បំបែកសំឡេង → ត្រឡប់ no_vocals.wav
    w = work_dir(Path(video).stem)  # ថតការងារ
    wav, out = w / "orig.wav", w / "demucs"  # ឯកសារ audio ដើម និងថតលទ្ធផល
    target = out / "htdemucs" / "orig" / "no_vocals.wav"  # ទីតាំង music+SFX
    if target.exists():  # មាន cache ហើយ
        return str(target)  # មិនបំបែកម្តងទៀត
    run_ffmpeg(["-y", "-i", video, "-vn", "-ac", "2", "-ar", "44100", wav], cancel=cancel)  # ទាញ audio ដើម
    cmd = [sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-d", device, "-o", str(out), str(wav)]  # voice vs ផ្សេងៗ
    log = out / "demucs.log"  # ទុក output ដើម្បីចៀសវាង pipe ពេញ
    out.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8", errors="replace") as lf:
        try:
            p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)  # រត់ demucs
        except Exception as e:  # noqa
            raise RuntimeError(f"Cannot start Demucs: {e}") from e  # មិនអាចរត់
        while p.poll() is None:  # រង់ចាំ ហើយពិនិត្យ STOP
            if cancel and cancel():
                p.kill(); p.wait()  # សម្លាប់ process
                raise Cancelled()
            time.sleep(0.3)
    if p.returncode != 0 or not target.exists():  # បរាជ័យ
        raise RuntimeError("Demucs failed (pip install demucs):\n" + log.read_text(encoding="utf-8", errors="replace")[-400:])  # បង្ហាញកំហុស
    if progress:
        progress(100)  # ចប់
    return str(target)  # ផ្លូវ no_vocals
