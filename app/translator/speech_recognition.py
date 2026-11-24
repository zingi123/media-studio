"""
Speech recognition (Chinese and other languages) with word timestamps.

Two interchangeable backends:
  * openai-whisper   (`pip install openai-whisper`)  - works on CPU or CUDA
  * faster-whisper   (`pip install faster-whisper`)  - faster on CPU (int8)
`asr_backend` in Settings: auto (faster-whisper if installed, else openai-whisper) | faster-whisper | openai-whisper.

Design points:
  * audio is passed to the model IN MEMORY (our extracted 16 kHz WAV) - openai-whisper therefore does not need
    `ffmpeg` on PATH (it normally shells out to it).
  * openai-whisper has no progress/cancel hook, so long audio is processed in ~3 minute pieces cut at the quietest point;
    cancel + progress work between pieces and timestamps are offset exactly (no drift).
  * models load on demand through the model manager (one heavy model at a time, RAM check, unloadable).
  * start/end times are those of the audio; text is never retimed.
"""
import re
import time
from pathlib import Path

import numpy as np

from ..shared.ffmpeg_util import Cancelled
from ..shared.model_manager import MM, WHISPER_MB, record_speed, estimate_seconds
from ..shared.paths import work_dir
from .audio_extractor import extract_audio
from .subtitles import Cue

MODELS = ["tiny", "base", "small", "medium", "large-v3"]
# Rough guide only (CPU): tiny/base are usable on weak PCs, medium+ need a strong CPU (and ~5-10 GB free RAM) or an NVIDIA GPU.
PIECE_S = 180.0
SR = 16000


# ------------------------------------------------------------------ backend selection
def _dist_version(name):
    try:
        import importlib.metadata as md
        return md.version(name)
    except Exception:
        return None


def installed_backends():
    """-> list of installed backends in preference order."""
    out = []
    if _dist_version("faster-whisper"):
        out.append("faster-whisper")
    if _dist_version("openai-whisper"):
        out.append("openai-whisper")
    return out


def pick_backend(preferred="auto"):
    have = installed_backends()
    if preferred in have:
        return preferred
    if not have:
        raise RuntimeError("No speech-recognition package is installed. Install ONE of:  pip install openai-whisper   "
                           "or   pip install faster-whisper")
    return have[0]


def _pref():
    try:
        from ..shared.settings import settings
        return settings().get("asr_backend") or "auto"
    except Exception:
        return "auto"


# ------------------------------------------------------------------ unified engine
class Seg:
    __slots__ = ("start", "end", "text", "words", "nsp", "lp", "cr")

    def __init__(self, start, end, text, words, nsp=0.0, lp=0.0, cr=0.0):
        self.start, self.end, self.text, self.words = float(start), float(end), text, words   # words: [(text, start, end)]
        self.nsp, self.lp, self.cr = float(nsp or 0.0), float(lp or 0.0), float(cr or 0.0)    # no_speech_prob, avg_logprob, compression_ratio (0 = unknown)


# ------------------------------------------------------------------ hallucination / music filter + hints
_JUNK = ("thanks for watching", "thank you for watching", "subscribe", "like and subscribe", "amara.org", "subtitles by",
         "字幕", "请不吝", "點贊", "点赞", "订阅", "謝謝觀看", "谢谢观看", "ご視聴ありがとう", "チャンネル登録", "시청해 주셔서", "시청해주셔서", "구독")


def filter_segments(segs, say=None):
    """Drop what Whisper invents on music / silence: no-speech + low confidence, runaway repetition (compression ratio), known stock phrases
    ('thanks for watching', '字幕...'), and the 3rd+ identical line in a row. Real speech is kept: a rule needs BOTH signals where it can."""
    out, drop, last, rep = [], 0, None, 0
    for g in segs:
        t = (g.text or "").strip()
        low = t.lower()
        bad = (g.nsp > 0.6 and g.lp < -1.0) or g.cr > 2.4 or any(j in low for j in _JUNK)
        rep = rep + 1 if t == last else 1
        last = t
        if bad or rep >= 3 or not t:
            drop += 1
            continue
        out.append(g)
    if drop and say:
        say(f"ASR filter: {drop} line(s) removed (music / silence / repeated or stock phrases Whisper invents).")
    return out


