"""Hear every Human voice (Boy 1 ... Baby Girl 3) without opening the app.
Run from the folder that contains `app/`:
    python -m app.voice_demo              -> all 30 voices
    python -m app.voice_demo "Boy 2"      -> one voice (or a group: "Old Men")
Needs: internet (edge-tts), ffmpeg. Output: ~/MediaStudio/voice_demo/*.wav (+ opens the folder)."""
import sys
from .shared.paths import data_dir
from .shared.utils import open_path
from .translator.khmer_tts import synthesize
from .translator.voice_presets import BASE, GROUPS, default_preset, process

TEXT = "សួស្តី ខ្ញុំជាសំឡេងសាកល្បង។ តើអ្នកស្តាប់ឮច្បាស់ទេ?"  # Khmer test sentence


def main(args):
    want = " ".join(args).strip().lower()
    out = data_dir() / "voice_demo"; out.mkdir(parents=True, exist_ok=True)
    names = [n for g, vs in GROUPS["Human"].items() for n in vs if not want or want in (n.lower(), g.lower())]
    if not names:
        print("No such voice. Examples: 'Boy 2', 'Old Men'"); return 1
    for n in names:
        p = default_preset(n)
        raw = synthesize(TEXT, out / f"{n}.raw.mp3", BASE[p["base"]], 1 + p["speed"] / 100)
        f = process(raw, out / f"{n}.wav", p)
        print(f"OK {n:14} [{p['base']} | pitch {p['pitch']:+d}% | speed {p['speed']:+d}% | formant {p['formant']:+d}%]  -> {f}")
    open_path(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
