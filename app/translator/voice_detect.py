"""
Generate Voice Type: analyse the REAL audio of a video, find speakers, estimate voice type, match saved presets.

Pipeline (each stage degrades gracefully if its library is missing):
 1. FFmpeg        -> 16 kHz mono WAV
 2. pyannote 4.x  -> speaker diarization (community-1; needs a Hugging Face token + accepted model terms). NEVER falls back silently.
    fallback      -> energy VAD + embeddings, ONLY when the user explicitly selects diar_mode='fallback'
 3. SpeechBrain   -> ECAPA embeddings; used to SUGGEST (never auto-apply) speaker merges
 4. Librosa       -> f0 (pyin), spectral centroid, flatness, formant proxy (LPC), speaking rate
 5. Hugging Face  -> audeering age/gender model (optional) ; custom sklearn model for Fantasy/Animal (optional)
 6. Rules         -> Human types only when the evidence is enough, otherwise "Unknown"

Honest limits: nothing here "knows" Fantasy/Animal voices. Without a model trained by you
(see train_voice_classifier.py) those categories are never guessed.  Age is an estimate, not a fact.
Presets are NEVER invented or edited: they are taken from the user's saved Voice Types, else the built-in catalog.
"""
from pathlib import Path
import numpy as np

from ..shared.ffmpeg_util import Cancelled
from ..shared.paths import work_dir, data_dir
from .audio_extractor import extract_audio
from .voice_presets import default_preset, resolve

SR = 16000
MIN_SEG = 0.8            # seconds; shorter pieces give unreliable pitch/embedding
MERGE_COS = 0.62         # cosine similarity above which two speaker clusters are the same person
CUSTOM_MODEL = "voice_type_model.joblib"   # optional, made by train_voice_classifier.py


# ------------------------------------------------------------------ helpers
def _chk(cancel):
    if cancel and cancel():
        raise Cancelled()


def _load(wav):
    import librosa
    y, _ = librosa.load(str(wav), sr=SR, mono=True)
    return y


def _cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# ------------------------------------------------------------------ 2. diarization
def _diarize_pyannote(wav, token, device, model=None, cache_dir=None, num_speakers=None):
    """pyannote.audio 4.x via translator.diarization. Raises DiarizationError (never falls back by itself).
    Returns (segments [(start, end, speaker)] with overlaps kept, DiarizationResult)."""
    from . import diarization as D
    res = D.diarize(wav, token, model or D.DEFAULT_MODEL, device, num_speakers=num_speakers, cache_dir=cache_dir)
    return [(t.start, t.end, t.speaker) for t in res.turns], res


def _diarize_fallback(y, embedder, cancel):
    """Energy VAD -> embeddings -> agglomerative clustering. Less accurate than pyannote (esp. overlaps)."""
    import librosa
    if embedder is None:
        raise RuntimeError("No diarization available. Install pyannote.audio (+ set Hugging Face token in Settings) "
                           "or speechbrain. See requirements-voice.txt")
    iv = librosa.effects.split(y, top_db=32, frame_length=2048, hop_length=512)
    segs = [(a / SR, b / SR) for a, b in iv if (b - a) / SR >= MIN_SEG]
    embs = []
    for a, b in segs:
        _chk(cancel)
        embs.append(embedder(y[int(a * SR):int(b * SR)]))
    if len(segs) < 2:
        return [(a, b, "S0") for a, b in segs]
    from sklearn.cluster import AgglomerativeClustering
    E = np.vstack(embs)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    lab = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                  distance_threshold=1 - MERGE_COS).fit_predict(E)
    return [(a, b, f"S{l}") for (a, b), l in zip(segs, lab)]


# ------------------------------------------------------------------ 3. speaker identity
def _make_embedder(device):
    try:
        import torch
        try:
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError:
            from speechbrain.pretrained import EncoderClassifier
        m = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb",
                                           savedir=str(data_dir() / "models" / "ecapa"),
                                           run_opts={"device": device})

        def emb(x):
            with torch.no_grad():
                e = m.encode_batch(torch.from_numpy(x).float().unsqueeze(0).to(device))
            return e.squeeze().cpu().numpy()
        return emb
    except Exception:
        return None


def _speaker_centroids(y, segs, embedder, cancel):
    cent = {}
    for sid in sorted({s for _, _, s in segs}):
        _chk(cancel)
        mine = sorted([(a, b) for a, b, s in segs if s == sid], key=lambda t: t[0] - t[1])[:6]  # longest 6
        vs = [embedder(y[int(a * SR):int(b * SR)]) for a, b in mine if b - a >= MIN_SEG]
        if vs:
            c = np.mean(vs, axis=0)
            cent[sid] = c / (np.linalg.norm(c) + 1e-9)
    return cent