def hint_text(*parts, limit=200):
    """Names / terms for Whisper's initial_prompt (spelling of character names and places). Whisper reads at most ~224 tokens - keep it short."""
    seen, out = set(), []
    for p in parts:
        for w in re.split(r"[,;\n、，；]+", p or ""):
            w = w.strip()
            if w and w not in seen:
                seen.add(w)
                out.append(w)
    return ("、".join(out))[:limit]


def asr_source(video):
    """'vocals' when Demucs stems exist (music is already removed = far fewer false words), else 'mix'."""
    try:
        from ..studio import audio as _AU
        return "vocals" if _AU.have_stems(video) else "mix"
    except Exception:  # noqa
        return "mix"


def split_points(audio, sr=SR, piece_s=PIECE_S, search_s=4.0):
    """[(start_sample, end_sample)] pieces of ~piece_s, each boundary moved to the quietest 50 ms window within +-search_s."""
    n = len(audio)
    if n <= piece_s * sr * 1.25:
        return [(0, n)]
    win = int(0.05 * sr)
    cuts = [0]
    while n - cuts[-1] > piece_s * sr * 1.25:
        target = cuts[-1] + int(piece_s * sr)
        lo, hi = max(cuts[-1] + sr, target - int(search_s * sr)), min(n - sr, target + int(search_s * sr))
        seg = np.abs(audio[lo:hi])
        k = (len(seg) // win) * win
        if k >= win:
            e = seg[:k].reshape(-1, win).mean(axis=1)
            cut = lo + int(np.argmin(e)) * win + win // 2
        else:
            cut = target
        cuts.append(cut)
    cuts.append(n)
    return list(zip(cuts[:-1], cuts[1:]))


def _model_key(backend, model, device):
    return (backend, model, device)


def load_model(backend, model, device, warn=None):
    mb = WHISPER_MB.get(model, WHISPER_MB.get(model.split("-")[0], 2000))
    if warn:
        MM.warn = warn
    if backend == "openai-whisper":
        def loader():
            import whisper
            return whisper.load_model(model, device=device)
    else:
        def loader():
            from faster_whisper import WhisperModel
            return WhisperModel(model, device=device, compute_type="float16" if device == "cuda" else "int8")
    return MM.get(_model_key(backend, model, device), loader, est_mb=mb)


def run_asr(wav, model="base", language=None, device="cpu", backend=None, progress=None, cancel=None, warn=None, initial_prompt=""):
    """-> (segments [Seg], language_code, duration_seconds). Raises Cancelled / RuntimeError with an actionable message."""
    backend = pick_backend(backend or _pref())
    from .diarization import read_wav_mono
    audio, sr = read_wav_mono(wav)
    if sr != SR:
        raise RuntimeError(f"Expected a 16 kHz WAV but got {sr} Hz (re-extract the audio).")
    dur = len(audio) / sr
    t0 = time.time()
    m = load_model(backend, model, device, warn)
    segs, lang = [], language or ""
    if backend == "faster-whisper":
        gen, info = m.transcribe(audio, language=language or None, vad_filter=True, word_timestamps=True,
                                  initial_prompt=initial_prompt or None, condition_on_previous_text=False)
        lang = info.language or lang
        for s in gen:
            if cancel and cancel():
                raise Cancelled()
            ws = [((w.word or "").strip(), float(w.start), float(w.end)) for w in (s.words or [])]
            segs.append(Seg(s.start, s.end, (s.text or "").strip(), [w for w in ws if w[0]],
                            getattr(s, "no_speech_prob", 0.0), getattr(s, "avg_logprob", 0.0), getattr(s, "compression_ratio", 0.0)))
            if progress and dur:
                progress(min(99, int(s.end * 100 / dur)))
    else:
        for a, b in split_points(audio, sr):
            if cancel and cancel():
                raise Cancelled()
            off = a / sr
            res = m.transcribe(audio[a:b], language=lang or None, word_timestamps=True, fp16=(device == "cuda"), verbose=None,
                           initial_prompt=initial_prompt or None, condition_on_previous_text=False)
            lang = lang or res.get("language", "") or ""
            for s in res.get("segments", []):
                ws = [((w.get("word") or "").strip(), float(w["start"]) + off, float(w["end"]) + off) for w in s.get("words", []) or []]
                segs.append(Seg(float(s["start"]) + off, float(s["end"]) + off, (s.get("text") or "").strip(), [w for w in ws if w[0]],
                                s.get("no_speech_prob", 0.0), s.get("avg_logprob", 0.0), s.get("compression_ratio", 0.0)))
            if progress and dur:
                progress(min(99, int(b * 100 / len(audio))))
    record_speed("asr", f"{backend}|{model}|{device}", dur, time.time() - t0)
    if progress:
        progress(100)
    segs = filter_segments(segs)
    return segs, lang, dur


def estimate_asr(model, device, audio_seconds, backend=None):
    """Seconds, only from a measurement made on this PC - None when there is none yet."""
    try:
        backend = pick_backend(backend or _pref())
    except RuntimeError:
        return None
    return estimate_seconds("asr", f"{backend}|{model}|{device}", audio_seconds)


def _wav_for(video, cancel):
    """16 kHz mono audio for Whisper. Uses the Demucs VOCALS stem when it exists (no music -> fewer invented words), else the full mix."""
    wd = Path(work_dir(Path(video).stem))
    if asr_source(video) == "vocals":
        try:
            from ..studio import audio as _AU
            from ..shared.ffmpeg_util import run_ffmpeg
            out = wd / "vocals16k.wav"
            src = _AU.stem_paths(video)["vocals"]
            if not out.exists() or out.stat().st_mtime < Path(src).stat().st_mtime:
                run_ffmpeg(["-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)], cancel=cancel)
            return out
        except Cancelled:
            raise
        except Exception:  # noqa - fall back to the full mix
            pass
    wav = wd / "audio16k.wav"
    extract_audio(video, wav, cancel=cancel)
    return wav


# ------------------------------------------------------------------ public API (unchanged signatures)
def transcribe(video, model="base", language=None, device="cpu", progress=None, cancel=None, initial_prompt=""):
    """ASR -> [Cue]. Downloads the model on first use (needs internet once)."""
    segs, _, _ = run_asr(_wav_for(video, cancel), model, language, device, None, progress, cancel, initial_prompt=initial_prompt)
    cues = []
    for s in segs:
        if not s.text:
            continue
        ws = [w for w in s.words if w[2] > w[1]]
        # Whisper segment bounds stretch across pauses. Use the first spoken word as START and the last as END,
        # so every line has its own time and the silence between two lines is kept.
        a, b = (ws[0][1], ws[-1][2]) if ws else (s.start, s.end)
        if cues and a < cues[-1].end:
            a = min(max(a, cues[-1].start), b)           # never begin before the previous line has ended
        cues.append(Cue(a, max(b, a + 0.2), s.text))
    out = []
    for c in cues:
        q = out[-1] if out else None
        if q and (c.end - c.start < 0.6 or q.end - q.start < 0.6) and c.start - q.end < 0.5:   # short and close -> join
            out[-1] = Cue(q.start, c.end, q.text + " " + c.text)
        else:
            out.append(c)
    return out


def transcribe_words(video, model="base", language=None, device="cpu", progress=None, cancel=None, initial_prompt=""):
    """(Smart Script) every WORD with its real start/end time -> (words: [script_builder.Word], language_code)"""
    from .script_builder import Word
    segs, lang, _ = run_asr(_wav_for(video, cancel), model, language, device, None, progress, cancel, initial_prompt=initial_prompt)
    words = [Word(t, a, b) for s in segs for (t, a, b) in s.words if b > a]
    return words, (lang or language or "")
