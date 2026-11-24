from pathlib import Path
from ..shared.ffmpeg_util import probe, run_ffmpeg

_done = {}                                                           # ចងចាំ audio ដែល extract រួច (ក្នុង session នេះ)


def extract_audio(video, out_wav, progress=None, cancel=None):
    """16 kHz mono WAV, the format speech-recognition models expect."""
    st = Path(video).stat()                                          # អត្តសញ្ញាណ video
    key = (str(video), st.st_size, int(st.st_mtime))                 # path + ទំហំ + ពេលកែ
    if _done.get(str(out_wav)) == key and Path(out_wav).exists():    # video ដដែល + wav នៅមាន → រំលង
        return str(out_wav)
    dur = probe(video)["duration"]
    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000", out_wav],
               duration=dur, progress=progress, cancel=cancel)
    _done[str(out_wav)] = key                                        # កត់ត្រាថា extract រួច
    return str(out_wav)