def suggest_merges(y, segs, embedder, cancel=None, threshold=MERGE_COS):
    """SUGGESTIONS ONLY: [(speaker_a, speaker_b, cosine)] for pairs that sound alike. Nothing is merged here -
    similar voices are not proof of the same person; the user decides in the Speaker Manager."""
    if embedder is None:
        return []
    cent = _speaker_centroids(y, segs, embedder, cancel)
    keys = list(cent)
    out = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            c = _cos(cent[a], cent[b])
            if c >= threshold:
                out.append((a, b, round(c, 3)))
    return sorted(out, key=lambda t: -t[2])


def _merge_speakers(y, segs, embedder, cancel):
    """Kept for old callers: automatic merging (OFF by default - see detect_voice_types(auto_merge=...))."""
    pairs = suggest_merges(y, segs, embedder, cancel)
    remap = {}
    for a, b, _ in pairs:
        if b not in remap and a not in remap:
            remap[b] = a
    return [(a, b, remap.get(s, s)) for a, b, s in segs]


# ------------------------------------------------------------------ 4. voice features
def features(y):
    """Acoustic summary of one speaker's audio (numpy float32 @16 kHz). Returns dict (also the ML feature vector)."""
    import librosa
    f0, voiced, _ = librosa.pyin(y, fmin=60, fmax=900, sr=SR, frame_length=1024)
    f0v = f0[~np.isnan(f0)] if f0 is not None else np.array([])
    cent = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=SR)))
    flat = float(np.mean(librosa.feature.spectral_flatness(y=y)))
    rms = librosa.feature.rms(y=y)[0]
    onsets = librosa.onset.onset_detect(y=y, sr=SR)
    rate = len(onsets) / max(len(y) / SR, 1e-3)
    # formant proxy: LPC roots on voiced, high-energy frames
    fm = []
    try:
        hop = SR // 50
        for i in range(0, len(y) - 512, hop * 5):
            if rms[min(i // 512, len(rms) - 1)] < np.percentile(rms, 60):
                continue
            fr = y[i:i + 512] * np.hamming(512)
            a = librosa.lpc(fr, order=12)
            r = [x for x in np.roots(a) if np.imag(x) > 0.01]
            fr_hz = sorted(np.angle(r) * SR / (2 * np.pi))
            fr_hz = [f for f in fr_hz if 250 < f < 3500]
            if len(fr_hz) >= 2:
                fm.append(fr_hz[:3])
            if len(fm) >= 60:
                break
    except Exception:
        pass
    F = np.median(np.array([f + [np.nan] * (3 - len(f)) for f in fm]), axis=0) if fm else np.full(3, np.nan)
    return {
        "f0_med": float(np.median(f0v)) if len(f0v) else 0.0,
        "f0_p10": float(np.percentile(f0v, 10)) if len(f0v) else 0.0,
        "f0_p90": float(np.percentile(f0v, 90)) if len(f0v) else 0.0,
        "voiced_ratio": float(len(f0v) / max(len(f0), 1)),
        "centroid": cent, "flatness": flat,
        "f1": float(F[0]), "f2": float(F[1]), "f3": float(F[2]),
        "rate": float(rate), "rms_std": float(np.std(rms)),
    }


FEATURE_ORDER = ["f0_med", "f0_p10", "f0_p90", "voiced_ratio", "centroid", "flatness", "f1", "f2", "f3", "rate", "rms_std"]


def feature_vector(feat):
    return np.nan_to_num(np.array([feat[k] for k in FEATURE_ORDER], dtype=float))


# ------------------------------------------------------------------ 5. classification
_AG = {}


def _age_gender(y, device):
    """audeering/wav2vec2-large-robust-24-ft-age-gender -> (age_years, {'female','male','child'} probs). None if unavailable."""
    try:
        import torch
        import torch.nn as nn
        from transformers import Wav2Vec2Processor
        from transformers.models.wav2vec2.modeling_wav2vec2 import Wav2Vec2Model, Wav2Vec2PreTrainedModel

        if "m" not in _AG:
            class Head(nn.Module):
                def __init__(s, c, n):
                    super().__init__()
                    s.dense = nn.Linear(c.hidden_size, c.hidden_size)
                    s.dropout = nn.Dropout(c.final_dropout)
                    s.out_proj = nn.Linear(c.hidden_size, n)

                def forward(s, x):
                    return s.out_proj(torch.tanh(s.dense(s.dropout(x))))

            class AG(Wav2Vec2PreTrainedModel):
                def __init__(s, c):
                    super().__init__(c)
                    s.wav2vec2 = Wav2Vec2Model(c)
                    s.age = Head(c, 1)
                    s.gender = Head(c, 3)
                    s.init_weights()

                def forward(s, x):
                    h = s.wav2vec2(x)[0].mean(dim=1)
                    return s.age(h), torch.softmax(s.gender(h), dim=1)

            name = "audeering/wav2vec2-large-robust-24-ft-age-gender"
            _AG["p"] = Wav2Vec2Processor.from_pretrained(name)
            _AG["m"] = AG.from_pretrained(name).to(device).eval()
        x = y[:SR * 20]                                   # 20 s is plenty
        inp = _AG["p"](x, sampling_rate=SR, return_tensors="pt").input_values.to(device)
        with torch.no_grad():
            age, g = _AG["m"](inp)
        gp = g.squeeze().cpu().numpy()
        return float(age.item()) * 100, {"female": float(gp[0]), "male": float(gp[1]), "child": float(gp[2])}
    except Exception:
        return None


def _custom_model():
    p = data_dir() / "models" / CUSTOM_MODEL
    if not p.exists():
        return None
    try:
        import joblib
        return joblib.load(p)
    except Exception:
        return None


def classify(feat, ag, custom, min_conf):
    """Returns (voice_type, confidence 0..1, note). Unknown when evidence is weak."""
    f0 = feat["f0_med"]
    if feat["voiced_ratio"] < 0.15 or f0 <= 0:
        return "Unknown", 0.0, "too little voiced speech"

    # Fantasy/Animal only from a model YOU trained - never guessed from rules.
    if custom is not None:
        try:
            p = custom.predict_proba([feature_vector(feat)])[0]
            k = int(np.argmax(p))
            if custom.classes_[k] and float(p[k]) >= max(min_conf, 0.6):   # stricter for the exotic classes
                return str(custom.classes_[k]), float(p[k]), "custom model"
        except Exception:
            pass

    # Human types
    if ag is not None:
        age, gp = ag
        g = max(gp, key=gp.get)
        gc = gp[g]
        if g == "child":
            if age < 4 or f0 > 380:
                return "Baby / Toddler", min(0.9, gc), f"age~{age:.0f}"
            girl = (f0 > 300) + (feat["f2"] > 2600 if feat["f2"] == feat["f2"] else 0)   # weak tie-break only
            return ("Girl" if girl >= 1 else "Boy"), min(0.55, gc), "child - boy/girl is a weak guess"
        if age >= 60:
            t = "Old Men" if g == "male" else "Old Women"
        elif age >= 40:
            t = "Adult Men" if g == "male" else "Adult Women"
        else:
            t = "Men" if g == "male" else "Women"
        return t, float(gc * (0.9 if 38 < age < 42 or 58 < age < 62 else 1.0)), f"age~{age:.0f}, {g}"

    # Rules only (no HF model): pitch-based, lower confidence
    if f0 > 380:
        return "Baby / Toddler", 0.55, "pitch only"
    if f0 > 280:
        return "Girl", 0.45, "pitch only (child; boy/girl uncertain)"
    if f0 > 165:
        return "Women", 0.7 if f0 > 190 else 0.5, "pitch only"
    if f0 > 85:
        return "Men", 0.7 if f0 < 140 else 0.5, "pitch only"
    return "Unknown", 0.3, "pitch very low - could be deep voice/effect"


# ------------------------------------------------------------------ main entry
def detect_voice_types(video, saved_presets, hf_token="", device="cpu", min_conf=0.45, progress=None, cancel=None,
                       diar_mode="pyannote", diar_model=None, num_speakers=None, auto_merge=False):
    """
    diar_mode: "pyannote" (default; any failure RAISES DiarizationError with an actionable message),
               "fallback" (energy VAD + embeddings - the user explicitly chose the less accurate method),
               "off" (one speaker; no diarization at all).
    auto_merge: False (default) -> similar speakers are only SUGGESTED in result["merge_suggestions"].
    Returns {"speakers": [{id, type, confidence, note, seconds, feat, preset, preset_source}], "segments": [(a,b,id)],
             "notes": [str], "diar_method": str, "merge_suggestions": [(id_a, id_b, cosine)], "overlaps": [(a,b,[ids])],
             "embeddings": {id: [float]}}.  Never synthesises audio and never edits presets.
    """
    notes = []

    def prog(p):
        if progress:
            progress(p)

    wav = Path(work_dir(Path(video).stem)) / "audio16k.wav"
    prog(2)
    extract_audio(video, wav, cancel=cancel)                              # 1
    _chk(cancel)
    y = _load(wav)
    prog(10)

    embedder = _make_embedder(device)                                     # 3 (loaded early; used by 2 fallback too)
    if embedder is None:
        notes.append("SpeechBrain not available: speaker merging skipped.")

    segs, dres, method = None, None, diar_mode
    if diar_mode == "pyannote":
        segs, dres = _diarize_pyannote(wav, hf_token, device, diar_model, cache_dir=Path(work_dir(Path(video).stem)))   # raises on failure
        method = "pyannote"
    elif diar_mode == "fallback":
        notes.append("Diarization: energy-VAD fallback (chosen by the user) - less accurate, overlaps are not detected.")
        segs = _diarize_fallback(y, embedder, cancel)
    elif diar_mode == "off":
        notes.append("Speaker detection is OFF - everything is treated as one speaker.")
        segs = [(0.0, len(y) / SR, "S0")]
    else:
        raise ValueError(f"Unknown diar_mode '{diar_mode}'")
    prog(45)
    suggestions = []
    if diar_mode != "off":
        suggestions = suggest_merges(y, segs, embedder, cancel)
        if auto_merge and suggestions:
            segs = _merge_speakers(y, segs, embedder, cancel)
            notes.append("Speakers with similar voices were merged automatically (auto_merge=True).")
    prog(55)

    # concat each speaker's audio (cap 60 s) for analysis
    by = {}
    for a, b, s in segs:
        if b - a >= MIN_SEG:
            by.setdefault(s, []).append((a, b))
    if not by:
        raise RuntimeError("No speech detected in the audio.")
    order = sorted(by, key=lambda s: -sum(b - a for a, b in by[s]))
    rename = {s: f"Speaker {i + 1}" for i, s in enumerate(order)}

    custom = _custom_model()
    if custom is None:
        notes.append("No custom Fantasy/Animal model found -> only Human types are detected; others shown as Unknown/Human.")
    ag_ok = True
    out = []
    for n, s in enumerate(order):
        _chk(cancel)
        parts, tot = [], 0.0
        for a, b in sorted(by[s], key=lambda t: t[0] - t[1]):
            parts.append(y[int(a * SR):int(b * SR)])
            tot += b - a
            if tot >= 60:
                break
        ys = np.concatenate(parts)
        feat = features(ys)                                               # 4
        ag = _age_gender(ys, device) if ag_ok else None                   # 5
        if ag is None:
            ag_ok = False
        t, conf, why = classify(feat, ag, custom, min_conf)               # 5
        t = resolve(t)                                                    # ឈ្មោះចាស់ → ឈ្មោះថ្មី (Boy → Boy 1)
        if conf < min_conf:
            t, why = "Unknown", f"low confidence ({conf:.2f}); best guess was {t}. {why}"
        preset, src = None, ""
        if t != "Unknown":                                                # 6/7
            if t in saved_presets:
                preset, src = dict(saved_presets[t]), "saved"
            else:
                preset, src = default_preset(t), "catalog"
        out.append({"id": rename[s], "type": t, "confidence": round(conf, 2), "note": why,
                    "seconds": round(sum(b - a for a, b in by[s]), 1), "feat": feat, "preset": preset,
                    "preset_source": src})
        prog(55 + int(40 * (n + 1) / len(order)))
    if not ag_ok:
        notes.append("Age/gender model (transformers+torch) not available -> pitch-only rules, lower confidence.")
    prog(100)
    ren = lambda x: rename.get(x, x)   # noqa
    return {"speakers": out, "segments": [(a, b, rename[s]) for a, b, s in segs if s in rename], "notes": notes,
            "diar_method": method,
            "merge_suggestions": [(ren(a), ren(b), c) for a, b, c in suggestions if a in rename and b in rename],
            "overlaps": [(a, b, [ren(x) for x in ids]) for a, b, ids in (dres.overlaps() if dres else [])],
            "embeddings": ({ren(k): v for k, v in dres.embeddings.items() if k in rename} if dres else {})}


def assign_to_cues(cues, segments, speaker_types):
    """cue index -> voice type, by the speaker with the largest time overlap. speaker_types: {'Speaker 1': 'Adult Man'}"""
    res = {}
    for i, c in enumerate(cues):
        ov = {}
        for a, b, s in segments:
            o = min(b, c.end) - max(a, c.start)
            if o > 0:
                ov[s] = ov.get(s, 0) + o
        if ov:
            t = speaker_types.get(max(ov, key=ov.get))
            if t and t != "Unknown":
                res[i] = t
    return res
